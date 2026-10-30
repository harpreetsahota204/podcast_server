"""Discovery files published alongside a new episode: transcript, chapters, and episode artwork.

Everything is validated before the episode's audio is uploaded, so a bad file
stops the publish with nothing public. Keys carry the publish timestamp because
R2 objects are immutable (served with year-long caching).
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from core.feed_generator import CHAPTERS_MIME
from core.formatting import parse_chapters
from core.storage import R2StorageClient

# Apple reads VTT or SRT (and prefers VTT); text/plain still serves other apps and search.
TRANSCRIPT_TYPES = {".vtt": "text/vtt", ".srt": "application/x-subrip", ".txt": "text/plain"}
# Apple's episode artwork rules match the show artwork: square JPEG or PNG, 1400-3000 px.
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
MIN_ART_PX, MAX_ART_PX = 1400, 3000
# Podcasting 2.0 JSON chapters format version.
CHAPTERS_VERSION = "1.2.0"


@dataclass(frozen=True)
class EpisodeAssets:
    """Validated local files and generated chapters, ready to upload once the publish goes ahead."""

    transcript: Path | None
    image: Path | None
    chapters_json: bytes | None


def _existing_file(path_str: str, allowed: dict[str, str], what: str) -> Path:
    """Resolve a local file and check its extension; raise if it's missing or of the wrong type."""
    path = Path(path_str).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{what} not found: {path}")
    if path.suffix.lower() not in allowed:
        raise ValueError(f"Unsupported {what} type {path.suffix!r}; allowed: {', '.join(allowed)}")
    return path


def _check_artwork(path: Path) -> None:
    """Refuse episode art Apple would reject: not square, or outside 1400-3000 px."""
    with Image.open(path) as image:
        width, height = image.size
    if width != height or not MIN_ART_PX <= width <= MAX_ART_PX:
        raise ValueError(
            f"Episode art must be square, {MIN_ART_PX}-{MAX_ART_PX} px (Apple's rule); {path.name} is {width}x{height}."
        )


def chapters_json(show_notes_markdown: str) -> bytes | None:
    """A Podcasting 2.0 chapters file from the show notes' timestamp lines, or None if there are none."""
    chapters = parse_chapters(show_notes_markdown)
    if not chapters:
        return None
    document = {"version": CHAPTERS_VERSION, "chapters": [{"startTime": c["seconds"], "title": c["title"]} for c in chapters]}
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def prepare_assets(transcript_path: str | None, episode_image_path: str | None, show_notes_markdown: str) -> EpisodeAssets:
    """Validate the optional transcript and artwork and build the chapters file, all before any upload."""
    transcript = _existing_file(transcript_path, TRANSCRIPT_TYPES, "transcript") if transcript_path else None
    image = _existing_file(episode_image_path, IMAGE_TYPES, "episode art") if episode_image_path else None
    if image:
        _check_artwork(image)
    return EpisodeAssets(transcript=transcript, image=image, chapters_json=chapters_json(show_notes_markdown))


def upload_assets(client: R2StorageClient, assets: EpisodeAssets, slug: str, stamp: str) -> dict[str, Any]:
    """Upload the prepared files under new keys and return the catalog columns they fill."""
    columns: dict[str, Any] = {}
    if assets.transcript:
        suffix = assets.transcript.suffix.lower()
        columns["transcript_type"] = TRANSCRIPT_TYPES[suffix]
        columns["transcript_url"] = client.upload_file(
            assets.transcript, f"transcripts/{slug}-{stamp}{suffix}", content_type=columns["transcript_type"]
        )
    if assets.chapters_json:
        columns["chapters_url"] = client.upload_bytes(assets.chapters_json, f"chapters/{slug}-{stamp}.json", CHAPTERS_MIME)
    if assets.image:
        suffix = assets.image.suffix.lower()
        columns["cover_image_url"] = client.upload_file(
            assets.image, f"episode-art/{slug}-{stamp}{suffix}", content_type=IMAGE_TYPES[suffix]
        )
    return columns
