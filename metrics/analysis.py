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
from metrics.registry import available_collectors, metric_specs
from metrics.store import MetricsStore

# Entity types that describe the whole show or account rather than one item (episode, post, video).
UNLINKED = (SHOW, ACCOUNT)
# "First N days after release" columns: the standard launch-performance yardstick.
LAUNCH_DAYS = 7
TOP_BREAKDOWN_ROWS = 10

Specs = dict[tuple[str, str], MetricSpec]


def series_key(source: str, metric: str, entity_type: str) -> str:
    """Id of one metric series, shared by its number, chart, or trend so the dashboard can link them."""
    return f"{source}.{metric}.{entity_type}"


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


def headline_numbers(
    store: MetricsStore, specs: Specs, days: int, today: date, headline_only: bool = True
) -> list[dict[str, Any]]:
    """Top-line numbers for headline metrics (or every metric), each compared with the window before it.

    Day metrics are summed over the window. Account snapshots (e.g. followers) report the
    latest reading. Item snapshots (post impressions, video lifetime views) sum every
    tracked item's latest reading, compared with the same sum just before the window.
    """
    prev_start, start, end = _windows(days, today)
    numbers = []
    for item in store.metric_inventory():
        source, metric, granularity, entity_type = item["source"], item["metric"], item["granularity"], item["entity_type"]
        spec = spec_for(specs, source, metric)
        if headline_only and not spec.headline:
            continue
        if granularity == "day" and entity_type == SHOW:
            current = sum(store.daily_series(source, metric, start.isoformat(), end.isoformat()).values())
            # A previous window the source only partly covers would make any change look huge, so it isn't compared.
            covered = item["first_period"] <= prev_start.isoformat()
            previous = (
                sum(store.daily_series(source, metric, prev_start.isoformat(), start.isoformat()).values())
                if covered else None
            )
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
            "key": series_key(source, metric, entity_type), "source": source, "metric": metric,
            "granularity": granularity, "entity_type": entity_type, "headline": spec.headline,
            "label": spec.label, "description": spec.description,
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
            "key": series_key(source, metric, SHOW), "kind": "day", "source": source, "metric": metric,
            "label": spec.label, "description": spec.description,
            "headline": spec.headline, "series": [[d, values.get(d, 0.0)] for d in dates],
        })
    return sorted(charts, key=lambda c: (not c["headline"], c["source"], c["metric"]))


def snapshot_trends(store: MetricsStore, specs: Specs, days: int, today: date) -> list[dict[str, Any]]:
    """Daily level of every snapshot metric: the sum of each entity's latest reading as of that day.

    For an account that's the running count (followers over time); for posts or videos it's
    the running total across every tracked item, so a new post shows up as a step. Readings
    from before the window seed the first day; days before any reading are None, not zero.
    """
    _, start, end = _windows(days, today)
    dates = _date_range(start, end)
    trends = []
    for item in store.metric_inventory():
        if item["granularity"] != "snapshot":
            continue
        source, metric, entity_type = item["source"], item["metric"], item["entity_type"]
        spec = spec_for(specs, source, metric)
        readings = store.snapshot_readings(source, metric, entity_type)
        # Walk the readings (oldest first) alongside the dates, carrying each entity's last value forward.
        latest: dict[str, float] = {}
        cursor, series = 0, []
        for day in dates:
            while cursor < len(readings) and readings[cursor][0] <= day:
                _, entity_id, value = readings[cursor]
                latest[entity_id] = value
                cursor += 1
            series.append([day, sum(latest.values()) if latest else None])
        trends.append({
            "key": series_key(source, metric, entity_type), "kind": "snapshot", "source": source, "metric": metric,
            "entity_type": entity_type, "label": spec.label, "description": spec.description,
            "headline": spec.headline, "tracked": len({entity_id for _, entity_id, _ in readings}), "series": series,
        })
    return sorted(trends, key=lambda t: (not t["headline"], t["source"], t["metric"]))


def breakdowns(
    store: MetricsStore, specs: Specs, days: int, today: date, with_daily: bool = False
) -> list[dict[str, Any]]:
    """Top values for every breakdown kind (apps, countries, ...) of every show-level day metric.

    `with_daily` adds each top value's day-by-day amounts (and the rest's, as `other_daily`),
    aligned to the window's dates, so the dashboard can total any range or stretch inside it.
    """
    _, start, end = _windows(days, today)
    dates = _date_range(start, end)
    result = []
    for item in store.metric_inventory():
        if item["granularity"] != "day" or item["entity_type"] != SHOW:
            continue
        source, metric = item["source"], item["metric"]
        for kind in store.dimension_kinds(source, metric):
            rows = store.dimension_totals(source, metric, start.isoformat(), end.isoformat(), kind)
            if not rows:
                continue
            top = rows[:TOP_BREAKDOWN_ROWS]
            entry: dict[str, Any] = {
                "source": source, "metric": metric, "label": spec_for(specs, source, metric).label,
                "kind": kind, "rows": [[name, value] for name, value in top],
                "other": sum(value for _, value in rows[TOP_BREAKDOWN_ROWS:]),
            }
            if with_daily:
                daily = store.dimension_daily(source, metric, start.isoformat(), end.isoformat(), kind)
                top_names = {name for name, _ in top}
                entry["daily"] = {name: [daily[name].get(d, 0.0) for d in dates] for name in top_names}
                entry["other_daily"] = [
                    sum(by_day.get(d, 0.0) for name, by_day in daily.items() if name not in top_names) for d in dates
                ]
            result.append(entry)
    return result


def episode_daily(
    store: MetricsStore, columns: list[dict[str, Any]], dates: list[str]
) -> dict[str, dict[str, list[float]]]:
    """Day-by-day amounts per episode for the episode table's daily columns, aligned to `dates`.

    Column key ("youtube.views") → slug → one value per date; episodes with nothing in the
    window are left out. Lets the dashboard total each episode over any range or stretch.
    """
    result: dict[str, dict[str, list[float]]] = {}
    for column in columns:
        if column["kind"] != "day":
            continue
        by_slug = store.episode_daily_by_slug(column["source"], column["metric"])
        result[column["key"]] = {
            slug: [daily.get(d, 0.0) for d in dates]
            for slug, daily in by_slug.items()
            if any(daily.get(d) for d in dates)
        }
    return result


def episode_table(
    store: MetricsStore, specs: Specs, episodes: EpisodeIndex, headline_only: bool = True
) -> dict[str, Any]:
    """One row per catalog episode with all-time and launch-window totals for headline metrics (or every metric).

    The launch column ("first 7 days") is only filled when the source's data starts on or
    before the release date; otherwise it would silently undercount older episodes.
    """
    columns: list[dict[str, Any]] = []
    values: defaultdict[str, dict[str, float | None]] = defaultdict(dict)

    for item in store.metric_inventory():
        source, metric, granularity, entity_type = item["source"], item["metric"], item["granularity"], item["entity_type"]
        spec = spec_for(specs, source, metric)
        if headline_only and not spec.headline:
            continue
        key = f"{source}.{metric}"
        # What every column carries, so the dashboard can pick one platform's columns, or only headline ones.
        about = {"label": spec.label, "source": source, "metric": metric, "headline": spec.headline}
        if granularity == "day" and entity_type not in UNLINKED:
            columns.append({"key": key, **about, "kind": "day", "basis": "all collected"})
            launch_key = f"{key}.launch"
            columns.append({
                "key": launch_key, **about, "kind": "launch", "label": f"{spec.label}, first {LAUNCH_DAYS}d",
                "basis": f"first {LAUNCH_DAYS} days after release",
            })
            daily_by_slug = store.episode_daily_by_slug(source, metric)
            for slug, daily in daily_by_slug.items():
                values[slug][key] = sum(daily.values())
            for slug, info in episodes.episodes.items():
                if info.published_on and info.published_on >= item["first_period"]:
                    daily = daily_by_slug.get(slug, {})
                    launch_end = (date.fromisoformat(info.published_on) + timedelta(days=LAUNCH_DAYS)).isoformat()
                    values[slug][launch_key] = sum(v for d, v in daily.items() if info.published_on <= d < launch_end)
        elif granularity == "snapshot" and entity_type not in UNLINKED:
            columns.append({"key": key, **about, "kind": "snapshot", "basis": f"{entity_type}s linked to the episode"})
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


def platforms(store: MetricsStore) -> list[dict[str, str]]:
    """Sources that hold data or have run, in collector order, each with what its collector measures.

    A source with stored data but no collector any more (a removed plugin) is still listed, last.
    """
    present = {item["source"] for item in store.metric_inventory()} | set(store.latest_runs())
    known = {name: cls.description for name, cls in available_collectors().items()}
    ordered = [name for name in known if name in present] + sorted(present - known.keys())
    return [{"source": name, "description": known.get(name, "")} for name in ordered]


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
    """Everything the HTML dashboard renders, as one JSON-serializable dict.

    Every metric is included, not only headline ones: the overview shows the headlines,
    and each platform's tab shows all of that source's numbers, series, and tables.
    """
    today = _today(today)
    specs = metric_specs()
    episodes = EpisodeIndex.from_catalog()
    _, start, end = _windows(days, today)
    dates = _date_range(start, end)
    with MetricsStore(settings.metrics_db_path) as store:
        table = episode_table(store, specs, episodes, headline_only=False)
        return {
            "show_title": settings.podcast_title,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "days": days,
            "today": today.isoformat(),
            "dates": dates,
            "platforms": platforms(store),
            "numbers": headline_numbers(store, specs, days, today, headline_only=False),
            "charts": daily_charts(store, specs, days, today),
            "trends": snapshot_trends(store, specs, days, today),
            "breakdowns": breakdowns(store, specs, days, today, with_daily=True),
            "episodes": table,
            "episode_daily": episode_daily(store, table["columns"], dates),
            "events": events(episodes, (today - timedelta(days=days - 1)).isoformat()),
            # Platform tabs list every tracked post or video, not just the newest few.
            "items": items_table(store, specs, limit=1000),
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
