"""Cloudflare R2 (S3-compatible) client for episode audio and blog media.

Uploads use boto3's managed transfer, which streams in multipart chunks with
retries instead of loading whole files into memory.
"""

import logging
from pathlib import Path
from typing import Any, BinaryIO

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from core.config import settings
from core.db import CatalogDB
from core.net import open_https

logger = logging.getLogger("r2_storage")

# Every object key embeds its content identity (slug or filename), so CDN caches may keep it forever.
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
# 16 MB parts keep a 100 MB episode to ~7 requests while bounding memory per part.
_TRANSFER_CONFIG = TransferConfig(multipart_threshold=16 * 1024 * 1024, multipart_chunksize=16 * 1024 * 1024)


class R2StorageClient:
    """Uploads to and inspects the configured R2 bucket; inert if credentials are missing."""

    def __init__(self) -> None:
        """Build an S3 client against the R2 endpoint; on missing or malformed credentials, record why instead."""
        self.bucket_name = settings.r2_bucket_name
        self.s3_client: Any = None
        self.config_error: str | None = None

        if not (settings.r2_account_id and settings.r2_access_key_id and settings.r2_secret_access_key):
            self.config_error = "R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not all set in .env"
            return
        try:
            self.s3_client = boto3.client(
                "s3",
                endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=settings.r2_access_key_id,
                aws_secret_access_key=settings.r2_secret_access_key,
                config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
                region_name="auto",
            )
        except ValueError as e:
            # boto3 rejects the endpoint when R2_ACCOUNT_ID is a placeholder or malformed.
            self.config_error = f"R2_ACCOUNT_ID looks invalid: {e}"

    def is_configured(self) -> bool:
        """True when usable R2 credentials were found in settings."""
        return self.s3_client is not None

    def _require_client(self) -> Any:
        """Return the boto3 client or raise a clear error if credentials are missing."""
        if self.s3_client is None:
            raise RuntimeError(self.config_error or "Cloudflare R2 is not configured")
        return self.s3_client

    @staticmethod
    def public_url(key: str) -> str:
        """Public CDN URL for an object key."""
        return f"{settings.r2_public_domain}/{key}"

    def object_size(self, key: str) -> int | None:
        """Byte length of `key` in the bucket, or None if it doesn't exist."""
        try:
            head = self._require_client().head_object(Bucket=self.bucket_name, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None
            raise
        return head["ContentLength"]

    def object_exists(self, key: str) -> bool:
        """True if `key` exists; False when missing or when R2 isn't configured."""
        return self.s3_client is not None and self.object_size(key) is not None

    def upload_file(
        self,
        local_path: Path,
        key: str,
        content_type: str = "audio/mpeg",
        cache_control: str = IMMUTABLE_CACHE,
    ) -> str:
        """Stream a local file to R2 (immutable caching unless overridden); return its public URL."""
        client = self._require_client()
        logger.info("Uploading %s to r2://%s/%s", local_path, self.bucket_name, key)
        client.upload_file(
            str(local_path),
            self.bucket_name,
            key,
            ExtraArgs={"ContentType": content_type, "CacheControl": cache_control},
            Config=_TRANSFER_CONFIG,
        )
        return self.public_url(key)

    def download_file(self, key: str, local_path: Path) -> None:
        """Stream an object from R2 to a local file using multipart ranged GETs."""
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._require_client().download_file(self.bucket_name, key, str(local_path), Config=_TRANSFER_CONFIG)

    def _upload_stream(self, stream: BinaryIO, key: str, content_type: str) -> None:
        """Stream a non-seekable file-like object (e.g. an HTTP response) to R2."""
        self._require_client().upload_fileobj(
            stream,
            self.bucket_name,
            key,
            ExtraArgs={"ContentType": content_type, "CacheControl": IMMUTABLE_CACHE},
            Config=_TRANSFER_CONFIG,
        )

    def mirror_url_to_r2(self, source_url: str, target_filename: str) -> int:
        """Copy remote audio into `episodes/<target_filename>` unless already present; return its stored size.

        Uploads are atomic, so an existing key is a complete copy. The returned size
        comes from R2 itself: old hosts often report enclosure lengths that differ
        from the bytes they actually serve.
        """
        target_key = f"episodes/{target_filename}"
        if not self.object_exists(target_key):
            with open_https(source_url) as resp:
                self._upload_stream(resp, target_key, "audio/mpeg")
        size = self.object_size(target_key)
        if size is None:
            raise RuntimeError(f"Upload of {target_key} did not land in R2")
        return size


def sync_episodes_audio(limit: int | None = None) -> int:
    """Mirror audio for not-yet-synced episodes (all, or up to `limit`) into R2; return how many synced."""
    client = R2StorageClient()
    if not client.is_configured():
        logger.warning("R2 credentials missing; add them to .env to enable sync.")
        return 0

    synced_count = 0
    with CatalogDB(settings.database_path) as db:
        pending = [ep for ep in db.list_episodes() if not ep["audio_synced"] and ep["original_audio_url"]]
        pending = pending[:limit]
        logger.info("Starting audio sync for %d episodes...", len(pending))

        for ep in pending:
            # One bad source URL shouldn't abort the whole batch; log it and move on.
            try:
                size = client.mirror_url_to_r2(ep["original_audio_url"], ep["audio_filename"])
            except Exception:
                logger.exception("Failed to sync %s", ep["audio_filename"])
                continue
            db.mark_audio_synced(ep["id"], size)
            synced_count += 1

    logger.info("Audio sync complete. Synced %d episodes.", synced_count)
    return synced_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sync_episodes_audio()
