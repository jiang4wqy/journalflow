from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx

from journalflow.discovery import discover, normalize
from journalflow.http import Http


def test_deduplicates_dois_and_ignores_index_noise(config, state, raw_record):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"message": {"items": [raw_record]}})

    http = Http(config, transport=httpx.MockTransport(handler), validate_hosts=False)
    try:
        first = discover(config, state, http)
        assert first["changed"] == 1
        assert len(calls) == 6  # two ISSNs and three date bases
        state.fetch_result("10.1234/example", "downloaded")
        raw_record["indexed"] = {"date-time": "2099-01-01T00:00:00Z"}
        second = discover(config, state, http)
        assert second["changed"] == 0
        assert state.paper("10.1234/example")["fetch_state"] == "downloaded"
        assert state.summary()["papers"] == 1
        assert "from-index-date" in str(calls[-1].url)
    finally:
        http.close()


def test_changed_metadata_updates_catalog_without_redownloading(config, state, raw_record):
    original = normalize(raw_record, config.journals[0])
    state.upsert(original)
    state.fetch_result(original["doi"], "downloaded")
    raw_record["title"] = ["Corrected research title"]
    assert state.upsert(normalize(raw_record, config.journals[0]))
    assert state.paper(original["doi"])["fetch_state"] == "downloaded"
    assert state.paper(original["doi"])["title"] == "Corrected research title"
    assert state.summary()["papers"] == 1


def test_partial_discovery_does_not_advance_checkpoint(config, state, raw_record):
    def handler(request):
        if "online-pub" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, json={"message": {"items": [raw_record]}})

    http = Http(config, transport=httpx.MockTransport(handler), validate_hosts=False)
    try:
        result = discover(config, state, http)
        assert result["errors"]
        assert state.checkpoint("DEMO") is None
        assert state.summary()["papers"] == 1
    finally:
        http.close()


def test_rate_limit_stops_source_without_advancing(config, state):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    http = Http(config, transport=httpx.MockTransport(handler), validate_hosts=False)
    try:
        result = discover(config, state, http)
        assert result["errors"] and len(calls) == 1
        assert state.checkpoint("DEMO") is None
    finally:
        http.close()


def test_historical_backfill_does_not_move_live_checkpoint(config, state, raw_record):
    today = datetime.now(timezone.utc).date()
    state.save_checkpoint("DEMO", today.isoformat())
    http = Http(config, transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"message": {"items": [raw_record]}})), validate_hosts=False)
    try:
        discover(config, state, http, today - timedelta(days=90), today - timedelta(days=60))
        assert state.checkpoint("DEMO") == today.isoformat()
    finally:
        http.close()


def test_filters_issue_covers_but_keeps_corrections(config, raw_record):
    raw_record["title"] = ["Editorial Board"]
    assert normalize(raw_record, config.journals[0]) is None
    raw_record["title"] = ["Correction: research title"]
    assert normalize(raw_record, config.journals[0])["kind"] == "correction_or_retraction"


def test_bad_cursor_does_not_advance(config, state, raw_record):
    config = replace(config, journals=(config.journals[0],))
    records = [{**raw_record, "DOI": f"10.1234/example{i}"} for i in range(200)]
    http = Http(config, transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"message": {"items": records, "next-cursor": "*"}})), validate_hosts=False)
    try:
        assert discover(config, state, http)["errors"]
        assert state.checkpoint("DEMO") is None
    finally:
        http.close()
