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
from core.backup import backup_status, start_backup
from core.config import PROJECT_ROOT, settings
from core.db import CatalogDB
from core.formatting import op3_url, slugify, validate_slug
from core.media_uploader import upload_blog_media
from core.site import deploy_site, rebuild_site
from core.storage import R2StorageClient
from core.thumbnail import generate_youtube_thumbnail, render_thumbnail
from exporters.blog_post import blog_post_path, create_blog_post
from exporters.devto import crosspost_to_devto
from exporters.instagram import PostType, post_to_instagram
from exporters.social import draft_social_package
from exporters.substack_post import crosspost_to_substack
from exporters.x_post import post_to_x
from exporters.youtube import PrivacyStatus, set_video_thumbnail, upload_video_to_youtube
from importers.descript import (
    download_audio_from_url,
    extract_chapters_from_markers_or_transcript,
    format_show_notes_with_chapters,
)
from metrics.analysis import episode_metrics, summarize
from metrics.collect import run_collection
from metrics.report import write_report

logger = logging.getLogger("podcast_server")

# Sent to every client when it connects, so an agent gets the rules even if nobody points it at the docs.
INSTRUCTIONS = f"""\
Self-hosted podcast publishing engine. Before any podcast task, read the show playbook \
({settings.data_dir / "PLAYBOOK.md"}) and the engine's runbook ({PROJECT_ROOT / "AGENTS.md"}); \
the playbook wins on anything show-specific.
Rules that always apply:
- Nothing goes public without explicit human approval of the exact content. Public tools: podcast_publish, \
podcast_update_episode, podcast_deploy_site, podcast_post_to_x, podcast_post_to_instagram, \
podcast_publish_to_youtube, podcast_set_youtube_thumbnail.
- Episode GUIDs and slugs are permanent, and excluded episodes stay excluded. When a tool refuses, report why; \
never work around it.
- If a public call fails, read {settings.data_dir / "social"}/*.jsonl before retrying, so nothing is posted twice.
- Use these tools instead of editing catalog.db, the website repo, or R2 directly.
- After publishing or editing an episode, run podcast_backup.
- podcast_crosspost_to_devto and podcast_crosspost_to_substack only create unpublished drafts, and only for a blog \
post already live on the site; the human publishes each draft on its platform.
- Never ask the human to paste a password, token, or session cookie into chat, and never read credential files. When a \
tool says a feature is off or a login expired, relay its message and the matching setup step in \
{PROJECT_ROOT / "ADOPTING.md"}."""

mcp = FastMCP("podcast-server", instructions=INSTRUCTIONS)

# MCP tool annotations: clients use them to decide which calls run without asking the human.
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
# Writes local files only (staging, renders, reports); never overwrites anything public.
LOCAL_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
# Talks to outside services but only adds or refreshes data, and repeating the call changes nothing more.
ADDITIVE_EXTERNAL = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
# Changes what the public sees; safe to retry because a repeat has no further effect.
PUBLIC_EDIT = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True}
# Irreversible public releases; a repeat would publish or post a second time.
PUBLIC_RELEASE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}

MAX_LIST_LIMIT = 200


def _open_catalog() -> CatalogDB:
    """Open the catalog configured in settings."""
    return CatalogDB(settings.database_path)


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=READ_ONLY)
def podcast_inspect(audio_path: str) -> dict[str, Any]:
    """Inspect a local audio MP3 file to extract exact duration, file size, bitrate, and sample rate."""
    return inspect_audio(Path(audio_path))


@mcp.tool(annotations=READ_ONLY)
def podcast_list_episodes(limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
    """List episodes (newest first, max 200 per call) with publication date, duration, and audio sync status."""
    # Clamp agent-supplied paging so one call can't dump the whole catalog (or pass a negative "no limit").
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


@mcp.tool(annotations=READ_ONLY)
def podcast_get_episode(slug: str) -> dict[str, Any] | None:
    """Fetch complete metadata, show notes, and audio links for a specific episode by its slug."""
    with _open_catalog() as db:
        return db.get_episode_by_slug(slug)


@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_rebuild_feed() -> dict[str, Any]:
    """Regenerate the RSS feed and episode pages from the catalog. Astro mode: writes them into the website repo; push with podcast_deploy_site. None mode: uploads the feed to R2, live at once."""
    with _open_catalog() as db:
        return {"status": "success", **rebuild_site(db)}


@mcp.tool(annotations={**LOCAL_WRITE, "destructiveHint": True})  # replaces an existing post with the same slug
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


@mcp.tool(annotations=ADDITIVE_EXTERNAL)
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
    Stage a release package from a Descript export for human review:
    1. Downloads the master audio to DATA_DIR/raw/<slug>.mp3 (or uses local_audio_path).
    2. Measures duration, file size, and bitrate.
    3. Parses transcript markers into timestamped chapters.
    4. Drafts show notes with chapters, guest info, and links.
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
        "next_step": "Human review gate: get explicit approval of title, show notes, and chapters before calling podcast_publish.",
    }


@mcp.tool(annotations=PUBLIC_RELEASE)
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
    Publish a new episode end-to-end (only after human approval):
    1. Rejects invalid, already-published, or excluded slugs, and measures the audio, before any upload.
    2. Uploads the MP3 to R2 and inserts the catalog row with a new, permanent GUID.
    3. Optionally writes a companion blog post (astro mode).
    4. Rebuilds the feed (and episode pages) and deploys: R2 feed upload, or a scoped git push.
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
        raise RuntimeError(client.config_error)

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

    # Optional companion blog post shares the episode slug.
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


@mcp.tool(annotations=PUBLIC_EDIT)
def podcast_update_episode(
    slug: str,
    title: str | None = None,
    description: str | None = None,
    show_notes_markdown: str | None = None,
    tags: list[str] | None = None,
    episode_number: int | None = None,
    season: int | None = None,
    explicit: bool | None = None,
) -> dict[str, Any]:
    """Edit a published episode (only after human approval of the new text): title, description, show notes, tags, number, season, or explicit flag. Omitted fields stay as they are; GUID, slug, and audio never change. Rebuilds the feed and pages. Astro mode: push them with podcast_deploy_site. None mode: the feed is live at once."""
    # Parameter names match podcast_publish; map them onto catalog columns, dropping the ones not given.
    columns = {
        "title": title,
        "summary": description,
        "content_markdown": show_notes_markdown,
        "tags": None if tags is None else json.dumps(tags),
        "episode_number": episode_number,
        "season": season,
        "explicit": None if explicit is None else int(explicit),
    }
    changes = {column: value for column, value in columns.items() if value is not None}
    with _open_catalog() as db:
        if not db.update_episode(validate_slug(slug), changes):
            raise ValueError(f"No episode with slug '{slug}'.")
        site = rebuild_site(db)
    return {"status": "updated", "slug": slug, "changed_fields": sorted(changes), **site}


@mcp.tool(annotations=PUBLIC_EDIT)
def podcast_deploy_site(message: str, blog_slugs: list[str] | None = None) -> dict[str, Any]:
    """Push the rebuilt feed and episode pages live (only after human approval). Astro mode: commits only engine-generated files on WEBSITE_BRANCH with `message` and pushes; blog_slugs adds posts written by podcast_create_blog. None mode: nothing to do. Use after podcast_update_episode or podcast_rebuild_feed, or to retry a publish whose git_deploy was skipped or failed."""
    blog_paths = [blog_post_path(slug) for slug in blog_slugs or []]
    # deploy_paths skips missing files silently; a mistyped slug must fail loudly instead.
    if missing := [str(path) for path in blog_paths if not path.exists()]:
        raise FileNotFoundError(f"No blog post at: {', '.join(missing)}")
    return deploy_site(message, blog_paths)


@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_backup(check_only: bool = False) -> dict[str, Any]:
    """Back up audio, catalog, and show assets to the private Hugging Face dataset (HF_BACKUP_REPO). Runs in the background because even an incremental run takes minutes: returns "started" (or "running") at once. Call again with check_only=true for "running", "completed", or "failed" plus the end of the log. Run after every publish or episode edit."""
    return backup_status() if check_only else start_backup()


@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_upload_media_asset(
    file_path: str,
    target_filename: str | None = None,
    prefix: str = "media",
) -> dict[str, Any]:
    """Upload blog assets (GIF, PNG, JPEG, WebP, MP4, WebM, MOV) to the public Cloudflare R2 bucket. Refuses to overwrite an existing key. Returns the public URL and a Markdown embed."""
    return upload_blog_media(file_path=file_path, target_filename=target_filename, prefix=prefix)


@mcp.tool(annotations=PUBLIC_RELEASE)
def podcast_publish_to_youtube(
    video_path: str,
    title: str,
    description: str,
    tags: list[str] | None = None,
    privacy_status: PrivacyStatus = "unlisted",
    episode_slug: str | None = None,
) -> dict[str, Any]:
    """Upload a video to the authenticated YouTube channel using resumable chunked upload. Pass episode_slug so the video's metrics are credited to that episode."""
    return upload_video_to_youtube(
        video_path=video_path,
        title=title,
        description=description,
        tags=tags,
        privacy_status=privacy_status,
        episode_slug=episode_slug,
    )


@mcp.tool(annotations=READ_ONLY)
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


@mcp.tool(annotations=PUBLIC_RELEASE)
def podcast_post_to_x(
    text: str, media_path: str | None = None, dry_run: bool = False, episode_slug: str | None = None
) -> dict[str, Any]:
    """Post to X as the configured account (only after explicit human approval of the exact text and media). Attaches one optional image, GIF, or video; dry_run checks everything without posting. Pass episode_slug when promoting an episode so the post's metrics are credited to it. Returns the post id and URL."""
    return post_to_x(text=text, media_path=media_path, dry_run=dry_run, episode_slug=episode_slug)


@mcp.tool(annotations=PUBLIC_RELEASE)
def podcast_post_to_instagram(
    media_paths: list[str],
    caption: str = "",
    post_type: PostType = "image",
    dry_run: bool = False,
    episode_slug: str | None = None,
) -> dict[str, Any]:
    """Publish a feed post to the configured Instagram professional account (only after explicit human approval of the exact image and caption). post_type "image": one PNG, JPEG, or WebP with an aspect ratio between 4:5 and 1.91:1, converted to JPEG and staged on the public R2 domain for Instagram to fetch. Captions: up to 2,200 characters and 30 hashtags; links in captions aren't clickable on Instagram. dry_run checks everything (including the image) without uploading or posting. The same image with the same caption is never posted twice: that post is returned instead. Pass episode_slug when promoting an episode. Returns the media id and post URL."""
    return post_to_instagram(
        media_paths=media_paths, caption=caption, post_type=post_type, dry_run=dry_run, episode_slug=episode_slug
    )


# Additive, not public: it only creates an unpublished draft, which the human publishes on dev.to.
@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_crosspost_to_devto(blog_slug: str, tags: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Cross-post a published companion blog post (from podcast_publish or podcast_create_blog) to dev.to as an unpublished draft, with a canonical URL back to the website (or the post's own `canonical` frontmatter, if it was first published elsewhere) and site-relative links made absolute. Up to 4 tags, lowercase letters and digits only. Never publishes: the human reviews and publishes the draft on dev.to. If a draft of the post already exists, returns it instead of creating another, so retrying is safe. dry_run checks everything without calling dev.to."""
    return crosspost_to_devto(slug=blog_slug, tags=tags, dry_run=dry_run)


# Additive, not public: it only creates an unpublished draft, which the human publishes (and emails) on Substack.
@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_crosspost_to_substack(blog_slug: str, dry_run: bool = False) -> dict[str, Any]:
    """Cross-post a published companion blog post to Substack as an unpublished draft, opened by an "Originally published at" line linking to the original (Substack has no canonical URL setting), with site-relative links made absolute and the post's tags. Images must be https URLs. Returns warnings for content Substack can't show (tables, raw HTML) so the human can fix the draft. Never publishes or emails: the human does that in Substack's editor. If a draft of the post already exists, returns it instead of creating another, so retrying is safe. dry_run checks everything without contacting Substack. Uses Substack's private API, which can change; report failures as they are."""
    return crosspost_to_substack(slug=blog_slug, dry_run=dry_run)


@mcp.tool(annotations=LOCAL_WRITE)
def podcast_stage_thumbnail(
    prompt_text: str,
    output_filename: str,
    style: str | None = None,
) -> dict[str, Any]:
    """Step 1 of a thumbnail: builds a 16:9 art prompt (no lettering, empty right side) and returns where to save the rendered art. Does not generate the image; render it with your image model, then call podcast_render_thumbnail."""
    return generate_youtube_thumbnail(prompt_text=prompt_text, output_filename=output_filename, style=style)


@mcp.tool(annotations=LOCAL_WRITE)
def podcast_render_thumbnail(
    base_image_path: str,
    headline_lines: list[str],
    slug: str,
    label: str | None = None,
    accent_line: int | None = -1,
) -> dict[str, Any]:
    """Step 2 of a thumbnail: composites 1-4 headline lines (2-3 short words read best; don't repeat the title) and an optional small label onto the art, in the show's style ($DATA_DIR/templates/thumbnail_style.json). accent_line picks the highlighted line (default last; null for none). Writes DATA_DIR/thumbnails/<slug>-final.png (JPEG if over 2 MB). Show the result to the human before setting it."""
    output = settings.thumbnails_dir / f"{validate_slug(slug)}-final.png"
    return render_thumbnail(Path(base_image_path), headline_lines, output, label=label, accent_line=accent_line)


@mcp.tool(annotations=PUBLIC_EDIT)
def podcast_set_youtube_thumbnail(video_id: str, thumbnail_path: str, episode_slug: str | None = None) -> dict[str, Any]:
    """Sets a video's custom thumbnail on YouTube (public; only after the human approved the exact image). JPEG/PNG up to 2 MB. Logs the change so the metrics dashboard marks it; episode_slug defaults to the video's tracked episode."""
    return set_video_thumbnail(video_id=video_id, thumbnail_path=thumbnail_path, episode_slug=episode_slug)


@mcp.tool(annotations=ADDITIVE_EXTERNAL)
def podcast_metrics_collect(sources: list[str] | None = None, days: int | None = None) -> list[dict[str, Any]]:
    """Fetch audience metrics (read-only API calls) into the local metrics store. Default: every configured source, resuming where each left off; `days` re-fetches that many days. Returns per-source status."""
    if days is not None and days < 1:
        raise ValueError("days must be at least 1.")
    return run_collection(sources, days)


@mcp.tool(annotations=READ_ONLY)
def podcast_metrics_summary(days: int = 30) -> dict[str, Any]:
    """Summarize collected audience metrics for the last `days` days: headline numbers vs the previous period, top episodes, top apps/countries, and each source's freshness."""
    return summarize(days=max(1, days))


@mcp.tool(annotations=READ_ONLY)
def podcast_episode_metrics(slug: str) -> dict[str, Any]:
    """All collected metrics for one episode: daily downloads/listens per source and social posts linking to it."""
    return episode_metrics(validate_slug(slug))


@mcp.tool(annotations=LOCAL_WRITE)
def podcast_metrics_report(days: int = 90) -> dict[str, Any]:
    """Write the self-contained HTML metrics dashboard for the last `days` days and return its path."""
    return {"status": "written", "path": str(write_report(days=max(1, days)))}


if __name__ == "__main__":
    # basicConfig logs to stderr, keeping stdout clean for the MCP protocol stream.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    mcp.run()
