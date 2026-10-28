"""Import a show's back catalog from any podcast host's public RSS feed.

Every host (Fireside, Libsyn, Transistor, Buzzsprout, Spotify for Creators, ...)
publishes RSS, so this is the migration path for a new adopter. Each item's
<guid> is copied verbatim; podcast apps track played/unplayed state by GUID, so a
changed GUID would show every subscriber the whole archive as new episodes.

Re-running is safe: existing episodes are never overwritten, and slugs or GUIDs
in the show's excluded_episodes.json are skipped. Only MP3 enclosures are
imported, matching what the feed generator serves.

Usage:
    uv run python -m importers.rss <feed-url-or-file> [--mirror]

--mirror copies each episode's audio from the old host into R2 and records the
byte length R2 actually stores (hosts' reported lengths are often slightly off).
"""

import argparse
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.config import settings
from core.db import CatalogDB
from core.formatting import format_duration, is_slug, op3_url, parse_duration, slugify
from core.net import open_https
from core.storage import sync_episodes_audio

logger = logging.getLogger("rss_importer")

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def read_feed(source: str) -> bytes:
    """Return raw feed XML from an https URL or a local file path."""
    if source.startswith("https://"):
        with open_https(source) as resp:
            return resp.read()
    return Path(source).expanduser().read_bytes()


def _int_or_none(text: str | None) -> int | None:
    """Parse an integer from element text; None when missing or malformed."""
    try:
        return int(text.strip()) if text else None
    except ValueError:
        return None


def _slug_for(item: ET.Element, title: str, taken: set[str]) -> str:
    """Prefer the last path segment of the episode's <link>; fall back to the slugified title; dedupe."""
    link_tail = urlparse((item.findtext("link") or "").strip()).path.rstrip("/").rsplit("/", 1)[-1].lower()
    base = link_tail if is_slug(link_tail) else slugify(title) or "episode"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug)
    return slug


def parse_feed(xml_bytes: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn feed XML into catalog rows (oldest first); also return titles skipped as non-MP3 or audio-less."""
    root = ET.fromstring(xml_bytes)  # noqa: S314 - feed of the show being migrated; Expat limits entity expansion
    records: list[dict[str, Any]] = []
    skipped: list[str] = []
    taken: set[str] = set()

    # Feeds list newest first; reversing makes duplicate-title slugs number in publish order.
    for item in reversed(root.findall("channel/item")):
        title = (item.findtext("title") or "").strip()
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url", "") if enclosure is not None else ""
        if not audio_url or not urlparse(audio_url).path.lower().endswith(".mp3"):
            skipped.append(title or "(untitled)")
            continue

        slug = _slug_for(item, title, taken)
        # Without a <guid>, apps identify an episode by its enclosure URL, so that is the identity to keep.
        guid = (item.findtext("guid") or "").strip() or audio_url
        pub_date = item.findtext("pubDate")
        duration = parse_duration(item.findtext("itunes:duration", namespaces=NS))
        notes_html = item.findtext("content:encoded", namespaces=NS) or item.findtext("description") or ""
        summary = (
            item.findtext("itunes:summary", namespaces=NS)
            or item.findtext("itunes:subtitle", namespaces=NS)
            or re.sub(r"<[^>]+>", "", item.findtext("description") or "")
        ).strip()
        r2_url = f"{settings.r2_public_domain}/episodes/{slug}.mp3"

        records.append({
            "id": slug,
            "guid": guid,
            "episode_number": _int_or_none(item.findtext("itunes:episode", namespaces=NS)),
            "season": _int_or_none(item.findtext("itunes:season", namespaces=NS)) or 1,
            "title": title,
            "slug": slug,
            "summary": summary,
            "content_html": notes_html,
            "content_markdown": "",
            "published_at": parsedate_to_datetime(pub_date).isoformat() if pub_date else "",
            "original_audio_url": audio_url,
            "audio_filename": f"{slug}.mp3",
            "audio_url": r2_url,
            "audio_op3_url": op3_url(r2_url) if settings.use_op3_prefix else r2_url,
            "duration_seconds": duration,
            "duration_formatted": format_duration(duration),
            "file_size_bytes": _int_or_none(enclosure.get("length")) or 0,
            "explicit": int((item.findtext("itunes:explicit", namespaces=NS) or "").strip().lower() in ("yes", "true")),
            "cover_image_url": settings.podcast_image_url,
            "tags": "[]",
            # Audio still lives on the old host until --mirror copies it.
            "audio_synced": 0,
        })
    return records, skipped


def import_feed(source: str, mirror: bool = False) -> dict[str, Any]:
    """Add the feed's episodes to the catalog (never overwriting), optionally mirroring audio into R2."""
    records, skipped = parse_feed(read_feed(source))
    with CatalogDB(settings.database_path) as db:
        # Filter exclusions first: the database trigger would otherwise abort on them.
        allowed = [r for r in records if not db.is_excluded(r["slug"], r["guid"])]
        added = sum(db.insert_episode_if_new(r) for r in allowed)
        total = db.count_episodes()

    result: dict[str, Any] = {
        "in_feed": len(records) + len(skipped),
        "added": added,
        "already_present": len(allowed) - added,
        "excluded": len(records) - len(allowed),
        "skipped_non_mp3": skipped,
        "catalog_total": total,
    }
    # Mirroring runs after the insert so every new row (audio_synced=0) is picked up.
    if mirror:
        result["audio_mirrored"] = sync_episodes_audio()
    logger.info("Import result: %s", result)
    return result


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="https URL of the old host's RSS feed, or a local XML file")
    parser.add_argument("--mirror", action="store_true", help="copy audio from the old host into R2")
    args = parser.parse_args()
    import_feed(args.source, mirror=args.mirror)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
