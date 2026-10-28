"""Records every collector produces, independent of where the numbers came from.

A collector turns one source's API responses into `Observation`s (numbers) and
`Entity`s (the things those numbers describe, such as a social post), so storage,
analysis, and the dashboard never need source-specific code.
"""

from dataclasses import dataclass, field
from typing import Literal

# "day": the amount within one UTC day (downloads on 2026-01-05). Days can be summed.
# "snapshot": a running total read on that day (a post's impressions so far). Take the
# latest reading; summing snapshots double-counts.
Granularity = Literal["day", "snapshot"]

# Entity types the analysis layer understands; collectors may add their own.
SHOW = "show"
EPISODE = "episode"
POST = "post"
ACCOUNT = "account"


@dataclass(frozen=True, slots=True)
class Observation:
    """One number: `metric` for one entity (and optional breakdown) on one UTC date."""

    source: str
    metric: str
    value: float
    period: str  # YYYY-MM-DD in UTC
    granularity: Granularity = "day"
    entity_type: str = SHOW
    entity_id: str = ""
    dimension: str = ""  # breakdown such as "country=US" or "app=Overcast"; "" means the total
    episode_slug: str | None = None


@dataclass(frozen=True, slots=True)
class Entity:
    """Descriptive record for something observations refer to (e.g. a social post or an account)."""

    source: str
    entity_type: str
    entity_id: str
    label: str = ""
    url: str = ""
    created_at: str = ""  # ISO 8601; used to group posts by when they went out
    episode_slug: str | None = None


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """How a collector's metric is presented; unknown metrics still work, just with a generic label."""

    label: str
    description: str = ""
    headline: bool = False  # shown as a top-line number and as an episode-table column


@dataclass(slots=True)
class CollectionResult:
    """Everything one collector run produced.

    `replace_start`/`replace_end` (YYYY-MM-DD, end exclusive) name the days this run
    fetched completely. The store deletes that source's stored "day" rows in the range
    before inserting, so re-running a window replaces totals instead of adding to them.
    """

    observations: list[Observation] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    replace_start: str | None = None
    replace_end: str | None = None
    notes: list[str] = field(default_factory=list)
