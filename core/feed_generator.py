"""Builds the public RSS 2.0 + iTunes + Podcasting 2.0 feed from the catalog.

Pure XML generation; where the feed is written or uploaded is decided by `core.site`.
"""

import os
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from core.config import settings

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
ATOM_NS = "http://www.w3.org/2005/Atom"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"
# Podcasting 2.0 JSON chapters (https://github.com/Podcastindex-org/podcast-namespace/blob/main/chapters/jsonChapters.md).
CHAPTERS_MIME = "application/json+chapters"

# Without registration ElementTree emits `ns0:`/`ns1:` prefixes; many podcast
# apps string-match on the conventional `itunes:` prefix instead of resolving namespaces.
for _prefix, _uri in (("itunes", ITUNES_NS), ("content", CONTENT_NS), ("atom", ATOM_NS), ("podcast", PODCAST_NS)):
    ET.register_namespace(_prefix, _uri)

# CommonMark renderer with raw HTML disabled, so show notes can't inject markup into the feed.
_MARKDOWN = MarkdownIt("commonmark", {"html": False})


def _itunes(tag: str) -> str:
    """Clark-notation tag name in the iTunes namespace."""
    return f"{{{ITUNES_NS}}}{tag}"


def _rfc822(published_at: str) -> str:
    """Convert a stored ISO-8601 timestamp to the RFC 822 form RSS requires; pass through if unparseable."""
    try:
        return format_datetime(datetime.fromisoformat(published_at))
    except ValueError:
        return published_at


def _show_notes_html(episode: dict[str, Any]) -> str:
    """Prefer imported HTML show notes; otherwise render the Markdown ones."""
    if episode["content_html"]:
        return episode["content_html"]
    if episode["content_markdown"]:
        return _MARKDOWN.render(episode["content_markdown"])
    return ""


def _add_channel_metadata(channel: ET.Element) -> None:
    """Populate show-level tags: title, links, iTunes owner, artwork, and categories."""
    ET.SubElement(channel, "title").text = settings.podcast_title
    ET.SubElement(channel, "link").text = settings.podcast_link
    ET.SubElement(channel, "description").text = settings.podcast_description
    ET.SubElement(channel, "language").text = settings.podcast_language
    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {"href": settings.podcast_feed_url, "rel": "self", "type": "application/rss+xml"},
    )

    ET.SubElement(channel, _itunes("author")).text = settings.podcast_author
    ET.SubElement(channel, _itunes("summary")).text = settings.podcast_description
    ET.SubElement(channel, _itunes("explicit")).text = "yes" if settings.podcast_explicit else "no"

    owner = ET.SubElement(channel, _itunes("owner"))
    ET.SubElement(owner, _itunes("name")).text = settings.podcast_author
    ET.SubElement(owner, _itunes("email")).text = settings.podcast_owner_email

    # Tells hosting platforms to refuse imports of this feed unless the owner email
    # confirms, which blocks someone else from moving the show to their account.
    ET.SubElement(channel, f"{{{PODCAST_NS}}}locked", {"owner": settings.podcast_owner_email}).text = "yes"
    # Stable show identity that survives feed moves; OP3 and Podcast Index key show stats on it.
    ET.SubElement(channel, f"{{{PODCAST_NS}}}guid").text = settings.podcast_guid

    # Artwork is declared twice: iTunes reads its own tag, generic RSS readers read <image>.
    ET.SubElement(channel, _itunes("image"), {"href": settings.podcast_image_url})
    image = ET.SubElement(channel, "image")
    ET.SubElement(image, "url").text = settings.podcast_image_url
    ET.SubElement(image, "title").text = settings.podcast_title
    ET.SubElement(image, "link").text = settings.podcast_link

    category = ET.SubElement(channel, _itunes("category"), {"text": settings.podcast_category})
    for subcategory in settings.podcast_subcategories:
        ET.SubElement(category, _itunes("category"), {"text": subcategory})


def _add_item(channel: ET.Element, ep: dict[str, Any]) -> None:
    """Append one episode as an <item>."""
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = ep["title"]
    # Only astro mode generates per-episode pages; otherwise link to the show homepage.
    link = f"{settings.podcast_link.rstrip('/')}/{ep['slug']}/" if settings.site_mode == "astro" else settings.podcast_link
    ET.SubElement(item, "link").text = link

    # isPermaLink=false: the GUID is an opaque identity, preserved verbatim from any previous host.
    ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = ep["guid"]
    ET.SubElement(item, "pubDate").text = _rfc822(ep["published_at"])

    enclosure_url = ep["audio_op3_url"] if settings.use_op3_prefix else ep["audio_url"]
    ET.SubElement(
        item,
        "enclosure",
        {"url": enclosure_url, "length": str(ep["file_size_bytes"]), "type": "audio/mpeg"},
    )

    if ep["duration_formatted"]:
        ET.SubElement(item, _itunes("duration")).text = ep["duration_formatted"]
    if ep["episode_number"]:
        ET.SubElement(item, _itunes("episode")).text = str(ep["episode_number"])
    if ep["season"]:
        ET.SubElement(item, _itunes("season")).text = str(ep["season"])
    ET.SubElement(item, _itunes("explicit")).text = "yes" if ep["explicit"] else "no"

    if ep["summary"]:
        ET.SubElement(item, "description").text = ep["summary"]
        ET.SubElement(item, _itunes("summary")).text = ep["summary"]

    # Full show notes (including chapter timestamps) for apps that render content:encoded.
    show_notes = _show_notes_html(ep)
    if show_notes:
        ET.SubElement(item, f"{{{CONTENT_NS}}}encoded").text = show_notes

    # Discovery extras, set only on episodes published with them: Apple shows (and searches)
    # transcripts it gets from this tag, and apps render chapters and episode art.
    if ep.get("transcript_url"):
        ET.SubElement(
            item,
            f"{{{PODCAST_NS}}}transcript",
            {"url": ep["transcript_url"], "type": ep["transcript_type"], "language": settings.podcast_language},
        )
    if ep.get("chapters_url"):
        ET.SubElement(item, f"{{{PODCAST_NS}}}chapters", {"url": ep["chapters_url"], "type": CHAPTERS_MIME})
    if ep.get("cover_image_url") and ep["cover_image_url"] != settings.podcast_image_url:
        ET.SubElement(item, _itunes("image"), {"href": ep["cover_image_url"]})


def build_rss_xml(episodes: list[dict[str, Any]]) -> str:
    """Serialize the channel plus every episode (newest first) into an RSS XML string."""
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    _add_channel_metadata(channel)
    for ep in episodes:
        _add_item(channel, ep)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True).decode("utf-8")


def write_atomic(path: Path, content: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written public feed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)
