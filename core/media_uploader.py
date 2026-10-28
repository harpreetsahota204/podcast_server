"""Uploads blog media (GIFs, images, short videos) to the public R2 bucket.

The bucket is world-readable, so only known media extensions are accepted:
an agent must not be able to publish arbitrary local files such as `.env`.
"""

from pathlib import Path
from typing import Any

from core.formatting import slugify, validate_slug
from core.storage import R2StorageClient

# Explicit map instead of `mimetypes`, whose answers vary by OS. SVG is excluded
# because it can carry script.
MEDIA_CONTENT_TYPES = {
    ".gif": "image/gif",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


def build_media_key(path: Path, target_filename: str | None, prefix: str) -> str:
    """Validate the source file type, name, and prefix, and return the R2 object key.

    The extension always comes from the source file, so a secret can't be smuggled
    out by renaming it to `x.gif`. The stem is slugified (`Robot Demo` -> `robot-demo`)
    and each `/`-separated prefix segment must already be slug-safe.
    """
    suffix = path.suffix.lower()
    if suffix not in MEDIA_CONTENT_TYPES:
        raise ValueError(f"Unsupported media type {suffix!r}; allowed: {', '.join(sorted(MEDIA_CONTENT_TYPES))}")
    stem = validate_slug(slugify(Path(target_filename).stem if target_filename else path.stem))
    for segment in prefix.strip("/").split("/"):
        validate_slug(segment)
    return f"{prefix.strip('/')}/{stem}{suffix}"


def upload_blog_media(file_path: str, target_filename: str | None = None, prefix: str = "media") -> dict[str, Any]:
    """Upload one media file under `prefix/` and return its public URL plus a Markdown embed."""
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Media file not found: {path}")

    target_key = build_media_key(path, target_filename, prefix)
    content_type = MEDIA_CONTENT_TYPES[path.suffix.lower()]

    client = R2StorageClient()
    # Objects are served with immutable cache headers, so an overwrite would leave
    # CDNs and browsers showing the old file indefinitely; require a new name instead.
    if client.object_exists(target_key):
        raise FileExistsError(f"r2://{client.bucket_name}/{target_key} already exists; choose a new target_filename.")

    file_size = path.stat().st_size
    public_url = client.upload_file(path, target_key, content_type=content_type)

    return {
        "status": "success",
        "file_name": Path(target_key).name,
        "content_type": content_type,
        "file_size_bytes": file_size,
        "target_key": target_key,
        "public_url": public_url,
        "markdown_embed": f"![{path.stem}]({public_url})",
    }
