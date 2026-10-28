"""Fake show configuration shared by every test module; import it before anything from the engine.

Settings are built when core.config is first imported, so a complete fake show
must be in the environment before any engine import. The env file points at
/dev/null so a developer's real .env never leaks into tests, and every data path
is a fresh temp directory, so the real catalog, website repo, R2 bucket, and
metrics store are never touched.
"""

import os
import tempfile

os.environ.update({
    "PODCAST_SERVER_ENV_FILE": os.devnull,
    "PODCAST_TITLE": "Test Show",
    "PODCAST_DESCRIPTION": "A show used only by the test suite.",
    "PODCAST_AUTHOR": "Test Author",
    "PODCAST_OWNER_EMAIL": "owner@example.com",
    "PODCAST_LINK": "https://example.com/podcast",
    "PODCAST_FEED_URL": "https://example.com/podcast/rss.xml",
    "PODCAST_IMAGE_URL": "https://example.com/cover.jpg",
    "EPISODE_GUID_PREFIX": "test",
    "R2_BUCKET_NAME": "test-bucket",
    "R2_PUBLIC_DOMAIN": "https://audio.example.com",
    "USE_OP3_PREFIX": "true",
    "SITE_MODE": "astro",
    "WEBSITE_PATH": tempfile.mkdtemp(prefix="podcast-test-site-"),
    "DATA_DIR": tempfile.mkdtemp(prefix="podcast-test-data-"),
})
