from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class Journal:
    key: str
    name: str
    issns: tuple[str, ...]


@dataclass(frozen=True)
class Vision:
    base_url: str = "http://127.0.0.1:8118/v1"
    model: str = "PaddleOCR-VL-1.6-0.9B"
    layout_device: str = "cpu"
    batch_pages: int = 8
    concurrency: int = 16
    render_dpi: int = 300
    max_pixels: int = 2007040
    max_new_tokens: int = 8192
    refine: bool = True
    refine_dpi: int = 450
    refine_max_pixels: int = 4014080

    @property
    def policy_hash(self) -> str:
        policy = asdict(self)
        # Endpoint location and execution parallelism do not change recognition policy.
        for key in ("base_url", "layout_device", "batch_pages", "concurrency"):
            policy.pop(key)
        policy.update(pipeline="PaddleOCR-VL-1.6", quality_policy=2, chart_policy="original_images")
        return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Config:
    path: Path
    data_dir: Path
    journals: tuple[Journal, ...]
    lookback_days: int = 30
    overlap_days: int = 3
    request_interval_seconds: float = 1.1
    request_timeout_seconds: float = 45
    max_pdf_megabytes: int = 128
    contact_email_env: str = "JOURNALFLOW_CONTACT_EMAIL"
    unpaywall_email_env: str = "UNPAYWALL_EMAIL"
    vision: Vision = Vision()


def initialize(path: Path) -> Path:
    path = path.resolve()
    if path.exists():
        raise ValueError(f"Configuration already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(files("journalflow").joinpath("presets/four-journals.toml").read_text(), encoding="utf-8")
    return path


def load(path: Path) -> Config:
    path = path.resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    journals = tuple(Journal(j["key"], j["name"], tuple(j["issns"])) for j in raw.get("journals", []))
    if not journals or len({j.key for j in journals}) != len(journals):
        raise ValueError("Configure at least one journal, with unique journal keys")
    for journal in journals:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", journal.key):
            raise ValueError("Journal keys may contain only letters, digits, underscores, and hyphens")
        if not journal.issns or any(not re.fullmatch(r"\d{4}-\d{3}[\dXx]", i) for i in journal.issns):
            raise ValueError(f"Invalid ISSN configuration for {journal.key}")
    vision = Vision(**raw.get("vision", {}))
    if not 1 <= vision.batch_pages <= 64 or not 1 <= vision.concurrency <= 128:
        raise ValueError("batch_pages must be 1–64 and concurrency 1–128")
    if not 72 <= vision.render_dpi <= vision.refine_dpi <= 600:
        raise ValueError("Require 72 <= render_dpi <= refine_dpi <= 600")
    if not 112896 <= vision.max_pixels <= vision.refine_max_pixels <= 4014080:
        raise ValueError("Image budgets must be 112896–4014080, with refinement >= primary")
    if not 128 <= vision.max_new_tokens <= 8192:
        raise ValueError("max_new_tokens must be 128–8192 for the documented model context")
    values = {key: raw[key] for key in Config.__dataclass_fields__ if key in raw and key not in
              {"path", "data_dir", "journals", "vision"}}
    config = Config(path, (path.parent / raw.get("data_dir", "data")).resolve(), journals,
                    vision=vision, **values)
    if config.lookback_days < 1 or config.overlap_days < 0:
        raise ValueError("lookback_days must be positive and overlap_days nonnegative")
    if config.request_interval_seconds < 0 or config.request_timeout_seconds <= 0:
        raise ValueError("Invalid HTTP pacing or timeout")
    if config.max_pdf_megabytes <= 0:
        raise ValueError("max_pdf_megabytes must be positive")
    return config
