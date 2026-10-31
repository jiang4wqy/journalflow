# Contributing

Use Python 3.11 or 3.12 and an isolated environment:

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest -q
python -m build
```

Tests are offline and create synthetic PDF fixtures. Do not add publisher PDFs, model weights, institutional credentials, session files, or private data to a change. Real vision checks require an independently configured compatible model service and should be reported separately from the offline suite.

Keep acquisition status separate from source identity, article completeness, and transcription quality. New source adapters must document access requirements, rate limits, retry behavior, and how authorization failures become visible to the user. Preserve prior PDF versions and recognition artifacts.

Submit focused pull requests describing the concrete behavior and the validation performed. Upstream package and model licenses remain applicable.
