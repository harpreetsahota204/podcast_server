"""The contract every metric collector implements, built-in or plugged in.

To add a source, subclass `Collector`, set `name`, `description`, and `metrics`,
implement `config_problem()` and `collect()`, and list the class in
`METRICS_EXTRA_COLLECTORS` as `module:ClassName`. The pipeline handles windows,
storage, run logging, and failure isolation.

Conventions analysis relies on:
- "day" observations: emit complete totals for every day in the window and set
  `replace_start`/`replace_end`, so re-runs replace rather than add.
- Link anything about one episode with `episode_slug` (use `ctx.episodes` to resolve URLs).
- Snapshot metrics on posts or similar items: also emit an `Entity` with `created_at`,
  which places the item in time for "posts from the last N days" totals.
"""

import json
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, ClassVar

from core.net import open_https
from metrics.episodes import EpisodeIndex
from metrics.models import CollectionResult, MetricSpec

# Enough of an error body to diagnose a failed API call without flooding logs.
ERROR_BODY_CHARS = 300


class CollectorError(RuntimeError):
    """A source refused or failed a request; the message is safe to show (never contains credentials)."""


@dataclass(frozen=True, slots=True)
class CollectionWindow:
    """UTC date range to collect: `start` inclusive, `end` exclusive."""

    start: date
    end: date

    @property
    def start_iso(self) -> str:
        """Start date as YYYY-MM-DD."""
        return self.start.isoformat()

    @property
    def end_iso(self) -> str:
        """End date (exclusive) as YYYY-MM-DD."""
        return self.end.isoformat()

    def clamp_start(self, earliest: date) -> "CollectionWindow":
        """Same window, but starting no earlier than `earliest` (a source's retention limit)."""
        return CollectionWindow(max(self.start, earliest), self.end)

    def chunks(self, max_days: int) -> list["CollectionWindow"]:
        """Split into consecutive windows of at most `max_days` days (APIs cap query ranges)."""
        pieces, cursor = [], self.start
        while cursor < self.end:
            nxt = min(cursor + timedelta(days=max_days), self.end)
            pieces.append(CollectionWindow(cursor, nxt))
            cursor = nxt
        return pieces


@dataclass(frozen=True, slots=True)
class CollectionContext:
    """What a collector gets for one run: the window to fetch, the catalog, and today's date."""

    window: CollectionWindow
    episodes: EpisodeIndex
    today: date


class Collector(ABC):
    """One metrics source. Subclasses must be constructible with no arguments (they read `settings`)."""

    name: ClassVar[str]
    description: ClassVar[str]
    # Presentation for each metric this collector emits; drives dashboard labels and headline numbers.
    metrics: ClassVar[dict[str, MetricSpec]] = {}

    @abstractmethod
    def config_problem(self) -> str | None:
        """Return what's missing to run this collector (shown to the user), or None when it's ready."""

    @abstractmethod
    def collect(self, ctx: CollectionContext) -> CollectionResult:
        """Fetch the window from the source and return normalized records; raise CollectorError on failure."""


def fetch_json(url: str, headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> Any:
    """GET (or POST `body` as JSON) an HTTPS endpoint and decode the JSON reply.

    HTTP errors become `CollectorError` with the status and a short body excerpt. The
    request headers (which carry tokens) are never included in the message.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    all_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    try:
        with open_https(url, all_headers, data=data) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        excerpt = e.read(ERROR_BODY_CHARS).decode("utf-8", "replace")
        # Strip the query string: some APIs accept tokens there, and it must not reach logs.
        raise CollectorError(f"HTTP {e.code} from {url.split('?', 1)[0]}: {excerpt}") from None
    except urllib.error.URLError as e:
        raise CollectorError(f"Could not reach {url.split('?', 1)[0]}: {e.reason}") from None
