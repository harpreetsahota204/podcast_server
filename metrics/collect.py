"""Runs metric collectors and stores their output.

Usage:
    uv run python -m metrics.collect                 # every configured source, incremental
    uv run python -m metrics.collect --source op3    # one source (repeatable)
    uv run python -m metrics.collect --days 30       # re-fetch the last 30 days
    uv run python -m metrics.collect --report        # also rewrite the HTML dashboard

Each source picks up where its last successful run ended (with a few days of overlap,
because sources revise recent days), or backfills `METRICS_BACKFILL_DAYS` on its first
run. One source failing never stops the others. Exit code 1 if any source failed.
"""

import argparse
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any

from core.config import settings
from metrics.collectors.base import CollectionContext, CollectionWindow, Collector
from metrics.episodes import EpisodeIndex
from metrics.registry import select_collectors
from metrics.store import MetricsStore

logger = logging.getLogger("metrics.collect")

# Recent days are re-fetched on every run: OP3 and Cloudflare keep filling in the
# current day, and late-arriving records can revise yesterday.
OVERLAP_DAYS = 3


def plan_window(store: MetricsStore, source: str, today: date, days: int | None) -> CollectionWindow:
    """Choose the date range for one source: explicit `days`, else resume, else first-run backfill."""
    end = today + timedelta(days=1)
    if days:
        return CollectionWindow(end - timedelta(days=days), end)
    last_end = store.last_successful_window_end(source)
    if last_end:
        return CollectionWindow(date.fromisoformat(last_end) - timedelta(days=OVERLAP_DAYS), end)
    return CollectionWindow(end - timedelta(days=settings.metrics_backfill_days), end)


def run_one(store: MetricsStore, collector: Collector, episodes: EpisodeIndex, today: date, days: int | None) -> dict[str, Any]:
    """Run a single collector end to end and return a summary; failures are reported, not raised."""
    problem = collector.config_problem()
    if problem:
        return {"source": collector.name, "status": "skipped", "message": problem}

    window = plan_window(store, collector.name, today, days)
    run_id = store.start_run(collector.name, window.start_iso, window.end_iso)
    try:
        result = collector.collect(CollectionContext(window=window, episodes=episodes, today=today))
        store.save(collector.name, result)
    except Exception as e:
        # A broken source (expired token, API change) must not block the others, and the
        # failure is recorded so the dashboard can flag stale data.
        message = f"{type(e).__name__}: {e}"
        store.finish_run(run_id, "failed", message=message)
        logger.warning("%s failed: %s", collector.name, message)
        return {"source": collector.name, "status": "failed", "window": [window.start_iso, window.end_iso], "message": message}

    message = "; ".join(result.notes)
    store.finish_run(run_id, "ok", len(result.observations), len(result.entities), message)
    return {
        "source": collector.name,
        "status": "ok",
        "window": [window.start_iso, window.end_iso],
        "observations": len(result.observations),
        "entities": len(result.entities),
        "message": message,
    }


def run_collection(sources: list[str] | None = None, days: int | None = None, today: date | None = None) -> list[dict[str, Any]]:
    """Run the selected collectors (default: all, or METRICS_SOURCES) and return one summary per source."""
    today = today or datetime.now(UTC).date()
    collectors = select_collectors(sources)
    episodes = EpisodeIndex.from_catalog()
    with MetricsStore(settings.metrics_db_path) as store:
        return [run_one(store, collector, episodes, today, days) for collector in collectors]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; prints one line per source."""
    parser = argparse.ArgumentParser(description="Collect podcast audience metrics.")
    parser.add_argument("--source", action="append", help="collector name (repeatable); default: all configured")
    parser.add_argument("--days", type=int, help="re-fetch this many days instead of resuming")
    parser.add_argument("--report", action="store_true", help="rewrite the HTML dashboard afterwards")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("--days must be at least 1")

    results = run_collection(args.source, args.days)
    for r in results:
        window = f" {r['window'][0]}..{r['window'][1]}" if "window" in r else ""
        counts = f" ({r['observations']} values)" if r["status"] == "ok" else ""
        print(f"  [{r['status'].upper()}] {r['source']}{window}{counts}: {r.get('message', '')}")

    if args.report:
        # Imported lazily so plain collection never pays for report rendering.
        from metrics.report import write_report

        print(f"Dashboard: {write_report()}")
    return 1 if any(r["status"] == "failed" for r in results) else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sys.exit(main())
