import hashlib
import json
import sqlite3

import pytest

from journalflow.baseline import baseline_dois, export_holdout, import_baseline
from journalflow.state import State


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def regional(tmp_path):
    root = tmp_path / "old library"
    root.mkdir()
    markdown = root / "markdown"
    markdown.mkdir()
    index = root / "index.sqlite"
    db = sqlite3.connect(index)
    db.executescript("""
        CREATE TABLE papers(id,article_id,doi,title,journal,year,pdf,source_sha256,pages,eligible,
                            source_version,source_completeness,source_identity_status,source_metadata);
        CREATE TABLE page_sources(paper_id,pdf_page,markdown,sha256,variant,needs_review,chunks);
        CREATE TABLE chunks(id,paper_id,pdf_page,text,sha256,content_kind,bbox,section,printed_page,
                            page_variant,page_review_required,answer_eligible,region_confirmed,region_evidence);
        CREATE VIRTUAL TABLE lexical USING fts5(text,title,paper_id UNINDEXED);
    """)
    for number in range(12):
        pid = f"paper-{number:02d}"
        journal = ("IJOC", "ISR", "MISQ", "MS")[number % 4]
        pdf = f"pdfs/{pid}.pdf"
        (root / "pdfs").mkdir(exist_ok=True)
        (root / pdf).write_bytes(b"Synthetic baseline PDF " + pid.encode())
        db.execute("INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            pid, pid, f"10.99999/{pid}", f"Synthetic baseline {number}", journal, 2025, pdf,
            sha((root / pdf).read_bytes()), 2, 1, "published", "full", "verified", '{"synthetic":true}'))
        for page in (1, 2):
            text = f"Synthetic selected source {pid} page {page}."
            data = ("\ufeff" + text if page == 1 else text).encode()
            md = f"{pid}-{page}.md"
            (markdown / md).write_bytes(data)
            db.execute("INSERT INTO page_sources VALUES(?,?,?,?,?,?,?)", (pid, page, md, sha(data), "third_pass", page == 2, 1))
            db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                number * 10 + page, pid, page, text, sha(text.encode()), "prose", "[]", "Results", None,
                "third_pass", page == 2, 1, page == 2, '{"strict_match":true}' if page == 2 else None))
            db.execute("INSERT INTO lexical(rowid,text,title,paper_id) VALUES(?,?,?,?)",
                       (number * 10 + page, text, f"Embedding title {number}", pid))
        db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            number * 10 + 3, pid, 2, "Uncertain formula must remain isolated.", "recorded-source-hash",
            "formula", "[]", "Results", None, "third_pass", 1, 0, 0, None))
    db.commit()
    db.close()
    (root / "embedding-receipt.json").write_text(json.dumps({"model": "synthetic-model", "revision": "pinned-test"}))
    return root, index, markdown


@pytest.mark.parametrize("missing_doi", [None, ""])
def test_baseline_is_external_idempotent_and_keeps_versions(regional, tmp_path, missing_doi):
    root, index, _ = regional
    original = sha(index.read_bytes())
    db = sqlite3.connect(index)
    db.execute("UPDATE papers SET doi='10.99999/paper-00',article_id='paper-00' WHERE id='paper-01'")
    db.execute("UPDATE papers SET doi=?,eligible=0,source_identity_status='unverified' WHERE id='paper-02'",
               (missing_doi,))
    db.commit()
    db.close()
    original = sha(index.read_bytes())
    state = State(tmp_path / "new state")
    try:
        first = import_baseline(state, index, root)
        assert first["versions"] == first["added_versions"] == 12
        assert first["article_ids"] == 11 and first["dois"] == 10
        assert first["versions_without_valid_doi"] == 1 and first["pdf_files_copied"] == 0
        assert state.pending_fetch(100, retry=True) == []
        assert len(baseline_dois(state)) == 10
        assert not (state.root / "pdfs").exists()
        assert state.connection.execute("SELECT count(*) FROM pdfs").fetchone()[0] == 0
        quality = json.loads(state.connection.execute(
            "SELECT quality FROM baseline_sources WHERE paper_id='paper-02'").fetchone()[0])
        assert not quality["eligible"] and quality["source_identity_status"] == "unverified"
        second = import_baseline(state, index, root)
        assert second["added_versions"] == second["seeded_dois"] == 0
        assert state.connection.execute("SELECT count(*) FROM baseline_sources").fetchone()[0] == 12
        assert sha(index.read_bytes()) == original
    finally:
        state.close()


def test_conflicting_identity_rolls_back_without_overwriting_user_state(regional, tmp_path):
    root, index, _ = regional
    state = State(tmp_path / "state")
    try:
        import_baseline(state, index, root)
        state.connection.execute("UPDATE papers SET title='User metadata',fetch_state='downloaded' WHERE doi='10.99999/paper-00'")
        state.connection.commit()
        import_baseline(state, index, root)
        assert state.paper("10.99999/paper-00")["title"] == "User metadata"
        assert state.paper("10.99999/paper-00")["fetch_state"] == "downloaded"
        db = sqlite3.connect(index)
        db.execute("""INSERT INTO papers
            SELECT 'new-before','new-article','10.99999/new-before','New baseline version',journal,year,
                   pdf,source_sha256,pages,eligible,source_version,source_completeness,source_identity_status,source_metadata
            FROM papers WHERE id='paper-00'
        """)
        db.execute("UPDATE papers SET source_sha256=? WHERE id='paper-03'", ("a" * 64,))
        db.commit()
        db.close()
        with pytest.raises(ValueError, match="Conflicting baseline"):
            import_baseline(state, index, root)
        assert state.connection.execute("SELECT pdf_sha256 FROM baseline_sources WHERE paper_id='paper-03'").fetchone()[0] != "a" * 64
        assert state.paper("10.99999/new-before") is None
        assert state.connection.execute("SELECT count(*) FROM baseline_sources").fetchone()[0] == 12
    finally:
        state.close()


def test_missing_pdf_cannot_be_marked_available(regional, tmp_path):
    root, index, _ = regional
    (root / "pdfs/paper-00.pdf").unlink()
    state = State(tmp_path / "state")
    try:
        assert baseline_dois(state) == set()
        with pytest.raises(FileNotFoundError):
            import_baseline(state, index, root)
        assert state.summary()["papers"] == 0
    finally:
        state.close()


def test_holdout_keeps_all_selected_pages_and_isolated_chunks(regional, tmp_path):
    _, index, markdown = regional
    original = sha(index.read_bytes())
    output = tmp_path / "private outputs/holdout.json"
    report = export_holdout(index, markdown, output)
    assert report["documents"] == 10 and report["pages"] == 20 and report["new_downloads"] == 0
    content = json.loads(output.read_bytes())
    assert content["kind"] == "baseline_holdout" and content["new_downloads"] is False
    assert {p["journal"] for p in content["papers"]} == {"IJOC", "ISR", "MISQ", "MS"}
    assert content["embedding_identity"] == {"model": "synthetic-model", "revision": "pinned-test"}
    for paper in content["papers"]:
        assert paper["page_count"] == len(paper["pages"]) == 2
        assert paper["embedding_title"].startswith("Embedding title ")
        for page in paper["pages"]:
            assert sha(page["markdown_text"].encode()) == page["markdown_sha256"]
        flagged = paper["pages"][1]
        assert flagged["needs_review"]
        assert {c["answer_eligible"] for c in flagged["chunks"]} == {0, 1}
        assert all("source_chunk_id" in c for c in flagged["chunks"])
    before = output.read_bytes()
    assert export_holdout(index, markdown, output)["sha256"] == report["sha256"]
    assert output.read_bytes() == before and sha(index.read_bytes()) == original


def test_holdout_rejects_source_tampering_and_existing_different_output(regional, tmp_path):
    _, index, markdown = regional
    output = tmp_path / "holdout.json"
    output.write_text("preserve an unrelated file")
    with pytest.raises(FileExistsError):
        export_holdout(index, markdown, output)
    assert output.read_text() == "preserve an unrelated file"
    (markdown / "paper-00-1.md").write_text("Modified source")
    with pytest.raises(ValueError, match="Markdown hash differs"):
        export_holdout(index, markdown, tmp_path / "other.json")
