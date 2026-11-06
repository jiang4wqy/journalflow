import http.client
import json
import sqlite3
import threading

import pytest

from journalflow.config import initialize, load
from journalflow.desktop import Dashboard, make_server


@pytest.fixture
def dashboard(tmp_path):
    path = initialize(tmp_path / "中文 space" / "journalflow.toml")
    base = path.read_text(encoding="utf-8").split("\n[automation]", 1)[0]
    path.write_text(base + '\n[automation]\nvision_enabled=false\n[regional]\nbase_index="index path"\n',
                    encoding="utf-8")
    return Dashboard(load(path))


@pytest.fixture
def endpoint(dashboard, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "start", lambda action, limit: calls.append((action, limit)) or {"state": "running"})
    server = make_server(dashboard)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield dashboard, server.server_port, calls
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def request(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, headers=headers or {}, body=body)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def action_headers(dashboard, port):
    return {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}",
            "X-JournalFlow-Token": dashboard.token}


def test_post_requires_local_origin_host_and_random_token(endpoint):
    dashboard, port, calls = endpoint
    body = json.dumps({"action": "discover", "limit": 10})
    headers = action_headers(dashboard, port)
    assert request(port, "POST", "/api/action", headers, body)[0] == 202
    assert calls == [("discover", 10)]
    for field, value in (("Origin", "https://attacker.example"), ("Origin", "null"),
                         ("X-JournalFlow-Token", "wrong"), ("Host", f"attacker.example:{port}")):
        assert request(port, "POST", "/api/action", {**headers, field: value}, body)[0] == 403
    missing_origin = {key: value for key, value in headers.items() if key != "Origin"}
    assert request(port, "POST", "/api/action", missing_origin, body)[0] == 403
    assert len(calls) == 1
    assert request(port, "GET", "/api/status")[0] == 200


def test_get_cannot_trigger_action_and_cors_cannot_read_status(endpoint):
    dashboard, port, calls = endpoint
    assert request(port, "GET", "/api/action")[0] == 405
    assert request(port, "GET", "/api/status", {"Origin": "https://attacker.example"})[0] == 403
    status, headers, body = request(port, "GET", "/")
    assert status == 200 and dashboard.token.encode() in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in headers
    assert not calls


def test_json_content_type_and_payload_size_are_checked(endpoint):
    dashboard, port, calls = endpoint
    headers = action_headers(dashboard, port)
    assert request(port, "POST", "/api/action", {**headers, "Content-Type": "text/plain"}, "{}")[0] == 415
    assert request(port, "POST", "/api/action", headers, "x" * 2050)[0] == 400
    assert request(port, "POST", "/api/action", headers, '["batch"]')[0] == 400
    assert not calls


def test_commands_are_argument_arrays_and_allowlisted(dashboard):
    command = dashboard.command("batch", 10)
    assert command[-3:] == ["pipeline", "--limit", "10"]
    assert str(dashboard.config.path) in command
    assert dashboard.command("discover")[-1] == "discover"
    assert dashboard.command("connect")[-1] == "connect-codex"
    for action, limit in (("batch; rm -rf", 10), ("batch", True), ("batch", 0), ("batch", "10")):
        with pytest.raises(ValueError):
            dashboard.command(action, limit)


def test_status_exposes_pause_and_paths_not_environment_values(dashboard, monkeypatch):
    monkeypatch.setenv(dashboard.config.contact_email_env, "private@example.invalid")
    monkeypatch.setenv(dashboard.config.unpaywall_email_env, "hidden@example.invalid")
    status = dashboard.status()
    assert status["paused_vision"] is True
    assert status["paused_download"] is True
    assert status["contact_email_configured"] and status["unpaywall_email_configured"]
    assert status["regional_configuration"]["base_index"].endswith("index path")
    assert not status["schedule_installed"] and not status["schedule_gate"]["ready"]
    assert "example.invalid" not in json.dumps(status)
    assert not (dashboard.config.data_dir / "library.sqlite").exists()  # GET status stays read-only.


def test_batch_child_has_no_shell_and_second_active_job_is_rejected(dashboard, monkeypatch, tmp_path):
    calls = []

    class Process:
        pid = 123

        def wait(self):
            return 1

    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr("journalflow.desktop.subprocess.Popen", launch)
    dashboard.job = {"state": "running"}
    with pytest.raises(RuntimeError, match="already running"):
        dashboard.start("batch")
    dashboard._execute(dashboard.command("batch"), tmp_path / "cli.log")
    assert calls[0][0][-3:] == ["pipeline", "--limit", "10"]
    assert calls[0][1]["shell"] is False
    assert dashboard.job["state"] == "needs_attention"


def test_metadata_and_external_baseline_counts_are_not_reported_as_new_pdfs(dashboard):
    dashboard.config.data_dir.mkdir(parents=True)
    with sqlite3.connect(dashboard.config.data_dir / "library.sqlite") as connection:
        connection.executescript("""
            CREATE TABLE papers(fetch_state TEXT);
            INSERT INTO papers VALUES('pending'),('baseline_available'),('waiting_access');
            CREATE TABLE baseline_sources(available INTEGER);
            INSERT INTO baseline_sources VALUES(1),(1),(0);
        """)
    counts = dashboard.status()["library"]
    assert counts["papers"] == 3
    assert counts["pdfs"] == 0
    assert counts["external_pdf_versions"] == 2
    assert counts["fetch_states"]["pending"] == 1
    assert counts["fetch_states"]["waiting_access"] == 1
