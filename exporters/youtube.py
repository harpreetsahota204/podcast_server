"""Uploads episode videos and sets custom thumbnails on the authenticated YouTube channel (Data API v3).

Every upload is logged to `$DATA_DIR/social/youtube_uploads.jsonl`. That log, plus an
optional `$DATA_DIR/youtube_videos.json` mapping for videos uploaded some other way,
defines which videos the YouTube metrics collector measures. Thumbnail changes are
logged to `$DATA_DIR/social/youtube_thumbnails.jsonl` so the dashboard can mark them.
"""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.http import MediaFileUpload

from core.config import settings
from core.formatting import validate_slug
from core.jsonl import append_record, read_records
from core.thumbnail import MAX_THUMBNAIL_BYTES

logger = logging.getLogger("youtube_exporter")

PrivacyStatus = Literal["public", "private", "unlisted"]

VIDEO_CONTENT_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
# YouTube Data API hard limits on snippet fields.
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
SCIENCE_AND_TECH_CATEGORY = "28"
# Needed only for daily views and watch time; uploads and lifetime counts work without it.
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
# YouTube's custom-thumbnail formats (the 2 MB size cap lives with the renderer).
THUMBNAIL_CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def load_credentials() -> Credentials:
    """Read the saved OAuth token (refreshed in memory when expired); its scopes decide what's available."""
    token_file = settings.youtube_token_path
    if not token_file.exists():
        raise FileNotFoundError(f"YouTube OAuth token not found at {token_file} (set YOUTUBE_TOKEN_PATH).")
    return Credentials.from_authorized_user_file(str(token_file))


def get_authenticated_youtube_service() -> Resource:
    """Build a YouTube Data API client from the saved OAuth token."""
    return build("youtube", "v3", credentials=load_credentials())


def read_tracked_videos() -> dict[str, str | None]:
    """Videos that belong to the show: video id → episode slug (None when unknown).

    Sources, later ones winning: `youtube_videos.json` (`{"videos": [{"video_id", "episode_slug"}]}`,
    for back catalogs) and the upload log. Malformed log lines are skipped.
    """
    tracked: dict[str, str | None] = {}
    if settings.youtube_videos_file.exists():
        for video in json.loads(settings.youtube_videos_file.read_text(encoding="utf-8")).get("videos", []):
            if video.get("video_id"):
                tracked[video["video_id"]] = video.get("episode_slug")
    for record in read_records(settings.youtube_upload_log_path):
        if record.get("video_id"):
            # A slug recorded at upload time wins, but a log line without one keeps the mapping's.
            tracked[record["video_id"]] = record.get("episode_slug") or tracked.get(record["video_id"])
    return tracked


def set_video_thumbnail(video_id: str, thumbnail_path: str, episode_slug: str | None = None) -> dict[str, Any]:
    """Replace a video's thumbnail and log the change; validates the file before contacting YouTube.

    The episode defaults to the one the video is tracked under, so the change shows up
    on that episode's metrics.
    """
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError(f"{video_id!r} doesn't look like a YouTube video id.")
    path = Path(thumbnail_path).expanduser().resolve()
    mimetype = THUMBNAIL_CONTENT_TYPES.get(path.suffix.lower())
    if mimetype is None:
        raise ValueError(f"Thumbnails must be {', '.join(THUMBNAIL_CONTENT_TYPES)}; got {path.suffix!r}.")
    if not path.is_file():
        raise FileNotFoundError(f"Thumbnail not found: {path}")
    if path.stat().st_size > MAX_THUMBNAIL_BYTES:
        raise ValueError(f"{path.name} is over YouTube's 2 MB thumbnail limit.")
    slug = validate_slug(episode_slug) if episode_slug else read_tracked_videos().get(video_id)

    youtube = get_authenticated_youtube_service()
    youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(path), mimetype=mimetype)).execute()
    result: dict[str, Any] = {
        "status": "set",
        "video_id": video_id,
        "video_url": f"https://youtu.be/{video_id}",
        "thumbnail_file": str(path),
        "episode_slug": slug,
    }
    # Already live on YouTube, so a log failure is reported, never raised.
    record = {"changed_at": datetime.now(UTC).isoformat(), **result}
    if log_error := append_record(settings.youtube_thumbnail_log_path, record):
        result["log_error"] = log_error
    return result


def read_thumbnail_changes() -> list[dict[str, Any]]:
    """Every thumbnail change made through the engine, oldest first."""
    return [r for r in read_records(settings.youtube_thumbnail_log_path) if r.get("video_id") and r.get("changed_at")]


def upload_video_to_youtube(
    video_path: str,
    title: str,
    description: str,
    tags: list[str] | None = None,
    category_id: str = SCIENCE_AND_TECH_CATEGORY,
    privacy_status: PrivacyStatus = "unlisted",
    notify_subscribers: bool = True,
    episode_slug: str | None = None,
) -> dict[str, Any]:
    """Upload a video with resumable 10 MB chunks and log it; return its id, URL, and final privacy status.

    `episode_slug` credits the video's metrics to that episode.
    """
    file_path = Path(video_path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Video file not found at: {file_path}")
    mimetype = VIDEO_CONTENT_TYPES.get(file_path.suffix.lower())
    if mimetype is None:
        raise ValueError(f"Unsupported video type {file_path.suffix!r}; allowed: {', '.join(VIDEO_CONTENT_TYPES)}")
    slug = validate_slug(episode_slug) if episode_slug else None

    youtube = get_authenticated_youtube_service()
    body = {
        "snippet": {
            "title": title[:MAX_TITLE_CHARS],
            "description": description[:MAX_DESCRIPTION_CHARS],
            "tags": tags or settings.default_tags,
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
    }

    logger.info("Initiating YouTube upload for %s", file_path)
    media = MediaFileUpload(str(file_path), mimetype=mimetype, chunksize=10 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        # Notifications only make sense for public uploads; YouTube ignores them otherwise.
        notifySubscribers=notify_subscribers and privacy_status == "public",
    )

    # Each next_chunk() call sends one chunk; response stays None until the final chunk lands.
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info("YouTube upload progress: %d%%", int(status.progress() * 100))

    video_url = f"https://youtu.be/{response['id']}"
    logger.info("YouTube upload complete: %s", video_url)
    result: dict[str, Any] = {
        "status": "success",
        "video_id": response["id"],
        "video_url": video_url,
        "title": response["snippet"]["title"],
        "privacy_status": response["status"]["privacyStatus"],
        "episode_slug": slug,
    }

    # The video is already live here, so a log failure is reported, never raised (a retry would upload twice).
    record = {"uploaded_at": datetime.now(UTC).isoformat(), **result}
    if log_error := append_record(settings.youtube_upload_log_path, record):
        result["log_error"] = log_error
    return result
