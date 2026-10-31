from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Config
from .pdf import Document
from .quality import assess, select_refinement
from .state import State
from .util import digest_file, now, read_json, slug, write_json

IMAGE_PATTERN = r'!\[[^\]]*\]\(([^\s)]+)\)|\bsrc=["\x27]([^"\x27]+)["\x27]'


def image_urls(markdown: str):
    return [a or b for a, b in re.findall(IMAGE_PATTERN, markdown)]


def check_page(directory: Path):
    if not (directory / "result.json").is_file():
        raise ValueError("Recognition JSON is missing")
    text = (directory / "page.md").read_text(encoding="utf-8")
    for url in image_urls(text):
        if url.startswith(("http:", "https:", "data:")):
            continue
        target = (directory / url).resolve()
        target.relative_to(directory.resolve())
        if not target.is_file():
            raise ValueError(f"Page image is missing: {url}")
    return text, read_json(directory / "result.json").get("parsing_res_list", [])


def relocate(text: str, prefix: str):
    def substitute(match):
        opening, url, closing = match.groups()
        if url.startswith(("http:", "https:", "data:", "/", "#")):
            return match.group(0)
        return opening + prefix + "/" + url + closing
    text = re.sub(r'(!\[[^\]]*\]\()([^\s)]+)(\))', substitute, text)
    return re.sub(r'(\bsrc=["\x27])([^"\x27]+)(["\x27])', substitute, text)


def recognize_batch(config: Config, pdf: dict, document: Document, indexes: list[int], root: Path,
                    backend, *, refined: bool):
    settings = config.vision
    dpi = settings.refine_dpi if refined else settings.render_dpi
    pixels = settings.refine_max_pixels if refined else settings.max_pixels
    branch = "refinement" if refined else "primary"
    directories = [root / branch / f"{index + 1:04d}" for index in indexes]
    missing = []
    for index, directory in zip(indexes, directories):
        marker = directory / "page-status.json"
        if marker.is_file():
            state = read_json(marker)
            if state.get("source_sha256") == pdf["sha256"] and state.get("policy") == settings.policy_hash:
                check_page(directory)
                continue
        missing.append((index, directory))
    if missing:
        images = [document.render(index, dpi) for index, _ in missing]
        backend.recognize(images, [directory for _, directory in missing], max_pixels=pixels)
        for index, directory in missing:
            text, blocks = check_page(directory)
            write_json(directory / "page-status.json", dict(
                source_sha256=pdf["sha256"], source_pdf_page=index + 1,
                policy=settings.policy_hash, render_dpi=dpi, max_pixels=pixels,
                recognition_method="visual", quality=assess(document.text(index), text, blocks),
                finished_at=now()))


def assemble(config: Config, pdf: dict, root: Path, *, refined: bool):
    source = json.loads(pdf["metadata"])
    metadata = dict(doi=pdf["doi"], title=pdf["title"], journal=pdf["journal"], authors=source["authors"],
                    source_sha256=pdf["sha256"], source_version=pdf["version"],
                    source_pdf=pdf["path"], source_url=pdf["source_url"], source_pages=pdf["pages"],
                    recognition_method="visual", model=config.vision.model,
                    primary_dpi=config.vision.render_dpi, refinement_dpi=config.vision.refine_dpi,
                    policy=config.vision.policy_hash, high_resolution_review=refined)
    header = ["---"] + [key + ": " + json.dumps(value, ensure_ascii=False)
                         for key, value in metadata.items()] + ["---", "", "# " + pdf["title"], ""]
    body, indexed, review_pages, selected_pages = [], [], [], []
    for number in range(1, pdf["pages"] + 1):
        primary = root / "primary" / f"{number:04d}"
        chosen = primary
        quality = read_json(primary / "page-status.json")["quality"]
        alternative = root / "refinement" / f"{number:04d}"
        if refined and (alternative / "page-status.json").is_file():
            candidate = read_json(alternative / "page-status.json")["quality"]
            if select_refinement(quality, candidate):
                chosen, quality = alternative, candidate
                selected_pages.append(number)
        text, _ = check_page(chosen)
        branch = chosen.relative_to(root).as_posix()
        body += [f"<!-- source_pdf_page: {number} -->", "", relocate(text, branch), ""]
        indexed.append((number, text, (chosen / "page.md").relative_to(config.data_dir).as_posix()))
        if quality["review_flags"]:
            review_pages.append(number)
    target = root / ("document-refined.md" if refined else "document.md")
    temporary = target.with_suffix(".md.tmp")
    temporary.write_text("\n".join(header + body), encoding="utf-8")
    temporary.replace(target)
    return indexed, review_pages, selected_pages


def convert(config: Config, state: State, limit: int = 20, *, backend=None):
    settings = config.vision
    report = dict(completed=0, failed=0, errors=[])
    for pdf in state.conversion_queue(settings.policy_hash, limit):
        root = config.data_dir / "markdown" / slug(pdf["doi"]) / pdf["sha256"] / settings.policy_hash
        try:
            source = config.data_dir / pdf["path"]
            if digest_file(source) != pdf["sha256"]:
                raise ValueError("Source PDF hash mismatch")
            state.conversion(pdf["id"], settings.policy_hash, "processing", root)
            if backend is None:
                from .vision import PaddleVision
                backend = PaddleVision(settings)
            with Document(source) as document:
                if len(document) != pdf["pages"]:
                    raise ValueError("Source page count changed")
                for start in range(0, len(document), settings.batch_pages):
                    indexes = list(range(start, min(start + settings.batch_pages, len(document))))
                    recognize_batch(config, pdf, document, indexes, root, backend, refined=False)
                indexed, reviews, selected = assemble(config, pdf, root, refined=False)
                candidates = [index for index in range(len(document)) if read_json(
                    root / "primary" / f"{index + 1:04d}" / "page-status.json"
                )["quality"]["refinement_reasons"]]
                if settings.refine and candidates:
                    for start in range(0, len(candidates), 2):
                        recognize_batch(config, pdf, document, candidates[start:start + 2], root, backend, refined=True)
                    indexed, reviews, selected = assemble(config, pdf, root, refined=True)
                status = dict(doi=pdf["doi"], source_sha256=pdf["sha256"], policy=settings.policy_hash,
                              pages_expected=len(document), pages_saved=len(indexed),
                              refined_pages=[index + 1 for index in candidates] if settings.refine else [],
                              selected_pages=selected,
                              review_pages=reviews, finished_at=now())
                write_json(root / "status.json", status)
                state.index_pages(pdf, indexed)
                state.conversion(pdf["id"], settings.policy_hash, "completed", root,
                                 review_pages=len(reviews), refined_pages=len(status["refined_pages"]))
            report["completed"] += 1
        except Exception as error:
            state.conversion(pdf["id"], settings.policy_hash, "failed", root, str(error))
            report["failed"] += 1
            report["errors"].append(dict(doi=pdf["doi"], error=str(error)))
    return report
