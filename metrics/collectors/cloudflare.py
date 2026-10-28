"""Cloudflare edge-traffic collector for the public audio domain.

Every audio request reaches the R2 custom domain through Cloudflare's edge, whether
it came via OP3, a website player, or a direct link. This collector reads the zone's
GraphQL Analytics API for that host and records, per episode per day:

- `requests`: successful GET requests (200 full, 206 partial). Players fetch in ranges,
  so one listen is often several requests.
- `bytes`: bytes served.
- `est_listens`: bytes served divided by the episode's file size, i.e. how many complete
  copies went out. A rough, client-independent cross-check on OP3 downloads.

Only listener traffic counts toward those three. Verified bots are excluded in the
query, and each user agent is classified: directory ingest servers (Spotify and Apple
fetch every episode once and then serve listeners from their own CDNs) and scripts
(curl, HTTP libraries, this engine) are recorded separately as `filtered_requests`,
broken down by client, so a catalog re-ingest never looks like a listening spike.

How far back data goes depends on the zone's plan; the collector asks the API for its
limits and clamps the window rather than failing.

API reference: https://developers.cloudflare.com/analytics/graphql-api/
"""

from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Any
from urllib.parse import urlparse

from core.config import settings
from metrics.collectors.base import CollectionContext, CollectionWindow, Collector, CollectorError, fetch_json
from metrics.episodes import EpisodeIndex
from metrics.models import EPISODE, CollectionResult, MetricSpec, Observation

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
DATASET = "httpRequestsAdaptiveGroups"
SUCCESS_STATUSES = [200, 206]
SECONDS_PER_DAY = 86400
# The dataset's per-query row cap; a window that hits it would be silently truncated.
ROW_LIMIT = 10000

LIMITS_QUERY = """
query Limits($zone: string!) {
  viewer { zones(filter: {zoneTag: $zone}) { settings {
    httpRequestsAdaptiveGroups { enabled maxDuration notOlderThan }
  } } }
}
"""

# Shared filter: eyeball (real client), non-verified-bot GET requests to the audio host that
# succeeded. The user agent is a dimension so ingest servers and scripts can be split out.
TRAFFIC_QUERY = """
query Traffic($zone: string!, $start: Time!, $end: Time!, $host: string!, $pathLike: string!, $statuses: [uint16!]) {
  viewer { zones(filter: {zoneTag: $zone}) {
    paths: httpRequestsAdaptiveGroups(limit: 10000, filter: {
      datetime_geq: $start, datetime_lt: $end, clientRequestHTTPHost: $host, clientRequestPath_like: $pathLike,
      requestSource: "eyeball", clientRequestHTTPMethodName: "GET", edgeResponseStatus_in: $statuses,
      verifiedBotCategory: ""
    }) { count sum { edgeResponseBytes } dimensions { date clientRequestPath userAgent } }
    countries: httpRequestsAdaptiveGroups(limit: 10000, filter: {
      datetime_geq: $start, datetime_lt: $end, clientRequestHTTPHost: $host, clientRequestPath_like: $pathLike,
      requestSource: "eyeball", clientRequestHTTPMethodName: "GET", edgeResponseStatus_in: $statuses,
      verifiedBotCategory: ""
    }) { count dimensions { date clientCountryName userAgent } }
  } }
}
"""

# User-agent prefixes of servers that fetch audio to re-host it (one fetch per episode,
# not per listener). Listening through these platforms never reaches your domain.
INGEST_AGENTS = {
    "Spotify/": "Spotify (ingest)",
    "iTMS": "Apple (ingest)",
    "Amazon Music Podcast": "Amazon Music (ingest)",
    "Deezer Podcasters": "Deezer (ingest)",
    "Googlebot": "Google (ingest)",
}
# Automation: HTTP tools and libraries, including this engine's own checks.
TOOL_AGENTS = ("curl/", "Wget/", "python-requests", "Python-urllib", "Go-http-client", "podcast_server/", "node-fetch", "axios/")
BROWSER_MARKERS = ("Chrome/", "Firefox/", "Safari/", "Edg/")


def classify_agent(user_agent: str) -> tuple[str, bool]:
    """(client label, is_listener) for a user agent; ingest servers and scripts are not listeners."""
    ua = (user_agent or "").strip()
    for prefix, label in INGEST_AGENTS.items():
        if ua.startswith(prefix):
            return label, False
    if ua.startswith(TOOL_AGENTS):
        return "Scripts and tools", False
    if not ua:
        return "Unknown", True
    # Apps identify themselves first ("PodcastAddict/v5 ..."); browsers all start with "Mozilla/".
    if ua.startswith("Mozilla/") and any(marker in ua for marker in BROWSER_MARKERS):
        return "Web browser", True
    return ua.split("/", 1)[0].split(" (", 1)[0][:40] or "Unknown", True


def _rfc3339(moment: datetime) -> str:
    """A UTC datetime in the second-precision `...Z` form GraphQL `Time` expects."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def aggregate_traffic(
    path_rows: list[dict[str, Any]], country_rows: list[dict[str, Any]], episodes: EpisodeIndex
) -> tuple[list[Observation], int]:
    """Turn GraphQL groups into per-episode and show-level daily observations; also count unmatched paths.

    Listener traffic becomes requests/bytes/est_listens (with country and client
    breakdowns); ingest and script traffic becomes `filtered_requests` by client.
    """
    per_episode: defaultdict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    listener_clients: Counter[tuple[str, str]] = Counter()
    filtered_clients: Counter[tuple[str, str]] = Counter()
    unmatched = 0
    for row in path_rows:
        dims = row["dimensions"]
        label, is_listener = classify_agent(dims.get("userAgent", ""))
        if not is_listener:
            filtered_clients[(dims["date"], label)] += row["count"]
            continue
        slug = episodes.slug_for_path(dims["clientRequestPath"])
        if slug is None:
            unmatched += 1
            continue
        listener_clients[(dims["date"], label)] += row["count"]
        totals = per_episode[(dims["date"], slug)]
        totals[0] += row["count"]
        totals[1] += row["sum"]["edgeResponseBytes"]

    observations: list[Observation] = []
    # Show-level totals are the sum over episodes, so they exclude stray paths (robots.txt, probes).
    show_totals: Counter[tuple[str, str]] = Counter()
    for (day, slug), (requests, sent_bytes) in per_episode.items():
        info = episodes.get(slug)
        size = info.file_size_bytes if info else 0
        listens = sent_bytes / size if size else 0.0
        for metric, value in (("requests", requests), ("bytes", sent_bytes), ("est_listens", listens)):
            observations.append(
                Observation("cloudflare", metric, value, day, entity_type=EPISODE, entity_id=slug, episode_slug=slug)
            )
            show_totals[(day, metric)] += value
    observations += [Observation("cloudflare", metric, value, day) for (day, metric), value in show_totals.items()]
    # Country rows are split by user agent too, so only listener traffic is counted.
    countries: Counter[tuple[str, str]] = Counter()
    for row in country_rows:
        dims = row["dimensions"]
        if classify_agent(dims.get("userAgent", ""))[1]:
            countries[(dims["date"], dims.get("clientCountryName") or "Unknown")] += row["count"]
    observations += [
        Observation("cloudflare", "requests", n, day, dimension=f"country={country}")
        for (day, country), n in countries.items()
    ]
    observations += [
        Observation("cloudflare", "requests", n, day, dimension=f"client={label}")
        for (day, label), n in listener_clients.items()
    ]
    filtered_days: Counter[str] = Counter()
    for (day, label), n in filtered_clients.items():
        filtered_days[day] += n
        observations.append(Observation("cloudflare", "filtered_requests", n, day, dimension=f"client={label}"))
    observations += [Observation("cloudflare", "filtered_requests", n, day) for day, n in filtered_days.items()]
    return observations, unmatched


class CloudflareCollector(Collector):
    """Audio-domain traffic from Cloudflare's GraphQL Analytics API."""

    name = "cloudflare"
    description = "Requests and bytes served from the audio domain (Cloudflare edge analytics)"
    metrics = {
        "est_listens": MetricSpec(
            "Est. full listens", "Bytes served ÷ episode file size (Cloudflare edge)", headline=True
        ),
        "requests": MetricSpec("Audio requests", "Successful GET requests for episode audio (one listen can be several)"),
        "bytes": MetricSpec("Bytes served", "Episode audio bytes sent by the edge"),
        "filtered_requests": MetricSpec(
            "Filtered requests", "Directory ingest servers and scripts, excluded from listens"
        ),
    }

    def config_problem(self) -> str | None:
        """Needs an analytics-read token and the zone that serves the audio domain."""
        if not settings.cloudflare_api_token:
            return "Set CLOUDFLARE_API_TOKEN (a token with Zone > Analytics > Read)."
        if not settings.cloudflare_zone_id:
            return "Set CLOUDFLARE_ZONE_ID (the zone serving R2_PUBLIC_DOMAIN)."
        return None

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run one GraphQL query; GraphQL reports failures in the body, so check `errors` too."""
        reply = fetch_json(
            GRAPHQL_URL,
            {"Authorization": f"Bearer {settings.cloudflare_api_token}"},
            body={"query": query, "variables": variables},
        )
        if reply.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in reply["errors"])
            hint = " Edit the token: add Zone > Analytics > Read for this zone." if "does not have permission" in messages else ""
            raise CollectorError(f"Cloudflare GraphQL error: {messages}{hint}")
        zones = (reply.get("data") or {}).get("viewer", {}).get("zones") or []
        if not zones:
            raise CollectorError("Cloudflare returned no zone; check CLOUDFLARE_ZONE_ID and the token's zone access.")
        return zones[0]

    def dataset_limits(self) -> tuple[int, int]:
        """(maximum days per query, oldest day available) for this zone's plan."""
        limits = self._graphql(LIMITS_QUERY, {"zone": settings.cloudflare_zone_id})["settings"][DATASET]
        if not limits or not limits.get("enabled"):
            raise CollectorError(f"{DATASET} is not available on this zone's plan.")
        return max(1, limits["maxDuration"] // SECONDS_PER_DAY), limits["notOlderThan"] // SECONDS_PER_DAY

    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """Query the window in plan-sized chunks and aggregate per episode and per country."""
        max_days, retention_days = self.dataset_limits()
        # The oldest retained day is only partly available, so start at the first whole day.
        earliest = (datetime.now(UTC) - timedelta(days=retention_days)).date() + timedelta(days=1)
        window = ctx.window.clamp_start(earliest)
        result = CollectionResult(replace_start=window.start_iso, replace_end=window.end_iso)
        if window.start >= window.end:
            result.notes.append("window is older than this plan's analytics retention; nothing to fetch")
            return result
        if window.start > ctx.window.start:
            result.notes.append(f"started at {window.start_iso}: the plan keeps {retention_days} days of request data")

        host = urlparse(settings.r2_public_domain).hostname or ""
        path_prefix = ctx.episodes.audio_path_prefix()
        path_rows, country_rows = [], []
        for chunk in window.chunks(max_days):
            zone = self._fetch_chunk(chunk, host, path_prefix)
            for name, rows in (("paths", zone["paths"]), ("countries", zone["countries"])):
                if len(rows) >= ROW_LIMIT:
                    result.notes.append(f"{chunk.start_iso}..{chunk.end_iso} hit the {ROW_LIMIT}-row cap for {name}")
            path_rows += zone["paths"]
            country_rows += zone["countries"]

        observations, unmatched = aggregate_traffic(path_rows, country_rows, ctx.episodes)
        result.observations = observations
        if unmatched:
            result.notes.append(f"{unmatched} path groups matched no catalog episode (ignored)")
        return result

    def _fetch_chunk(self, chunk: CollectionWindow, host: str, path_prefix: str) -> dict[str, Any]:
        """One GraphQL call covering `chunk`; the end is capped at now because future times are rejected."""
        end = min(datetime.combine(chunk.end, time.min, tzinfo=UTC), datetime.now(UTC))
        return self._graphql(
            TRAFFIC_QUERY,
            {
                "zone": settings.cloudflare_zone_id,
                "start": _rfc3339(datetime.combine(chunk.start, time.min, tzinfo=UTC)),
                "end": _rfc3339(end),
                "host": host,
                "pathLike": f"{path_prefix}%",
                "statuses": SUCCESS_STATUSES,
            },
        )
