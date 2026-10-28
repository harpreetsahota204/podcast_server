"""Exports every catalog episode to the website's Astro content collection (`WEBSITE_EPISODES_DIR`).

Files are only rewritten when their content changes, so the website's git diff
after a publish shows just the episodes that actually changed.
"""

import json
import logging
from pathlib import Path
from typing import Any

from core.config import settings
from core.db import CatalogDB
from core.formatting import one_line, yaml_str

logger = logging.getLogger("astro_exporter")


def render_episode_markdown(ep: dict[str, Any]) -> str:
    """Render one episode as Markdown with YAML frontmatter matching the site's content schema."""
    enclosure_url = ep["audio_op3_url"] if settings.use_op3_prefix else ep["audio_url"]
    # Frontmatter descriptions are single-line; fall back to the title.
    description = one_line(ep["summary"] or ep["title"])
    episode_number = ep["episode_number"] if ep["episode_number"] is not None else "null"
    tags = json.loads(ep["tags"] or "[]")
    # Prefer Markdown notes; imports from other hosts may only have HTML.
    body = (ep["content_markdown"] or "").strip() or (ep["content_html"] or "").strip()

    return f"""---
title: {yaml_str(ep["title"])}
description: {yaml_str(description)}
pubDate: {ep["published_at"]}
audioUrl: {yaml_str(enclosure_url)}
duration: {yaml_str(ep["duration_formatted"] or "00:00")}
durationSeconds: {ep["duration_seconds"]}
fileSizeBytes: {ep["file_size_bytes"]}
episodeNumber: {episode_number}
season: {ep["season"]}
explicit: {str(bool(ep["explicit"])).lower()}
guid: {yaml_str(ep["guid"])}
coverArt: {yaml_str(ep["cover_image_url"] or "")}
tags: {json.dumps(tags, ensure_ascii=False)}
draft: false
---

{body}
"""


def _write_if_changed(path: Path, content: str) -> bool:
    """Write `content` to `path` only if it differs from what's on disk; return True if written."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def export_astro_markdown(db: CatalogDB) -> int:
    """Sync all episodes into the website's content collection; return how many files changed."""
    target_dir = settings.site_episodes_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    # Pages for episodes whose audio isn't in R2 yet would ship a broken player; skip them until synced.
    written = sum(
        _write_if_changed(target_dir / f"{ep['slug']}.md", render_episode_markdown(ep))
        for ep in db.list_episodes()
        if ep["audio_synced"]
    )
    logger.info("Astro export: %d changed file(s) in %s", written, target_dir)
    return written
