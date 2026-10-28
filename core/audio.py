"""Pre-flight inspection of MP3 files before upload.

Podcast directories reject enclosures whose byte length or duration is wrong,
so these numbers are measured from the file rather than trusted from input.
"""

from pathlib import Path
from typing import Any

from mutagen.mp3 import MP3

from core.formatting import format_duration


def inspect_audio(file_path: Path) -> dict[str, Any]:
    """Return exact size, duration, sample rate, and bitrate for an MP3.

    Raises mutagen's HeaderNotFoundError for non-MP3 input, which also stops
    arbitrary files from being uploaded as episodes.
    """
    file_path = Path(file_path).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    size_bytes = file_path.stat().st_size
    info = MP3(file_path).info
    duration_seconds = float(info.length)

    return {
        "file_path": str(file_path),
        "file_size_bytes": size_bytes,
        "file_size_mb": round(size_bytes / (1024 * 1024), 2),
        "duration_seconds": round(duration_seconds, 2),
        "duration_formatted": format_duration(duration_seconds),
        "sample_rate_hz": info.sample_rate,
        "bitrate_kbps": int(info.bitrate / 1000) if info.bitrate else None,
        "mime_type": "audio/mpeg",
    }
