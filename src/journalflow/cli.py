from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import httpx
from filelock import FileLock, Timeout

from . import __version__
from .config import initialize, load
from .conversion import convert
from .discovery import discover, normalize
from .http import Http
from .intake import VERSIONS, fetch, install_pdf
from .state import State
from .util import normalize_doi


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def parser():
    root = argparse.ArgumentParser(prog="journalflow", description="Incremental journal PDF and visual Markdown library")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", type=Path, default=Path("journalflow.toml"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Write a four-journal example configuration")
    source = commands.add_parser("discover", help="Discover bibliographic records without downloading PDFs")
    source.add_argument("--since", type=date.fromisoformat)
    source.add_argument("--until", type=date.fromisoformat)
    source.add_argument("--retry-source", action="store_true", help="Retry an access-blocked metadata source")
    download = commands.add_parser("fetch", help="Fetch available OA PDFs through Unpaywall")
    download.add_argument("--limit", type=positive, default=20)
    download.add_argument("--retry-access", action="store_true", help="Recheck items waiting for access")
    intake = commands.add_parser("import-pdf", help="Import an already-authorized local PDF")
    intake.add_argument("path", type=Path)
    intake.add_argument("--doi", required=True)
    intake.add_argument("--version", choices=VERSIONS, default="unknown")
    intake.add_argument("--journal", help="Configured journal key; required for uncataloged DOIs")
    intake.add_argument("--title", help="Required for uncataloged DOIs")
    conversion = commands.add_parser("convert", help="Convert queued PDFs with the full visual pipeline")
    conversion.add_argument("--limit", type=positive, default=20)
    for command in ("run", "watch"):
        cycle = commands.add_parser(command, help="Run one cycle" if command == "run" else "Repeat cycles")
        cycle.add_argument("--limit", type=positive, default=20)
        cycle.add_argument("--without-vision", action="store_true", help="Discover/fetch only; keep conversion queued")
        if command == "watch":
            cycle.add_argument("--interval-hours", type=float, default=24)
    commands.add_parser("status", help="Show queue states and discovery checkpoints")
    search = commands.add_parser("search", help="Search selected page transcriptions with SQLite FTS5")
    search.add_argument("query")
    search.add_argument("--limit", type=positive, default=10)
    doctor = commands.add_parser("doctor", help="Check the core environment and optional vision service")
    doctor.add_argument("--vision", action="store_true")
    return root


def doctor(config, check_vision: bool):
    report = dict(version=__version__, python=sys.version.split()[0],
                  pdfium=importlib.metadata.version("pypdfium2"),
                  contact_email_configured=bool(os.environ.get(config.contact_email_env)),
                  unpaywall_email_configured=bool(os.environ.get(config.unpaywall_email_env)),
                  vision_client_installed=importlib.util.find_spec("paddleocr") is not None)
    if check_vision:
        try:
            with httpx.Client(timeout=5) as client:
                response = client.get(config.vision.base_url.rstrip("/") + "/models")
                response.raise_for_status()
                report["model_available"] = any(model["id"] == config.vision.model
                                                for model in response.json()["data"])
        except (httpx.HTTPError, ValueError, KeyError) as error:
            report.update(model_available=False, model_error=type(error).__name__)
    return report


def operate(config, args):
    state = State(config.data_dir)
    http = None
    try:
        if args.command == "status":
            return state.summary()
        if args.command == "search":
            return state.search(args.query, args.limit)
        if args.command == "doctor":
            return doctor(config, args.vision)
        if args.command == "import-pdf":
            doi = normalize_doi(args.doi)
            if not state.paper(doi):
                if not args.title or not args.journal:
                    raise ValueError("For an uncataloged DOI, provide --journal and --title")
                journal = next((journal for journal in config.journals if journal.key == args.journal), None)
                if journal is None:
                    raise ValueError("--journal must be a key in your configuration")
                state.upsert(normalize(dict(DOI=doi, title=[args.title], type="journal-article"), journal))
            return install_pdf(config, state, args.path, doi, args.version)
        if args.command == "convert":
            return convert(config, state, args.limit)
        http = Http(config, state=state)
        if args.command == "discover":
            if args.retry_source:
                state.retry_access("api.crossref.org")
            return discover(config, state, http, args.since, args.until)
        if args.command == "fetch":
            if args.retry_access:
                state.retry_access()
            return fetch(config, state, http, args.limit, args.retry_access)
        result = dict(discovery=discover(config, state, http), fetch=fetch(config, state, http, args.limit))
        if not args.without_vision:
            result["conversion"] = convert(config, state, args.limit)
        result["status"] = state.summary()
        return result
    finally:
        if http:
            http.close()
        state.close()


def failed(result):
    if isinstance(result, dict):
        return bool(result.get("failed") or result.get("errors") or result.get("deferred") or
                    any(failed(result.get(stage)) for stage in ("discovery", "fetch", "conversion")))
    return False


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            emit(dict(configuration=str(initialize(args.config))))
            return 0
        config = load(args.config)
        if args.command == "watch" and args.interval_hours <= 0:
            raise ValueError("--interval-hours must be positive")
        while True:
            if args.command in ("status", "search", "doctor"):
                result = operate(config, args)
            else:
                config.data_dir.mkdir(parents=True, exist_ok=True)
                with FileLock(str(config.data_dir / ".worker.lock"), timeout=0):
                    result = operate(config, args)
            emit(result)
            if args.command != "watch":
                if args.command == "doctor" and args.vision:
                    return 0 if result["vision_client_installed"] and result["model_available"] else 1
                return 1 if failed(result) else 0
            time.sleep(args.interval_hours * 3600)
    except KeyboardInterrupt:
        return 130
    except Timeout:
        emit(dict(error="Another JournalFlow process is mutating this data directory"))
        return 2
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        emit(dict(error=str(error)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
