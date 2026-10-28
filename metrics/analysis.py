"""Turns stored observations into answers: headline numbers, trends, breakdowns, per-episode tables.

Nothing here knows about specific sources. It reads whatever metrics are in the store
and uses each collector's `MetricSpec`s for labels and for deciding which numbers are
headlines, so a newly plugged-in collector shows up in the dashboard and MCP tools
without changes here.
"""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from core.config import settings
from exporters.youtube import read_thumbnail_changes
from metrics.episodes import EpisodeIndex
from metrics.models import ACCOUNT, SHOW, MetricSpec
from metrics.registry import metric_specs
from metrics.store import MetricsStore

# Entity types that describe the whole show or account rather than one item (episode, post, video).
UNLINKED = (SHOW, ACCOUNT)
# "First N days after release" columns: the standard launch-performance yardstick.
LAUNCH_DAYS = 7
TOP_BREAKDOWN_ROWS = 10

Specs = dict[tuple[str, str], MetricSpec]


def spec_for(specs: Specs, source: str, metric: str) -> MetricSpec:
    """The collector's spec for a metric, or a readable fallback for metrics nobody described."""
    return specs.get((source, metric)) or MetricSpec(f"{metric.replace('_', ' ').capitalize()} ({source})")


def _date_range(start: date, end: date) -> list[str]:
    """Every date in [start, end) as YYYY-MM-DD."""
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days)]


def _change_pct(current: float | None, previous: float | None) -> float | None:
    """Percent change, or None when there's no meaningful baseline."""
    if current is None or not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _windows(days: int, today: date) -> tuple[date, date, date]:
    """(previous window start, current window start, end exclusive) for a `days`-long window ending today."""
    end = today + timedelta(days=1)
    start = end - timedelta(days=days)
    return start - timedelta(days=days), start, end


def headline_numbers(store: MetricsStore, specs: Specs, days: int, today: date) -> list[dict[str, Any]]:
    """Top-line numbers for headline metrics, each compared with the window before it.

    Day metrics are summed over the window. Account snapshots (e.g. followers) report the
    latest reading. Item snapshots (post impressions, video lifetime views) sum every
    tracked item's latest reading, compared with the same sum just before the window.
    """
    prev_start, start, end = _windows(days, today)
    numbers = []
    for item in store.metric_inventory():
        source, metric, granularity, entity_type = item["source"], item["metric"], item["granularity"], item["entity_type"]
        spec = spec_for(specs, source, metric)
        if not spec.headline:
            continue
        if granularity == "day" and entity_type == SHOW:
            current = sum(store.daily_series(source, metric, start.isoformat(), end.isoformat()).values())
            previous = sum(store.daily_series(source, metric, prev_start.isoformat(), start.isoformat()).values())
            basis = f"total, last {days} days"
        elif granularity == "snapshot" and entity_type == ACCOUNT:
            current, previous = _account_latest(store, source, metric, start)
            basis = "latest"
        elif granularity == "snapshot":
            latest = store.latest_snapshots(source, metric, entity_type)
            current = sum(value for value, _ in latest.values())
            # Each item's last reading before the window; None until collection history reaches back that far.
            before = store.latest_snapshots(source, metric, entity_type, before=start.isoformat())
            previous = sum(value for value, _ in before.values()) if before else None
            basis = f"all {len(latest)} tracked {entity_type}s, latest"
        else:
            continue
        numbers.append({
            "source": source, "metric": metric, "label": spec.label, "description": spec.description,
            "basis": basis, "current": current, "previous": previous, "change_pct": _change_pct(current, previous),
        })
    return numbers


def _account_latest(store: MetricsStore, source: str, metric: str, window_start: date) -> tuple[float | None, float | None]:
    """(latest value, last value before the window) summed over accounts."""
    by_period: defaultdict[str, float] = defaultdict(float)
    for entity in store.entities(source, ACCOUNT):
        for period, value in store.snapshot_series(source, metric, ACCOUNT, entity["entity_id"]).items():
            by_period[period] += value
    if not by_period:
        return None, None
    periods = sorted(by_period)
    before = [p for p in periods if p < window_start.isoformat()]
    return by_period[periods[-1]], (by_period[before[-1]] if before else None)


def daily_charts(store: MetricsStore, specs: Specs, days: int, today: date) -> list[dict[str, Any]]:
    """Zero-filled daily show-level series for every day metric, headline metrics first."""
    _, start, end = _windows(days, today)
    dates = _date_range(start, end)
    charts = []
    for item in store.metric_inventory():
        if item["granularity"] != "day" or item["entity_type"] != SHOW:
            continue
        source, metric = item["source"], item["metric"]
        spec = spec_for(specs, source, metric)
        values = store.daily_series(source, metric, start.isoformat(), end.isoformat())
        charts.append({
            "source": source, "metric": metric, "label": spec.label, "description": spec.description,
            "headline": spec.headline, "series": [[d, values.get(d, 0.0)] for d in dates],
        })
    return sorted(charts, key=lambda c: (not c["headline"], c["source"], c["metric"]))


def breakdowns(store: MetricsStore, specs: Specs, days: int, today: date) -> list[dict[str, Any]]:
    """Top values for every breakdown kind (apps, countries, ...) of every show-level day metric."""
    _, start, end = _windows(days, today)
    result = []
    for item in store.metric_inventory():
        if item["granularity"] != "day" or item["entity_type"] != SHOW:
            continue
        source, metric = item["source"], item["metric"]
        for kind in store.dimension_kinds(source, metric):
            rows = store.dimension_totals(source, metric, start.isoformat(), end.isoformat(), kind)
            if rows:
                result.append({
                    "source": source, "metric": metric, "label": spec_for(specs, source, metric).label,
                    "kind": kind, "rows": [[name, value] for name, value in rows[:TOP_BREAKDOWN_ROWS]],
                    "other": sum(value for _, value in rows[TOP_BREAKDOWN_ROWS:]),
                })
    return result


def episode_table(store: MetricsStore, specs: Specs, episodes: EpisodeIndex) -> dict[str, Any]:
    """One row per catalog episode with all-time and launch-window totals for headline metrics.

    The launch column ("first 7 days") is only filled when the source's data starts on or
    before the release date; otherwise it would silently undercount older episodes.
    """
    columns: list[dict[str, Any]] = []
    values: defaultdict[str, dict[str, float | None]] = defaultdict(dict)

    for item in store.metric_inventory():
        source, metric, granularity, entity_type = item["source"], item["metric"], item["granularity"], item["entity_type"]
        spec = spec_for(specs, source, metric)
        if not spec.headline:
            continue
        key = f"{source}.{metric}"
        if granularity == "day" and entity_type not in UNLINKED:
            columns.append({"key": key, "label": spec.label, "source": source, "basis": "all collected"})
            launch_key = f"{key}.launch"
            columns.append({"key": launch_key, "label": f"{spec.label}, first {LAUNCH_DAYS}d", "source": source,
                            "basis": f"first {LAUNCH_DAYS} days after release"})
            daily_by_slug = store.episode_daily_by_slug(source, metric)
            for slug, daily in daily_by_slug.items():
                values[slug][key] = sum(daily.values())
            for slug, info in episodes.episodes.items():
                if info.published_on and info.published_on >= item["first_period"]:
                    daily = daily_by_slug.get(slug, {})
                    launch_end = (date.fromisoformat(info.published_on) + timedelta(days=LAUNCH_DAYS)).isoformat()
                    values[slug][launch_key] = sum(v for d, v in daily.items() if info.published_on <= d < launch_end)
        elif granularity == "snapshot" and entity_type not in UNLINKED:
            columns.append({"key": key, "label": spec.label, "source": source, "basis": f"{entity_type}s linked to the episode"})
            linked = {e["entity_id"]: e["episode_slug"] for e in store.entities(source, entity_type) if e["episode_slug"]}
            for entity_id, (value, _) in store.latest_snapshots(source, metric, entity_type).items():
                if entity_id in linked:
                    slug = linked[entity_id]
                    values[slug][key] = (values[slug].get(key) or 0.0) + value

    rows = [
        {"slug": info.slug, "title": info.title, "published_on": info.published_on,
         "values": {c["key"]: values[info.slug].get(c["key"]) for c in columns}}
        for info in episodes.episodes.values()
    ]
    # Sort by the first column so the strongest episodes lead; missing values sink.
    if columns:
        first = columns[0]["key"]
        rows.sort(key=lambda r: r["values"][first] or 0.0, reverse=True)
    return {"columns": columns, "rows": rows}


def items_table(store: MetricsStore, specs: Specs, limit: int = 50) -> list[dict[str, Any]]:
    """Newest items (posts, videos, ...) per source and type, with latest snapshot values and linked episode."""
    tables = []
    for (source, entity_type), metric_names in store.snapshot_metrics_by_entity_type().items():
        if entity_type in UNLINKED:
            continue
        latest = {m: store.latest_snapshots(source, m, entity_type) for m in metric_names}
        # Headline metrics first, so the most important column sits next to the item's title.
        ordered = sorted(metric_names, key=lambda m: not spec_for(specs, source, m).headline)
        rows = [
            {"id": e["entity_id"], "label": e["label"], "url": e["url"], "created_at": e["created_at"],
             "episode_slug": e["episode_slug"],
             "values": {m: latest[m].get(e["entity_id"], (None, ""))[0] for m in ordered}}
            for e in store.entities(source, entity_type)[:limit]
        ]
        tables.append({
            "source": source,
            "entity_type": entity_type,
            "columns": [{"key": m, "label": spec_for(specs, source, m).label} for m in ordered],
            "rows": rows,
        })
    return tables


def coverage(store: MetricsStore) -> list[dict[str, Any]]:
    """Per source: date range held and how the latest run went (flags stale or failing sources)."""
    spans: dict[str, list[str]] = {}
    for item in store.metric_inventory():
        span = spans.setdefault(item["source"], [item["first_period"], item["last_period"]])
        span[0], span[1] = min(span[0], item["first_period"]), max(span[1], item["last_period"])
    latest_run = store.latest_runs()
    rows = []
    # A source can have data but no logged run (e.g. imported), or a run but no data yet.
    for source in sorted(set(spans) | set(latest_run)):
        first, last = spans.get(source, (None, None))
        run = latest_run.get(source, {})
        rows.append({
            "source": source, "first_period": first, "last_period": last,
            "last_run_status": run.get("status"), "last_run_at": run.get("finished_at") or run.get("started_at"),
            "last_run_message": run.get("message"),
        })
    return rows


def events(episodes: EpisodeIndex, since: str) -> list[dict[str, Any]]:
    """Things that can explain a change in the numbers, on or after `since`: releases and thumbnail changes."""
    found = [
        {"date": info.published_on, "kind": "release", "slug": info.slug, "label": info.title}
        for info in episodes.episodes.values()
    ]
    for change in read_thumbnail_changes():
        slug = change.get("episode_slug")
        info = episodes.get(slug) if slug else None
        found.append({
            "date": change["changed_at"][:10], "kind": "thumbnail", "slug": slug,
            "label": f"New thumbnail: {info.title if info else change['video_id']}",
        })
    return sorted((e for e in found if e["date"] >= since), key=lambda e: e["date"])


def _today(today: date | None) -> date:
    """`today` or the current UTC date."""
    return today or datetime.now(UTC).date()


def build_dashboard_data(days: int = 90, today: date | None = None) -> dict[str, Any]:
    """Everything the HTML dashboard renders, as one JSON-serializable dict."""
    today = _today(today)
    specs = metric_specs()
    episodes = EpisodeIndex.from_catalog()
    with MetricsStore(settings.metrics_db_path) as store:
        return {
            "show_title": settings.podcast_title,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "days": days,
            "today": today.isoformat(),
            "headlines": headline_numbers(store, specs, days, today),
            "charts": daily_charts(store, specs, days, today),
            "breakdowns": breakdowns(store, specs, days, today),
            "episodes": episode_table(store, specs, episodes),
            "events": events(episodes, (today - timedelta(days=days - 1)).isoformat()),
            "items": items_table(store, specs),
            "coverage": coverage(store),
        }


def summarize(days: int = 30, today: date | None = None, top: int = 5) -> dict[str, Any]:
    """Compact overview for agents: headline numbers, top episodes, top breakdowns, source health."""
    today = _today(today)
    specs = metric_specs()
    episodes = EpisodeIndex.from_catalog()
    with MetricsStore(settings.metrics_db_path) as store:
        table = episode_table(store, specs, episodes)
        return {
            "window_days": days,
            "headlines": headline_numbers(store, specs, days, today),
            "top_episodes": [
                {"slug": r["slug"], "title": r["title"], **{c["label"]: r["values"][c["key"]] for c in table["columns"]}}
                for r in table["rows"][:top]
            ],
            "breakdowns": [
                {"metric": b["label"], "by": b["kind"], "top": b["rows"][:top]}
                for b in breakdowns(store, specs, days, today)
            ],
            "sources": coverage(store),
        }


def episode_metrics(slug: str) -> dict[str, Any]:
    """Everything collected for one episode: daily series per metric and the posts/videos linked to it."""
    specs = metric_specs()
    episodes = EpisodeIndex.from_catalog()
    info = episodes.get(slug)
    if info is None:
        raise ValueError(f"No episode with slug {slug!r} in the catalog.")
    with MetricsStore(settings.metrics_db_path) as store:
        series = {}
        for item in store.metric_inventory():
            if item["granularity"] == "day" and item["entity_type"] not in UNLINKED:
                daily = store.episode_daily(item["source"], item["metric"], slug)
                if daily:
                    label = spec_for(specs, item["source"], item["metric"]).label
                    series[f"{item['source']}.{item['metric']}"] = {
                        "label": label, "total": sum(daily.values()), "daily": daily,
                    }
        linked_items = [
            {"source": table["source"], "type": table["entity_type"], **row,
             "values": {k: v for k, v in row["values"].items() if v is not None}}
            for table in items_table(store, specs, limit=10_000)
            for row in table["rows"] if row["episode_slug"] == slug
        ]
    thumbnail_changes = [
        {"date": c["changed_at"][:10], "video_id": c["video_id"], "file": c.get("thumbnail_file")}
        for c in read_thumbnail_changes() if c.get("episode_slug") == slug
    ]
    return {"slug": slug, "title": info.title, "published_on": info.published_on, "series": series,
            "linked_items": linked_items, "thumbnail_changes": thumbnail_changes}
