"""Stdlib unittest suite for the podcast engine's pure logic and safety guards.

Run from the repo root:
    uv run python -m unittest discover -s tests -v

Every test points settings at temp directories, so the real catalog, website
repo, and R2 bucket are never touched.
"""

# Settings are built when core.config is first imported, so the fake show's
# environment (see fake_show.py) must be set before the imports below.
import fake_show  # noqa: F401 - must precede every engine import

import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from core.config import settings
from core.db import CatalogDB
from core.feed_generator import CONTENT_NS, build_rss_xml
from core.formatting import format_duration, op3_url, parse_duration, slugify, validate_slug, yaml_str
from core.media_uploader import build_media_key, upload_blog_media
from core.net import open_https
from exporters.astro_markdown import render_episode_markdown
from importers.descript import _is_descript_host, extract_chapters_from_markers_or_transcript


def make_episode(slug: str = "ep-one", guid: str = "guid-1", **overrides: Any) -> dict[str, Any]:
    """Build a complete catalog row with sensible defaults for tests."""
    episode = {
        "id": slug,
        "guid": guid,
        "episode_number": None,
        "season": 1,
        "title": "Episode One",
        "slug": slug,
        "summary": "Summary",
        "content_html": "",
        "content_markdown": "",
        "published_at": "2026-01-01T00:00:00+00:00",
        "original_audio_url": "https://example.com/a.mp3",
        "audio_filename": f"{slug}.mp3",
        "audio_url": f"https://cdn.example.com/episodes/{slug}.mp3",
        "audio_op3_url": f"https://op3.dev/e/cdn.example.com/episodes/{slug}.mp3",
        "duration_seconds": 61.0,
        "duration_formatted": "01:01",
        "file_size_bytes": 1234,
        "explicit": 0,
        "cover_image_url": "https://example.com/cover.jpg",
        "tags": "[]",
        "audio_synced": 1,
    }
    episode.update(overrides)
    return episode


class FormattingTests(unittest.TestCase):
    """Duration, slug, URL, and YAML string helpers."""

    def test_format_duration(self) -> None:
        """Sub-hour durations use MM:SS; longer ones use HH:MM:SS; junk yields 00:00."""
        self.assertEqual(format_duration(59.9), "00:59")
        self.assertEqual(format_duration(3725), "01:02:05")
        self.assertEqual(format_duration(None), "00:00")
        self.assertEqual(format_duration(-5), "00:00")

    def test_parse_duration(self) -> None:
        """All three timestamp shapes parse; malformed input yields 0.0."""
        self.assertEqual(parse_duration("01:02:05"), 3725)
        self.assertEqual(parse_duration("02:05"), 125)
        self.assertEqual(parse_duration("42.5"), 42.5)
        self.assertEqual(parse_duration("ab:cd"), 0.0)
        self.assertEqual(parse_duration(None), 0.0)

    def test_slugify_output_always_validates(self) -> None:
        """Titles with punctuation, underscores, and spaces become valid slugs."""
        slug = slugify("  Why Vision-Only Fails: Tactile_Policies & More!  ")
        self.assertEqual(slug, "why-vision-only-fails-tactile-policies-more")
        self.assertEqual(validate_slug(slug), slug)

    def test_validate_slug_rejects_unsafe_values(self) -> None:
        """Path traversal, separators, uppercase, and spaces are refused."""
        for bad in ["../etc/passwd", "a/b", "Upper", "has space", "", "-leading", "dot.md"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_slug(bad)

    def test_op3_url(self) -> None:
        """The scheme is stripped before prefixing with the OP3 redirect."""
        self.assertEqual(op3_url("https://cdn.x/episodes/a.mp3"), "https://op3.dev/e/cdn.x/episodes/a.mp3")

    def test_yaml_str_round_trips_hostile_input(self) -> None:
        """Quotes, backslashes, newlines, and YAML syntax survive a YAML parse unchanged."""
        hostile = 'He said "hi" \\ C:\\path\nnext: line # not a comment'
        self.assertEqual(yaml.safe_load(f"k: {yaml_str(hostile)}")["k"], hostile)


class DescriptTests(unittest.TestCase):
    """Chapter parsing and credential-scoping in the Descript importer."""

    def test_extract_chapters_all_marker_shapes_sorted(self) -> None:
        """Bracket, leading-time, and trailing-time headings parse; untimed headings are skipped."""
        transcript = "\n".join([
            "## Closing thoughts 1:02:03",
            "[00:00] Intro",
            "## Untimed heading",
            "(06:40) Architecture",
            "## 14:22 Trade-offs",
        ])
        chapters = extract_chapters_from_markers_or_transcript(transcript)
        self.assertEqual(
            [(c["time"], c["title"]) for c in chapters],
            [("00:00", "Intro"), ("06:40", "Architecture"), ("14:22", "Trade-offs"), ("01:02:03", "Closing thoughts")],
        )

    def test_token_only_sent_to_descript_hosts(self) -> None:
        """Substring tricks in the query or a lookalike domain don't count as Descript."""
        self.assertTrue(_is_descript_host("https://descript.com/x"))
        self.assertTrue(_is_descript_host("https://cdn.descript.com/x.mp3"))
        self.assertFalse(_is_descript_host("https://evil.com/?u=descript.com"))
        self.assertFalse(_is_descript_host("https://notdescript.com/x"))
        self.assertFalse(_is_descript_host("https://descript.com.evil.com/x"))

    def test_non_https_urls_rejected(self) -> None:
        """file:// and http:// downloads are refused before any I/O."""
        for url in ["file:///home/user/.ssh/id_ed25519", "http://descript.com/a.mp3"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                open_https(url)


class MediaUploadTests(unittest.TestCase):
    """The public-bucket uploader must only accept media and safe keys."""

    def test_build_media_key_slugifies_filename(self) -> None:
        """Display-style filenames are normalized into slug-safe keys."""
        self.assertEqual(build_media_key(Path("x.gif"), "Robot Demo_v2.GIF", "media/gifs"), "media/gifs/robot-demo-v2.gif")

    def test_build_media_key_rejects_bad_prefix_and_type(self) -> None:
        """Traversal prefixes and non-media extensions raise."""
        with self.assertRaises(ValueError):
            build_media_key(Path("x.gif"), None, "../secrets")
        for name in [".env", "auth.yml", "logo.svg"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                build_media_key(Path(name), None, "media")

    def test_upload_refuses_secret_files_before_touching_r2(self) -> None:
        """A real non-media file is rejected without ever constructing an R2 client."""
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / ".env"
            secret.write_text("R2_SECRET_ACCESS_KEY=x")
            with mock.patch("core.media_uploader.R2StorageClient") as client, self.assertRaises(ValueError):
                upload_blog_media(str(secret))
            client.assert_not_called()


class CatalogTests(unittest.TestCase):
    """Insert semantics protect GUIDs and hand-edited rows."""

    def setUp(self) -> None:
        """Open a fresh catalog in a temp dir."""
        self.tmp = tempfile.mkdtemp()
        self.db = CatalogDB(Path(self.tmp) / "catalog.db")

    def tearDown(self) -> None:
        """Close and delete the temp catalog."""
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_insert_episode_rejects_duplicate_slug(self) -> None:
        """A second publish under the same slug fails instead of minting a new GUID."""
        self.db.insert_episode(make_episode())
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.insert_episode(make_episode(guid="guid-2"))

    def test_insert_if_new_never_overwrites(self) -> None:
        """Re-importing an existing episode leaves its edited title and sync flag intact."""
        self.db.insert_episode(make_episode(title="Edited title", audio_synced=1))
        self.assertFalse(self.db.insert_episode_if_new(make_episode(title="Imported title", audio_synced=0)))
        row = self.db.get_episode_by_slug("ep-one")
        self.assertEqual((row["title"], row["audio_synced"]), ("Edited title", 1))

    def test_markdown_edit_clears_stale_imported_html(self) -> None:
        """Raw-SQL rewrites of the notes (as agents may do) drop the old imported HTML automatically."""
        self.db.insert_episode(make_episode(content_html="<p>old imported notes</p>", content_markdown="old text"))
        self.db.conn.execute("UPDATE episodes SET title = 'New title' WHERE slug = 'ep-one'")
        self.assertEqual(self.db.get_episode_by_slug("ep-one")["content_html"], "<p>old imported notes</p>")
        self.db.conn.execute("UPDATE episodes SET content_markdown = '### New notes' WHERE slug = 'ep-one'")
        self.assertEqual(self.db.get_episode_by_slug("ep-one")["content_html"], "")

    def test_excluded_episodes_can_never_be_inserted(self) -> None:
        """Exclusions from the tracked JSON block inserts by slug or GUID, even via raw SQL or insert-if-new."""
        excluded_file = Path(self.tmp) / "excluded.json"
        excluded_file.write_text(json.dumps({"episodes": [{"slug": "hh99", "guid": "fs-guid", "title": "HH 99"}]}))
        db = CatalogDB(Path(self.tmp) / "excl.db", excluded_file=excluded_file)
        try:
            self.assertTrue(db.is_excluded("hh99"))
            for episode in [make_episode(slug="hh99", guid="new"), make_episode(slug="other", guid="fs-guid")]:
                with self.subTest(episode=episode["slug"]), self.assertRaises(sqlite3.IntegrityError):
                    db.insert_episode_if_new(episode)
            self.assertEqual(db.count_episodes(), 0)
        finally:
            db.close()

    def test_list_episodes_default_returns_all(self) -> None:
        """The default limit means "no limit", ordered newest first."""
        for i in range(3):
            self.db.insert_episode(make_episode(slug=f"ep-{i}", guid=f"g{i}", published_at=f"2026-01-0{i + 1}"))
        self.assertEqual([e["slug"] for e in self.db.list_episodes()], ["ep-2", "ep-1", "ep-0"])


class StorageConfigTests(unittest.TestCase):
    """A placeholder or malformed R2 account id must degrade to "not configured", not crash."""

    def test_placeholder_account_id_reports_instead_of_raising(self) -> None:
        """podcast_status and the doctor rely on this to report setup problems."""
        from core.storage import R2StorageClient

        saved = (settings.r2_account_id, settings.r2_access_key_id, settings.r2_secret_access_key)
        settings.r2_account_id, settings.r2_access_key_id, settings.r2_secret_access_key = (
            "your_cloudflare_account_id", "key", "secret")
        try:
            client = R2StorageClient()
        finally:
            settings.r2_account_id, settings.r2_access_key_id, settings.r2_secret_access_key = saved
        self.assertFalse(client.is_configured())
        self.assertIn("R2_ACCOUNT_ID looks invalid", client.config_error)


class FeedAndExportTests(unittest.TestCase):
    """RSS and Astro output formats."""

    def test_rss_uses_conventional_prefixes(self) -> None:
        """Namespaced tags use itunes:/atom:, never ElementTree's ns0: fallbacks."""
        xml = build_rss_xml([make_episode()])
        self.assertIn("<itunes:duration>01:01</itunes:duration>", xml)
        self.assertIn("<atom:link", xml)
        self.assertNotIn("ns0:", xml)

    def test_feed_is_locked_to_owner(self) -> None:
        """The channel carries podcast:locked so other hosts refuse unauthorized imports."""
        xml = build_rss_xml([])
        self.assertIn(f'<podcast:locked owner="{settings.podcast_owner_email}">yes</podcast:locked>', xml)

    def test_feed_carries_spec_derived_podcast_guid(self) -> None:
        """The show GUID is UUIDv5 of the scheme-less feed URL, as the Podcasting 2.0 spec requires."""
        from core.config import podcast_guid_for_feed

        # Reference value from the Podcast Index spec's worked example.
        self.assertEqual(podcast_guid_for_feed("https://mp3s.nashownotes.com/pc20rss.xml/"),
                         "917393e3-1b1e-5cef-ace4-edaa54e1f810")
        self.assertIn(f"<podcast:guid>{settings.podcast_guid}</podcast:guid>", build_rss_xml([]))

    def test_markdown_show_notes_reach_content_encoded_without_raw_html(self) -> None:
        """New episodes (Markdown only) get rendered HTML show notes; embedded HTML is escaped."""
        ep = make_episode(content_markdown="### Chapters\n\n- **06:40** — Arch\n\n<script>x</script>")
        feed = ET.fromstring(build_rss_xml([ep]))  # noqa: S314 - parsing our own generated XML
        encoded = feed.find(f"channel/item/{{{CONTENT_NS}}}encoded").text
        self.assertIn("<strong>06:40</strong>", encoded)
        self.assertNotIn("<script>", encoded)

    def test_astro_frontmatter_parses_with_hostile_strings(self) -> None:
        """Titles with quotes/backslashes and CRLF summaries still produce valid single-line YAML."""
        ep = make_episode(title='The "Wizard" of C:\\Data', summary="line one\r\nline two", tags='["Robotics"]')
        front = yaml.safe_load(render_episode_markdown(ep).split("---")[1])
        self.assertEqual(front["title"], 'The "Wizard" of C:\\Data')
        self.assertEqual(front["description"], "line one line two")
        self.assertEqual(front["tags"], ["Robotics"])


class DeployAndPublishTests(unittest.TestCase):
    """Website deploy scoping and publish pre-checks, against a throwaway git repo."""

    def setUp(self) -> None:
        """Create a website repo cloned from a bare 'origin', plus a temp catalog."""
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.site = self.tmp / "site"
        git_env = ["-c", "init.defaultBranch=main"]
        subprocess.run(["git", *git_env, "init", "--bare", "-q", str(self.origin)], check=True)
        subprocess.run(["git", *git_env, "init", "-q", str(self.site)], check=True)
        for key, value in [("user.email", "t@example.com"), ("user.name", "Test")]:
            subprocess.run(["git", "config", key, value], cwd=self.site, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.origin)], cwd=self.site, check=True)
        (self.site / "README.md").write_text("site\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.site, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.site, check=True)

        self._saved = (settings.website_path, settings.database_path)
        settings.website_path = self.site
        settings.database_path = self.tmp / "catalog.db"

    def tearDown(self) -> None:
        """Restore settings and delete temp repos."""
        settings.website_path, settings.database_path = self._saved
        shutil.rmtree(self.tmp)

    def test_deploy_commits_only_generated_paths(self) -> None:
        """Unrelated untracked and staged files in the website repo are not published."""
        from core.site import deploy_paths

        (self.site / "setup-netlify-domain.mjs").write_text("secret wip\n")
        (self.site / "staged-wip.txt").write_text("wip\n")
        subprocess.run(["git", "add", "staged-wip.txt"], cwd=self.site, check=True)
        settings.site_episodes_dir.mkdir(parents=True)
        (settings.site_episodes_dir / "ep-one.md").write_text("episode\n")

        result = deploy_paths([settings.site_rss_path, settings.site_episodes_dir], "publish ep-one")

        self.assertEqual(result["status"], "pushed")
        pushed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "main"], cwd=self.origin, capture_output=True, text=True, check=True
        ).stdout.split()
        self.assertEqual(pushed, ["src/content/podcast/ep-one.md"])

    def test_deploy_skips_off_main(self) -> None:
        """Nothing is pushed when the website repo is on a feature branch."""
        from core.site import deploy_paths

        subprocess.run(["git", "checkout", "-qb", "feature"], cwd=self.site, check=True)
        self.assertEqual(deploy_paths([self.site / "README.md"], "x")["status"], "skipped")

    def test_publish_rejects_existing_slug_before_upload(self) -> None:
        """Re-publishing a slug fails before audio is inspected or anything reaches R2."""
        from mcp_server.server import podcast_publish

        with CatalogDB(settings.database_path) as db:
            db.insert_episode(make_episode())
        with mock.patch("mcp_server.server.R2StorageClient") as client, self.assertRaises(ValueError):
            podcast_publish(audio_file_path="/nonexistent.mp3", title="x", description="d", show_notes_markdown="n", slug="ep-one")
        client.assert_not_called()


SAMPLE_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Old Host Show</title>
    <item>
      <title>Second Episode</title>
      <link>https://oldhost.example/show/second-episode</link>
      <guid isPermaLink="false">guid-2</guid>
      <pubDate>Tue, 02 Jan 2024 10:00:00 +0000</pubDate>
      <enclosure url="https://cdn.oldhost.example/2.mp3" length="2000" type="audio/mpeg"/>
      <itunes:duration>01:02:03</itunes:duration>
      <itunes:episode>2</itunes:episode>
      <content:encoded><![CDATA[<p>Notes <b>two</b></p>]]></content:encoded>
    </item>
    <item>
      <title>First Episode!</title>
      <link>https://oldhost.example/?p=123</link>
      <pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate>
      <enclosure url="https://cdn.oldhost.example/1.mp3" length="1000" type="audio/mpeg"/>
      <itunes:duration>59:00</itunes:duration>
      <description>Plain &lt;i&gt;notes&lt;/i&gt;</description>
    </item>
    <item>
      <title>Video Bonus</title>
      <enclosure url="https://cdn.oldhost.example/bonus.m4v" length="5" type="video/mp4"/>
    </item>
  </channel>
</rss>"""


class RssImporterTests(unittest.TestCase):
    """Migration from another host's feed must keep identities exactly."""

    def test_parse_feed_preserves_guids_and_metadata(self) -> None:
        """GUIDs are copied verbatim; a missing GUID falls back to the enclosure URL, as apps do."""
        from importers.rss import parse_feed

        records, skipped = parse_feed(SAMPLE_FEED)
        by_slug = {r["slug"]: r for r in records}
        self.assertEqual(set(by_slug), {"second-episode", "first-episode"})
        self.assertEqual(by_slug["second-episode"]["guid"], "guid-2")
        self.assertEqual(by_slug["first-episode"]["guid"], "https://cdn.oldhost.example/1.mp3")
        self.assertEqual(by_slug["second-episode"]["duration_formatted"], "01:02:03")
        self.assertEqual(by_slug["second-episode"]["episode_number"], 2)
        self.assertEqual(by_slug["second-episode"]["content_html"], "<p>Notes <b>two</b></p>")
        self.assertEqual(by_slug["first-episode"]["summary"], "Plain notes")
        self.assertEqual(by_slug["first-episode"]["audio_url"], "https://audio.example.com/episodes/first-episode.mp3")
        self.assertEqual(by_slug["first-episode"]["audio_synced"], 0)
        self.assertEqual(skipped, ["Video Bonus"])

    def test_import_is_additive_and_respects_exclusions(self) -> None:
        """Re-imports add nothing, and excluded episodes never enter the catalog."""
        from importers.rss import import_feed

        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "feed.xml"
            feed.write_bytes(SAMPLE_FEED)
            excluded = Path(tmp) / "excluded.json"
            excluded.write_text(json.dumps({"episodes": [{"slug": "first-episode", "guid": "x", "title": "t"}]}))
            saved = (settings.database_path, settings.data_dir)
            settings.database_path, settings.data_dir = Path(tmp) / "catalog.db", Path(tmp)
            excluded.rename(settings.excluded_episodes_file)
            try:
                first = import_feed(str(feed))
                second = import_feed(str(feed))
            finally:
                settings.database_path, settings.data_dir = saved
        self.assertEqual((first["added"], first["excluded"], first["catalog_total"]), (1, 1, 1))
        self.assertEqual((second["added"], second["already_present"]), (0, 1))


class ConfigurationTests(unittest.TestCase):
    """Show-specific behavior comes from settings, not code."""

    def test_astro_mode_requires_website_path(self) -> None:
        """A misconfigured site mode fails at startup with a clear message."""
        from pydantic import ValidationError

        from core.config import Settings

        with mock.patch.dict("os.environ", {"SITE_MODE": "astro", "WEBSITE_PATH": ""}), self.assertRaises(ValidationError):
            Settings(website_path=None)

    def test_blog_url_template_must_be_https_with_slug(self) -> None:
        """The dev.to canonical URL template fails at startup when it isn't https or lacks {slug}."""
        from pydantic import ValidationError

        from core.config import Settings

        for template in ("http://example.com/blog/{slug}/", "https://example.com/blog/"):
            with self.subTest(template=template), self.assertRaises(ValidationError):
                Settings(blog_url_template=template)
        with self.assertRaises(ValidationError):
            Settings(substack_publication_url="http://show.substack.com")

    def test_none_mode_links_items_to_homepage_and_uploads_feed(self) -> None:
        """Without a website, items link to the show page and the feed goes to R2 with a short cache."""
        from core.site import FEED_CACHE, deploy_site, rebuild_site

        with tempfile.TemporaryDirectory() as tmp:
            saved = (settings.site_mode, settings.data_dir)
            settings.site_mode, settings.data_dir = "none", Path(tmp)
            try:
                with CatalogDB(Path(tmp) / "c.db") as db, mock.patch("core.site.R2StorageClient") as client:
                    db.insert_episode(make_episode())
                    # Imported but not yet mirrored: must stay out of the public feed.
                    db.insert_episode(make_episode(slug="not-mirrored", guid="g-nm", audio_synced=0))
                    client.return_value.upload_file.return_value = "https://audio.example.com/feed.xml"
                    result = rebuild_site(db)
                    xml = settings.rss_output_path.read_text()
                    deploy = deploy_site("msg")
            finally:
                settings.site_mode, settings.data_dir = saved
        upload_kwargs = client.return_value.upload_file.call_args.kwargs
        self.assertEqual(upload_kwargs["cache_control"], FEED_CACHE)
        self.assertEqual(result["feed_url"], "https://audio.example.com/feed.xml")
        self.assertIn(f"<item><title>Episode One</title><link>{settings.podcast_link}</link>", xml)
        self.assertNotIn("g-nm", xml)
        self.assertEqual(deploy["status"], "not_applicable")

    def test_social_templates_overridable_per_show(self) -> None:
        """A show's template file replaces the built-in wording."""
        from exporters.social import draft_social_package

        with tempfile.TemporaryDirectory() as tmp:
            saved = settings.data_dir
            settings.data_dir = Path(tmp)
            settings.templates_dir.mkdir()
            (settings.templates_dir / "social_short.txt").write_text("{hook} | {guest_name} | {link}")
            try:
                posts = draft_social_package("T", "Ada", " hook ", ["a"], "https://ep")
            finally:
                settings.data_dir = saved
        self.assertEqual(posts["post_short"], "hook | Ada | https://ep")
        self.assertIn("key points:\n- a", posts["post_long"])


class XPostTests(unittest.TestCase):
    """Posting to X: xurl output parsing, pre-upload guards, and the post log. xurl is always mocked."""

    def setUp(self) -> None:
        """Point settings at a temp data dir and a fake xurl binary, with one PNG ready to attach."""
        self.tmp = Path(tempfile.mkdtemp())
        self.fake_xurl = self.tmp / "xurl"
        self.fake_xurl.write_text("#!/bin/sh\n")
        self.image = self.tmp / "clip.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 20)
        self.saved = (settings.x_username, settings.xurl_app, settings.xurl_path, settings.data_dir)
        settings.x_username, settings.xurl_app = "testshow", "test-app"
        settings.xurl_path, settings.data_dir = self.fake_xurl, self.tmp

    def tearDown(self) -> None:
        """Restore settings and remove the temp dir."""
        settings.x_username, settings.xurl_app, settings.xurl_path, settings.data_dir = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _xurl_ok(*stdout: str) -> mock.MagicMock:
        """Mock subprocess.run returning each stdout in turn with exit code 0."""
        return mock.MagicMock(side_effect=[subprocess.CompletedProcess([], 0, out, "") for out in stdout])

    def test_parse_handles_color_codes_and_trailing_text(self) -> None:
        """Colored JSON followed by status noise still parses to the post id."""
        from exporters.x_post import id_from_payload, parse_xurl_json

        raw = '\x1b[32m{"data": {"id": "123", "text": "hi"}}\x1b[0m\nrate limit: 99 left'
        self.assertEqual(id_from_payload(parse_xurl_json(raw)), "123")

    def test_parse_raises_on_x_errors_and_missing_id(self) -> None:
        """An `errors` payload or a response without an id is a failure, not a silent success."""
        from exporters.x_post import XPostError, id_from_payload, parse_xurl_json

        with self.assertRaises(XPostError):
            parse_xurl_json('{"errors": [{"message": "duplicate content"}]}')
        with self.assertRaises(XPostError):
            id_from_payload(parse_xurl_json('{"data": {}}'))

    def test_non_media_file_refused_before_xurl_runs(self) -> None:
        """A secret file can't be attached, even when it exists."""
        from exporters.x_post import post_to_x

        secret = self.tmp / ".env"
        secret.write_text("TOKEN=abc")
        with mock.patch("exporters.x_post.subprocess.run") as run:
            with self.assertRaises(ValueError):
                post_to_x("hello", media_path=str(secret))
            with self.assertRaises(ValueError):
                post_to_x("hello", episode_slug="../not-a-slug")
        run.assert_not_called()

    def test_dry_run_and_unconfigured_account_never_call_xurl(self) -> None:
        """dry_run reports what would be sent; with no X_USERNAME, posting refuses."""
        from exporters.x_post import XPostError, post_to_x

        with mock.patch("exporters.x_post.subprocess.run") as run:
            preview = post_to_x("  hello  ", media_path=str(self.image), dry_run=True)
            settings.x_username = None
            with self.assertRaises(XPostError):
                post_to_x("hello")
        run.assert_not_called()
        self.assertEqual(preview["status"], "dry_run")
        self.assertEqual((preview["chars"], preview["media_type"]), (5, "image/png"))

    def test_post_uploads_media_attaches_it_and_logs(self) -> None:
        """Media is uploaded first, its id attached to the post, and the accepted post logged."""
        from exporters.x_post import post_to_x

        run = self._xurl_ok('{"data": {"id": "m-1"}}', '{"data": {"id": "t-9"}}')
        with mock.patch("exporters.x_post.subprocess.run", run):
            result = post_to_x("-new episode", media_path=str(self.image), episode_slug="ep-one")

        upload_cmd, post_cmd = (call.args[0] for call in run.call_args_list)
        account = [str(self.fake_xurl), "--app", "test-app", "--username", "testshow"]
        self.assertEqual(upload_cmd, [*account, "media", "upload", str(self.image)])
        self.assertEqual(post_cmd, [*account, "post", "--media-id", "m-1", "--", "-new episode"])
        self.assertEqual(result["url"], "https://x.com/testshow/status/t-9")
        logged = json.loads(settings.x_post_log_path.read_text())
        self.assertEqual((logged["tweet_id"], logged["text"]), ("t-9", "-new episode"))
        # The metrics collector reads this log, so the episode credit must be recorded with the post.
        self.assertEqual(logged["episode_slug"], "ep-one")


class DevtoTests(unittest.TestCase):
    """Cross-posting blog posts to dev.to as drafts: pre-request guards, the payload, and duplicate checks.

    open_https is always mocked, so no test talks to dev.to.
    """

    CANONICAL = "https://example.com/blog/ep-one/"

    def setUp(self) -> None:
        """Point settings at temp dirs with cross-posting configured, and write one published blog post."""
        from exporters.blog_post import create_blog_post

        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (settings.website_path, settings.data_dir, settings.devto_api_key, settings.blog_url_template)
        settings.website_path, settings.data_dir = self.tmp / "site", self.tmp / "data"
        settings.devto_api_key, settings.blog_url_template = "test-key", "https://example.com/blog/{slug}/"
        create_blog_post(
            title="Episode One Notes",
            description="What we learned.",
            content_markdown="See [the last post](/blog/older/) and [docs](https://docs.example.com/x).\n\n![art](/media/a.png)",
            slug="ep-one",
        )

    def tearDown(self) -> None:
        """Restore settings and remove the temp dir."""
        settings.website_path, settings.data_dir, settings.devto_api_key, settings.blog_url_template = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _replies(*bodies: Any) -> mock.MagicMock:
        """Mock open_https returning each body in turn as a JSON response."""
        return mock.MagicMock(side_effect=[io.BytesIO(json.dumps(body).encode()) for body in bodies])

    def test_relative_links_point_at_the_site_and_full_urls_stay(self) -> None:
        """Site-relative links and images become absolute; links that are already full URLs are untouched."""
        from exporters.blog_post import absolutize_links

        markdown = "[a](/blog/older/) [b](https://docs.example.com/x) ![c](/media/a.png)"
        self.assertEqual(
            absolutize_links(markdown, self.CANONICAL),
            "[a](https://example.com/blog/older/) [b](https://docs.example.com/x) "
            "![c](https://example.com/media/a.png)",
        )

    def test_bad_input_and_missing_config_refused_before_any_request(self) -> None:
        """Invalid tags, a missing post, a site draft, or missing settings never reach dev.to."""
        from exporters.blog_post import create_blog_post
        from exporters.devto import DevtoError, crosspost_to_devto

        create_blog_post(title="WIP", description="d", content_markdown="body", slug="wip", draft=True)
        with mock.patch("exporters.devto.open_https") as request:
            for tags in (["a", "b", "c", "d", "e"], ["Machine Learning"]):
                with self.subTest(tags=tags), self.assertRaises(ValueError):
                    crosspost_to_devto("ep-one", tags=tags)
            with self.assertRaises(FileNotFoundError):
                crosspost_to_devto("missing-post")
            with self.assertRaises(ValueError):
                crosspost_to_devto("wip")
            settings.blog_url_template = None
            with self.assertRaises(ValueError):
                crosspost_to_devto("ep-one")
            settings.blog_url_template, settings.devto_api_key = "https://example.com/blog/{slug}/", None
            with self.assertRaises(DevtoError):
                crosspost_to_devto("ep-one", dry_run=True)
        request.assert_not_called()

    def test_dry_run_reports_without_calling_devto(self) -> None:
        """dry_run shows the title, canonical URL, and tags that would be sent."""
        from exporters.devto import crosspost_to_devto

        with mock.patch("exporters.devto.open_https") as request:
            preview = crosspost_to_devto("ep-one", tags=["robotics"], dry_run=True)
        request.assert_not_called()
        self.assertEqual(
            (preview["status"], preview["title"], preview["canonical_url"], preview["tags"]),
            ("dry_run", "Episode One Notes", self.CANONICAL, ["robotics"]),
        )

    def test_canonical_declared_in_frontmatter_wins(self) -> None:
        """A post first published elsewhere keeps that original as its canonical URL."""
        from exporters.blog_post import blog_post_path
        from exporters.devto import crosspost_to_devto

        path = blog_post_path("guest-post")
        path.write_text('---\ntitle: "Guest"\ncanonical: "https://partner.example.com/post"\n---\n\nBody\n')
        preview = crosspost_to_devto("guest-post", dry_run=True)
        self.assertEqual(preview["canonical_url"], "https://partner.example.com/post")

    def test_creates_unpublished_draft_with_canonical_url_and_logs_it(self) -> None:
        """The draft is never published, credits the site, drops the frontmatter, and is logged."""
        from exporters.devto import crosspost_to_devto

        request = self._replies([], {"id": 7, "url": "https://dev.to/someone/episode-one-notes-temp"})
        with mock.patch("exporters.devto.open_https", request):
            result = crosspost_to_devto("ep-one", tags=["robotics", "ai"])

        lookup, create = request.call_args_list
        self.assertTrue(lookup.args[0].startswith("https://dev.to/api/articles/me/all"))
        url, headers, data = create.args
        article = json.loads(data)["article"]
        self.assertEqual((url, headers["api-key"]), ("https://dev.to/api/articles", "test-key"))
        self.assertEqual((article["published"], article["canonical_url"]), (False, self.CANONICAL))
        self.assertEqual((article["title"], article["tags"]), ("Episode One Notes", ["robotics", "ai"]))
        self.assertTrue(article["body_markdown"].startswith("See [the last post](https://example.com/blog/older/)"))
        self.assertEqual((result["status"], result["article_id"]), ("draft_created", 7))
        logged = json.loads(settings.devto_post_log_path.read_text())
        self.assertEqual((logged["slug"], logged["article_id"]), ("ep-one", 7))

    def test_existing_draft_is_returned_instead_of_creating_another(self) -> None:
        """A logged draft answers without a request; an unlogged one (e.g. a timed-out POST) is found on dev.to."""
        from core.jsonl import append_record
        from exporters.devto import crosspost_to_devto

        remote = {"id": 3, "url": "https://dev.to/someone/remote", "canonical_url": self.CANONICAL}
        request = self._replies([remote])
        with mock.patch("exporters.devto.open_https", request):
            found_remotely = crosspost_to_devto("ep-one")
        self.assertEqual(request.call_count, 1)  # the lookup only, no POST
        self.assertEqual((found_remotely["status"], found_remotely["article_id"]), ("exists", 3))

        append_record(settings.devto_post_log_path, {"slug": "ep-one", "article_id": 9, "url": "https://dev.to/x"})
        with mock.patch("exporters.devto.open_https") as request:
            found_in_log = crosspost_to_devto("ep-one")
        request.assert_not_called()
        self.assertEqual((found_in_log["status"], found_in_log["article_id"]), ("exists", 9))

    def test_rejection_reports_devto_message_without_the_key(self) -> None:
        """dev.to's own explanation reaches the agent; the API key never appears in the error."""
        import urllib.error

        from exporters.devto import DevtoError, crosspost_to_devto

        rejection = urllib.error.HTTPError(
            "https://dev.to/api/articles/me/all", 401, "Unauthorized", None, io.BytesIO(b'{"error": "unauthorized"}')
        )
        with mock.patch("exporters.devto.open_https", side_effect=rejection), self.assertRaises(DevtoError) as caught:
            crosspost_to_devto("ep-one")
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertIn("unauthorized", str(caught.exception))
        self.assertNotIn("test-key", str(caught.exception))


class SubstackTests(unittest.TestCase):
    """Cross-posting blog posts to Substack as drafts: guards, attribution, duplicate checks, and the cookie file.

    python-substack's Api is always mocked, so no test signs in to Substack.
    """

    def setUp(self) -> None:
        """Point settings at temp dirs with Substack configured and a cookie file, and write one published post."""
        from exporters.blog_post import create_blog_post

        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (
            settings.website_path, settings.data_dir, settings.blog_url_template,
            settings.substack_publication_url, settings.substack_cookies_path,
        )
        settings.website_path, settings.data_dir = self.tmp / "site", self.tmp / "data"
        settings.blog_url_template = "https://example.com/blog/{slug}/"
        settings.substack_publication_url = "https://show.substack.com"
        settings.substack_cookies_path = self.tmp / "cookies.json"
        settings.substack_cookies_path.write_text('{"substack.sid": "s:abc"}')
        create_blog_post(
            title="Episode One Notes",
            description="What we learned.",
            content_markdown="See [the last post](/blog/older/).\n\n![art](/media/a.png)",
            slug="ep-one",
            tags=["Field Notes"],
        )

    def tearDown(self) -> None:
        """Restore settings and remove the temp dir."""
        (
            settings.website_path, settings.data_dir, settings.blog_url_template,
            settings.substack_publication_url, settings.substack_cookies_path,
        ) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_local_images_refused_and_unsupported_content_reported(self) -> None:
        """A non-https image never reaches the library; tables and raw HTML come back as warnings."""
        from exporters.substack_post import inspect_body

        with self.assertRaises(ValueError):
            inspect_body("![secret](notes/secret.png)")
        warnings = inspect_body("| a | b |\n|---|---|\n| 1 | 2 |\n\n<div>x</div>\n\n![ok](https://cdn.example.com/a.png)")
        self.assertEqual(len(warnings), 2)

    def test_missing_config_refused_and_dry_run_never_signs_in(self) -> None:
        """No publication URL or cookie file means no sign-in; dry_run previews without one."""
        from exporters.substack_post import SubstackError, crosspost_to_substack

        with mock.patch("exporters.substack_post.Api") as api:
            preview = crosspost_to_substack("ep-one", dry_run=True)
            settings.substack_cookies_path = self.tmp / "missing.json"
            with self.assertRaises(SubstackError):
                crosspost_to_substack("ep-one")
            settings.substack_publication_url = None
            with self.assertRaises(SubstackError):
                crosspost_to_substack("ep-one", dry_run=True)
        api.assert_not_called()
        self.assertEqual((preview["status"], preview["tags"], preview["warnings"]), ("dry_run", ["Field Notes"], []))

    def test_creates_unpublished_draft_with_attribution_and_logs_it(self) -> None:
        """The draft is never published, credits the original first, uses absolute links, and is logged."""
        from exporters.substack_post import crosspost_to_substack

        with mock.patch("exporters.substack_post.Api") as api:
            client = api.return_value
            client.get_drafts.return_value = []
            client.get_published_posts.return_value = {"posts": []}
            client.create_draft_from_markdown.return_value = {"draft": {"id": 42}}
            result = crosspost_to_substack("ep-one")

        self.assertEqual(api.call_args.kwargs["publication_url"], "https://show.substack.com")
        kwargs = client.create_draft_from_markdown.call_args.kwargs
        self.assertFalse(kwargs["publish"])
        self.assertEqual((kwargs["slug"], kwargs["tags"]), ("ep-one", ["Field Notes"]))
        self.assertTrue(kwargs["markdown"].startswith(
            "*Originally published at [example.com](https://example.com/blog/ep-one/).*\n\n"
            "See [the last post](https://example.com/blog/older/)."
        ))
        self.assertEqual((result["status"], result["edit_url"]), ("draft_created", "https://show.substack.com/publish/post/42"))
        logged = json.loads(settings.substack_post_log_path.read_text())
        self.assertEqual((logged["slug"], logged["draft_id"]), ("ep-one", 42))

    def test_existing_draft_is_returned_instead_of_creating_another(self) -> None:
        """A logged draft answers without signing in; one found on Substack (e.g. after a failed call) is reused."""
        from core.jsonl import append_record
        from exporters.substack_post import crosspost_to_substack

        with mock.patch("exporters.substack_post.Api") as api:
            api.return_value.get_drafts.return_value = [{"id": 7, "draft_title": "Episode One Notes"}]
            api.return_value.get_published_posts.return_value = {"posts": []}
            found_remotely = crosspost_to_substack("ep-one")
        api.return_value.create_draft_from_markdown.assert_not_called()
        self.assertEqual((found_remotely["status"], found_remotely["draft_id"]), ("exists", 7))

        append_record(settings.substack_post_log_path, {"slug": "ep-one", "draft_id": 9, "edit_url": "https://x"})
        with mock.patch("exporters.substack_post.Api") as api:
            found_in_log = crosspost_to_substack("ep-one")
        api.assert_not_called()
        self.assertEqual((found_in_log["status"], found_in_log["draft_id"]), ("exists", 9))

    def test_cookie_file_is_private_and_decoded(self) -> None:
        """The saved cookie is readable only by its owner and stored decoded under both session names."""
        from exporters.substack_post import save_session_cookie

        path = save_session_cookie(" s%3Aabc.def \n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"substack.sid": "s:abc.def", "connect.sid": "s:abc.def"})


if __name__ == "__main__":
    unittest.main()
