# JournalFlow: reusable incremental journal library

## Scope

Package the previously agreed discovery → deduplication → authorized PDF acquisition → visual Markdown conversion → difficult-page review → searchable library workflow as an independent Python command-line project. The first release is source-only and public. Users supply their own data, access permissions, contact email, and compatible model service. Existing private collections and running batch jobs are outside this repository.

## Approach

Three possible delivery forms were considered: a collection of standalone scripts, a configurable CLI with persistent state, and a hosted web application. The CLI is the first-release scope because it supports reproducible installations and scheduled operation without requiring accounts, a web service, or a particular GPU provider.

## Components and boundaries

- TOML configuration supplies journals, ISSNs, storage paths, request pacing, and model settings. The current four journals are a removable example preset.
- Crossref discovers bibliographic records. Historical discovery covers publication-date variants; incremental discovery uses successful checkpoints and overlapping update windows. Unchanged records do not create duplicate work.
- SQLite stores records, PDF versions, discovery checkpoints, conversion states, and a page-level FTS index. A process lock prevents simultaneous mutation of one workspace.
- Unpaywall supplies open-access PDF locations when the user configures their contact email. Institutional PDFs enter through explicit local import. Missing permission, unavailable PDFs, and authentication requirements remain visible queue states.
- PDFium validates and renders documents. The optional PaddleOCR-VL 1.6 client uses a separate vLLM service, preserving the full layout-and-recognition pipeline. Native text is used for quality checks, not as a substitute for visual transcription.
- The primary pass renders at 300 DPI. Difficult pages receive a 450 DPI pass with a larger image budget. Both results are retained; the selected output and unresolved flags are recorded per page. Charts retain their source images.
- Completed documents retain page boundaries, image references, source hashes, versions, and metadata. The index selects the reviewed page when accepted and otherwise the initial page, avoiding duplicate indexing.
- `run` executes one cycle; `watch` repeats cycles in an independently supervised process. No Codex scheduler, cloud rental, account login, or publication of article contents is created by this software.

## Failure and recovery

Partial discovery does not advance a journal checkpoint. HTTP rate limits honor Retry-After; authorization failures do not trigger bypass attempts. Downloads are installed only after PDF validation. Page outputs become resumable only after their JSON, Markdown, image references, and checkpoint are saved. New PDF hashes and conversion policies preserve prior versions. Quality metrics are screening signals rather than guarantees of transcription accuracy.

## Verification

Test DOI deduplication, changed metadata, interrupted discovery, rate limits, non-PDF responses, versioned imports, interrupted page conversion, high-resolution selection, image links, and page-level search. Run an offline CLI workflow on synthetic documents and a small integration check against a compatible live vision service when available. Build and install a wheel to verify packaged presets and entry points. CI runs offline tests on Windows and Linux. Before publication, inspect the exact staged file list and scan for private paths, connection details, credentials, data files, and model weights.

## Release acceptance

A clean checkout can install the core CLI, initialize a workspace, discover records, import valid PDFs, report pending work, and search converted pages. A user with the documented optional vision environment and model service can convert and review queued PDFs. Unsupported access and unresolved review cases are reported explicitly. Documentation distinguishes verified functionality from deployment examples that have not been executed.
