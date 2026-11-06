"""Immutable increments for an existing regional SQLite/NPY paper index.

The caller supplies reviewed, complete selected pages and chunks; this module does
not OCR, infer missing numbers, or decide that disputed evidence is correct.
``embed(texts)`` must use the base model/revision and passage-prefix recipe. Texts
are already prepared as short-title + newline + body, or body alone. The caller
also owns token-budget checks and durable PDF asset placement.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import shutil
import sqlite3
import struct
import tempfile
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath

from .util import digest_file, now, read_json, write_json

VECTOR_FILES = ("vectors.npy", "vector-ids.npy", "body-vectors.npy", "body-vector-ids.npy")
CHUNK_FIELDS = ("printed_page", "section", "text", "bbox", "sha256", "page_variant",
                "page_review_required", "answer_eligible", "content_kind", "region_confirmed", "region_evidence")
ELIGIBLE = ("p.eligible=1 AND c.answer_eligible=1 AND (c.page_review_required=0 OR "
            "(c.region_confirmed=1 AND c.region_evidence IS NOT NULL AND length(c.region_evidence)>2))")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _index(path):
    path = Path(path).resolve()
    return path.parent if path.name == "index.sqlite" else path


def _relative(value):
    value = str(value).replace("\\", "/")
    if (not value or PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
            or PureWindowsPath(value).drive or ".." in PurePosixPath(value).parts):
        raise ValueError("Expected a corpus-relative source path")
    return value


def _npy(path):
    with Path(path).open("rb") as stream:
        if stream.read(6) != b"\x93NUMPY":
            raise ValueError("Not an NPY file: " + str(path))
        version = stream.read(2)
        if version not in (b"\x01\x00", b"\x02\x00", b"\x03\x00"):
            raise ValueError("Unsupported NPY version")
        size = struct.unpack("<H" if version[0] == 1 else "<I", stream.read(2 if version[0] == 1 else 4))[0]
        if size > 65536:
            raise ValueError("Unexpected NPY header size")
        header = ast.literal_eval(stream.read(size).decode("utf-8" if version[0] == 3 else "latin1"))
        offset = stream.tell()
    if header.get("fortran_order") or header.get("descr") not in ("<f4", "<i8"):
        raise ValueError("Expected C-order little-endian float32 vectors or int64 IDs")
    shape = header["shape"]
    if not isinstance(shape, tuple) or not shape or any(type(n) is not int or n < 0 for n in shape):
        raise ValueError("Invalid NPY shape")
    width = 4 if header["descr"] == "<f4" else 8
    if Path(path).stat().st_size != offset + math.prod(shape) * width:
        raise ValueError("NPY payload size differs from its header")
    return {**header, "offset": offset, "width": width}


def _header(stream, dtype, shape):
    header = repr({"descr": dtype, "fortran_order": False, "shape": tuple(shape)}).encode("latin1")
    header += b" " * ((64 - (10 + len(header) + 1) % 64) % 64) + b"\n"
    stream.write(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header)


def _ids(path):
    header = _npy(path)
    if header["descr"] != "<i8" or len(header["shape"]) != 1:
        raise ValueError("Expected a one-dimensional int64 ID array")
    with Path(path).open("rb") as stream:
        stream.seek(header["offset"])
        return [value[0] for value in struct.iter_unpack("<q", stream.read())]


def _append_npy(source, destination, rows):
    header = _npy(source)
    shape = (header["shape"][0] + len(rows), *header["shape"][1:])
    hasher = hashlib.sha256()
    with Path(source).open("rb") as incoming, Path(destination).open("wb") as outgoing:
        _header(outgoing, header["descr"], shape)
        incoming.seek(header["offset"])
        while block := incoming.read(1024 * 1024):
            outgoing.write(block)
            hasher.update(block)
        for row in rows:
            values = row if len(shape) == 2 else [row]
            outgoing.write(struct.pack("<" + ("f" if header["descr"] == "<f4" else "q") * len(values), *values))
    return hasher.hexdigest()


def _payload_hash(path, rows):
    header = _npy(path)
    remaining = rows * math.prod(header["shape"][1:]) * header["width"]
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        stream.seek(header["offset"])
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError("Truncated preserved vector payload")
            hasher.update(block)
            remaining -= len(block)
    return hasher.hexdigest()


def _row(path, position, dimensions):
    header = _npy(path)
    with Path(path).open("rb") as stream:
        stream.seek(header["offset"] + position * dimensions * 4)
        return struct.unpack("<" + "f" * dimensions, stream.read(dimensions * 4))


def _identity(index):
    receipt = read_json(index / "embedding-receipt.json")
    return {"model": receipt.get("model"), "revision": receipt.get("revision")}


def _chunk(chunk, page):
    text = chunk["text"]
    if not isinstance(text, str) or not text.strip() or _sha(text) != chunk["sha256"]:
        raise ValueError("Chunk text/hash mismatch")
    evidence = chunk.get("region_evidence")
    if isinstance(evidence, str):
        evidence = json.loads(evidence) if evidence else None
    if evidence is not None and not isinstance(evidence, dict):
        raise ValueError("Region evidence must be a JSON object")
    flagged = int(bool(chunk.get("page_review_required", page["needs_review"])))
    if page["needs_review"] and not flagged:
        raise ValueError("A chunk cannot clear its selected page's review flag")
    allowed = int(bool(chunk.get("answer_eligible", False)))
    confirmed = int(bool(chunk.get("region_confirmed", False)))
    if allowed and flagged and not (confirmed and evidence):
        raise ValueError("Unconfirmed disputed regions cannot become answer eligible")
    bbox = chunk.get("bbox")
    if isinstance(bbox, (dict, list)):
        bbox = _canonical(bbox)
    return dict(printed_page=chunk.get("printed_page"), section=chunk.get("section", ""), text=text,
                bbox=bbox, sha256=chunk["sha256"], page_variant=page["variant"],
                page_review_required=flagged, answer_eligible=allowed,
                content_kind=chunk.get("content_kind", "prose"), region_confirmed=confirmed,
                region_evidence=_canonical(evidence) if evidence else None)


def _key(chunk, title):
    return (title, *[chunk[name] for name in CHUNK_FIELDS])


def _review_manifest(manifest):
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("papers"), list):
        raise ValueError("Expected a schema_version=1 reviewed batch manifest")
    seen = set()
    for paper in manifest["papers"]:
        if not paper["paper_id"] or paper["paper_id"] in seen:
            raise ValueError("Missing or duplicate paper ID")
        seen.add(paper["paper_id"])
        _relative(paper["pdf_path"])
        pdf_sha = paper["pdf_sha256"]
        if not isinstance(pdf_sha, str) or len(pdf_sha) != 64 or any(c not in "0123456789abcdef" for c in pdf_sha):
            raise ValueError("Expected the recorded SHA256 of the complete source PDF")
        if type(paper["page_count"]) is not int or paper["page_count"] < 1:
            raise ValueError("Source PDFs must have a positive physical-page count")
        if not isinstance(paper.get("source_metadata", {}), dict):
            raise ValueError("Source metadata must be a JSON object")
        if [p["pdf_page"] for p in paper["pages"]] != list(range(1, paper["page_count"] + 1)):
            raise ValueError("Selected pages must cover the complete PDF in physical-page order")
        for page in paper["pages"]:
            _relative(page["markdown_path"])
            if not isinstance(page["markdown_text"], str) or _sha(page["markdown_text"]) != page["markdown_sha256"]:
                raise ValueError("Selected Markdown text/hash mismatch")
            if type(page["needs_review"]) not in (bool, int) or not isinstance(page["variant"], str):
                raise ValueError("Missing reviewed page state")
            for chunk in page["chunks"]:
                _chunk(chunk, page)


def _encode(plans, embed, dimensions, batch_size):
    vectors = {}
    missing = list(dict.fromkeys(value for value in plans if isinstance(value, str)))
    if missing and embed is None:
        raise ValueError("An embedding callback is required for new or changed text")
    for start in range(0, len(missing), batch_size):
        texts = missing[start:start + batch_size]
        encoded = list(embed(texts))
        if len(encoded) != len(texts):
            raise ValueError("Embedding callback returned the wrong row count")
        for text, vector in zip(texts, encoded):
            vector = [float(value) for value in vector]
            if (len(vector) != dimensions or not all(math.isfinite(value) for value in vector)
                    or abs(math.fsum(value * value for value in vector) - 1) > 0.02):
                raise ValueError("Expected finite, normalized vectors with the base dimensions")
            vectors[text] = vector
    return vectors, len(missing)


def build_incremental(base_index, manifest, output_root, embed=None, *, embedding_identity=None, batch_size=16):
    """Build a separate immutable generation; never mutate the base or publish it.

    Papers with changed PDF hashes must receive a distinct source-version ID.
    Replaced old chunks retain their IDs/text/vectors and can only lose eligibility.
    Replaying the same manifest is a no-op, including when its generation is now the base.
    """
    base = _index(base_index)
    output_root = Path(output_root).resolve()
    if output_root == base or output_root.is_relative_to(base):
        raise ValueError("Generation storage must be outside the immutable base index")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    manifest = read_json(Path(manifest)) if not isinstance(manifest, dict) else manifest
    _review_manifest(manifest)
    identity = embedding_identity or manifest.get("embedding_identity")
    if (not identity or identity != _identity(base) or not all(identity.values())
            or manifest.get("embedding_identity", identity) != identity):
        raise ValueError("Embedding model/revision differs from the base index")
    manifest_sha = _sha(_canonical(manifest))
    with closing(sqlite3.connect((base / "index.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        imported = db.execute("SELECT name FROM sqlite_master WHERE name='regional_imports'").fetchone()
        if imported and db.execute("SELECT 1 FROM regional_imports WHERE manifest_sha256=?", (manifest_sha,)).fetchone():
            return {"index": str(base), "generation": base.name, "manifest_sha256": manifest_sha,
                    "reused": True, "new_chunks": 0, "encoded_title_vectors": 0,
                    "encoded_body_vectors": 0, "validation": validate_generation(base)}
    fingerprint = {name: digest_file(base / name) for name in ("index.sqlite", *VECTOR_FILES)}
    generation = "regional-" + _sha(_canonical(fingerprint) + manifest_sha)[:24]
    destination = output_root / generation
    if destination.exists():
        receipt = read_json(destination / "incremental-receipt.json")
        if receipt["manifest_sha256"] != manifest_sha or receipt["base_files"] != fingerprint:
            raise ValueError("Existing generation has different provenance")
        return {**receipt["result"], "index": str(destination), "reused": True,
                "validation": validate_generation(destination)}
    output_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="." + generation + "-", dir=output_root))
    ids = _ids(base / "vector-ids.npy")
    if ids != _ids(base / "body-vector-ids.npy") or len(ids) != len(set(ids)):
        raise ValueError("Base vector IDs are duplicated or routes differ")
    positions, used = {value: pos for pos, value in enumerate(ids)}, set(ids)
    dimensions = read_json(base / "corpus-receipt.json")["dimensions"]
    for name in ("vectors.npy", "body-vectors.npy"):
        header = _npy(base / name)
        if header["descr"] != "<f4" or header["shape"] != (len(ids), dimensions):
            raise ValueError("Base vector dimensions differ from the receipt")
    for name in ("body-index-receipt.json", "embedding-receipt.json"):
        value = read_json(base / name)
        if (value["index_sha256"] != fingerprint["index.sqlite"] or value["total"] != len(ids)
                or value["dimensions"] != dimensions):
            raise ValueError("Base embedding receipt is stale")
        if name == "body-index-receipt.json" and (value["vectors_sha256"] != fingerprint["body-vectors.npy"]
                                                  or value["ids_sha256"] != fingerprint["body-vector-ids.npy"]):
            raise ValueError("Base body-vector hashes differ from their receipt")
    shutil.copy2(base / "index.sqlite", stage / "index.sqlite")
    new_ids, title_plans, body_plans = [], [], []
    retired = 0
    next_id = max(ids, default=0) + 1
    with closing(sqlite3.connect(stage / "index.sqlite")) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("CREATE TABLE IF NOT EXISTS regional_imports(manifest_sha256 TEXT PRIMARY KEY,created_at TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS regional_page_history(paper_id TEXT,pdf_page INTEGER,manifest_sha256 TEXT,old_source TEXT)")
        for paper in manifest["papers"]:
            pid = paper["paper_id"]
            old_paper = db.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
            if old_paper and (old_paper["source_sha256"] != paper["pdf_sha256"]
                              or old_paper["pages"] != paper["page_count"]):
                raise ValueError("Changed source PDF requires a new source-version paper ID")
            title = paper.get("embedding_title", paper["title"])
            old = db.execute("SELECT c.*,l.title AS embedding_title FROM chunks c JOIN lexical l ON l.rowid=c.id WHERE c.paper_id=?", (pid,)).fetchall()
            matches, title_cache, body_cache = {}, {}, {}
            for row in old:
                normalized = dict(row)
                if normalized["region_evidence"]:
                    normalized["region_evidence"] = _canonical(json.loads(normalized["region_evidence"]))
                matches.setdefault((row["pdf_page"], _key(normalized, row["embedding_title"])), []).append(row["id"])
                title_cache[(row["embedding_title"], row["text"])] = positions[row["id"]]
                body_cache[row["text"]] = positions[row["id"]]
            quality = paper.get("quality", {})
            def source_field(name, default):
                return paper.get(name, quality.get(name, old_paper[name] if old_paper else default))
            values = {"id": pid, "title": paper["title"], "doi": paper.get("doi", ""), "journal": paper["journal"],
                      "year": paper["year"], "pdf": _relative(paper["pdf_path"]), "eligible": int(bool(paper["eligible"])),
                      "article_id": paper.get("article_id", pid), "source_sha256": paper["pdf_sha256"],
                      "source_version": source_field("source_version", "selected"),
                      "source_completeness": source_field("source_completeness", "unknown"),
                      "source_identity_status": source_field("source_identity_status", "unknown"),
                      "source_metadata": _canonical(paper.get("source_metadata", {})),
                      "document": source_field("document", ""), "document_sha256": source_field("document_sha256", ""),
                      "pages": paper["page_count"]}
            if old_paper:
                values["eligible"] = min(values["eligible"], old_paper["eligible"])
                db.execute("UPDATE papers SET " + ",".join(name + "=?" for name in values if name != "id") + " WHERE id=?",
                           [value for name, value in values.items() if name != "id"] + [pid])
            else:
                db.execute("INSERT INTO papers(" + ",".join(values) + ") VALUES(" + ",".join("?" for _ in values) + ")", list(values.values()))
            for page in paper["pages"]:
                number = page["pdf_page"]
                previous = db.execute("SELECT * FROM page_sources WHERE paper_id=? AND pdf_page=?", (pid, number)).fetchone()
                if previous:
                    db.execute("INSERT INTO regional_page_history VALUES(?,?,?,?)", (pid, number, manifest_sha, _canonical(dict(previous))))
                selected = []
                for incoming in page["chunks"]:
                    chunk = _chunk(incoming, page)
                    matching = matches.get((number, _key(chunk, title)), [])
                    identifier = next((value for value in matching if value not in selected), None)
                    if identifier is None:
                        requested = incoming.get("source_chunk_id")
                        identifier = requested if type(requested) is int and requested > 0 and requested not in used else next_id
                        next_id = max(next_id, identifier + 1)
                        used.add(identifier)
                        fields = {"id": identifier, "paper_id": pid, "pdf_page": number, **chunk}
                        db.execute("INSERT INTO chunks(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")", list(fields.values()))
                        db.execute("INSERT INTO lexical(rowid,text,title,paper_id) VALUES(?,?,?,?)", (identifier, chunk["text"], title, pid))
                        new_ids.append(identifier)
                        title_plans.append(title_cache.get((title, chunk["text"]), title + "\n" + chunk["text"]))
                        body_plans.append(body_cache.get(chunk["text"], chunk["text"]))
                    selected.append(identifier)
                old_active = [row["id"] for row in old if row["pdf_page"] == number and row["answer_eligible"] and row["id"] not in selected]
                db.executemany("UPDATE chunks SET answer_eligible=0 WHERE id=?", [(value,) for value in old_active])
                retired += len(old_active)
                db.execute("INSERT OR REPLACE INTO page_sources VALUES(?,?,?,?,?,?,?)", (pid, number, page["markdown_path"], page["markdown_sha256"], page["variant"], int(bool(page["needs_review"])), len(selected)))
                asset = stage / "selected-pages" / _sha(pid)[:24] / f"{number:06d}.md"
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_bytes(page["markdown_text"].encode("utf-8"))
        db.execute("INSERT INTO regional_imports VALUES(?,?)", (manifest_sha, now()))
        db.commit()
    encoded_title, title_count = _encode(title_plans, embed, dimensions, batch_size)
    encoded_body, body_count = _encode(body_plans, embed, dimensions, batch_size)
    preserved = {}
    for name, plans, encoded in (("vectors.npy", title_plans, encoded_title), ("body-vectors.npy", body_plans, encoded_body)):
        rows = [_row(base / name, plan, dimensions) if isinstance(plan, int) else encoded[plan] for plan in plans]
        preserved[name] = _append_npy(base / name, stage / name, rows)
    for name in ("vector-ids.npy", "body-vector-ids.npy"):
        preserved[name] = _append_npy(base / name, stage / name, new_ids)
    receipt = read_json(base / "corpus-receipt.json")
    with closing(sqlite3.connect(stage / "index.sqlite")) as db:
        papers, pages, unique = db.execute("SELECT count(*),sum(pages),count(distinct article_id) FROM papers").fetchone()
        receipt.update(papers_total=papers, pages=pages or 0, unique_article_ids=unique,
                       eligible_papers=db.execute("SELECT count(*) FROM papers WHERE eligible=1").fetchone()[0],
                       chunks=db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                       answer_eligible_chunks=db.execute("SELECT count(*) FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE " + ELIGIBLE).fetchone()[0],
                       unresolved_pages=db.execute("SELECT count(*) FROM page_sources WHERE needs_review=1").fetchone()[0],
                       region_confirmed_chunks=db.execute("SELECT count(*) FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.region_confirmed=1 AND " + ELIGIBLE).fetchone()[0],
                       pages_with_region_recovery=db.execute("SELECT count(*) FROM (SELECT c.paper_id,c.pdf_page FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE c.region_confirmed=1 AND " + ELIGIBLE + " GROUP BY c.paper_id,c.pdf_page)").fetchone()[0],
                       journals=dict(db.execute("SELECT journal,count(*) FROM papers GROUP BY journal")),
                       incremental_manifest_sha256=manifest_sha)
    write_json(stage / "corpus-receipt.json", receipt)
    index_sha = digest_file(stage / "index.sqlite")
    for name in ("body-index-receipt.json", "embedding-receipt.json"):
        value = read_json(base / name)
        value.update(index_sha256=index_sha, total=receipt["chunks"], dimensions=dimensions)
        if name == "body-index-receipt.json":
            value.update(vectors_sha256=digest_file(stage / "body-vectors.npy"), ids_sha256=digest_file(stage / "body-vector-ids.npy"))
        write_json(stage / name, value)
    if (base / "page-rotations.json").is_file():
        shutil.copy2(base / "page-rotations.json", stage / "page-rotations.json")
    result = {"generation": generation, "manifest_sha256": manifest_sha, "reused": False,
              "new_chunks": len(new_ids), "retired_chunks": retired,
              "encoded_title_vectors": title_count, "encoded_body_vectors": body_count}
    write_json(stage / "incremental-manifest.json", manifest)
    write_json(stage / "incremental-receipt.json", {"schema_version": 1, "base_files": fingerprint,
               "base_rows": len(ids), "preserved_payloads": preserved, "manifest_sha256": manifest_sha,
               "result": result, "files": {name: digest_file(stage / name) for name in
               ("index.sqlite", *VECTOR_FILES, "corpus-receipt.json", "body-index-receipt.json", "embedding-receipt.json")}})
    validation = validate_generation(stage)
    write_json(stage / "incremental-verification.json", validation)
    # A concurrent base change must never silently enter an immutable generation.
    if any(digest_file(base / name) != sha for name, sha in fingerprint.items()):
        raise ValueError("Base index changed during incremental construction")
    stage.rename(destination)
    return {**result, "index": str(destination), "validation": validation}


def validate_generation(index):
    """Validate SQLite, both vector routes, preserved payloads, and reviewed sources."""
    index = _index(index)
    receipt = read_json(index / "corpus-receipt.json")
    ids = _ids(index / "vector-ids.npy")
    if ids != _ids(index / "body-vector-ids.npy") or len(ids) != len(set(ids)):
        raise ValueError("Vector route IDs differ or are duplicated")
    dimensions = receipt["dimensions"]
    for name in ("vectors.npy", "body-vectors.npy"):
        header = _npy(index / name)
        if header["descr"] != "<f4" or header["shape"] != (len(ids), dimensions):
            raise ValueError("Vector dimensions or counts differ")
    with closing(sqlite3.connect((index / "index.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("SQLite integrity failed")
        chunk_ids = {row[0] for row in db.execute("SELECT id FROM chunks")}
        if chunk_ids != set(ids) or len(ids) != receipt["chunks"]:
            raise ValueError("SQLite chunks and vector IDs differ")
        if {row[0] for row in db.execute("SELECT rowid FROM lexical")} != chunk_ids:
            raise ValueError("Lexical rows and vector IDs differ")
        if db.execute("SELECT 1 FROM chunks c JOIN lexical l ON l.rowid=c.id WHERE c.text<>l.text LIMIT 1").fetchone():
            raise ValueError("Lexical text differs from its source chunk")
        eligible = db.execute("SELECT count(*) FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE " + ELIGIBLE).fetchone()[0]
        if eligible != receipt["answer_eligible_chunks"]:
            raise ValueError("Eligible evidence count differs")
        papers, pages = db.execute("SELECT count(*),sum(pages) FROM papers").fetchone()
        if (papers, pages or 0) != (receipt["papers_total"], receipt["pages"]):
            raise ValueError("Paper/page counts differ")
        if db.execute("SELECT count(*) FROM page_sources").fetchone()[0] != (pages or 0):
            raise ValueError("Selected-page coverage is incomplete")
        if db.execute("SELECT 1 FROM page_sources s JOIN papers p ON p.id=s.paper_id WHERE s.pdf_page<1 OR s.pdf_page>p.pages LIMIT 1").fetchone():
            raise ValueError("Selected-page number leaves the source PDF")
    index_sha = digest_file(index / "index.sqlite")
    for name in ("body-index-receipt.json", "embedding-receipt.json"):
        value = read_json(index / name)
        if value["index_sha256"] != index_sha or value["total"] != len(ids):
            raise ValueError("Embedding receipt is stale")
        if name == "body-index-receipt.json" and (value["vectors_sha256"] != digest_file(index / "body-vectors.npy")
                                                  or value["ids_sha256"] != digest_file(index / "body-vector-ids.npy")):
            raise ValueError("Body embedding receipt hash differs")
    incremental = read_json(index / "incremental-receipt.json")
    for name, sha in incremental["files"].items():
        if digest_file(index / name) != sha:
            raise ValueError("Generation file hash differs: " + name)
    for name, sha in incremental["preserved_payloads"].items():
        if _payload_hash(index / name, incremental["base_rows"]) != sha:
            raise ValueError("An original vector/ID payload was modified")
    for name in ("vectors.npy", "body-vectors.npy"):
        with (index / name).open("rb") as stream:
            header = _npy(index / name)
            stream.seek(header["offset"] + incremental["base_rows"] * dimensions * 4)
            for _ in range(len(ids) - incremental["base_rows"]):
                row = struct.unpack("<" + "f" * dimensions, stream.read(dimensions * 4))
                if not all(math.isfinite(value) for value in row) or abs(math.fsum(value * value for value in row) - 1) > 0.02:
                    raise ValueError("New vector is not finite and normalized")
    manifest = read_json(index / "incremental-manifest.json")
    if _sha(_canonical(manifest)) != incremental["manifest_sha256"]:
        raise ValueError("Increment manifest hash differs")
    _review_manifest(manifest)
    for paper in manifest["papers"]:
        for page in paper["pages"]:
            asset = index / "selected-pages" / _sha(paper["paper_id"])[:24] / f"{page['pdf_page']:06d}.md"
            if digest_file(asset) != page["markdown_sha256"]:
                raise ValueError("Selected page asset hash differs")
    return {"success": True, "index_sha256": index_sha, "papers": papers, "pages": pages or 0,
            "chunks": len(ids), "answer_eligible_chunks": eligible, "original_payloads_preserved": True,
            "new_vectors_finite_normalized": True,
            "legacy_vector_policy": "byte-preserved; prior base validation remains authoritative",
            "manifest_sha256": incremental["manifest_sha256"], "new_downloads": manifest.get("new_downloads", False)}


def publish_generation(index, active_pointer, validation):
    """Atomically switch a private pointer after caller-supplied acceptance passes."""
    index = _index(index)
    actual = validate_generation(index)
    if validation.get("success") is not True or validation.get("index_sha256") != actual["index_sha256"]:
        raise ValueError("Publication requires successful validation of this exact database")
    pointer = {"schema_version": 1, "index": str(index), "index_sha256": actual["index_sha256"],
               "manifest_sha256": actual["manifest_sha256"], "new_downloads": actual["new_downloads"], "activated_at": now()}
    write_json(Path(active_pointer), pointer)
    return pointer


def resolve_active(active_pointer, fallback=None):
    """Resolve a private pointer, verifying that it still names the accepted database."""
    pointer = Path(active_pointer)
    if not pointer.is_file():
        if fallback is None:
            raise FileNotFoundError(pointer)
        return _index(fallback)
    value = read_json(pointer)
    index = _index(value["index"])
    if digest_file(index / "index.sqlite") != value["index_sha256"]:
        raise ValueError("Active database differs from its accepted pointer")
    return index
