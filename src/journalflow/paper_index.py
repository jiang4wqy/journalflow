from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .pdf import Document
from .util import digest_file, read_json

NATIVE_POLICY = "pdf-text-v1"
PAGE_SELECT = """
SELECT s.doi,s.pdf_sha,s.journal,s.title,s.page,s.markdown,x.path,x.version,
       COALESCE(json_extract(p.metadata,'$.dates.published."date-parts"[0][0]'),
                json_extract(p.metadata,'$.dates."published-online"."date-parts"[0][0]'),
                json_extract(p.metadata,'$.dates."published-print"."date-parts"[0][0]')) AS year
"""
PAGE_FROM = """
FROM page_search s JOIN pdfs x ON x.doi=s.doi AND x.sha256=s.pdf_sha
JOIN papers p ON p.doi=s.doi
WHERE s.page BETWEEN 1 AND x.pages
"""


def index_text(state, limit: int) -> dict:
    report = {"indexed": 0, "pages": 0, "pages_without_text": 0, "failed": 0, "errors": []}
    for pdf in state.conversion_queue(NATIVE_POLICY, limit, unindexed_only=True):
        try:
            source = (state.root / pdf["path"]).resolve()
            source.relative_to(state.root.resolve())
            if digest_file(source) != pdf["sha256"]:
                raise ValueError("Source PDF hash mismatch")
            with Document(source) as doc:
                if len(doc) != pdf["pages"]:
                    raise ValueError("Source PDF page count changed")
                pages = []
                for index in range(len(doc)):
                    text = doc.text(index).strip()
                    if any(character.isalnum() for character in text):
                        pages.append((index + 1, text, ""))
            state.index_pages(pdf, pages)
            state.conversion(pdf["id"], NATIVE_POLICY, "completed")
            report["indexed"] += bool(pages)
            report["pages"] += len(pages)
            report["pages_without_text"] += pdf["pages"] - len(pages)
        except (OSError, ValueError) as error:
            state.conversion(pdf["id"], NATIVE_POLICY, "failed", error=str(error))
            report["failed"] += 1
            report["errors"].append({"doi": pdf["doi"], "error": str(error)})
    return report


class PaperIndex:
    """Read the selected pages written by the existing JournalFlow pipeline."""

    def __init__(self, data_dir: Path):
        self.root = data_dir.resolve()

    def connect(self):
        db = sqlite3.connect((self.root / "library.sqlite").as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return closing(db)

    def source(self, row, maximum: int):
        pdf = (self.root / row["path"]).resolve()
        pdf.relative_to(self.root)
        if not pdf.is_file():
            return None
        quality = "pdf_text_layer"
        if row["markdown"]:
            status = (self.root / row["markdown"]).with_name("page-status.json").resolve()
            status.relative_to(self.root)
            if not status.is_file():
                return None
            review = read_json(status)
            if (review.get("source_sha256") != row["pdf_sha"] or review.get("source_pdf_page") != row["page"]
                    or review.get("quality", {}).get("review_flags") != []):
                return None
            quality = "visual_page_without_automatic_review_flags"
        text = row["evidence"]
        return dict(paper_id=f'{row["doi"]}@{row["pdf_sha"]}', doi=row["doi"], title=row["title"],
                    journal=row["journal"], year=row["year"], source_sha256=row["pdf_sha"],
                    source_version=row["version"], pdf_page=row["page"], quality=quality,
                    text=text[:maximum], truncated=len(text) > maximum, pdf_path=str(pdf),
                    pdf_url=f'{pdf.as_uri()}#page={row["page"]}')

    def search(self, query, journal, year, doi, limit):
        words = re.findall(r"[^\W_]+", query, flags=re.UNICODE)[:16]
        if not words:
            return []
        expression = " AND ".join(f'"{word}"' for word in words)
        where, params = ["page_search MATCH ?"], [expression]
        for column, value in (("s.journal", journal), ("year", year), ("s.doi", doi)):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        sql = (PAGE_SELECT + ", snippet(page_search,5,'','',' … ',110) AS evidence "
               + PAGE_FROM + " AND " + " AND ".join(where) + " ORDER BY bm25(page_search)")
        results = []
        with self.connect() as db:
            for row in db.execute(sql, params):
                source = self.source(row, 1800)
                if source:
                    results.append(source)
                if len(results) >= limit:
                    break
        return results

    def get_page(self, paper_id, pdf_page):
        doi, _, sha = paper_id.rpartition("@")
        sql = PAGE_SELECT + ", s.text AS evidence " + PAGE_FROM + " AND s.doi=? AND s.pdf_sha=? AND s.page=?"
        with self.connect() as db:
            row = db.execute(sql, (doi, sha, pdf_page)).fetchone()
        return self.source(row, 6000) if row else None
