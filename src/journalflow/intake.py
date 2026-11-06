from __future__ import annotations

import json
import os
import shutil
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .config import Config
from .http import AccessRequired, FetchError, Http, RetryLater
from .pdf import Document
from .state import State
from .util import digest_file, normalize_doi, public_url, slug

UNPAYWALL = "https://api.unpaywall.org/v2"
VERSIONS = ("published", "accepted", "preprint", "unknown")


def apply_fetch_scope(config: Config, state: State):
    """Recent metadata updates can concern old articles; do not backfill blindly."""
    raw = tomllib.loads(config.path.read_text(encoding="utf-8")) if config.path.is_file() else {}
    since = raw.get("automation", {}).get("publication_since")
    if not since:
        return 0
    from .discovery import in_window
    start, end = date.fromisoformat(since), datetime.now(timezone.utc).date()
    changes = []
    for row in state.connection.execute("SELECT doi,metadata,fetch_state FROM papers WHERE fetch_state "
                                        "IN ('pending','failed','waiting_access','outside_scope')").fetchall():
        metadata = json.loads(row["metadata"])
        keep = in_window(metadata.get("dates", {}), start, end)
        if not keep and row["fetch_state"] != "outside_scope":
            changes.append(('outside_scope', 'Publication dates outside the configured collection', row['doi']))
        elif keep and row["fetch_state"] == "outside_scope":
            changes.append(('pending', None, row['doi']))
    with state.connection:
        state.connection.executemany('UPDATE papers SET fetch_state=?,last_error=?,retry_at=NULL WHERE doi=?', changes)
    return sum(status == 'outside_scope' for status, _, _ in changes)


def install_pdf(config: Config, state: State, path: Path, doi: str,
                version: str = "unknown", source: str = "local_import"):
    doi = normalize_doi(doi)
    if not state.paper(doi):
        raise ValueError("DOI is not in the catalog; discover it first or import metadata")
    if version not in VERSIONS:
        raise ValueError("Unknown source version")
    with Document(path) as document:
        pages = len(document)
    sha = digest_file(path)
    target = config.data_dir / "pdfs" / slug(doi) / f"{sha}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if digest_file(target) != sha:
            raise ValueError("Existing PDF artifact failed its hash check")
    else:
        temporary = target.with_suffix(".pdf.part")
        shutil.copyfile(path, temporary)
        if digest_file(temporary) != sha:
            temporary.unlink()
            raise ValueError("PDF copy failed its hash check")
        temporary.replace(target)
    return state.add_pdf(doi, sha, target, pages, version, public_url(source) if "://" in source else source)


def fetch(config: Config, state: State, http: Http, limit: int = 20, retry: bool = False):
    report = dict(downloaded=0, waiting_access=0, failed=0, deferred=0,
                  excluded_from_fetch_scope=apply_fetch_scope(config, state))
    email = os.environ.get(config.unpaywall_email_env)
    from .baseline import baseline_dois
    available = baseline_dois(state)
    for paper in state.pending_fetch(limit, retry):
        doi = paper["doi"]
        if doi in available:
            state.fetch_result(doi, "baseline_available")
            continue
        temporary = config.data_dir / "incoming" / (slug(doi) + ".part")
        try:
            if not email:
                raise AccessRequired(f"Set {config.unpaywall_email_env} for OA discovery, or import an authorized PDF")
            record = http.json(f"{UNPAYWALL}/{quote(doi, safe='/')}", dict(email=email))
            locations = [record.get("best_oa_location")] + (record.get("oa_locations") or [])
            location = next((loc for loc in locations if loc and loc.get("url_for_pdf")), None)
            if not record.get("is_oa") or not location:
                raise AccessRequired("No open-access PDF location found; import an authorized PDF")
            url = location["url_for_pdf"]
            temporary.parent.mkdir(parents=True, exist_ok=True)
            maximum = config.max_pdf_megabytes * 1024 ** 2
            with http.stream(url) as response, temporary.open("wb") as stream:
                length = response.headers.get("content-length")
                if length and int(length) > maximum:
                    raise FetchError("PDF exceeds the configured size limit")
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > maximum:
                        raise FetchError("PDF exceeds the configured size limit")
                    stream.write(chunk)
            version = {"publishedVersion": "published", "acceptedVersion": "accepted",
                       "submittedVersion": "preprint"}.get(location.get("version"), "unknown")
            install_pdf(config, state, temporary, doi, version, url)
            report["downloaded"] += 1
        except RetryLater as error:
            state.fetch_result(doi, "failed", str(error), error.retry_at)
            report["deferred"] += 1
        except AccessRequired as error:
            state.fetch_result(doi, "waiting_access", str(error))
            report["waiting_access"] += 1
        except (FetchError, ValueError, OSError) as error:
            state.fetch_result(doi, "failed", str(error))
            report["failed"] += 1
        finally:
            temporary.unlink(missing_ok=True)
    return report
