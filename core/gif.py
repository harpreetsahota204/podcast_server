"""Short GIFs from video clips (paper demos, screen recordings), sized to load quickly in an email.

Newsletters and blog posts often show a few seconds of a demo video. A GIF plays inline
in every email client, but a full-size one runs to tens of megabytes. `make_gif` cuts the
clip and steps down the width and frame rate until the file fits the size budget.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.config import settings
from core.formatting import validate_slug

# (max width px, frames per second), tried in order until the GIF fits the size budget.
ATTEMPTS = ((640, 12), (560, 10), (480, 8), (400, 8), (320, 6))
# Email clients load every GIF in full before playing it; about 2 MB keeps that quick on a phone.
DEFAULT_MAX_MB = 2.0
MAX_CLIP_SECONDS = 30
FFMPEG_TIMEOUT_SECONDS = 300
# One palette per clip, built from what changes between frames, then Bayer dithering: smaller than
# error diffusion and steadier across frames.
PALETTE_FILTER = (
    "split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];"
    "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
)


def _ffmpeg() -> Path:
    """The ffmpeg binary from FFMPEG_PATH or PATH; raise with the fix if there isn't one."""
    binary = settings.ffmpeg_path or (Path(found) if (found := shutil.which("ffmpeg")) else None)
    if binary is None or not binary.is_file():
        raise FileNotFoundError(f"ffmpeg not found at {binary or 'PATH'}; install ffmpeg or set FFMPEG_PATH.")
    return binary


def make_gif(
    video_path: Path,
    name: str,
    start_seconds: float = 0.0,
    duration_seconds: float = 6.0,
    max_mb: float = DEFAULT_MAX_MB,
) -> dict[str, Any]:
    """Cut `duration_seconds` from `video_path` at `start_seconds` into `$DATA_DIR/media/<name>.gif` under `max_mb`.

    Never upscales a small clip. Raises ValueError if even the smallest setting is over budget,
    so the caller shortens the clip instead of shipping a heavy file.
    """
    # 1. Check the inputs before running ffmpeg.
    video_path = Path(video_path).expanduser().resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"No video at {video_path}; download the clip first.")
    if not 0 < duration_seconds <= MAX_CLIP_SECONDS or start_seconds < 0:
        raise ValueError(f"Use a start of 0 or more and a duration of 0-{MAX_CLIP_SECONDS} seconds.")
    output = settings.media_dir / f"{validate_slug(name)}.gif"
    output.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(max_mb * 1024 * 1024)
    binary = _ffmpeg()

    # 2. Step down width and frame rate until the GIF fits.
    for width, fps in ATTEMPTS:
        scale = f"fps={fps},scale='min(iw,{width})':-1:flags=lanczos,{PALETTE_FILTER}"
        completed = subprocess.run(
            [str(binary), "-v", "error", "-y", "-ss", str(start_seconds), "-t", str(duration_seconds),
             "-i", str(video_path), "-vf", scale, str(output)],
            capture_output=True, text=True, check=False, timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise ValueError(f"ffmpeg couldn't make a GIF from {video_path.name}: {completed.stderr.strip()[:300]}")
        size = output.stat().st_size
        if size <= max_bytes:
            return {
                "status": "made",
                "path": str(output),
                "file_size_bytes": size,
                "max_width": width,
                "fps": fps,
                "next_step": "Show it to the human, then host it with podcast_upload_media_asset.",
            }
    output.unlink()
    raise ValueError(
        f"The clip is still {size / 1_048_576:.1f} MB at {ATTEMPTS[-1][0]} px and {ATTEMPTS[-1][1]} fps; "
        f"use a shorter duration_seconds (or raise max_mb above {max_mb})."
    )
