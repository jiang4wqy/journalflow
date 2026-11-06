"""Local stdio entry, also usable by a standalone Python with no pip installation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from journalflow.cli import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main([*sys.argv[1:], 'mcp']))
