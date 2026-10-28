"""FastMCP server exposing the podcast engine to agents (e.g. Claude Desktop, Cursor) over stdio.

stdout carries the MCP JSON-RPC stream, so nothing in this process may print to
it; all diagnostics go through `logging`, which is routed to stderr.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from core.audio import inspect_audio
from core.config import settings
from core.db import CatalogDB
from core.formatting import op3_url, slugify, validate_slug
from core.media_uploader import upload_blog_media
from core.site import deploy_site, rebuild_site
from core.storage import R2StorageClient
from core.thumbnail import generate_youtube_thumbnail
from exporters.blog_post import create_blog_post
from exporters.social import draft_social_package
from exporters.youtube import PrivacyStatus, upload_video_to_youtube
from importers.descript import (
    download_audio_from_url,
    extract_chapters_from_markers_or_transcript,
    format_show_notes_with_chapters,
)

logger = logging.getLogger("podcast_server")
mcp = FastMCP("podcast-server")

MAX_LIST_LIMIT = 200


def _open_catalog() -> CatalogDB:
    """Open the catalog configured in settings."""
    return CatalogDB(settings.database_path)


@mcp.tool()
def podcast_status() -> dict[str, Any]:
    """Get overall health and statistics of the podcast catalog and audio hosting."""
    with _open_catalog() as db:
        total_episodes = db.count_episodes()
        synced_audio = db.count_synced_audio()
        latest_episodes = db.list_episodes(limit=3)

    return {
        "podcast_title": settings.podcast_title,
        "feed_url": settings.podcast_feed_url,
        "public_audio_domain": settings.r2_public_domain,
        "use_op3_analytics": settings.use_op3_prefix,
        "storage_backend": settings.storage_backend,
        "r2_storage_ready": R2StorageClient().is_configured(),
        "total_episodes_in_catalog": total_episodes,
        "audio_synced_to_r2": synced_audio,
        "audio_pending_sync": total_episodes - synced_audio,
        "latest_episodes": [
            {
                "slug": ep["slug"],
                "title": ep["title"],
                "published_at": ep["published_at"],
                "duration": ep["duration_formatted"],
                "synced": bool(ep["audio_synced"]),
            }
            for ep in latest_episodes
        ],
    }


@mcp.tool()
def podcast_inspect(audio_path: str) -> dict[str, Any]:
    """Inspect a local audio MP3 file to extract exact duration, file size, bitrate, and sample rate."""
    return inspect_audio(Path(audio_path))


@mcp.tool()
def podcast_list_episodes(limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
    """List episodes (newest first, max 200 per call) with publication date, duration, and audio sync status."""
    with _open_catalog() as db:
        episodes = db.list_episodes(limit=max(1, min(limit, MAX_LIST_LIMIT)), offset=max(0, offset))
    return [
        {
            "id": ep["id"],
            "slug": ep["slug"],
            "title": ep["title"],
            "episode_number": ep["episode_number"],
            "published_at": ep["published_at"],
            "duration": ep["duration_formatted"],
            "file_size_bytes": ep["file_size_bytes"],
            "audio_url": ep["audio_url"],
            "audio_synced": bool(ep["audio_synced"]),
        }
        for ep in episodes
    ]


@mcp.tool()
def podcast_get_episode(slug: str) -> dict[str, Any] | None:
    """Fetch complete metadata, show notes, and audio links for a specific episode by its slug."""
    with _open_catalog() as db:
        return db.get_episode_by_slug(slug)


@mcp.tool()
def podcast_rebuild_feed() -> dict[str, Any]:
    """Regenerate the RSS feed and publish it (astro mode: also sync episode pages into the website repo; no deploy)."""
    with _open_catalog() as db:
        return {"status": "success", **rebuild_site(db)}


@mcp.tool()
def podcast_create_blog(
    slug: str,
    title: str,
    description: str,
    content_markdown: str,
    tags: list[str] | None = None,
    draft: bool = False,
) -> dict[str, Any]:
    """Write a standalone companion blog post into the website's blog directory (astro mode only)."""
    output_path = create_blog_post(
        title=title,
        description=description,
        content_markdown=content_markdown,
        slug=slug,
        tags=tags,
        draft=draft,
    )
    return {"status": "created", "file_path": str(output_path), "slug": slug, "draft": draft}


@mcp.tool()
def podcast_stage_from_descript(
    transcript_markdown: str,
    slug: str,
    audio_download_url: str | None = None,
    local_audio_path: str | None = None,
    guest_name: str | None = None,
    summary: str | None = None,
    links: list[str] | None = None,
) -> dict[str, Any]:
    """
    Stage and assemble a release package from Descript export data:
    1. Downloads master audio from Descript published URL to data/raw/<slug>.mp3 (or uses local_audio_path).
    2. Runs mutagen pre-flight inspection (audio duration, file size, bitrate).
    3. Parses markers into timestamped chapter objects from the transcript.
    4. Formats draft show notes with chapters, guest info, and links.
    5. Returns the staged package to the agent/human for review gate (Stage 3).
    """
    validate_slug(slug)

    # 1. Resolve audio: a Descript download wins over a local path when both are given.
    audio_path: Path | None = None
    if audio_download_url:
        audio_path = download_audio_from_url(audio_download_url, settings.raw_audio_dir / f"{slug}.mp3")
    elif local_audio_path:
        audio_path = Path(local_audio_path).resolve()
        if not audio_path.exists():
            raise FileNotFoundError(f"Local audio file does not exist: {local_audio_path}")

    # 2. Pre-flight inspection so the review bundle shows real duration and size.
    audio_meta = inspect_audio(audio_path) if audio_path else None

    # 3-4. Chapters from Descript markers, then draft show notes around them.
    chapters = extract_chapters_from_markers_or_transcript(transcript_markdown)
    default_summary = f"A conversation with {guest_name}." if guest_name else ""
    formatted_show_notes = format_show_notes_with_chapters(
        summary=summary or default_summary,
        chapters=chapters,
        guest_info=guest_name,
        links=links,
    )

    return {
        "status": "staged_for_review",
        "slug": slug,
        "audio_file_path": str(audio_path) if audio_path else None,
        "audio_meta": audio_meta,
        "chapter_count": len(chapters),
        "chapters": chapters,
        "draft_show_notes_markdown": formatted_show_notes,
        "next_step": "Human Review Gate (Stage 3): Review title options, chapter timestamps, and companion blog draft before calling podcast_publish.",
    }


@mcp.tool()
def podcast_publish(
    audio_file_path: str,
    title: str,
    description: str,
    show_notes_markdown: str,
    slug: str | None = None,
    episode_number: int | None = None,
    season: int = 1,
    explicit: bool = False,
    tags: list[str] | None = None,
    blog_content_markdown: str | None = None,
    blog_title: str | None = None,
    blog_draft: bool = False,
) -> dict[str, Any]:
    """
    Publish a new podcast episode end-to-end:
    1. Validates the slug is new and measures audio duration and byte length with mutagen.
    2. Uploads audio file directly to Cloudflare R2 bucket.
    3. Records metadata into SQLite catalog.
    4. Regenerates the public RSS feed and exports Astro Markdown to website.
    5. Optionally writes a companion blog post (astro mode).
    6. Astro mode: commits only the generated website files and pushes the site branch.
    """
    ep_slug = validate_slug(slug or slugify(title))

    # Every check that can fail runs before the upload, so a rejected publish leaves nothing behind.
    with _open_catalog() as db:
        if db.get_episode_by_slug(ep_slug):
            raise ValueError(f"Episode '{ep_slug}' is already published; pick a different slug.")
        if db.is_excluded(ep_slug):
            raise ValueError(f"Slug '{ep_slug}' belongs to a permanently excluded episode; pick another.")
    audio_path = Path(audio_file_path).resolve()
    audio_meta = inspect_audio(audio_path)
    client = R2StorageClient()
    if not client.is_configured():
        raise RuntimeError("Cloudflare R2 storage client is not configured in .env")

    target_filename = f"{ep_slug}.mp3"
    r2_url = client.upload_file(audio_path, f"episodes/{target_filename}")

    now = datetime.now(UTC)
    record = {
        "id": ep_slug,
        # Minted once here and never regenerated: podcast apps track listening state by GUID.
        "guid": f"{settings.episode_guid_prefix}-{ep_slug}-{now.strftime('%Y%m%d%H%M%S')}",
        "episode_number": episode_number,
        "season": season,
        "title": title,
        "slug": ep_slug,
        "summary": description,
        "content_html": "",
        "content_markdown": show_notes_markdown,
        "published_at": now.isoformat(),
        "original_audio_url": r2_url,
        "audio_filename": target_filename,
        "audio_url": r2_url,
        "audio_op3_url": op3_url(r2_url) if settings.use_op3_prefix else r2_url,
        "duration_seconds": audio_meta["duration_seconds"],
        "duration_formatted": audio_meta["duration_formatted"],
        "file_size_bytes": audio_meta["file_size_bytes"],
        "explicit": int(explicit),
        "cover_image_url": settings.podcast_image_url,
        "tags": json.dumps(tags or []),
        "audio_synced": 1,
    }

    # Optional companion field note shares the episode slug.
    blog_path: Path | None = None
    if blog_content_markdown and settings.site_mode == "astro":
        blog_path = create_blog_post(
            title=blog_title or title,
            description=description,
            content_markdown=blog_content_markdown,
            slug=ep_slug,
            tags=tags,
            draft=blog_draft,
        )

    with _open_catalog() as db:
        db.insert_episode(record)
        site = rebuild_site(db)

    git_deploy = deploy_site(f"feat(podcast): publish episode {ep_slug}", [blog_path] if blog_path else None)

    return {
        "status": "published",
        "slug": ep_slug,
        "guid": record["guid"],
        "title": title,
        "published_at": record["published_at"],
        "audio_url": r2_url,
        "audio_op3_url": record["audio_op3_url"],
        "duration": audio_meta["duration_formatted"],
        "file_size_bytes": audio_meta["file_size_bytes"],
        "rss_feed_path": site["rss_path"],
        "episode_pages_changed": site.get("episode_pages_changed"),
        "feed_url": site.get("feed_url", settings.podcast_feed_url),
        "companion_blog_path": str(blog_path) if blog_path else None,
        "git_deploy": git_deploy,
    }


@mcp.tool()
def podcast_upload_media_asset(
    file_path: str,
    target_filename: str | None = None,
    prefix: str = "media",
) -> dict[str, Any]:
    """Upload blog assets (GIF, PNG, JPEG, WebP, MP4, WebM, MOV) to the public Cloudflare R2 bucket. Refuses to overwrite an existing key. Returns the public URL and a Markdown embed."""
    return upload_blog_media(file_path=file_path, target_filename=target_filename, prefix=prefix)


@mcp.tool()
def podcast_publish_to_youtube(
    video_path: str,
    title: str,
    description: str,
    tags: list[str] | None = None,
    privacy_status: PrivacyStatus = "unlisted",
) -> dict[str, Any]:
    """Upload a video to the authenticated YouTube channel using resumable chunked upload."""
    return upload_video_to_youtube(
        video_path=video_path,
        title=title,
        description=description,
        tags=tags,
        privacy_status=privacy_status,
    )


@mcp.tool()
def podcast_draft_social(
    title: str,
    guest_name: str,
    core_tension: str,
    technical_specs: list[str],
    episode_url: str,
    youtube_url: str | None = None,
) -> dict[str, Any]:
    """Draft a long and a short social post for an episode (templates overridable per show)."""
    return draft_social_package(
        title=title,
        guest_name=guest_name,
        core_tension=core_tension,
        technical_specs=technical_specs,
        episode_url=episode_url,
        youtube_url=youtube_url,
    )


@mcp.tool()
def podcast_stage_thumbnail(
    prompt_text: str,
    output_filename: str,
    style: str | None = None,
) -> dict[str, Any]:
    """Builds a 16:9 YouTube thumbnail prompt and returns the local path and R2 key the rendered image should use. Does not generate the image."""
    return generate_youtube_thumbnail(
        prompt_text=prompt_text, output_filename=output_filename, style=style or settings.thumbnail_style
    )


if __name__ == "__main__":
    # basicConfig logs to stderr, keeping stdout clean for the MCP protocol stream.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    mcp.run()
