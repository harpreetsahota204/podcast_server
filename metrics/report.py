"""Writes the metrics dashboard: one self-contained HTML file with the data embedded.

Usage:
    uv run python -m metrics.report               # last 90 days → $DATA_DIR/metrics/dashboard.html
    uv run python -m metrics.report --days 30 --out /tmp/report.html

The file has no external scripts, fonts, or network calls, so it opens offline, can be
attached or archived as-is, and leaks nothing when viewed. Data is rendered with
`textContent` only, so post text from social APIs can't inject markup.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from core.config import settings
from core.feed_generator import write_atomic
from metrics.analysis import build_dashboard_data

TEMPLATE_PATH = Path(__file__).with_name("dashboard.html")
PLACEHOLDER = "__DASHBOARD_DATA__"


def embed_json(data: Any) -> str:
    """Serialize for a `<script type="application/json">` block.

    Escaping `<`, `>`, and `&` as JSON unicode escapes means no string in the data
    (e.g. a post containing `</script>`) can close the script element early.
    """
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_report(data: dict[str, Any]) -> str:
    """Fill the HTML template with the dashboard data."""
    return TEMPLATE_PATH.read_text(encoding="utf-8").replace(PLACEHOLDER, embed_json(data), 1)


def write_report(days: int = 90, path: Path | None = None) -> Path:
    """Build the dashboard for the last `days` days and write it atomically; return the file path."""
    target = path or settings.metrics_report_path
    write_atomic(target, render_report(build_dashboard_data(days=days)))
    return target


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; prints the written path."""
    parser = argparse.ArgumentParser(description="Write the podcast metrics dashboard (HTML).")
    parser.add_argument("--days", type=int, default=90, help="window length in days (default 90)")
    parser.add_argument("--out", type=Path, help=f"output file (default {settings.metrics_report_path})")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")
    print(write_report(days=args.days, path=args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
