from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .util import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
 doi TEXT PRIMARY KEY, journal TEXT NOT NULL, title TEXT NOT NULL, authors TEXT NOT NULL,
 metadata TEXT NOT NULL, metadata_hash TEXT NOT NULL, fetch_state TEXT NOT NULL DEFAULT 'pending',
 last_error TEXT, retry_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discoveries (
 journal TEXT PRIMARY KEY, checkpoint TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS origin_blocks (
 origin TEXT PRIMARY KEY, last_error TEXT NOT NULL, retry_at TEXT
);
CREATE TABLE IF NOT EXISTS pdfs (
 id INTEGER PRIMARY KEY, doi TEXT NOT NULL REFERENCES papers(doi), sha256 TEXT NOT NULL,
 path TEXT NOT NULL, pages INTEGER NOT NULL, version TEXT NOT NULL, source_url TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(doi, sha256)
);
CREATE TABLE IF NOT EXISTS conversions (
 pdf_id INTEGER NOT NULL REFERENCES pdfs(id), policy TEXT NOT NULL,
 state TEXT NOT NULL, output_path TEXT, last_error TEXT, review_pages INTEGER DEFAULT 0,
 refined_pages INTEGER DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY(pdf_id, policy)
);
CREATE VIRTUAL TABLE IF NOT EXISTS page_search USING fts5(
 doi UNINDEXED, pdf_sha UNINDEXED, journal UNINDEXED, title, page UNINDEXED, text, markdown UNINDEXED
);
"""


class State:
    def __init__(self, data_dir: Path):
        self.root = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(data_dir / "library.sqlite", timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    def paper(self, doi: str):
        row = self.connection.execute("SELECT * FROM papers WHERE doi=?", (doi,)).fetchone()
        return dict(row) if row else None

    def upsert(self, item: dict) -> bool:
        old = self.paper(item["doi"])
        if old and old["metadata_hash"] == item["metadata_hash"]:
            return False
        self.connection.execute("""
            INSERT INTO papers(doi,journal,title,authors,metadata,metadata_hash,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(doi) DO UPDATE SET
            journal=excluded.journal,title=excluded.title,authors=excluded.authors,
            metadata=excluded.metadata,metadata_hash=excluded.metadata_hash,
            fetch_state='pending',last_error=NULL,retry_at=NULL,updated_at=excluded.updated_at
            """, (item["doi"], item["journal"], item["title"], json.dumps(item["authors"]),
                  json.dumps(item, ensure_ascii=False), item["metadata_hash"], now(), now()))
        self.connection.commit()
        if old and (old["title"] != item["title"] or old["journal"] != item["journal"]):
            self.connection.execute("UPDATE page_search SET title=?,journal=? WHERE doi=?",
                                    (item["title"], item["journal"], item["doi"]))
            self.connection.commit()
        return True

    def origin_block(self, origin: str):
        row = self.connection.execute("SELECT * FROM origin_blocks WHERE origin=?", (origin,)).fetchone()
        if row and (row["retry_at"] is None or row["retry_at"] > now()):
            return dict(row)
        return None

    def block_origin(self, origin: str, error: str, retry_at: str | None = None):
        self.connection.execute("INSERT OR REPLACE INTO origin_blocks VALUES(?,?,?)", (origin, error, retry_at))
        self.connection.commit()

    def clear_origin(self, origin: str):
        self.connection.execute("DELETE FROM origin_blocks WHERE origin=?", (origin,))
        self.connection.commit()

    def retry_access(self, origin: str | None = None):
        if origin:
            self.connection.execute("DELETE FROM origin_blocks WHERE origin=? AND retry_at IS NULL", (origin,))
        else:
            self.connection.execute("DELETE FROM origin_blocks WHERE retry_at IS NULL")
        self.connection.commit()

    def checkpoint(self, journal: str):
        row = self.connection.execute("SELECT checkpoint FROM discoveries WHERE journal=?", (journal,)).fetchone()
        return row[0] if row else None

    def save_checkpoint(self, journal: str, checkpoint: str | None, error: str | None = None):
        self.connection.execute("""
            INSERT INTO discoveries(journal,checkpoint,last_error) VALUES(?,?,?)
            ON CONFLICT(journal) DO UPDATE SET
            checkpoint=COALESCE(excluded.checkpoint,discoveries.checkpoint),last_error=excluded.last_error
            """, (journal, checkpoint, error))
        self.connection.commit()

    def fetch_result(self, doi: str, state: str, error: str | None = None, retry_at: str | None = None):
        self.connection.execute("UPDATE papers SET fetch_state=?,last_error=?,retry_at=? WHERE doi=?",
                                (state, error, retry_at, doi))
        self.connection.commit()

    def pending_fetch(self, limit: int, retry: bool = False):
        states = ("pending", "failed", "waiting_access") if retry else ("pending", "failed")
        marks = ",".join("?" for _ in states)
        return [dict(row) for row in self.connection.execute(
            f"SELECT * FROM papers WHERE fetch_state IN ({marks}) AND "
            "(retry_at IS NULL OR retry_at<=?) ORDER BY created_at,doi LIMIT ?", (*states, now(), limit))]

    def add_pdf(self, doi: str, sha: str, path: Path, pages: int, version: str, source: str):
        self.connection.execute("""
            INSERT OR IGNORE INTO pdfs(doi,sha256,path,pages,version,source_url,created_at)
            VALUES(?,?,?,?,?,?,?)
            """, (doi, sha, path.relative_to(self.root).as_posix(), pages, version, source, now()))
        self.connection.execute("UPDATE papers SET fetch_state='downloaded',last_error=NULL,retry_at=NULL WHERE doi=?",
                                (doi,))
        self.connection.commit()
        return dict(self.connection.execute("SELECT * FROM pdfs WHERE doi=? AND sha256=?", (doi, sha)).fetchone())

    def conversion(self, pdf_id: int, policy: str, status: str, output: Path | None = None,
                   error: str | None = None, review_pages: int = 0, refined_pages: int = 0):
        self.connection.execute("""
            INSERT INTO conversions(pdf_id,policy,state,output_path,last_error,review_pages,refined_pages,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(pdf_id,policy) DO UPDATE SET
            state=excluded.state,output_path=excluded.output_path,last_error=excluded.last_error,
            review_pages=excluded.review_pages,refined_pages=excluded.refined_pages,updated_at=excluded.updated_at
            """, (pdf_id, policy, status, output.relative_to(self.root).as_posix() if output else None,
                  error, review_pages, refined_pages, now()))
        self.connection.commit()

    def conversion_queue(self, policy: str, limit: int):
        return [dict(row) for row in self.connection.execute("""
            SELECT pdfs.*,papers.title,papers.journal,papers.metadata FROM pdfs
            JOIN papers ON papers.doi=pdfs.doi
            LEFT JOIN conversions c ON c.pdf_id=pdfs.id AND c.policy=?
            WHERE c.state IS NULL OR c.state!='completed' ORDER BY pdfs.id LIMIT ?
            """, (policy, limit))]

    def index_pages(self, pdf: dict, pages: list[tuple[int, str, str]]):
        with self.connection:
            self.connection.execute("DELETE FROM page_search WHERE doi=? AND pdf_sha=?", (pdf["doi"], pdf["sha256"]))
            self.connection.executemany("INSERT INTO page_search VALUES(?,?,?,?,?,?,?)", [
                (pdf["doi"], pdf["sha256"], pdf["journal"], pdf["title"], number, text, path)
                for number, text, path in pages])

    def search(self, query: str, limit: int):
        return [dict(row) for row in self.connection.execute("""
            SELECT doi,pdf_sha,journal,title,page,markdown,
            snippet(page_search,5,'[',']',' … ',36) AS snippet
            FROM page_search WHERE page_search MATCH ? ORDER BY bm25(page_search) LIMIT ?
            """, (query, limit))]

    def summary(self):
        def count(table):
            return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return dict(papers=count("papers"), pdf_files=count("pdfs"),
                    pdf_pages=self.connection.execute("SELECT COALESCE(SUM(pages),0) FROM pdfs").fetchone()[0],
                    fetch_states={row[0]: row[1] for row in self.connection.execute(
                        "SELECT fetch_state,COUNT(*) FROM papers GROUP BY fetch_state")},
                    conversion_states={row[0]: row[1] for row in self.connection.execute(
                        "SELECT state,COUNT(*) FROM conversions GROUP BY state")},
                    indexed_pages=count("page_search"),
                    discovery=[dict(row) for row in self.connection.execute("SELECT * FROM discoveries")],
                    source_blocks=[dict(row) for row in self.connection.execute("SELECT * FROM origin_blocks")])
