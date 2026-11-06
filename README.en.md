# JournalFlow

Version 0.3 adds a local browser dashboard, project-scoped Codex setup, external-corpus deduplication baselines, reviewed regional-index increments, and gated weekly scheduling. This release provides GitHub source and a source ZIP, without a packaged installation-free Python runtime. Python 3.11/3.12 and network access for first-time dependency installation are required; regional retrieval models and the visual model service must be configured separately. Downloading or extracting the source starts nothing. Users may explicitly invoke the optional Windows/macOS launchers under `scripts/`.

New configurations pause downloads and vision by default. A real ten-document download/conversion trial and regional acceptance are required before weekly jobs can be enabled. Historical holdout replays never satisfy that live gate. Public distributions contain code and synthetic examples, not journal PDFs, private indexes, credentials, or model weights. See [weekly updates](docs/weekly-updates.md) and [regional integration](docs/regional-updates.md).

[中文](README.md) · [Vision setup](docs/vision.md) · [Scheduled operation](docs/automation.md)

A Python CLI connecting incremental journal discovery, authorized PDF intake, visual Markdown conversion, difficult-page review, and page-level keyword search.

## Features

- Configurable journal/ISSN lists; the bundled preset contains IJOC, MIS Quarterly, Information Systems Research, and Management Science.
- Crossref discovery with publication-date backfills, overlapping incremental windows, DOI deduplication, and durable checkpoints.
- OA PDF locations from Unpaywall, or explicit import of PDFs obtained through the user's authorized access.
- Real PDF validation, page counts, hashes, and separate content versions.
- The full PaddleOCR-VL 1.6 layout-and-recognition pipeline through a compatible vLLM service.
- 300 DPI primary recognition and selective 450 DPI rereading, retaining both outputs and unresolved review flags.
- Page-level Markdown, JSON, image assets, and provenance; SQLite FTS5 indexes the selected page version.
- Single cycles, repeated cycles, persistent queues, source cooldowns, and a single-writer lock.

## Core installation

Use Python 3.11 or 3.12:

```bash
git clone https://github.com/jiang4wqy/journalflow.git
cd journalflow
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .
journalflow init
journalflow discover
journalflow status
```

Edit the generated `journalflow.toml`. Relative paths resolve against that file. Core installation does not install Paddle, CUDA, or model weights.

Set your own email for OA discovery, then fetch a bounded batch:

```bash
export JOURNALFLOW_CONTACT_EMAIL="your-email@example.org"
export UNPAYWALL_EMAIL="your-email@example.org"
journalflow fetch --limit 20
```

No OA PDF or missing access remains a visible `waiting_access` state. Institutional login and security challenges require the user. There is no automated institutional sign-in or paywall bypass. After resolving access/configuration:

```bash
journalflow fetch --retry-access --limit 20
```

Import an authorized local PDF:

```bash
journalflow import-pdf ./paper.pdf --doi 10.1234/example --version published
```

For a DOI not yet in the catalog, also pass a configured `--journal` key and `--title`.

## Visual conversion

Configure the optional client and model service as described in [docs/vision.md](docs/vision.md). The documented service uses vLLM 0.21.0 and BF16; the client uses PaddleOCR 3.7.0 with an appropriate PaddlePaddle 3.2.1 installation. A CPU layout client can connect to a separate GPU model service.

```bash
journalflow doctor --vision
journalflow convert --limit 20
journalflow search 'optimization AND uncertainty'
```

Outputs include `document.md`, an optional `document-refined.md`, page artifacts, and `status.json`. Original PDFs are preserved. Changing PDF contents or recognition policy retains previous artifacts. Search results identify the DOI, PDF hash, source page, and selected Markdown path.

Quality checks are screening heuristics, **not OCR accuracy scores**. A higher-resolution pass does not guarantee correct formulas or numbers. Unresolved flags remain visible. Charts retain source images rather than generated numerical tables. Source version labels and successful PDF parsing do not certify article identity or completeness.

## Search papers from Codex

The optional MCP server exposes read-only search and page retrieval over stdio.
Index text-based PDFs on CPU, or use the page index produced by visual conversion.
See the [quickstart with a synthetic two-page example](docs/codex-mcp.md).

Use the [research workflow and templates](docs/research-workflow.md) to record page-level evidence and compare studies. The templates are in Chinese.

## Automation

```bash
journalflow run --limit 20
journalflow watch --interval-hours 24 --limit 20
journalflow run --without-vision --limit 20
```

`watch` waits after each completed cycle. Run it under your own process manager for unattended operation. It is not a hosted service and does not start a cloud GPU, keep a computer awake, or create a Codex automation. HTTP authorization blocks persist until explicit retry; `Retry-After` is respected across restarts.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest -q
python -m build
```

The offline suite uses synthetic documents and test-only recognition doubles. The production CLI contains no fake recognizer. A separate live test checked the packaged code against a compatible PaddleOCR-VL service, including two pages, 450 DPI rereading, and search. This is not an accuracy benchmark; see the [verification record](docs/verification.md). CI covers the core package on Windows and Linux.

The initial lookback is not a permanent publication-date boundary: incremental index updates may also discover older records updated recently.

The repository contains source code and examples only. PDFs, model weights, browser sessions, private databases, environment values, and account credentials are excluded. Use article content according to its source license and your access agreement.

Source code is licensed under [Apache-2.0](LICENSE). Dependencies, models, and article content retain their own licenses. See the [Chinese README](README.md) for the complete command walkthrough and upstream references.
