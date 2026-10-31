# Release verification

## Core package

- Local Windows/Python 3.12 validation: 26 offline tests and Ruff checks passed.
- A wheel was installed into a separate environment; the CLI entry point, packaged journal preset, initialization, and environment diagnostics were checked.
- Wheel and source-distribution contents were inspected; the source archive includes the test fixtures and deployment documentation.
- A live Crossref check used a short date interval for one journal. All publication-date queries completed without errors; that interval returned no records. Deduplication, update windows, cursor failures, and rate-limit persistence are covered by offline HTTP tests.
- GitHub Actions is configured for Python 3.11/3.12 on Windows and Linux. The Actions results are the authoritative record for remote CI, separate from local checks.

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
