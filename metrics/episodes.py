"""Maps URLs and paths seen by metric sources back to catalog episodes.

Every source identifies episodes differently: OP3 reports the prefixed enclosure URL,
Cloudflare reports a request path, and a social post contains a link to the episode
page. `EpisodeIndex` resolves all of them to the catalog slug, so metrics from
different sources line up per episode.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Self
from urllib.parse import urlparse

from core.config import settings
from core.db import CatalogDB

OP3_PREFIX = "op3.dev/e/"


@dataclass(frozen=True, slots=True)
class EpisodeInfo:
    """The catalog fields metrics analysis needs for one episode."""

    slug: str
    title: str
    published_on: str  # YYYY-MM-DD
    duration_seconds: float
    file_size_bytes: int


def normalize_url(url: str) -> str:
    """Reduce a URL to `host/path` for comparison: no scheme, query, fragment, OP3 prefix, or trailing slash."""
    bare = url.strip().split("://", 1)[-1]
    # The OP3 redirect embeds the real URL after its prefix; compare the destination.
    bare = bare.removeprefix(OP3_PREFIX)
    bare = bare.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    host, _, path = bare.partition("/")
    return f"{host.lower()}/{path}" if path else host.lower()


class EpisodeIndex:
    """Lookup tables from audio URLs, audio paths, and episode page URLs to catalog slugs."""

    def __init__(self, episodes: Iterable[dict[str, Any]], podcast_link: str | None = None) -> None:
        """Index catalog rows; `podcast_link` (default: the show homepage) is where episode pages live."""
        link = (podcast_link or settings.podcast_link).rstrip("/")
        self.episodes: dict[str, EpisodeInfo] = {}
        self._by_url: dict[str, str] = {}
        self._by_path: dict[str, str] = {}
        self._hosts: set[str] = {normalize_url(link).split("/", 1)[0]}
        for ep in episodes:
            slug = ep["slug"]
            self.episodes[slug] = EpisodeInfo(
                slug=slug,
                title=ep.get("title") or slug,
                published_on=(ep.get("published_at") or "")[:10],
                duration_seconds=float(ep.get("duration_seconds") or 0.0),
                file_size_bytes=int(ep.get("file_size_bytes") or 0),
            )
            # Every URL form this episode is reachable under maps to the same slug.
            for url in (ep.get("audio_url"), ep.get("audio_op3_url"), f"{link}/{slug}"):
                if url:
                    self._by_url[normalize_url(url)] = slug
            if ep.get("audio_url"):
                parsed = urlparse(ep["audio_url"])
                self._by_path[parsed.path] = slug
                self._hosts.add((parsed.hostname or "").lower())

    @classmethod
    def from_catalog(cls, db_path: Path | None = None) -> Self:
        """Build the index from the configured catalog."""
        with CatalogDB(db_path or settings.database_path) as db:
            return cls(db.list_episodes())

    def get(self, slug: str) -> EpisodeInfo | None:
        """Catalog info for a slug, or None if it isn't in the catalog."""
        return self.episodes.get(slug)

    def slug_for_url(self, url: str) -> str | None:
        """Slug for an enclosure, OP3, or episode-page URL; also matches `<show host>/.../<slug>[.mp3]`."""
        if not url:
            return None
        key = normalize_url(url)
        if key in self._by_url:
            return self._by_url[key]
        # Fallback for links shaped slightly differently from the catalog (e.g. a page
        # under a subpath): accept them only on the show's own hosts, so an unrelated
        # site that happens to use a slug-like path never gets credited to an episode.
        host, _, path = key.partition("/")
        if host not in self._hosts or not path:
            return None
        stem = PurePosixPath(path).stem
        return stem if stem in self.episodes else None

    def slug_for_path(self, path: str) -> str | None:
        """Slug for a request path on the audio domain (e.g. `/episodes/<slug>.mp3`)."""
        return self._by_path.get(path.split("?", 1)[0])

    def audio_path_prefix(self) -> str:
        """Longest directory shared by all audio paths (e.g. `/episodes/`), or `/` if none."""
        dirs = {str(PurePosixPath(p).parent) for p in self._by_path}
        if len(dirs) == 1:
            only = dirs.pop()
            return only.rstrip("/") + "/"
        return "/"
