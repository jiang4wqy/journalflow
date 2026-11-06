# Release verification

## Version 0.3 source release

This release adds external-library baselines, immutable regional increments, project-level Codex MCP setup, a local dashboard, and opt-in weekly task definitions. The public repository contains source code, synthetic fixtures, and example configuration; it does not include a personal paper library, model weights, or a prebuilt standalone Python runtime.

Offline tests cover baseline deduplication, preserved source/vector identities, review isolation, incremental publication, and scheduler acceptance checks. The Windows/Linux/macOS Python 3.11/3.12 matrix in [GitHub Actions](https://github.com/jiang4wqy/journalflow/actions) records the remote results for each commit. macOS CI does not constitute manual validation of Finder launchers or a deployed macOS scheduled task.

No real ten-new-paper download and vision-conversion trial is claimed for this release. A historical-library replay tests a different path and cannot unlock weekly downloading. Downloading, vision conversion, and weekly scheduling remain opt-in deployment steps; pushing the source to GitHub does not execute them.

The checks below describe earlier core and synthetic visual integration work.

## Core package

- Local Windows/Python 3.12 validation: 26 offline tests and Ruff checks passed.
- A wheel was installed into a separate environment; the CLI entry point, packaged journal preset, initialization, and environment diagnostics were checked.
- Wheel and source-distribution contents were inspected; the source archive includes the test fixtures and deployment documentation.
- A live Crossref check used a short date interval for one journal. All publication-date queries completed without errors; that interval returned no records. Deduplication, update windows, cursor failures, and rate-limit persistence are covered by offline HTTP tests.
- GitHub Actions now includes Python 3.11/3.12 on Windows, Linux, and macOS. The Actions results are the authoritative record for remote CI, separate from local checks.

## Live vision integration

A packaged wheel was installed into an isolated directory in a Linux/Python 3.12 environment with PaddleOCR 3.7.0 and PaddlePaddle 3.2.1. The layout client ran on CPU and connected to an existing PaddleOCR-VL 1.6 vLLM 0.21.0 GPU service using BF16.

The integration input was a generated two-page synthetic PDF, not a journal article. Checks verified:

- Both pages were processed through the actual visual pipeline at 300 DPI.
- The short-output detector triggered a real 450 DPI second pass on the second page.
- Source PDF bytes stayed unchanged.
- Initial and reviewed artifacts, page checkpoints, and the two-page search index were produced.
- The unresolved quality flag on the synthetic repeated-prose page remained visible.

This verifies pipeline integration and recovery-related artifacts. It does not measure transcription accuracy, establish that the second pass is always better, or benchmark scientific tables and formulas.

## Limits of these checks

OA fetching is tested with offline Unpaywall-shaped responses. A live OA fetch requires the operator's own contact email. Institutional sign-in is not automated. The optional vision client was checked on Linux; core CI does not install or execute GPU models. The systemd service file is a deployment example, not a tested hosted service. Different drivers, operating systems, and upstream model installations require environment-specific validation.
