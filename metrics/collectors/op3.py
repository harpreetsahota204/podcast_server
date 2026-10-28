"""OP3 (Open Podcast Prefix Project) download collector.

When `USE_OP3_PREFIX=true`, every enclosure is routed through op3.dev, which logs
the request and counts bot-filtered downloads. This collector reads those download
records and aggregates them into daily totals per show and per episode, with
breakdowns by listening app, country, and device type.

API reference: https://op3.dev/api/docs
"""

import base64
from collections import Counter, defaultdict
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urlencode

from core.config import settings
from metrics.collectors.base import CollectionContext, CollectionWindow, Collector, CollectorError, fetch_json
from metrics.episodes import EpisodeIndex
from metrics.models import EPISODE, SHOW, CollectionResult, Entity, MetricSpec, Observation

OP3_API = "https://op3.dev/api/1"
# OP3's maximum page size for download queries.
PAGE_LIMIT = 20000
# Safety stop: 100 pages x 20k rows is far beyond any small show's backfill.
MAX_PAGES = 100
# Breakdowns recorded at show level: (dimension kind, field in the OP3 download record).
BREAKDOWNS = (("app", "agentName"), ("country", "countryCode"), ("device", "deviceType"))


def aggregate_downloads(rows: list[dict[str, Any]], episodes: EpisodeIndex) -> tuple[list[Observation], int]:
    """Turn OP3 download records into daily observations; also return how many matched no episode.

    Each record is one bot-filtered download. Listeners per day count distinct
    `audienceId`s (OP3's privacy-preserving listener key) for that day.
    """
    show_downloads: Counter[str] = Counter()
    episode_downloads: Counter[tuple[str, str]] = Counter()
    breakdowns: Counter[tuple[str, str]] = Counter()
    audiences: defaultdict[str, set[str]] = defaultdict(set)
    unmatched = 0

    for row in rows:
        day = (row.get("time") or "")[:10]
        if not day:
            continue
        show_downloads[day] += 1
        if row.get("audienceId"):
            audiences[day].add(row["audienceId"])
        slug = episodes.slug_for_url(row.get("url", ""))
        if slug:
            episode_downloads[(day, slug)] += 1
        else:
            unmatched += 1
        for kind, field_name in BREAKDOWNS:
            breakdowns[(day, f"{kind}={row.get(field_name) or 'Unknown'}")] += 1

    observations = [Observation("op3", "downloads", n, day) for day, n in show_downloads.items()]
    observations += [Observation("op3", "listeners", len(ids), day) for day, ids in audiences.items()]
    observations += [
        Observation("op3", "downloads", n, day, entity_type=EPISODE, entity_id=slug, episode_slug=slug)
        for (day, slug), n in episode_downloads.items()
    ]
    observations += [
        Observation("op3", "downloads", n, day, dimension=dimension) for (day, dimension), n in breakdowns.items()
    ]
    return observations, unmatched


class OP3Collector(Collector):
    """Bot-filtered podcast downloads from op3.dev."""

    name = "op3"
    description = "Podcast app downloads counted by the OP3 prefix (op3.dev)"
    metrics = {
        "downloads": MetricSpec("Downloads", "Bot-filtered downloads counted by OP3", headline=True),
        "listeners": MetricSpec("Daily listeners", "Distinct OP3 audience ids per day (not additive across days)"),
    }

    def config_problem(self) -> str | None:
        """OP3 needs the prefix on enclosures and an API token."""
        if not settings.use_op3_prefix:
            return "USE_OP3_PREFIX is false, so OP3 never sees your downloads."
        if not settings.op3_api_token:
            return "Set OP3_API_TOKEN (free at https://op3.dev/api/keys)."
        return None

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call one OP3 API endpoint with the bearer token."""
        query = f"?{urlencode(params)}" if params else ""
        return fetch_json(f"{OP3_API}/{path}{query}", {"Authorization": f"Bearer {settings.op3_api_token}"})

    def lookup_show(self) -> dict[str, Any]:
        """Show info (uuid, title, stats page) by OP3_SHOW_UUID, else by <podcast:guid>, else by feed URL."""
        feed_b64 = base64.urlsafe_b64encode(settings.podcast_feed_url.encode()).decode().rstrip("=")
        candidates = [settings.op3_show_uuid] if settings.op3_show_uuid else [settings.podcast_guid, feed_b64]
        for key in candidates:
            try:
                return self._get(f"shows/{quote(str(key), safe='')}")
            except CollectorError as e:
                # 404 means "not under this key"; try the next identifier. Anything else is real.
                if "HTTP 404" not in str(e):
                    raise
        raise CollectorError(
            "OP3 doesn't know this show yet. It learns shows by crawling feeds whose enclosures use the "
            "prefix; check back a day after publishing, or set OP3_SHOW_UUID."
        )

    def iter_downloads(self, show_uuid: str, window: CollectionWindow) -> Iterator[dict[str, Any]]:
        """Yield every download record in the window, following OP3's continuation tokens."""
        params: dict[str, Any] = {
            "start": window.start_iso,
            "end": window.end_iso,
            "limit": PAGE_LIMIT,
            "format": "json",
        }
        for _ in range(MAX_PAGES):
            page = self._get(f"downloads/show/{show_uuid}", params)
            yield from page.get("rows", [])
            token = page.get("continuationToken")
            if not token:
                return
            params["continuationToken"] = token
        raise CollectorError(f"OP3 returned more than {MAX_PAGES} pages; collect a shorter window with --days.")

    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """Aggregate the window's downloads; the whole window is replaced in the store."""
        show = self.lookup_show()
        show_uuid = show["showUuid"]
        rows = list(self.iter_downloads(show_uuid, ctx.window))
        observations, unmatched = aggregate_downloads(rows, ctx.episodes)
        result = CollectionResult(
            observations=observations,
            entities=[Entity("op3", SHOW, show_uuid, label=show.get("title", ""), url=show.get("statsPageUrl", ""))],
            replace_start=ctx.window.start_iso,
            replace_end=ctx.window.end_iso,
            notes=[f"{len(rows)} downloads read"],
        )
        if unmatched:
            result.notes.append(f"{unmatched} downloads matched no catalog episode (counted in show totals only)")
        return result
