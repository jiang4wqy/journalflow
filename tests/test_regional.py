import copy
import hashlib
import json
import sqlite3
import struct

import pytest

from journalflow.regional import (
    build_incremental,
    publish_generation,
    resolve_active,
    validate_generation,
)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def put_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def npy(path, shape, values, dtype):
    # Independent tiny NPY fixture, with a deliberately non-64-byte-aligned header.
    header = repr({"descr": dtype, "fortran_order": False, "shape": shape}).encode() + b"\n"
    path.write_bytes(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header
                     + struct.pack("<" + ("f" if dtype == "<f4" else "q") * len(values), *values))


def payload(path):
    content = path.read_bytes()
    return content[10 + struct.unpack("<H", content[8:10])[0]:]


def page(number, text, flagged=False):
    return {"pdf_page": number, "markdown_path": f"selected/page-{number}.md", "markdown_text": text,
            "markdown_sha256": sha(text), "variant": "initial", "needs_review": flagged,
            "chunks": [{"text": text, "sha256": sha(text), "answer_eligible": not flagged,
                        "page_review_required": flagged, "region_confirmed": False,
                        "region_evidence": None, "content_kind": "prose"}]}


def paper(pid, pages):
    return {"paper_id": pid, "article_id": pid, "title": pid + " title", "embedding_title": pid + " title",
            "doi": "10.9999/" + pid, "journal": "ISR", "year": 2025, "pdf_path": pid + ".pdf",
            "pdf_sha256": sha(pid), "eligible": True, "source_metadata": {}, "page_count": len(pages),
            "pages": pages}


@pytest.fixture
def base(tmp_path):
    folder = tmp_path / "base"
    folder.mkdir()
    db = sqlite3.connect(folder / "index.sqlite")
    db.executescript("""
        CREATE TABLE papers(id TEXT PRIMARY KEY,title TEXT,doi TEXT,journal TEXT,year INTEGER,pdf TEXT,
            eligible INTEGER,article_id TEXT,source_sha256 TEXT,source_version TEXT,source_completeness TEXT,
            source_identity_status TEXT,source_metadata TEXT,document TEXT,document_sha256 TEXT,pages INTEGER);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY,paper_id TEXT REFERENCES papers(id),pdf_page INTEGER,
            printed_page TEXT,section TEXT,text TEXT,bbox TEXT,sha256 TEXT,page_variant TEXT,
            page_review_required INTEGER,answer_eligible INTEGER,content_kind TEXT,
            region_confirmed INTEGER,region_evidence TEXT);
        CREATE TABLE page_sources(paper_id TEXT REFERENCES papers(id),pdf_page INTEGER,markdown TEXT,
            sha256 TEXT,variant TEXT,needs_review INTEGER,chunks INTEGER,PRIMARY KEY(paper_id,pdf_page));
        CREATE VIRTUAL TABLE lexical USING fts5(text,title,paper_id UNINDEXED);
        CREATE INDEX chunks_paper_page ON chunks(paper_id,pdf_page);
    """)
    original = [paper("one", [page(1, "Confirmed original prose."), page(2, "Disputed value 7.", True)]),
                paper("two", [page(1, "Another confirmed paper.")])]
    chunk_id = 0
    for item in original:
        db.execute("INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            item["paper_id"], item["title"], item["doi"], item["journal"], item["year"], item["pdf_path"],
            1, item["article_id"], item["pdf_sha256"], "published", "complete", "verified", "{}", "", "", item["page_count"]))
        for selected in item["pages"]:
            chunk_id += 1
            selected["chunks"][0]["source_chunk_id"] = chunk_id
            db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                chunk_id, item["paper_id"], selected["pdf_page"], None, "", selected["markdown_text"], None,
                selected["markdown_sha256"], "initial", int(selected["needs_review"]),
                int(not selected["needs_review"]), "prose", 0, None))
            db.execute("INSERT INTO lexical(rowid,text,title,paper_id) VALUES(?,?,?,?)", (
                chunk_id, selected["markdown_text"], item["embedding_title"], item["paper_id"]))
            db.execute("INSERT INTO page_sources VALUES(?,?,?,?,?,?,?)", (
                item["paper_id"], selected["pdf_page"], selected["markdown_path"], selected["markdown_sha256"],
                "initial", int(selected["needs_review"]), 1))
    db.commit()
    db.close()
    for name in ("vectors.npy", "body-vectors.npy"):
        npy(folder / name, (3, 2), [1., 0., 0., 1., .6, .8], "<f4")
    for name in ("vector-ids.npy", "body-vector-ids.npy"):
        npy(folder / name, (3,), [1, 2, 3], "<i8")
    index_sha = hashlib.sha256((folder / "index.sqlite").read_bytes()).hexdigest()
    put_json(folder / "corpus-receipt.json", {"dimensions": 2, "papers_total": 2, "pages": 3, "chunks": 3,
             "answer_eligible_chunks": 2, "source": "full_visual_markdown", "regional_policy": {"version": 1}})
    for name in ("embedding-receipt.json", "body-index-receipt.json"):
        receipt = {"model": "test-model", "revision": "fixed", "index_sha256": index_sha,
                   "total": 3, "dimensions": 2}
        if name == "body-index-receipt.json":
            receipt.update(vectors_sha256=hashlib.sha256((folder / "body-vectors.npy").read_bytes()).hexdigest(),
                           ids_sha256=hashlib.sha256((folder / "body-vector-ids.npy").read_bytes()).hexdigest())
        put_json(folder / name, receipt)
    return folder, original


def batch(papers):
    return {"schema_version": 1, "kind": "baseline_holdout", "new_downloads": False,
            "embedding_identity": {"model": "test-model", "revision": "fixed"}, "papers": copy.deepcopy(papers)}


def snapshot(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}


def test_new_batch_is_immutable_idempotent_and_published_only_after_validation(base, tmp_path):
    folder, _ = base
    before = snapshot(folder)
    incoming = paper("three", [page(1, "New reviewed prose.")])
    incoming["pages"][0]["chunks"][0]["source_chunk_id"] = 100
    calls = []

    def encode(texts):
        calls.extend(texts)
        return [[.8, .6] for _ in texts]

    manifest = batch([incoming])
    result = build_incremental(folder, manifest, tmp_path / "generations", encode)
    from pathlib import Path
    candidate = Path(result["index"])
    assert calls == ["three title\nNew reviewed prose.", "New reviewed prose."]
    assert result["new_chunks"] == 1 and result["validation"]["papers"] == 3
    for name in ("vectors.npy", "body-vectors.npy", "vector-ids.npy", "body-vector-ids.npy"):
        assert payload(candidate / name).startswith(payload(folder / name))
    assert struct.unpack("<4q", payload(candidate / "vector-ids.npy")) == (1, 2, 3, 100)
    assert snapshot(folder) == before
    again = build_incremental(folder, manifest, tmp_path / "generations")
    replay = build_incremental(candidate, manifest, tmp_path / "generations")
    assert again["reused"] and replay["reused"] and replay["new_chunks"] == 0
    pointer = tmp_path / "private/active.json"
    with pytest.raises(ValueError, match="successful validation"):
        publish_generation(candidate, pointer, {"success": False})
    assert not pointer.exists()
    publish_generation(candidate, pointer, result["validation"])
    assert resolve_active(pointer) == candidate
    assert resolve_active(tmp_path / "missing.json", folder) == folder
    with (candidate / "index.sqlite").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="Active database"):
        resolve_active(pointer)


def test_changed_page_keeps_old_ids_vectors_and_never_promotes_old_isolation(base, tmp_path):
    folder, original = base
    incoming = batch(original[:1])
    changed = incoming["papers"][0]["pages"][0]
    changed.update(markdown_text="Reviewed replacement prose.", markdown_sha256=sha("Reviewed replacement prose."),
                   chunks=page(1, "Reviewed replacement prose.")["chunks"])
    result = build_incremental(folder, incoming, tmp_path / "generations", lambda texts: [[0., 1.] for _ in texts])
    with sqlite3.connect(result["index"] + "/index.sqlite") as db:
        assert db.execute("SELECT id,answer_eligible FROM chunks ORDER BY id").fetchall() == [(1, 0), (2, 0), (3, 1), (4, 1)]
        assert db.execute("SELECT text FROM chunks WHERE id=1").fetchone()[0] == "Confirmed original prose."
    assert result["new_chunks"] == result["retired_chunks"] == 1
    assert result["encoded_title_vectors"] == result["encoded_body_vectors"] == 1


def test_only_review_metadata_changes_reuse_vectors_without_embedding(base, tmp_path):
    folder, original = base
    incoming = batch(original[:1])
    incoming["papers"][0]["pages"][0]["variant"] = "review"
    result = build_incremental(folder, incoming, tmp_path / "generations")
    assert result["new_chunks"] == 1
    assert result["encoded_title_vectors"] == result["encoded_body_vectors"] == 0


@pytest.mark.parametrize("corruption", ["region", "markdown", "identity", "page", "pdf"])
def test_unapproved_or_inconsistent_input_is_rejected_without_mutating_base(base, tmp_path, corruption):
    folder, original = base
    incoming = batch(original[:1])
    before = snapshot(folder)
    if corruption == "region":
        incoming["papers"][0]["pages"][1]["chunks"][0]["answer_eligible"] = True
    elif corruption == "markdown":
        incoming["papers"][0]["pages"][0]["markdown_text"] = "unrecorded change"
    elif corruption == "identity":
        incoming["embedding_identity"]["revision"] = "other"
    elif corruption == "page":
        incoming["papers"][0]["pages"].pop()
    else:
        incoming["papers"][0]["pdf_sha256"] = sha("different PDF")
    with pytest.raises(ValueError):
        build_incremental(folder, incoming, tmp_path / "generations")
    assert snapshot(folder) == before
    assert not list((tmp_path / "generations").glob("regional-*"))


def test_bad_callback_does_not_publish_and_retry_can_recover(base, tmp_path):
    folder, _ = base
    incoming = batch([paper("new", [page(1, "New body.")])])
    before = snapshot(folder)
    with pytest.raises(ValueError, match="normalized"):
        build_incremental(folder, incoming, tmp_path / "generations", lambda texts: [[float("nan"), 0.] for _ in texts])
    assert snapshot(folder) == before
    assert not list((tmp_path / "generations").glob("regional-*"))
    result = build_incremental(folder, incoming, tmp_path / "generations", lambda texts: [[1., 0.] for _ in texts])
    from pathlib import Path
    vector = Path(result["index"]) / "body-vectors.npy"
    content = bytearray(vector.read_bytes())
    content[-1] ^= 1
    vector.write_bytes(content)
    with pytest.raises(ValueError, match="hash differs"):
        validate_generation(vector.parent)


@pytest.mark.parametrize("path", ["../escape.pdf", "/escape.pdf", "C:escape.pdf", "\\escape.pdf"])
def test_source_paths_cannot_escape_the_private_corpus(base, tmp_path, path):
    folder, original = base
    incoming = batch(original[:1])
    incoming["papers"][0]["pdf_path"] = path
    with pytest.raises(ValueError, match="corpus-relative"):
        build_incremental(folder, incoming, tmp_path / "generations")


def test_reviewed_region_is_a_new_record_and_old_isolation_is_retained(base, tmp_path):
    folder, original = base
    incoming = batch(original[:1])
    approved = incoming["papers"][0]["pages"][1]["chunks"][0]
    approved.update(answer_eligible=True, region_confirmed=True,
                    region_evidence={"method": "strict_source_agreement", "confirmed_by_review": True})
    result = build_incremental(folder, incoming, tmp_path / "generations")
    assert result["new_chunks"] == 1 and result["encoded_body_vectors"] == 0
    with sqlite3.connect(result["index"] + "/index.sqlite") as db:
        assert db.execute("SELECT answer_eligible FROM chunks WHERE id=2").fetchone() == (0,)
        assert db.execute("SELECT page_review_required,answer_eligible,region_confirmed FROM chunks WHERE id=4").fetchone() == (1, 1, 1)


def test_receipt_policy_tampering_is_detected(base, tmp_path):
    folder, original = base
    result = build_incremental(folder, batch(original[:1]), tmp_path / "generations")
    from pathlib import Path
    candidate = Path(result["index"])
    receipt = json.loads((candidate / "corpus-receipt.json").read_bytes())
    receipt["regional_policy"] = {}
    put_json(candidate / "corpus-receipt.json", receipt)
    with pytest.raises(ValueError, match="hash differs"):
        validate_generation(candidate)


def test_stale_base_vectors_are_not_silently_rebound_to_a_new_receipt(base, tmp_path):
    folder, original = base
    receipt = json.loads((folder / "embedding-receipt.json").read_bytes())
    receipt["index_sha256"] = "0" * 64
    put_json(folder / "embedding-receipt.json", receipt)
    with pytest.raises(ValueError, match="Base embedding receipt is stale"):
        build_incremental(folder, batch(original[:1]), tmp_path / "generations")
