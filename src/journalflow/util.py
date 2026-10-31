from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_doi(value: str) -> str:
    value = unquote(value.strip())
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I).lower()
    if not re.fullmatch(r"10\.\d{4,9}/[^\s]+", value):
        raise ValueError("Expected a DOI such as 10.1234/example")
    return value


def slug(doi: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", doi.lower()).strip("_")[:90] + "__" + hashlib.sha256(
        doi.encode()).hexdigest()[:10]


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def public_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
