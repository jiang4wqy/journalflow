"""Small loopback dashboard; all mutations go through explicit CLI subprocesses."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import tomllib
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import load
from .scheduling import WEEKDAYS, configured_schedule, gate_status

PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JournalFlow 本地工作台</title><style>
body{max-width:880px;margin:40px auto;padding:0 20px;font:16px/1.7 system-ui;color:#17212b;background:#f6f8fa}
section{background:white;padding:22px;border:1px solid #d7dfe6;border-radius:12px;margin:18px 0}
button{font:inherit;padding:10px 15px;margin:6px 8px 6px 0;cursor:pointer}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8fa;padding:14px}
#summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
#summary div{border:1px solid #d7dfe6;padding:12px;border-radius:8px}#summary span{display:block;font-size:23px}
small{color:#52606d}h1{font-size:28px}</style>
<h1>JournalFlow 本地工作台</h1>
<p>查新、查看队列并处理增量批次。论文保存在本机；本界面不会自动上传文件或启用定时任务。</p>
<section><h2>操作</h2>
<button data-action="discover">查新：只更新论文目录</button>
<button data-action="batch">推进批次：最多 10 条任务</button>
<button data-action="connect">接入 Codex</button>
<p><small>批次遵守配置及流水线的访问、视觉转换暂停和验证条件。疑难项保留待处理。
暂停下载或视觉阶段时只执行允许的查新与排队，不计为已下载或已转换。
关闭网页不会取消已启动的 CLI 批次；终端窗口用于维持工作台。</small></p>
<p><small>接入完成后，在 Codex 打开配置文件所在目录并信任此项目。只生成项目的 .codex/config.toml，
已有不同配置会拒绝覆盖，不修改全局账号，也不触发下载或视觉转换。</small></p>
<p id="message" role="status"></p></section>
<section><h2>库状态</h2><div id="summary" aria-live="polite">正在读取本机状态…</div>
<p><small>目录记录是书目信息；已登记旧 PDF 是外部基线版本，本机新增 PDF 是本项目获取或导入的文件。</small></p>
<details><summary>诊断信息与配置路径</summary><pre id="status"></pre></details></section>
<section><h2>每周更新</h2><p>默认：周一 10:00，Asia/Shanghai。首次必须完成基线、10 篇真实下载及转换试运行、区域索引验证。
历史留出样本回放不能代替真实试运行。任务安装需另行执行明确的安装命令；本页面不启用调度。</p></section>
<script nonce="__NONCE__">
const token=__TOKEN__;
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});const data=await r.json();
document.querySelector('#status').textContent=JSON.stringify(data,null,2);
if(!r.ok)throw new Error('status unavailable');const lib=data.library;const states=lib.fetch_states;
const pending=(states.pending||0)+(states.failed||0)+(states.waiting_access||0);
const rows=[['目录记录',lib.papers],['已登记旧 PDF',lib.external_pdf_versions],['本机新增 PDF',lib.pdfs],
['待获取',pending],['下载暂停',data.paused_download?'是':'否'],['识别暂停',data.paused_vision?'是':'否'],
['周任务',data.schedule_installed?'已启用':'未启用']];
const box=document.querySelector('#summary');box.replaceChildren();for(const [label,value] of rows){
const item=document.createElement('div');const title=document.createElement('small');title.textContent=label;
const number=document.createElement('span');number.textContent=String(value);item.append(title,number);box.append(item)}}
catch(e){document.querySelector('#status').textContent='本地工作台已停止，请重新打开启动脚本。'}}
document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',async()=>{
button.disabled=true;try{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json',
'X-JournalFlow-Token':token},body:JSON.stringify({action:button.dataset.action,limit:10})});
const data=await r.json();document.querySelector('#message').textContent=data.error||'已启动，请查看状态与本地日志。';
await refresh()}catch(e){document.querySelector('#message').textContent='请求未成功，请检查终端。'}finally{button.disabled=false}}));
refresh();setInterval(refresh,4000);
</script></html>"""


class Dashboard:
    def __init__(self, config):
        self.config = config
        self.token = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.job = None

    def command(self, action, limit=10):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Batch limit must be an integer between 1 and 100")
        command = [sys.executable, "-m", "journalflow", "--config", str(self.config.path)]
        if action == "discover":
            return command + ["discover"]
        if action == "batch":
            return command + ["pipeline", "--limit", str(limit)]
        if action == "connect":
            return command + ["connect-codex"]
        raise ValueError("Unknown dashboard action")

    def status(self):
        raw = tomllib.loads(self.config.path.read_text(encoding="utf-8"))
        automation = raw.get("automation", {})
        regional = raw.get("regional", {})
        regional_paths = {key: str((self.config.path.parent / value).resolve())
                          for key, value in regional.items()
                          if key in {"base_index", "library_root", "output_root", "active_pointer", "model_dir"}
                          and isinstance(value, str) and value}
        counts = {"papers": 0, "pdfs": 0, "conversions": 0, "external_pdf_versions": 0,
                  "fetch_states": {name: 0 for name in ("pending", "failed", "waiting_access",
                                                       "downloaded", "baseline_available")}}
        database = self.config.data_dir / "library.sqlite"
        if database.is_file():
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("papers", "pdfs", "conversions"):
                    if table in tables:
                        counts[table] = connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
                if "papers" in tables:
                    counts["fetch_states"].update(connection.execute("SELECT fetch_state,count(*) FROM papers GROUP BY fetch_state"))
                if "baseline_sources" in tables:
                    counts["external_pdf_versions"] = connection.execute(
                        "SELECT count(*) FROM baseline_sources WHERE available=1").fetchone()[0]
        inbox = self.config.data_dir / "inbox"
        count = sum(path.is_file() and path.suffix.lower() == ".pdf" for path in inbox.iterdir()) if inbox.is_dir() else 0
        with self.lock:
            job = dict(self.job) if self.job else None
        default = configured_schedule(self.config)
        return {"config": str(self.config.path), "data_directory": str(self.config.data_dir),
                "pdf_inbox": str(inbox), "pdfs_in_inbox": count, "library": counts,
                "current_session_job": job, "schedule_gate": gate_status(self.config),
                "paused_vision": automation.get("vision_enabled", False) is not True,
                "paused_download": automation.get("download_enabled", False) is not True,
                "regional_configuration": regional_paths,
                "contact_email_configured": bool(os.environ.get(self.config.contact_email_env)),
                "unpaywall_email_configured": bool(os.environ.get(self.config.unpaywall_email_env)),
                "schedule_installed": (self.config.data_dir / "automation/schedule-receipt.json").exists(),
                "configured_schedule": f"{WEEKDAYS[default.weekday]} {default.hour:02}:{default.minute:02} {default.timezone}"}

    def start(self, action, limit=10):
        command = self.command(action, limit)
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise RuntimeError("A batch is already running in this dashboard")
            folder = self.config.data_dir / "automation/desktop-logs"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            log = folder / f"{stamp}-{secrets.token_hex(3)}.log"
            self.job = {"action": action, "state": "running", "started_at": stamp, "log": str(log)}
            result = dict(self.job)
        threading.Thread(target=self._execute, args=(command, log), daemon=True).start()
        return result

    def _execute(self, command, log):
        code = 2
        try:
            with log.open("wb") as output:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                           stderr=subprocess.STDOUT, cwd=self.config.path.parent, shell=False)
                with self.lock:
                    self.job["pid"] = process.pid
                code = process.wait()
        except OSError:
            with log.open("ab") as output:
                output.write(b"The CLI could not be started. Check the local Python installation.\n")
        finally:
            with self.lock:
                self.job.update(state="completed" if code == 0 else "needs_attention", returncode=code,
                                finished_at=datetime.now(timezone.utc).isoformat())


def make_server(dashboard, port=0):
    class Handler(BaseHTTPRequestHandler):
        server_version = "JournalFlow"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format, *args):
            pass  # Do not persist request paths or the in-memory action token.

        def discard_small_body(self):
            # Draining a small rejected request prevents Windows TCP from
            # resetting the connection before the HTTP error reaches the client.
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return
            if 0 < size <= 4096:
                self.rfile.read(size)

        def allowed(self, mutation=False):
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Host") != [host]:
                return False
            origins = self.headers.get_all("Origin") or []
            expected = ["http://" + host]
            if origins and origins != expected:
                return False
            if mutation and (origins != expected or self.headers.get_all("X-JournalFlow-Token") != [dashboard.token]):
                return False
            return True

        def respond(self, code, value, html=False):
            body = value.encode("utf-8") if html else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
                             f"script-src 'nonce-{dashboard.nonce}'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed():
                return self.respond(403, {"error": "Local Host/Origin required"})
            if self.path == "/":
                page = PAGE.replace("__TOKEN__", json.dumps(dashboard.token)).replace("__NONCE__", dashboard.nonce)
                return self.respond(200, page, html=True)
            if self.path == "/api/status":
                try:
                    return self.respond(200, dashboard.status())
                except (OSError, sqlite3.Error) as error:
                    return self.respond(503, {"error": type(error).__name__ + ": local state is temporarily unavailable"})
            return self.respond(405 if self.path == "/api/action" else 404, {"error": "No GET action endpoint"})

        def do_POST(self):
            if not self.allowed(mutation=True):
                self.discard_small_body()
                return self.respond(403, {"error": "Local Origin and action token required"})
            if self.path != "/api/action":
                self.discard_small_body()
                return self.respond(404, {"error": "Unknown endpoint"})
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                self.discard_small_body()
                return self.respond(415, {"error": "Use application/json"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 1 <= size <= 2048:
                    self.discard_small_body()
                    return self.respond(400, {"error": "Invalid request size"})
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict) or set(value) - {"action", "limit"}:
                    raise ValueError("Invalid action request")
                job = dashboard.start(value.get("action"), value.get("limit", 10))
                return self.respond(202, {"job": job})
            except (ValueError, UnicodeError) as error:
                return self.respond(400, {"error": str(error)})
            except RuntimeError as error:
                return self.respond(409, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main(config_path=None, *, port=0, open_browser=True):
    if config_path is None:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--config", type=Path, default=Path("journalflow.toml"))
        parser.add_argument("--port", type=int, default=0)
        parser.add_argument("--no-browser", action="store_true")
        args = parser.parse_args()
        config_path, port, open_browser = args.config, args.port, not args.no_browser
    dashboard = Dashboard(load(Path(config_path)))
    with make_server(dashboard, port) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print("JournalFlow local dashboard: " + url, flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
