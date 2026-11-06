"""Issue a private weekly gate from one completed live cycle, never a replay."""
from __future__ import annotations

import sqlite3
import tomllib
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .regional import ELIGIBLE, validate_generation
from .util import digest_file, now, read_json, write_json


def issue_gate(config, state, report, options):
    """Return pending without changing an earlier gate when evidence is incomplete."""
    def require(condition, reason):
        if not condition:
            raise ValueError(reason)

    try:
        raw = tomllib.loads(config.path.read_text(encoding="utf-8")).get("automation", {})
        require(raw.get("download_enabled") is True and raw.get("vision_enabled") is True,
                "Downloads and visual conversion must both be enabled")
        config_sha = digest_file(config.path)
        require(report["config_sha256"] == config_sha and report["policy"] == config.vision.policy_hash,
                "The cycle used a different configuration or recognition policy")
        require(report.get("run_kind") == "live_pipeline" and report.get("published") is True,
                "A published live pipeline cycle is required")
        ids = report["new_pdf_ids"]
        require(isinstance(ids, list) and len(ids) == len(set(ids)) and len(ids) >= 10
                and all(type(value) is int and value > 0 for value in ids), "Ten new PDF IDs are required")
        require(report["fetch"].get("downloaded", 0) >= len(ids), "New IDs are not backed by this cycle's downloads")
        baseline = state.connection.execute("SELECT doi,pdf_sha256,pdf_path,source_index FROM baseline_sources WHERE available=1").fetchall()
        require(baseline and all(Path(row[2]).is_file() and Path(row[3]).is_file() for row in baseline),
                "An available baseline and its source files must exist")
        known = {row[1] for row in baseline}
        built = report["regional"]
        index = Path(built["index"]).resolve()
        manifest = read_json(index / "incremental-manifest.json")
        require(manifest.get("kind") == "visual_conversion" and manifest.get("source_kind") == "production_sources",
                "Historical holdout or non-production manifests cannot approve weekly execution")
        selected = {(paper["doi"].casefold(), paper["pdf_sha256"]): paper for paper in manifest["papers"]
                    if paper.get("eligible") is True}
        started, finished = (datetime.fromisoformat(report[key]) for key in ("started_at", "finished_at"))
        rows = state.connection.execute("SELECT x.id,x.doi,x.sha256,x.path,x.source_url,x.created_at,c.updated_at "
            "FROM pdfs x JOIN conversions c ON c.pdf_id=x.id WHERE c.policy=? AND c.state='completed' AND x.id IN ("
            + ",".join("?" for _ in ids) + ")", [report["policy"], *ids]).fetchall()
        proofs = {}
        for row in rows:
            key, url = (row[1].casefold(), row[2]), urlsplit(row[4])
            if (key[1] in known or key not in selected or url.scheme not in ("http", "https") or not url.netloc
                    or not started <= datetime.fromisoformat(row[5]) <= datetime.fromisoformat(row[6]) <= finished):
                continue
            source = (state.root / row[3]).resolve()
            require(source.is_relative_to(state.root.resolve()) and digest_file(source) == row[2], "Live source PDF hash differs")
            proofs[key[0]] = {"pdf_id": row[0], "doi": row[1], "sha256": row[2], "paper_id": selected[key]["paper_id"]}
        require(len(proofs) >= 10, "Ten distinct new DOIs must be downloaded, converted, and included in this generation")
        verified = validate_generation(index)
        require(verified["success"] is True and built["validation"]["success"] is True
                and verified["index_sha256"] == built["validation"]["index_sha256"]
                and verified["manifest_sha256"] == built["manifest_sha256"], "Fresh regional validation differs from the cycle")
        with closing(sqlite3.connect((index / "index.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
            answerable = {(doi.casefold(), sha, pid) for doi, sha, pid in db.execute(
                "SELECT DISTINCT p.doi,p.source_sha256,p.id FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE " + ELIGIBLE) if doi}
        proofs = {doi: value for doi, value in proofs.items() if (doi, value["sha256"], value["paper_id"]) in answerable}
        require(len(proofs) >= 10, "Ten verified sources must retain answer-eligible regional evidence")
        pointer = Path(options["active_pointer"])
        pointer_bytes = pointer.read_bytes()
        active = read_json(pointer)
        require(Path(active["index"]).resolve() == index and active["index_sha256"] == verified["index_sha256"]
                and active["manifest_sha256"] == verified["manifest_sha256"], "The active pointer does not name the validated generation")
        require(digest_file(config.path) == config_sha and pointer.read_bytes() == pointer_bytes,
                "Configuration or active pointer changed during acceptance")
        gate = {"schema": 1, "config_sha256": config_sha, "issued_at": now(), "policy": report["policy"],
                "baseline": {"passed": True, "versions": len(baseline)},
                "trial": {"passed": True, "documents": len(proofs), "kind": "live_download_conversion", "sources": list(proofs.values())},
                "regional": {"passed": True, "index_sha256": verified["index_sha256"], "manifest_sha256": verified["manifest_sha256"]}}
        path = config.data_dir / "automation/gate.json"
        write_json(path, gate)
        return {"state": "approved", "documents": len(proofs), "gate": str(path), "schedule_enabled": False}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        return {"state": "pending", "reason": str(error), "gate_unchanged": True, "schedule_enabled": False}
