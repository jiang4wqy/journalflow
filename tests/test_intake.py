from datetime import datetime, timezone

import httpx
import pytest

from journalflow.http import FetchError, Http, RetryLater
from journalflow.intake import fetch, install_pdf

from .conftest import TEXT, make_pdf


def test_import_is_content_addressed_and_preserves_versions(config, state, paper, tmp_path):
    first = make_pdf(tmp_path / "first.pdf")
    one = install_pdf(config, state, first, paper["doi"], "accepted")
    duplicate = install_pdf(config, state, first, paper["doi"], "accepted")
    assert one["id"] == duplicate["id"]
    changed = make_pdf(tmp_path / "changed.pdf", (TEXT + " Corrected experiment.",))
    two = install_pdf(config, state, changed, paper["doi"], "published")
    assert two["sha256"] != one["sha256"]
    assert (config.data_dir / one["path"]).read_bytes() == first.read_bytes()
    assert state.summary()["pdf_files"] == 2


def test_oa_download_uses_pdf_location(config, state, paper, tmp_path, monkeypatch):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "tester@example.org")
    body = make_pdf(tmp_path / "example.pdf").read_bytes()

    def handler(request):
        if request.url.host == "api.unpaywall.org":
            return httpx.Response(200, json={"is_oa": True, "best_oa_location": {
                "url_for_pdf": "https://papers.example.org/example.pdf?temporary=signed",
                "version": "publishedVersion"}, "oa_locations": []})
        return httpx.Response(200, content=body)

    http = Http(config, transport=httpx.MockTransport(handler), validate_hosts=False)
    try:
        assert fetch(config, state, http)["downloaded"] == 1
        pdf = state.conversion_queue(config.vision.policy_hash, 1)[0]
        assert pdf["version"] == "published"
        assert "temporary" not in pdf["source_url"]
    finally:
        http.close()


def test_html_response_is_not_marked_downloaded(config, state, paper, monkeypatch):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "tester@example.org")

    def handler(request):
        if request.url.host == "api.unpaywall.org":
            return httpx.Response(200, json={"is_oa": True, "best_oa_location": {
                "url_for_pdf": "https://papers.example.org/blocked"}, "oa_locations": []})
        return httpx.Response(200, text="<html>Please log in</html>")

    http = Http(config, transport=httpx.MockTransport(handler), validate_hosts=False)
    try:
        assert fetch(config, state, http)["failed"] == 1
        assert state.summary()["pdf_files"] == 0
        assert not list((config.data_dir / "incoming").glob("*.part"))
    finally:
        http.close()


def test_missing_oa_keeps_access_queue(config, state, paper, monkeypatch):
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    http = Http(config, validate_hosts=False)
    try:
        assert fetch(config, state, http)["waiting_access"] == 1
        assert state.paper(paper["doi"])["fetch_state"] == "waiting_access"
        assert state.pending_fetch(20) == []
        assert len(state.pending_fetch(20, retry=True)) == 1
    finally:
        http.close()


def test_retry_after_is_persisted(config, state, paper, monkeypatch):
    monkeypatch.setenv("UNPAYWALL_EMAIL", "tester@example.org")
    http = Http(config, transport=httpx.MockTransport(lambda request: httpx.Response(
        429, headers={"Retry-After": "3600"})), validate_hosts=False)
    try:
        assert fetch(config, state, http)["deferred"] == 1
        assert datetime.fromisoformat(state.paper(paper["doi"])["retry_at"]) > datetime.now(timezone.utc)
        assert state.pending_fetch(20) == []
    finally:
        http.close()


def test_private_urls_and_embedded_credentials_are_rejected(config):
    http = Http(config)
    try:
        with pytest.raises(FetchError):
            http.validate_url("http://127.0.0.1/private")
        with pytest.raises(FetchError):
            http.validate_url("https://name:password@example.org/file.pdf")
    finally:
        http.close()


def test_source_block_does_not_keep_requesting(config):
    calls = []
    http = Http(config, transport=httpx.MockTransport(lambda request: (
        calls.append(request) or httpx.Response(429, headers={"Retry-After": "60"}))), validate_hosts=False)
    try:
        for _ in range(2):
            with pytest.raises(RetryLater):
                http.json("https://papers.example.org/file")
        assert len(calls) == 1
    finally:
        http.close()


def test_rate_limit_survives_new_client_and_manual_access_retry(config, state):
    calls = []
    transport = httpx.MockTransport(lambda request: (
        calls.append(request) or httpx.Response(429, headers={"Retry-After": "3600"})))
    for _ in range(2):
        http = Http(config, state=state, transport=transport, validate_hosts=False)
        try:
            state.retry_access()  # Explicit access retries must not override Retry-After.
            with pytest.raises(RetryLater):
                http.json("https://papers.example.org/file")
        finally:
            http.close()
    assert len(calls) == 1
