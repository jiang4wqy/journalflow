from __future__ import annotations

import calendar
import hashlib
import html
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from .config import Config, Journal
from .http import AccessRequired, FetchError, Http, RetryLater
from .state import State
from .util import normalize_doi

CROSSREF = "https://api.crossref.org"


def date_interval(parts: list[int]):
    if len(parts) == 1:
        return date(parts[0], 1, 1), date(parts[0], 12, 31)
    if len(parts) == 2:
        return date(*parts, 1), date(*parts, calendar.monthrange(*parts)[1])
    return date(*parts[:3]), date(*parts[:3])


def in_window(raw: dict, since: date, until: date):
    spans = [date_interval(parts) for key in ("published", "published-online", "published-print")
             for parts in raw.get(key, {}).get("date-parts", []) if parts]
    return any(start <= until and end >= since for start, end in spans)


def normalize(raw: dict, journal: Journal):
    title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", " ".join(raw.get("title", []))))).strip()
    if raw.get("type") != "journal-article" or not title:
        return None
    if re.fullmatch(r"(?:\d{4}\s+)?(?:editorial board|front matter|back matter|front cover|back cover|"
                    r"issue information|table of contents|masthead|cover)", title, re.I):
        return None
    item = dict(doi=normalize_doi(raw["DOI"]), journal=journal.key, title=title,
                authors=[" ".join(filter(None, (a.get("given"), a.get("family")))) or a.get("name", "")
                         for a in raw.get("author", [])],
                dates={key: raw.get(key, {}) for key in ("published", "published-online", "published-print")},
                url=raw.get("URL", ""), links=raw.get("link", []), licenses=raw.get("license", []),
                relations=raw.get("relation", {}), updates=raw.get("update-to", []),
                kind="correction_or_retraction" if re.match(r"^(erratum|corrigendum|correction|retraction)\b", title, re.I)
                else "article")
    item["metadata_hash"] = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return item


def discover(config: Config, state: State, http: Http, since: date | None = None,
             until: date | None = None):
    today = datetime.now(timezone.utc).date()
    end = until or today
    if since and since > end:
        raise ValueError("--since must not be after --until")
    report = dict(seen=0, changed=0, journals_completed=[], errors=[])
    contact = os.environ.get(config.contact_email_env)
    for journal in config.journals:
        checkpoint = state.checkpoint(journal.key)
        historical = since is not None or checkpoint is None
        start = since or (date.fromisoformat(checkpoint) - timedelta(days=config.overlap_days)
                          if checkpoint else end - timedelta(days=config.lookback_days))
        bases = ("pub", "online-pub", "print-pub") if historical else ("index",)
        seen = set()
        try:
            for issn in journal.issns:
                for basis in bases:
                    cursor = "*"
                    cursors = set()
                    while True:
                        params = dict(filter=f"from-{basis}-date:{start},until-{basis}-date:{end}",
                                      rows=200, cursor=cursor)
                        if contact:
                            params["mailto"] = contact
                        message = http.json(f"{CROSSREF}/journals/{issn}/works", params)["message"]
                        records = message["items"]
                        for raw in records:
                            if historical and not in_window(raw, start, end):
                                continue
                            item = normalize(raw, journal)
                            if item and item["doi"] not in seen:
                                seen.add(item["doi"])
                                report["seen"] += 1
                                report["changed"] += state.upsert(item)
                        if len(records) < 200:
                            break
                        following = message.get("next-cursor")
                        if not following or following == cursor or following in cursors:
                            raise FetchError("Crossref returned a missing or repeated cursor")
                        cursors.add(cursor)
                        cursor = following
            # A historical backfill must not move the live incremental checkpoint backwards.
            if end >= today and (not checkpoint or end.isoformat() >= checkpoint):
                state.save_checkpoint(journal.key, end.isoformat())
            report["journals_completed"].append(journal.key)
        except (FetchError, KeyError, ValueError) as error:
            state.save_checkpoint(journal.key, None, str(error))
            report["errors"].append(dict(journal=journal.key, error=str(error)))
            if isinstance(error, (RetryLater, AccessRequired)):
                break
    return report
