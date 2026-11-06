"""Register an existing regional corpus and export a source-checked replay cohort.

The source SQLite and Markdown are read-only inputs. External PDFs stay where
they are; these records do not masquerade as new downloads or local PDF copies.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from .state import State
from .util import digest_file, normalize_doi, now

JOURNALS = ("IJOC", "ISR", "MISQ", "MS")
SAFE_CHUNK = ("answer_eligible=1 AND (page_review_required=0 OR "
              "(region_confirmed=1 AND region_evidence IS NOT NULL AND length(region_evidence)>2))")
BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS baseline_sources (
 paper_id TEXT PRIMARY KEY, article_id TEXT, doi TEXT, pdf_sha256 TEXT NOT NULL,
 pdf_path TEXT NOT NULL, source_index TEXT NOT NULL, identity_hash TEXT NOT NULL,
 source_record TEXT NOT NULL, quality TEXT NOT NULL, available INTEGER NOT NULL,
 imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS baseline_sources_doi ON baseline_sources(doi);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _index_file(index: Path) -> Path:
    index = Path(index).resolve()
    return index / "index.sqlite" if index.is_dir() else index


def _connect(index: Path):
    db = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return closing(db)


def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root):
        raise ValueError("Source path leaves its supplied root: " + relative)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _doi(value):
    try:
        return normalize_doi(value) if value else None
    except ValueError:
        return None


def _quality(paper, review_pages):
    return {"eligible": bool(paper.get("eligible")), "review_pages": review_pages,
            "source_version": paper.get("source_version"),
            "source_completeness": paper.get("source_completeness"),
            "source_identity_status": paper.get("source_identity_status"),
            "automatic_checks_only": True}


def baseline_dois(state: State) -> set[str]:
    """DOIs already available externally; fetch queues must exclude these records.

Keep this exclusion even after discovery updates paper metadata and resets its
fetch state. Missing-DOI versions remain in baseline_sources, never invented IDs.
"""
    exists = state.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='baseline_sources'").fetchone()
    if not exists:
        return set()
    return {row[0] for row in state.connection.execute(
        "SELECT DISTINCT doi FROM baseline_sources WHERE available=1 AND doi IS NOT NULL")}


def import_baseline(state: State, index: Path, pdf_root: Path) -> dict:
    """Seed external source versions and known DOI states, without copying PDFs.

PDF existence and recorded SHA256 syntax are checked; this import does not hash
every external PDF. Existing local downloads/metadata are retained. Reusing a
paper_id for a different identity or quality raises and rolls back the import.
"""
    index, pdf_root = _index_file(index), Path(pdf_root).resolve()
    if index == (state.root / "library.sqlite").resolve():
        raise ValueError("The baseline must be a separate, read-only source index")
    with _connect(index) as source:
        papers = [dict(row) for row in source.execute("SELECT * FROM papers ORDER BY id")]
        reviews = {row[0]: row[1] for row in source.execute(
            "SELECT paper_id,sum(needs_review) FROM page_sources GROUP BY paper_id")}
    records = []
    for paper in papers:
        sha = paper.get("source_sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            raise ValueError("Source PDF has no valid recorded SHA256: " + paper["id"])
        path = _inside(pdf_root, paper["pdf"])
        quality = _quality(paper, reviews.get(paper["id"], 0))
        doi = _doi(paper.get("doi"))
        identity = {key: paper.get(key) for key in (
            "id", "article_id", "title", "journal", "year", "pages", "source_version",
            "source_completeness", "source_identity_status", "eligible")}
        identity.update(doi=doi, pdf_sha256=sha.lower(), quality=quality)
        records.append((paper, doi, path, quality, hashlib.sha256(_json(identity).encode()).hexdigest()))
    added = seeded = 0
    state.connection.executescript(BASELINE_SCHEMA)
    with state.connection:
        for paper, doi, path, quality, identity in records:
            existing = state.connection.execute(
                "SELECT identity_hash FROM baseline_sources WHERE paper_id=?", (paper["id"],)).fetchone()
            if existing and existing[0] != identity:
                raise ValueError("Conflicting baseline identity or quality for paper_id: " + paper["id"])
            if not existing:
                state.connection.execute("INSERT INTO baseline_sources VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    paper["id"], paper.get("article_id"), doi, paper["source_sha256"].lower(), str(path),
                    str(index), identity, _json(paper), _json(quality), 1, now()))
                added += 1
            else:
                state.connection.execute(
                    "UPDATE baseline_sources SET pdf_path=?,source_index=? WHERE paper_id=?",
                    (str(path), str(index), paper["id"]))
            if doi is None:
                continue
            if state.paper(doi) is None:
                metadata = {"doi": doi, "journal": paper["journal"], "title": paper["title"],
                            "authors": [], "dates": {"published": {"date-parts": [[paper["year"]]]}}
                            if paper.get("year") else {}, "baseline_external": True}
                text = _json(metadata)
                state.connection.execute("""
                    INSERT INTO papers(doi,journal,title,authors,metadata,metadata_hash,fetch_state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,'baseline_available',?,?)
                """, (doi, paper["journal"], paper["title"], "[]", text,
                      hashlib.sha256(text.encode()).hexdigest(), now(), now()))
                seeded += 1
            state.connection.execute("""
                UPDATE papers SET fetch_state='baseline_available',last_error=NULL,retry_at=NULL
                WHERE doi=? AND fetch_state!='downloaded'
            """, (doi,))
    return {"versions": len(records), "article_ids": len({p.get("article_id") for p in papers
                                                       if p.get("article_id")}),
            "dois": len({doi for _, doi, *_ in records if doi is not None}),
            "versions_without_valid_doi": sum(doi is None for _, doi, *_ in records),
            "added_versions": added, "seeded_dois": seeded, "pdf_files_copied": 0,
            "pdf_hashes_verified": False, "source_modified": False}


def export_holdout(index: Path, markdown_root: Path, output: Path, *, limit: int = 10) -> dict:
    """Export full selected-page Markdown/chunks for a real four-journal replay.

This cohort is existing baseline material, not evidence of new downloads.
All chunks, including isolated content, retain their original quality flags.
"""
    if limit < len(JOURNALS):
        raise ValueError("The holdout needs at least one document from each of four journals")
    index, markdown_root, output = _index_file(index), Path(markdown_root).resolve(), Path(output).resolve()
    if output == index or output.is_relative_to(markdown_root):
        raise ValueError("Write the holdout outside the source index and Markdown tree")
    embedding = json.loads((index.parent / "embedding-receipt.json").read_bytes())
    payload = {"schema_version": 1, "kind": "baseline_holdout", "new_downloads": False,
               "embedding_identity": {"model": embedding["model"], "revision": embedding["revision"]},
               "source_index_sha256": digest_file(index), "papers": []}
    with _connect(index) as db:
        candidates = defaultdict(list)
        rows = db.execute("""
            SELECT p.*,r.review_pages FROM papers p
            JOIN (SELECT paper_id,max(page_review_required) recovered_review FROM chunks WHERE """ + SAFE_CHUNK + """
                  GROUP BY paper_id) safe ON safe.paper_id=p.id
            JOIN (SELECT paper_id,sum(needs_review) review_pages FROM page_sources GROUP BY paper_id) r
                 ON r.paper_id=p.id
            WHERE p.eligible=1 ORDER BY safe.recovered_review DESC,(r.review_pages>0) DESC,p.id
        """)
        for row in rows:
            if row["journal"] in JOURNALS and _doi(row["doi"]):
                candidates[row["journal"]].append(dict(row))
        if any(not candidates[journal] for journal in JOURNALS):
            raise ValueError("The baseline lacks eligible documents in one or more journals")
        selected = []
        while len(selected) < limit:
            previous = len(selected)
            for journal in JOURNALS:
                if candidates[journal] and len(selected) < limit:
                    selected.append(candidates[journal].pop(0))
            if len(selected) == previous:
                raise ValueError("Not enough eligible documents for the requested holdout")
        for paper in selected:
            pages = db.execute("SELECT * FROM page_sources WHERE paper_id=? ORDER BY pdf_page",
                               (paper["id"],)).fetchall()
            if [page["pdf_page"] for page in pages] != list(range(1, paper["pages"] + 1)):
                raise ValueError("Incomplete selected-page coverage: " + paper["id"])
            chunks = defaultdict(list)
            for row in db.execute("SELECT * FROM chunks WHERE paper_id=? ORDER BY pdf_page,id", (paper["id"],)):
                chunk = dict(row)
                chunk["source_chunk_id"] = chunk.pop("id")
                chunk.pop("paper_id")
                chunks[chunk["pdf_page"]].append(chunk)
            first_chunk = next(chunk for page_chunks in chunks.values() for chunk in page_chunks)
            embedding_title = db.execute("SELECT title FROM lexical WHERE rowid=?",
                                         (first_chunk["source_chunk_id"],)).fetchone()
            if embedding_title is None:
                raise ValueError("Missing original embedding title: " + paper["id"])
            metadata = paper.get("source_metadata")
            document = {"paper_id": paper["id"], "article_id": paper.get("article_id"), "doi": _doi(paper["doi"]),
                        "title": paper["title"], "journal": paper["journal"], "year": paper["year"],
                        "pdf_path": paper["pdf"], "pdf_sha256": paper["source_sha256"],
                        "page_count": paper["pages"], "eligible": bool(paper["eligible"]),
                        "source_version": paper.get("source_version"),
                        "source_completeness": paper.get("source_completeness"),
                        "source_identity_status": paper.get("source_identity_status"),
                        "embedding_title": embedding_title[0],
                        "source_metadata": json.loads(metadata) if metadata else {},
                        "quality": _quality(paper, paper["review_pages"]), "pages": []}
            for page in pages:
                data = _inside(markdown_root, page["markdown"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != page["sha256"]:
                    raise ValueError("Selected Markdown hash differs: " + page["markdown"])
                document["pages"].append({"pdf_page": page["pdf_page"], "markdown_path": page["markdown"],
                                          "markdown_sha256": page["sha256"], "markdown_text": data.decode("utf-8"),
                                          "variant": page["variant"], "needs_review": bool(page["needs_review"]),
                                          "chunks": chunks[page["pdf_page"]]})
            payload["papers"].append(document)
    if not any(page["needs_review"] for paper in payload["papers"] for page in paper["pages"]):
        raise ValueError("The holdout must include at least one page retaining a review flag")
    encoded = (_json(payload) + "\n").encode("utf-8")
    if output.exists():
        if output.read_bytes() != encoded:
            raise FileExistsError("Refusing to overwrite a different holdout manifest: " + str(output))
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(encoded)
        temporary.replace(output)
    return {"manifest": str(output), "documents": len(payload["papers"]),
            "pages": sum(p["page_count"] for p in payload["papers"]),
            "review_pages": sum(page["needs_review"] for p in payload["papers"] for page in p["pages"]),
            "new_downloads": 0, "source_modified": False, "sha256": hashlib.sha256(encoded).hexdigest()}
