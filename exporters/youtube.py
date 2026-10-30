"""Uploads episode videos and sets custom thumbnails on the authenticated YouTube channel (Data API v3).

Every upload is logged to `$DATA_DIR/social/youtube_uploads.jsonl`. That log, plus an
optional `$DATA_DIR/youtube_videos.json` mapping for videos uploaded some other way,
defines which videos the YouTube metrics collector measures. Thumbnail changes are
logged to `$DATA_DIR/social/youtube_thumbnails.jsonl` so the dashboard can mark them.
The channel's uploads can be listed, and a video deleted one at a time (permanent; logged
to `$DATA_DIR/social/youtube_deletions.jsonl`).

YouTube has no Shorts upload: it files a square or vertical video of up to 3 minutes as a
Short by itself. `expect_short` measures the file with ffprobe first and refuses one that
would land as a regular video.

Uploads are checked against the episode's discovery brief (`core.discovery`) before any
bytes are sent, and can add a caption track and a playlist entry once the video is up.
"""

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.http import MediaFileUpload

from core.config import settings
from core.discovery import check_youtube, gate
from core.formatting import validate_slug
from core.jsonl import append_record, read_records
from core.thumbnail import MAX_THUMBNAIL_BYTES

logger = logging.getLogger("youtube_exporter")

PrivacyStatus = Literal["public", "private", "unlisted"]

VIDEO_CONTENT_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
# YouTube Data API hard limits on snippet fields.
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
# Tags share a 500-character budget; YouTube counts the commas between them and quotes around multi-word tags.
MAX_TAGS_CHARS = 500
# Caption files YouTube reads; a plain-text transcript is timed to the audio by YouTube (`sync`).
CAPTION_TYPES = {".vtt", ".srt", ".txt"}
PLAYLIST_ID_RE = re.compile(r"[A-Za-z0-9_-]{2,64}")
SCIENCE_AND_TECH_CATEGORY = "28"
# Needed only for daily views and watch time; uploads and lifetime counts work without it.
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
# YouTube's custom-thumbnail formats (the 2 MB size cap lives with the renderer).
THUMBNAIL_CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
# The uploads playlist pages hold at most 50 videos; listing costs 1 quota unit per page, deleting 50.
MAX_LIST_LIMIT = 50
# Since October 15, 2024, YouTube files square or vertical uploads of up to 3 minutes as Shorts.
MAX_SHORT_SECONDS = 180
FFPROBE_TIMEOUT_SECONDS = 60


def measure_video(path: Path) -> dict[str, Any]:
    """Width, height (as displayed, after any rotation), and duration in seconds of a video file, via ffprobe."""
    binary = settings.ffprobe_path or (Path(found) if (found := shutil.which("ffprobe")) else None)
    if binary is None or not binary.is_file():
        raise FileNotFoundError(f"ffprobe not found at {binary or 'PATH'}; install ffmpeg or set FFPROBE_PATH.")
    completed = subprocess.run(
        [
            str(binary), "-v", "error", "-select_streams", "v:0", "-of", "json",
            "-show_entries", "stream=width,height:stream_side_data=rotation:stream_tags=rotate:format=duration",
            str(path),
        ],
        capture_output=True, text=True, check=False, timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    probe = json.loads(completed.stdout or "{}")
    if completed.returncode != 0 or not probe.get("streams"):
        raise ValueError(f"ffprobe couldn't read a video stream from {path.name}: {completed.stderr.strip()[:200]}")
    stream = probe["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    # Phones store portrait video as landscape frames plus a rotation flag; a quarter turn swaps what viewers see.
    rotations = [d.get("rotation") for d in stream.get("side_data_list", [])] + [stream.get("tags", {}).get("rotate")]
    if any(abs(int(float(r))) % 180 == 90 for r in rotations if r is not None):
        width, height = height, width
    return {"width": width, "height": height, "duration_seconds": float(probe.get("format", {}).get("duration", 0))}


def short_problem(video: dict[str, Any]) -> str | None:
    """Why YouTube would file this video as a regular video instead of a Short, or None if it will be a Short."""
    problems = []
    if video["width"] > video["height"]:
        problems.append(f"it's horizontal ({video['width']}x{video['height']})")
    if video["duration_seconds"] > MAX_SHORT_SECONDS:
        problems.append(f"it runs {video['duration_seconds']:.0f}s, over {MAX_SHORT_SECONDS}s")
    return " and ".join(problems) or None


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


def _own_channel(youtube: Resource) -> dict[str, Any]:
    """The authenticated channel: its id and the id of its uploads playlist."""
    return youtube.channels().list(part="id,contentDetails", mine=True).execute()["items"][0]


def list_videos(limit: int = 25, page_token: str | None = None) -> dict[str, Any]:
    """One page of the channel's uploads, newest first, with each video's tracked episode and the next page token."""
    youtube = get_authenticated_youtube_service()
    channel = _own_channel(youtube)
    page = youtube.playlistItems().list(
        part="snippet,status",
        playlistId=channel["contentDetails"]["relatedPlaylists"]["uploads"],
        maxResults=max(1, min(limit, MAX_LIST_LIMIT)),
        pageToken=page_token,
    ).execute()
    tracked = read_tracked_videos()
    videos = []
    for item in page.get("items", []):
        video_id = item["snippet"]["resourceId"]["videoId"]
        videos.append({
            "video_id": video_id,
            "title": item["snippet"].get("title"),
            "published_at": item["snippet"].get("publishedAt"),
            "privacy": item.get("status", {}).get("privacyStatus"),
            "url": f"https://youtu.be/{video_id}",
            "episode_slug": tracked.get(video_id),
        })
    return {"channel_id": channel["id"], "videos": videos, "next_page_token": page.get("nextPageToken")}


def delete_video(video_id: str, dry_run: bool = False) -> dict[str, Any]:
    """Permanently delete one of the channel's videos and log what it was; `dry_run` only shows what would go.

    The video is read first, so a malformed id, a missing video, or another channel's
    video is refused before anything is deleted. Needs the youtube.force-ssl (or youtube) scope.
    """
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError(f"{video_id!r} doesn't look like a YouTube video id.")
    youtube = get_authenticated_youtube_service()
    found = youtube.videos().list(part="snippet,status", id=video_id).execute().get("items", [])
    if not found:
        raise ValueError(f"No video {video_id} is visible to this channel; nothing was deleted.")
    snippet = found[0]["snippet"]
    if snippet.get("channelId") != _own_channel(youtube)["id"]:
        raise ValueError(f"Video {video_id} belongs to another channel ({snippet.get('channelTitle')}); not deleted.")
    summary = {
        "video_id": video_id,
        "title": snippet.get("title"),
        "published_at": snippet.get("publishedAt"),
        "privacy": found[0].get("status", {}).get("privacyStatus"),
        "url": f"https://youtu.be/{video_id}",
        "episode_slug": read_tracked_videos().get(video_id),
    }
    if dry_run:
        return {"status": "dry_run", **summary}

    youtube.videos().delete(id=video_id).execute()
    result: dict[str, Any] = {"status": "deleted", **summary}
    # The video is already gone; a log failure is reported, never raised. The description is kept as the record.
    record = {"deleted_at": datetime.now(UTC).isoformat(), **result, "description": snippet.get("description", "")}
    if log_error := append_record(settings.youtube_deletion_log_path, record):
        result["log_error"] = log_error
    return result


def _tags_length(tags: list[str]) -> int:
    """Characters YouTube counts against the tag budget: commas between tags, quotes around multi-word ones."""
    return sum(len(tag) + (2 if " " in tag else 0) for tag in tags) + max(len(tags) - 1, 0)


def _check_snippet_limits(title: str, description: str, tags: list[str]) -> None:
    """Refuse text YouTube would reject or cut, instead of silently truncating it."""
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"YouTube titles allow {MAX_TITLE_CHARS} characters; this one has {len(title)}.")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(f"YouTube descriptions allow {MAX_DESCRIPTION_CHARS} characters; this one has {len(description)}.")
    if (used := _tags_length(tags)) > MAX_TAGS_CHARS:
        raise ValueError(f"YouTube tags allow {MAX_TAGS_CHARS} characters in total; these use {used}.")


def _caption_file(captions_path: str) -> Path:
    """Resolve and check a caption file before anything is uploaded."""
    path = Path(captions_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Caption file not found: {path}")
    if path.suffix.lower() not in CAPTION_TYPES:
        raise ValueError(f"Unsupported caption type {path.suffix!r}; allowed: {', '.join(sorted(CAPTION_TYPES))}")
    return path


def _insert_captions(youtube: Any, video_id: str, path: Path) -> dict[str, Any]:
    """Add the caption track (400 quota units); YouTube times a plain-text transcript to the audio itself."""
    language = settings.podcast_language.split("-")[0]
    media = MediaFileUpload(str(path), mimetype="application/octet-stream", resumable=False)
    caption = youtube.captions().insert(
        part="snippet",
        body={"snippet": {"videoId": video_id, "language": language, "name": "", "isDraft": False}},
        media_body=media,
        sync=path.suffix.lower() == ".txt",
    ).execute()
    return {"caption_id": caption["id"], "language": language}


def _add_to_playlist(youtube: Any, video_id: str, playlist_id: str) -> dict[str, Any]:
    """Append the video to one of the channel's playlists (50 quota units)."""
    body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    item = youtube.playlistItems().insert(part="snippet", body=body).execute()
    return {"playlist_id": playlist_id, "playlist_item_id": item["id"]}


def _after_upload(step: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a follow-up step on an already-published video, reporting a failure instead of raising it."""
    try:
        return {"status": "added", **step()}
    except Exception as exc:  # the video is live; the agent retries this step by hand, never the upload
        return {"status": "failed", "error": str(exc)}


def upload_video_to_youtube(
    video_path: str,
    title: str,
    description: str,
    tags: list[str] | None = None,
    category_id: str = SCIENCE_AND_TECH_CATEGORY,
    privacy_status: PrivacyStatus = "unlisted",
    notify_subscribers: bool = True,
    episode_slug: str | None = None,
    expect_short: bool = False,
    captions_path: str | None = None,
    playlist_id: str | None = None,
    discovery_override: str | None = None,
) -> dict[str, Any]:
    """Upload a video with resumable 10 MB chunks and log it; return its id, URL, and final privacy status.

    `episode_slug` credits the video's metrics to that episode, and its discovery brief is checked
    (title, description hook, tags, chapters) before anything is uploaded, unless `discovery_override`
    gives the human's reason. `expect_short` measures the file first and refuses it unless YouTube will
    file it as a Short. After the upload, `captions_path` is added as the caption track and the video is
    added to `playlist_id`; those two steps report failures in the result instead of raising.
    """
    file_path = Path(video_path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Video file not found at: {file_path}")
    mimetype = VIDEO_CONTENT_TYPES.get(file_path.suffix.lower())
    if mimetype is None:
        raise ValueError(f"Unsupported video type {file_path.suffix!r}; allowed: {', '.join(VIDEO_CONTENT_TYPES)}")
    slug = validate_slug(episode_slug) if episode_slug else None
    upload_tags = tags or settings.default_tags
    _check_snippet_limits(title, description, upload_tags)
    captions = _caption_file(captions_path) if captions_path else None
    if playlist_id and not PLAYLIST_ID_RE.fullmatch(playlist_id):
        raise ValueError(f"Invalid playlist id {playlist_id!r}.")
    override = gate(
        "youtube", slug, discovery_override,
        lambda brief: check_youtube(brief, title, description, upload_tags, is_short=expect_short),
    )
    measured: dict[str, Any] | None = None
    if expect_short:
        measured = measure_video(file_path)
        if problem := short_problem(measured):
            raise ValueError(
                f"{file_path.name} would upload as a regular video, not a Short: {problem}. "
                f"Shorts must be square or vertical and at most {MAX_SHORT_SECONDS}s; nothing was uploaded."
            )

    youtube = get_authenticated_youtube_service()
    body = {
        "snippet": {"title": title, "description": description, "tags": upload_tags, "categoryId": category_id},
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
    if measured is not None:
        result["short"] = measured
        # The Shorts feed only shows public videos, so an unlisted or private Short reaches nobody there.
        if result["privacy_status"] != "public":
            result["note"] = "Uploaded as a Short, but only public Shorts appear in the Shorts feed."
    if override is not None:
        result["discovery_override"] = override

    # The video is already live from here on: captions, playlist, and log failures are reported, never raised,
    # because a retry would upload the video twice.
    if captions:
        result["captions"] = _after_upload(lambda: _insert_captions(youtube, response["id"], captions))
    if playlist_id:
        result["playlist"] = _after_upload(lambda: _add_to_playlist(youtube, response["id"], playlist_id))
    record = {"uploaded_at": datetime.now(UTC).isoformat(), **result}
    if log_error := append_record(settings.youtube_upload_log_path, record):
        result["log_error"] = log_error
    return result
