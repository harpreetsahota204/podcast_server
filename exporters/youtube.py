"""Uploads episode videos to the authenticated YouTube channel via the Data API v3."""

import logging
from pathlib import Path
from typing import Any, Literal

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.http import MediaFileUpload

from core.config import settings

logger = logging.getLogger("youtube_exporter")

PrivacyStatus = Literal["public", "private", "unlisted"]

VIDEO_CONTENT_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
# YouTube Data API hard limits on snippet fields.
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
SCIENCE_AND_TECH_CATEGORY = "28"


def get_authenticated_youtube_service() -> Resource:
    """Build a YouTube client from the saved OAuth token (refreshes in memory if expired)."""
    token_file = settings.youtube_token_path
    if not token_file.exists():
        raise FileNotFoundError(f"YouTube OAuth token not found at {token_file} (set YOUTUBE_TOKEN_PATH).")
    creds = Credentials.from_authorized_user_file(str(token_file))
    return build("youtube", "v3", credentials=creds)


def upload_video_to_youtube(
    video_path: str,
    title: str,
    description: str,
    tags: list[str] | None = None,
    category_id: str = SCIENCE_AND_TECH_CATEGORY,
    privacy_status: PrivacyStatus = "unlisted",
    notify_subscribers: bool = True,
) -> dict[str, Any]:
    """Upload a video with resumable 10 MB chunks; return its id, URL, and final privacy status."""
    file_path = Path(video_path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Video file not found at: {file_path}")
    mimetype = VIDEO_CONTENT_TYPES.get(file_path.suffix.lower())
    if mimetype is None:
        raise ValueError(f"Unsupported video type {file_path.suffix!r}; allowed: {', '.join(VIDEO_CONTENT_TYPES)}")

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
    return {
        "status": "success",
        "video_id": response["id"],
        "video_url": video_url,
        "title": response["snippet"]["title"],
        "privacy_status": response["status"]["privacyStatus"],
    }
