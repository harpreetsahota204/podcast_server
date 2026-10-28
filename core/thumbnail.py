"""Stages YouTube thumbnail prompts.

No image is generated here: the caller (an agent with an image model) renders the
prompt and saves the result to the returned `local_path`.
"""

from typing import Any

from core.config import settings
from core.formatting import validate_slug


def generate_youtube_thumbnail(
    prompt_text: str,
    output_filename: str,
    style: str = "clean, high-contrast studio aesthetic",
) -> dict[str, Any]:
    """Return a 16:9 thumbnail prompt plus the local path and R2 key the image should use."""
    validate_slug(output_filename)
    # Create the staging dir up front so the image generator can write straight into it.
    settings.thumbnails_dir.mkdir(parents=True, exist_ok=True)

    full_prompt = (
        f"{prompt_text}. YouTube video thumbnail composition, 16:9 wide, clean focal subject, "
        f"{style}, professional studio lighting, uncluttered background, dramatic lighting, 8k resolution."
    )

    return {
        "status": "staged",
        "prompt": full_prompt,
        "local_path": str(settings.thumbnails_dir / f"{output_filename}.png"),
        "target_key": f"media/thumbnails/{output_filename}.png",
    }
