"""SQLite store for collected metrics (`$DATA_DIR/metrics.db`).

Kept separate from `catalog.db` so collection can never touch episode records, and
so the file can be deleted and re-collected without risk. One long table holds every
number from every source (`observations`), which lets new collectors plug in without
schema changes.
"""

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from metrics.models import SHOW, CollectionResult


def _now_iso() -> str:
    """Current UTC time as an ISO 8601 string, second precision."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class MetricsStore:
    """Thin wrapper over one SQLite connection; use as a context manager so it always closes."""

    def __init__(self, db_path: Path) -> None:
        """Open (creating if needed) the metrics database at `db_path` and ensure the schema."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        # Rows behave like dicts so callers can index by column name.
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def __enter__(self) -> Self:
        """Return self for `with MetricsStore(...) as store:` usage."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection even when the body raised."""
        self.close()

    def init_schema(self) -> None:
        """Create the observations, entities, and run-log tables if they don't exist."""
        with self.conn:
            # The primary key is the identity of one number, so re-collecting the same
            # day or snapshot updates it in place instead of adding a duplicate.
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS observations (
                    source TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    granularity TEXT NOT NULL CHECK (granularity IN ('day', 'snapshot')),
                    period TEXT NOT NULL,
                    value REAL NOT NULL,
                    episode_slug TEXT,
                    collected_at TEXT NOT NULL,
                    PRIMARY KEY (source, entity_type, entity_id, metric, dimension, granularity, period)
                );
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_metric ON observations(source, metric, period);")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_obs_episode ON observations(episode_slug) WHERE episode_slug IS NOT NULL;"
            )
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    source TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    label TEXT,
                    url TEXT,
                    created_at TEXT,
                    episode_slug TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, entity_type, entity_id)
                );
            """)
            # One row per collector run: the audit trail and the "where did I leave off" cursor.
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    window_start TEXT,
                    window_end TEXT,
                    observations INTEGER DEFAULT 0,
                    entities INTEGER DEFAULT 0,
                    message TEXT
                );
            """)

    # --- Writing ---

    def save(self, source: str, result: CollectionResult) -> None:
        """Store one collector's output atomically: clear its replaced day range, then upsert."""
        now = _now_iso()
        with self.conn:
            # Day totals in a fully re-fetched range are replaced wholesale, so a breakdown
            # value that no longer appears (or a corrected total) can't linger from a past run.
            if result.replace_start and result.replace_end:
                self.conn.execute(
                    "DELETE FROM observations WHERE source = ? AND granularity = 'day' AND period >= ? AND period < ?",
                    (source, result.replace_start, result.replace_end),
                )
            self.conn.executemany(
                """
                INSERT INTO observations
                    (source, entity_type, entity_id, metric, dimension, granularity, period, value, episode_slug, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, entity_type, entity_id, metric, dimension, granularity, period)
                DO UPDATE SET value = excluded.value, episode_slug = excluded.episode_slug,
                              collected_at = excluded.collected_at
                """,
                [
                    (o.source, o.entity_type, o.entity_id, o.metric, o.dimension, o.granularity,
                     o.period, float(o.value), o.episode_slug, now)
                    for o in result.observations
                ],
            )
            self.conn.executemany(
                """
                INSERT INTO entities (source, entity_type, entity_id, label, url, created_at, episode_slug, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, entity_type, entity_id)
                DO UPDATE SET label = excluded.label, url = excluded.url, created_at = excluded.created_at,
                              episode_slug = excluded.episode_slug, updated_at = excluded.updated_at
                """,
                [
                    (e.source, e.entity_type, e.entity_id, e.label, e.url, e.created_at, e.episode_slug, now)
                    for e in result.entities
                ],
            )

    def start_run(self, source: str, window_start: str, window_end: str) -> int:
        """Log the start of a collector run and return its id."""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO collection_runs (source, started_at, status, window_start, window_end) VALUES (?, ?, 'running', ?, ?)",
                (source, _now_iso(), window_start, window_end),
            )
        return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, observations: int = 0, entities: int = 0, message: str = "") -> None:
        """Record how a run ended (`ok`, `failed`, or `skipped`) and what it wrote."""
        with self.conn:
            self.conn.execute(
                "UPDATE collection_runs SET finished_at = ?, status = ?, observations = ?, entities = ?, message = ? WHERE id = ?",
                (_now_iso(), status, observations, entities, message, run_id),
            )

    # --- Reading ---

    def last_successful_window_end(self, source: str) -> str | None:
        """End date (exclusive) of the newest successful run for `source`, or None if it never succeeded."""
        row = self.conn.execute(
            "SELECT MAX(window_end) FROM collection_runs WHERE source = ? AND status = 'ok'", (source,)
        ).fetchone()
        return row[0] if row else None

    def latest_runs(self) -> dict[str, dict[str, Any]]:
        """Each source's most recent run: source → run row."""
        rows = self.conn.execute(
            "SELECT * FROM collection_runs WHERE id IN (SELECT MAX(id) FROM collection_runs GROUP BY source)"
        ).fetchall()
        return {row["source"]: dict(row) for row in rows}

    def metric_inventory(self) -> list[dict[str, Any]]:
        """Every (source, metric, granularity, entity_type) present, with its date coverage."""
        rows = self.conn.execute("""
            SELECT source, metric, granularity, entity_type, MIN(period) AS first_period,
                   MAX(period) AS last_period, COUNT(*) AS rows
            FROM observations GROUP BY source, metric, granularity, entity_type
            ORDER BY source, metric
        """).fetchall()
        return [dict(row) for row in rows]

    def daily_series(self, source: str, metric: str, start: str, end: str) -> dict[str, float]:
        """Show-level day totals (no breakdown) per date in [start, end)."""
        rows = self.conn.execute(
            """
            SELECT period, SUM(value) FROM observations
            WHERE source = ? AND metric = ? AND granularity = 'day' AND dimension = ''
              AND entity_type = ? AND period >= ? AND period < ?
            GROUP BY period ORDER BY period
            """,
            (source, metric, SHOW, start, end),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def dimension_totals(self, source: str, metric: str, start: str, end: str, kind: str) -> list[tuple[str, float]]:
        """Show-level totals per breakdown value of one kind (e.g. kind='app'), largest first."""
        rows = self.conn.execute(
            """
            SELECT SUBSTR(dimension, LENGTH(?) + 2) AS value_name, SUM(value) AS total FROM observations
            WHERE source = ? AND metric = ? AND granularity = 'day' AND entity_type = ?
              AND SUBSTR(dimension, 1, LENGTH(?) + 1) = ? AND period >= ? AND period < ?
            GROUP BY dimension ORDER BY total DESC
            """,
            # Prefix compare instead of LIKE, whose `_` and `%` wildcards could match other kinds.
            (kind, source, metric, SHOW, kind, f"{kind}=", start, end),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def dimension_daily(self, source: str, metric: str, start: str, end: str, kind: str) -> dict[str, dict[str, float]]:
        """Show-level day values per breakdown value of one kind in [start, end): value name → {date → value}."""
        rows = self.conn.execute(
            """
            SELECT SUBSTR(dimension, LENGTH(?) + 2), period, SUM(value) FROM observations
            WHERE source = ? AND metric = ? AND granularity = 'day' AND entity_type = ?
              AND SUBSTR(dimension, 1, LENGTH(?) + 1) = ? AND period >= ? AND period < ?
            GROUP BY dimension, period
            """,
            (kind, source, metric, SHOW, kind, f"{kind}=", start, end),
        ).fetchall()
        by_value: defaultdict[str, dict[str, float]] = defaultdict(dict)
        for name, period, value in rows:
            by_value[name][period] = value
        return dict(by_value)

    def dimension_kinds(self, source: str, metric: str) -> list[str]:
        """Breakdown kinds (the part before '=') recorded for a show-level day metric."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT SUBSTR(dimension, 1, INSTR(dimension, '=') - 1) FROM observations
            WHERE source = ? AND metric = ? AND granularity = 'day' AND entity_type = ? AND dimension <> ''
            """,
            (source, metric, SHOW),
        ).fetchall()
        return sorted(row[0] for row in rows if row[0])

    def episode_daily_by_slug(self, source: str, metric: str) -> dict[str, dict[str, float]]:
        """Day totals for every episode in one query: slug → {date → value} (rows linked to an episode only)."""
        rows = self.conn.execute(
            """
            SELECT episode_slug, period, SUM(value) FROM observations
            WHERE source = ? AND metric = ? AND granularity = 'day' AND dimension = '' AND episode_slug IS NOT NULL
            GROUP BY episode_slug, period
            """,
            (source, metric),
        ).fetchall()
        by_slug: defaultdict[str, dict[str, float]] = defaultdict(dict)
        for slug, period, value in rows:
            by_slug[slug][period] = value
        return dict(by_slug)

    def episode_daily(self, source: str, metric: str, slug: str) -> dict[str, float]:
        """Day totals per date for one episode (summing any entities linked to it)."""
        rows = self.conn.execute(
            """
            SELECT period, SUM(value) FROM observations
            WHERE source = ? AND metric = ? AND granularity = 'day' AND dimension = '' AND episode_slug = ?
            GROUP BY period ORDER BY period
            """,
            (source, metric, slug),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def latest_snapshots(
        self, source: str, metric: str, entity_type: str, before: str | None = None
    ) -> dict[str, tuple[float, str]]:
        """Newest snapshot reading per entity (only readings dated before `before`, if given): id → (value, period)."""
        cutoff = before or "9999-12-31"
        rows = self.conn.execute(
            """
            SELECT o.entity_id, o.value, o.period FROM observations o
            JOIN (
                SELECT entity_id, MAX(period) AS period FROM observations
                WHERE source = ? AND metric = ? AND granularity = 'snapshot' AND entity_type = ? AND dimension = ''
                  AND period < ?
                GROUP BY entity_id
            ) latest ON latest.entity_id = o.entity_id AND latest.period = o.period
            WHERE o.source = ? AND o.metric = ? AND o.granularity = 'snapshot' AND o.entity_type = ? AND o.dimension = ''
            """,
            (source, metric, entity_type, cutoff, source, metric, entity_type),
        ).fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}

    def snapshot_series(self, source: str, metric: str, entity_type: str, entity_id: str) -> dict[str, float]:
        """Every snapshot reading for one entity, oldest first."""
        rows = self.conn.execute(
            """
            SELECT period, value FROM observations
            WHERE source = ? AND metric = ? AND granularity = 'snapshot' AND entity_type = ? AND entity_id = ?
              AND dimension = ''
            ORDER BY period
            """,
            (source, metric, entity_type, entity_id),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def snapshot_readings(self, source: str, metric: str, entity_type: str) -> list[tuple[str, str, float]]:
        """Every snapshot reading of one metric across all its entities, oldest first: (period, entity id, value)."""
        rows = self.conn.execute(
            """
            SELECT period, entity_id, value FROM observations
            WHERE source = ? AND metric = ? AND granularity = 'snapshot' AND entity_type = ? AND dimension = ''
            ORDER BY period
            """,
            (source, metric, entity_type),
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def entities(self, source: str | None = None, entity_type: str | None = None) -> list[dict[str, Any]]:
        """Stored entities, optionally filtered, newest `created_at` first."""
        sql, params = "SELECT * FROM entities WHERE 1 = 1", []
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        if entity_type is not None:
            sql += " AND entity_type = ?"
            params.append(entity_type)
        rows = self.conn.execute(sql + " ORDER BY created_at DESC", params).fetchall()
        return [dict(row) for row in rows]

    def snapshot_metrics_by_entity_type(self) -> dict[tuple[str, str], list[str]]:
        """(source, entity_type) → snapshot metric names present, for generic tables."""
        rows = self.conn.execute(
            "SELECT DISTINCT source, entity_type, metric FROM observations WHERE granularity = 'snapshot' ORDER BY metric"
        ).fetchall()
        grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in rows:
            grouped[(row[0], row[1])].append(row[2])
        return dict(grouped)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self.conn.close()
