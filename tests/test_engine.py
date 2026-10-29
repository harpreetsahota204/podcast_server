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
        url, headers, data, _method = create.args
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

    def test_unpublish_only_touches_the_accounts_published_articles(self) -> None:
        """Another account's id is refused, a draft is reported as already unpublished, and dry_run sends no PUT."""
        from exporters.devto import DevtoError, unpublish_article

        mine = [{"id": 7, "title": "Live", "published": True}, {"id": 8, "title": "Draft", "published": False}]
        request = self._replies(mine, mine, mine)
        with mock.patch("exporters.devto.open_https", request):
            with self.assertRaises(DevtoError):
                unpublish_article(99)
            self.assertEqual(unpublish_article(8)["status"], "already_unpublished")
            self.assertEqual(unpublish_article(7, dry_run=True)["status"], "dry_run")
        self.assertEqual(request.call_count, 3)  # three article lookups, no PUT

    def test_unpublish_sends_published_false_and_logs_it(self) -> None:
        """The article goes back to draft with a PUT of published=false, and the change is logged."""
        from exporters.devto import unpublish_article

        request = self._replies([{"id": 7, "title": "Live", "published": True}], {"id": 7, "published": False})
        with mock.patch("exporters.devto.open_https", request):
            result = unpublish_article(7)

        url, _headers, data, method = request.call_args_list[1].args
        self.assertEqual((url, method), ("https://dev.to/api/articles/7", "PUT"))
        self.assertEqual(json.loads(data), {"article": {"published": False}})
        self.assertEqual((result["status"], result["published"]), ("unpublished", False))
        self.assertEqual(json.loads(settings.devto_unpublish_log_path.read_text())["article_id"], 7)


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

    def test_list_posts_shows_published_posts_and_drafts(self) -> None:
        """Published posts link to their public page; drafts link to the editor."""
        from exporters.substack_post import list_posts

        with mock.patch("exporters.substack_post.Api") as api:
            api.return_value.get_published_posts.return_value = {"posts": [{"id": 1, "title": "Live", "slug": "live"}]}
            api.return_value.get_drafts.return_value = [{"id": 2, "draft_title": "WIP"}]
            posts = list_posts()

        self.assertEqual(api.return_value.get_drafts.call_args.kwargs["filter"], "draft")
        self.assertEqual(
            [(p["post_id"], p["state"], p["url"]) for p in posts],
            [
                (1, "published", "https://show.substack.com/p/live"),
                (2, "draft", "https://show.substack.com/publish/post/2"),
            ],
        )

    def test_delete_reads_the_post_first_and_logs_it(self) -> None:
        """An unknown id is refused without deleting; dry_run deletes nothing; a real delete is logged."""
        from exporters.substack_post import SubstackError, delete_post

        with mock.patch("exporters.substack_post.Api") as api:
            client = api.return_value
            client.get_draft.side_effect = RuntimeError("404")
            with self.assertRaises(SubstackError):
                delete_post(404)
            client.get_draft.side_effect = None
            client.get_draft.return_value = {"id": 5, "title": "Old", "slug": "old", "is_published": True}
            preview = delete_post(5, dry_run=True)
            client.delete_draft.assert_not_called()
            result = delete_post(5)

        client.delete_draft.assert_called_once_with(5)
        self.assertEqual((preview["status"], result["status"], result["state"]), ("dry_run", "deleted", "published"))
        logged = json.loads(settings.substack_deletion_log_path.read_text())
        self.assertEqual((logged["post_id"], logged["title"]), (5, "Old"))

    def test_a_deleted_draft_no_longer_blocks_a_new_crosspost(self) -> None:
        """Once the engine deleted a logged draft, cross-posting that blog post creates a fresh one."""
        from core.jsonl import append_record
        from exporters.substack_post import crosspost_to_substack

        append_record(settings.substack_post_log_path, {"slug": "ep-one", "draft_id": 9, "edit_url": "https://x"})
        append_record(settings.substack_deletion_log_path, {"post_id": 9})
        with mock.patch("exporters.substack_post.Api") as api:
            api.return_value.get_drafts.return_value = []
            api.return_value.get_published_posts.return_value = {"posts": []}
            api.return_value.create_draft_from_markdown.return_value = {"draft": {"id": 10}}
            result = crosspost_to_substack("ep-one")
        self.assertEqual((result["status"], result["draft_id"]), ("draft_created", 10))


class YouTubeManagementTests(unittest.TestCase):
    """Listing and deleting the channel's videos. The YouTube client is always mocked."""

    def setUp(self) -> None:
        """Use a temp data dir with one tracked video, and a fake client whose channel is UC-mine."""
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = settings.data_dir
        settings.data_dir = self.tmp
        tracked = {"videos": [{"video_id": "abcdefghijk", "episode_slug": "ep-one"}]}
        settings.youtube_videos_file.write_text(json.dumps(tracked))
        self.youtube = mock.MagicMock()
        channel = {"id": "UC-mine", "contentDetails": {"relatedPlaylists": {"uploads": "UU-mine"}}}
        self.youtube.channels.return_value.list.return_value.execute.return_value = {"items": [channel]}
        patcher = mock.patch("exporters.youtube.get_authenticated_youtube_service", return_value=self.youtube)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        """Restore settings and remove the temp dir."""
        settings.data_dir = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _video(self, channel_id: str) -> None:
        """Make videos.list return one video owned by `channel_id`."""
        snippet = {"channelId": channel_id, "title": "Ep One", "description": "full description"}
        self.youtube.videos.return_value.list.return_value.execute.return_value = {"items": [{"snippet": snippet}]}

    def test_list_reads_the_uploads_playlist_with_episode_credit(self) -> None:
        """Videos come from the channel's uploads playlist (capped at 50), credited to their tracked episode."""
        from exporters.youtube import list_videos

        item = {"snippet": {"resourceId": {"videoId": "abcdefghijk"}, "title": "Ep One"}}
        playlist = self.youtube.playlistItems.return_value.list
        playlist.return_value.execute.return_value = {"items": [item], "nextPageToken": "p2"}
        page = list_videos(limit=500)

        kwargs = playlist.call_args.kwargs
        self.assertEqual((kwargs["playlistId"], kwargs["maxResults"]), ("UU-mine", 50))
        self.assertEqual((page["videos"][0]["episode_slug"], page["next_page_token"]), ("ep-one", "p2"))

    def test_delete_refuses_bad_ids_and_other_channels(self) -> None:
        """A malformed id never reaches YouTube; another channel's video is read but never deleted."""
        from exporters.youtube import delete_video

        with self.assertRaises(ValueError):
            delete_video("not an id")
        self._video("UC-someone-else")
        with self.assertRaises(ValueError):
            delete_video("abcdefghijk")
        self.youtube.videos.return_value.delete.assert_not_called()

    def test_delete_dry_run_then_delete_logs_the_video(self) -> None:
        """dry_run deletes nothing; a real delete removes the video and logs it with its description."""
        from exporters.youtube import delete_video

        self._video("UC-mine")
        self.assertEqual(delete_video("abcdefghijk", dry_run=True)["status"], "dry_run")
        self.youtube.videos.return_value.delete.assert_not_called()
        result = delete_video("abcdefghijk")

        self.youtube.videos.return_value.delete.assert_called_once_with(id="abcdefghijk")
        self.assertEqual((result["status"], result["episode_slug"]), ("deleted", "ep-one"))
        logged = json.loads(settings.youtube_deletion_log_path.read_text())
        self.assertEqual((logged["video_id"], logged["description"]), ("abcdefghijk", "full description"))


class YouTubeShortsTests(unittest.TestCase):
    """The expect_short check: measurement, rotation, and refusal before upload (ffprobe and YouTube mocked)."""

    def setUp(self) -> None:
        """Point settings at a temp data dir and a fake ffprobe, with one video file to upload."""
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (settings.data_dir, settings.ffprobe_path)
        settings.data_dir, settings.ffprobe_path = self.tmp, self.tmp / "ffprobe"
        settings.ffprobe_path.write_text("#!/bin/sh\n")
        self.video = self.tmp / "clip.mp4"
        self.video.write_bytes(b"\x00" * 16)

    def tearDown(self) -> None:
        """Restore settings and remove the temp dir."""
        settings.data_dir, settings.ffprobe_path = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _probe(width: int, height: int, seconds: float, rotation: int | None = None) -> mock.MagicMock:
        """Mock subprocess.run returning ffprobe JSON for one video stream."""
        stream: dict[str, Any] = {"width": width, "height": height}
        if rotation is not None:
            stream["side_data_list"] = [{"rotation": rotation}]
        out = json.dumps({"streams": [stream], "format": {"duration": str(seconds)}})
        return mock.MagicMock(return_value=subprocess.CompletedProcess([], 0, out, ""))

    def test_rotation_flag_makes_a_landscape_frame_portrait(self) -> None:
        """A phone video stored 1920x1080 with a quarter-turn flag is measured as the 1080x1920 viewers see."""
        from exporters.youtube import measure_video, short_problem

        with mock.patch("exporters.youtube.subprocess.run", self._probe(1920, 1080, 42.5, rotation=-90)):
            video = measure_video(self.video)
        self.assertEqual(video, {"width": 1080, "height": 1920, "duration_seconds": 42.5})
        self.assertIsNone(short_problem(video))
        both = short_problem({"width": 1920, "height": 1080, "duration_seconds": 200})
        self.assertIn("horizontal", both)
        self.assertIn("200s", both)

    def test_expect_short_refuses_before_uploading(self) -> None:
        """A horizontal or too-long file never reaches YouTube when a Short is expected."""
        from exporters.youtube import upload_video_to_youtube

        for width, height, seconds in ((1920, 1080, 30), (1080, 1920, 181)):
            with (
                self.subTest(size=(width, height), seconds=seconds),
                mock.patch("exporters.youtube.subprocess.run", self._probe(width, height, seconds)),
                mock.patch("exporters.youtube.get_authenticated_youtube_service") as service,
                self.assertRaises(ValueError),
            ):
                upload_video_to_youtube(str(self.video), "t", "d", expect_short=True)
            service.assert_not_called()

    def test_expect_short_uploads_and_notes_a_non_public_short(self) -> None:
        """A vertical 3-minute file uploads, reports its size, and warns that unlisted Shorts skip the feed."""
        from exporters.youtube import upload_video_to_youtube

        response = {"id": "abcdefghijk", "snippet": {"title": "t"}, "status": {"privacyStatus": "unlisted"}}
        youtube = mock.MagicMock()
        youtube.videos.return_value.insert.return_value.next_chunk.return_value = (None, response)
        with (
            mock.patch("exporters.youtube.subprocess.run", self._probe(1080, 1920, 180)),
            mock.patch("exporters.youtube.get_authenticated_youtube_service", return_value=youtube),
        ):
            result = upload_video_to_youtube(str(self.video), "t", "d", expect_short=True)
        self.assertEqual(result["short"], {"width": 1080, "height": 1920, "duration_seconds": 180.0})
        self.assertIn("Shorts feed", result["note"])


class InstagramTests(unittest.TestCase):
    """Posting to Instagram: pre-upload guards, JPEG staging, Meta's container flow, the post log, and the token file.

    open_https and the R2 client are always mocked, so no test talks to Instagram or R2.
    """

    TOKEN = "IGAA-secret-token"  # noqa: S105 - fake token; tests assert it never leaks into URLs, errors, or logs

    def setUp(self) -> None:
        """Point settings at a temp data dir and token file with posting on, and draw one 16:9 PNG to post."""
        from PIL import Image

        from exporters.instagram import _save_token

        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (settings.data_dir, settings.instagram_username, settings.instagram_token_path)
        settings.data_dir, settings.instagram_username = self.tmp / "data", "testshow"
        settings.instagram_token_path = self.tmp / "instagram_token.json"
        _save_token({"user_id": "17841", "username": "testshow", "access_token": self.TOKEN})
        self.image = self.tmp / "ep-one-final.png"
        Image.new("RGBA", (1920, 1080), (10, 20, 30, 255)).save(self.image)

    def tearDown(self) -> None:
        """Restore settings and remove the temp dir."""
        settings.data_dir, settings.instagram_username, settings.instagram_token_path = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _replies(*bodies: Any) -> mock.MagicMock:
        """Mock open_https returning each body in turn as a JSON response."""
        return mock.MagicMock(side_effect=[io.BytesIO(json.dumps(body).encode()) for body in bodies])

    @staticmethod
    def _r2(exists: bool = False) -> mock.MagicMock:
        """Mock R2StorageClient class whose instance is configured and serves keys from the test audio domain."""
        r2 = mock.MagicMock()
        client = r2.return_value
        client.is_configured.return_value = True
        client.object_exists.return_value = exists
        client.public_url.side_effect = lambda key: f"https://audio.example.com/{key}"
        return r2

    @staticmethod
    def _offline(request: Any = mock.DEFAULT, r2: Any = mock.DEFAULT) -> Any:
        """Patch Meta requests and the R2 client together; yields the mocks created for the defaults."""
        return mock.patch.multiple("exporters.instagram", open_https=request, R2StorageClient=r2)

    def _write_token(self, **changes: Any) -> None:
        """Rewrite the saved token file with some fields changed."""
        token = json.loads(settings.instagram_token_path.read_text())
        settings.instagram_token_path.write_text(json.dumps({**token, **changes}))

    def test_bad_input_and_missing_config_refused_before_any_upload(self) -> None:
        """Secret files, bad shapes, long captions, bad slugs, and missing setup never reach R2 or Instagram."""
        from PIL import Image

        from exporters.instagram import InstagramError, post_to_instagram

        secret = self.tmp / ".env"
        secret.write_text("TOKEN=abc")
        too_tall = self.tmp / "tall.png"
        Image.new("RGB", (400, 1000)).save(too_tall)
        refusals: list[tuple[dict[str, Any], type[Exception]]] = [
            ({"media_paths": [str(secret)]}, ValueError),
            ({"media_paths": [str(too_tall)]}, ValueError),
            ({"media_paths": [str(self.image), str(self.image)]}, ValueError),
            ({"media_paths": [str(self.image)], "caption": "x" * 2201}, ValueError),
            ({"media_paths": [str(self.image)], "caption": " ".join(f"#tag{i}" for i in range(31))}, ValueError),
            ({"media_paths": [str(self.image)], "episode_slug": "../not-a-slug"}, ValueError),
        ]
        with self._offline() as patched:
            for kwargs, error in refusals:
                with self.subTest(kwargs=kwargs), self.assertRaises(error):
                    post_to_instagram(**kwargs)
            settings.instagram_username = None
            with self.assertRaises(InstagramError):
                post_to_instagram([str(self.image)], dry_run=True)
        for untouched in patched.values():
            untouched.assert_not_called()

    def test_token_saved_for_another_account_is_refused(self) -> None:
        """The token decides where a post lands, so one saved for another account stops the call before any request."""
        from exporters.instagram import InstagramError, post_to_instagram

        self._write_token(username="someoneelse")
        with mock.patch("exporters.instagram.open_https") as request, self.assertRaises(InstagramError):
            post_to_instagram([str(self.image)], dry_run=True)
        request.assert_not_called()

    def test_dry_run_converts_and_reports_without_uploading(self) -> None:
        """dry_run shows the account and the JPEG's size (scaled to 1440 wide) but uploads and posts nothing."""
        from exporters.instagram import post_to_instagram

        with self._offline() as patched:
            preview = post_to_instagram([str(self.image)], caption="  New episode  ", dry_run=True)
        for untouched in patched.values():
            untouched.assert_not_called()
        self.assertEqual(
            (preview["status"], preview["username"], preview["width"], preview["height"], preview["caption_chars"]),
            ("dry_run", "testshow", 1440, 810, 11),
        )

    def test_post_stages_jpeg_publishes_container_and_logs(self) -> None:
        """The JPEG goes to R2 under a hash key, Meta gets its URL, the container is published, and the post logged."""
        from exporters.instagram import post_to_instagram

        request = self._replies(
            {"id": "c-1"},
            {"status_code": "FINISHED"},
            {"id": "m-9"},
            {"permalink": "https://www.instagram.com/p/abc/"},
        )
        r2 = self._r2()
        with self._offline(request, r2):
            result = post_to_instagram([str(self.image)], caption="New episode #ai", episode_slug="ep-one")

        key = r2.return_value.upload_file.call_args.args[1]
        self.assertRegex(key, r"^instagram/[0-9a-f]{24}\.jpg$")
        self.assertEqual(r2.return_value.upload_file.call_args.kwargs["content_type"], "image/jpeg")
        create, status, publish, permalink = request.call_args_list
        url, headers, data, _method = create.args
        self.assertEqual(url, "https://graph.facebook.com/v26.0/17841/media")
        self.assertEqual(headers["Authorization"], f"Bearer {self.TOKEN}")
        self.assertEqual(
            json.loads(data), {"image_url": f"https://audio.example.com/{key}", "caption": "New episode #ai"}
        )
        self.assertTrue(status.args[0].endswith("/c-1?fields=status_code,status"))
        self.assertEqual(json.loads(publish.args[2]), {"creation_id": "c-1"})
        # The token travels only in the Authorization header, never in a URL.
        self.assertTrue(all(self.TOKEN not in call.args[0] for call in request.call_args_list))
        self.assertEqual((result["status"], result["url"]), ("posted", "https://www.instagram.com/p/abc/"))
        logged = json.loads(settings.instagram_post_log_path.read_text())
        self.assertEqual((logged["media_id"], logged["episode_slug"]), ("m-9", "ep-one"))
        self.assertNotIn(self.TOKEN, settings.instagram_post_log_path.read_text())

    def test_same_image_and_caption_is_never_posted_twice(self) -> None:
        """A logged post of the same image and caption is returned without uploading or contacting Instagram."""
        from exporters.instagram import post_to_instagram

        published = ({"id": "c-1"}, {"status_code": "FINISHED"}, {"id": "m-9"}, {"permalink": "https://i/p/1"})
        with self._offline(self._replies(*published), self._r2()):
            post_to_instagram([str(self.image)], caption="Same")
        with self._offline() as patched:
            repeat = post_to_instagram([str(self.image)], caption="Same")
        for untouched in patched.values():
            untouched.assert_not_called()
        self.assertEqual((repeat["status"], repeat["media_id"]), ("exists", "m-9"))

        # Once the engine has deleted that post, the same image and caption can go out again.
        from core.jsonl import append_record

        append_record(settings.instagram_deletion_log_path, {"media_id": "m-9"})
        with self._offline(self._replies(*published), self._r2()):
            self.assertEqual(post_to_instagram([str(self.image)], caption="Same")["status"], "posted")

    def test_failed_processing_publishes_nothing(self) -> None:
        """A container Meta can't process is reported, and media_publish is never called."""
        from exporters.instagram import InstagramError, post_to_instagram

        request = self._replies({"id": "c-1"}, {"status_code": "ERROR", "status": "image could not be fetched"})
        with self._offline(request, self._r2()), self.assertRaises(InstagramError) as caught:
            post_to_instagram([str(self.image)])
        self.assertEqual(request.call_count, 2)
        self.assertIn("nothing was published", str(caught.exception))
        self.assertFalse(settings.instagram_post_log_path.exists())

    def test_rejection_reports_meta_message_without_the_token(self) -> None:
        """Meta's explanation reaches the agent, an invalid token says how to fix it, and the token never appears."""
        import urllib.error

        from exporters.instagram import InstagramError, post_to_instagram

        body = json.dumps({"error": {"message": "Error validating access token", "code": 190}}).encode()
        rejection = urllib.error.HTTPError("https://graph.facebook.com/x", 400, "Bad Request", None, io.BytesIO(body))
        request = mock.MagicMock(side_effect=rejection)
        with self._offline(request, self._r2()), self.assertRaises(InstagramError) as caught:
            post_to_instagram([str(self.image)])
        message = str(caught.exception)
        self.assertIn("Error validating access token", message)
        self.assertIn("exporters.instagram login", message)
        self.assertNotIn(self.TOKEN, message)

    def test_list_posts_pages_by_cursor_and_trims_captions(self) -> None:
        """Pages are capped at 100, the cursor carries over, captions are trimmed, and the last page has no cursor."""
        from exporters.instagram import CAPTION_PREVIEW_CHARS, list_posts

        post = {"id": "1", "media_type": "IMAGE", "media_product_type": "FEED", "caption": "x" * 500}
        request = self._replies(
            {"data": [post], "paging": {"cursors": {"after": "c2"}, "next": "https://graph.facebook.com/next"}},
            {"data": [], "paging": {"cursors": {"after": "c3"}}},
        )
        with mock.patch("exporters.instagram.open_https", request):
            first = list_posts(limit=500)
            last = list_posts(after=first["next_cursor"])

        first_url, second_url = (call.args[0] for call in request.call_args_list)
        self.assertIn("/17841/media?", first_url)
        self.assertIn("limit=100", first_url)
        self.assertIn("after=c2", second_url)
        listed = first["posts"][0]
        self.assertEqual((listed["type"], len(listed["caption"])), ("feed/image", CAPTION_PREVIEW_CHARS))
        self.assertEqual((first["next_cursor"], last["next_cursor"]), ("c2", None))

    def test_delete_refuses_bad_ids_and_other_accounts_before_deleting(self) -> None:
        """A non-numeric id never reaches Meta, and another account's post is read but never deleted."""
        from exporters.instagram import InstagramError, delete_post

        with mock.patch("exporters.instagram.open_https") as request, self.assertRaises(ValueError):
            delete_post("me/accounts")
        request.assert_not_called()

        request = self._replies({"id": "9", "username": "someoneelse"})
        with mock.patch("exporters.instagram.open_https", request), self.assertRaises(InstagramError):
            delete_post("9")
        self.assertEqual(request.call_count, 1)

    def test_delete_dry_run_reads_only_and_delete_logs_the_full_post(self) -> None:
        """dry_run reads the post and stops; a real delete sends DELETE and logs the post with its full caption."""
        from exporters.instagram import delete_post

        post = {"id": "9", "username": "TestShow", "media_type": "VIDEO", "permalink": "https://i/p/9"}
        post["caption"] = "c" * 300
        request = self._replies(post, post, {"success": True, "deleted_id": "9"})
        with mock.patch("exporters.instagram.open_https", request):
            preview = delete_post("9", dry_run=True)
            result = delete_post("9")

        self.assertEqual((preview["status"], result["status"]), ("dry_run", "deleted"))
        self.assertEqual(request.call_count, 3)
        url, _headers, _data, method = request.call_args_list[2].args
        self.assertEqual((url, method), ("https://graph.facebook.com/v26.0/9", "DELETE"))
        logged = json.loads(settings.instagram_deletion_log_path.read_text())
        self.assertEqual((logged["media_id"], logged["url"], len(logged["caption"])), ("9", "https://i/p/9", 300))

    def test_transient_delete_error_says_retry_is_safe(self) -> None:
        """Meta's intermittent delete failure shows its user message and that nothing was deleted."""
        import urllib.error

        from exporters.instagram import InstagramError, delete_post

        body = json.dumps({"error": {"message": "Fatal", "error_subcode": 2207085, "error_user_msg": "Internal error"}})
        failure = urllib.error.HTTPError("https://graph.facebook.com/9", 500, "Error", None, io.BytesIO(body.encode()))
        request = mock.MagicMock(side_effect=[io.BytesIO(b'{"id": "9", "username": "testshow"}'), failure])
        with mock.patch("exporters.instagram.open_https", request), self.assertRaises(InstagramError) as caught:
            delete_post("9")
        self.assertIn("Internal error", str(caught.exception))
        self.assertIn("retrying is safe", str(caught.exception))
        self.assertFalse(settings.instagram_deletion_log_path.exists())

    def _login(
        self,
        pasted: str,
        shared: list[dict[str, str]] | None = None,
        linked: dict[str, str] | None = None,
        pages: list[dict[str, Any]] | None = None,
    ) -> tuple[int, mock.MagicMock, str]:
        """Run `login` with a pasted token; return exit code, request mock, and stderr.

        `shared` is the account list a Unified login token gets (None: that lookup fails, as for other
        tokens); `linked` is the Instagram account linked to a Page token's Page; `pages` is what a user
        token's /me/accounts returns (None: that lookup fails, as for other tokens).
        """
        import urllib.error

        from exporters.instagram import login

        settings.instagram_token_path.unlink(missing_ok=True)
        unsupported = urllib.error.HTTPError(
            "https://graph.facebook.com/x", 400, "Bad Request", None,
            io.BytesIO(b'{"error": {"message": "Tried accessing nonexisting field", "code": 100}}'),
        )
        replies: list[Any] = [
            io.BytesIO(b'{"id": "me-1"}'),
            unsupported if shared is None else io.BytesIO(json.dumps({"data": shared}).encode()),
            io.BytesIO(json.dumps({"instagram_business_account": linked} if linked else {"id": "me-1"}).encode()),
            unsupported if pages is None else io.BytesIO(json.dumps({"data": pages}).encode()),
        ]
        request = mock.MagicMock(side_effect=replies)
        with (
            mock.patch("exporters.instagram.getpass", return_value=pasted),
            mock.patch("exporters.instagram.open_https", request),
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            return login(), request, stderr.getvalue()

    def test_login_saves_the_configured_account_privately(self) -> None:
        """Paste noise is stripped, the shared accounts are listed, and INSTAGRAM_USERNAME's account is saved 0600."""
        shared = [{"id": "1", "username": "other"}, {"id": "17841", "username": "TestShow"}]
        code, request, _ = self._login("\x1b[200~EAA abc\r\ndef\x1b[201~\n", shared)

        self.assertEqual(code, 0)
        url, headers, _data, _method = request.call_args_list[1].args
        self.assertEqual(url, "https://graph.facebook.com/v26.0/me/assigned_instagram_accounts?fields=id,username")
        self.assertEqual(headers["Authorization"], "Bearer EAAabcdef")
        saved = json.loads(settings.instagram_token_path.read_text())
        stored = [saved[key] for key in ("access_token", "user_id", "username")]
        self.assertEqual(stored, ["EAAabcdef", "17841", "TestShow"])
        self.assertEqual(settings.instagram_token_path.stat().st_mode & 0o777, 0o600)

    def test_login_finds_the_account_linked_to_a_page_token(self) -> None:
        """A Page token's account list lookup fails, but the Page's linked Instagram account is found and saved."""
        code, request, _ = self._login("EAA-page", linked={"id": "17841", "username": "testshow"})

        self.assertEqual(code, 0)
        self.assertEqual(
            request.call_args_list[2].args[0],
            "https://graph.facebook.com/v26.0/me?fields=instagram_business_account%7Bid%2Cusername%7D",
        )
        saved = json.loads(settings.instagram_token_path.read_text())
        self.assertEqual([saved[k] for k in ("user_id", "username", "access_token")], ["17841", "testshow", "EAA-page"])

    def test_login_swaps_a_user_token_for_the_page_token(self) -> None:
        """A user token is never stored: the token saved is the Page token of the Page linked to the account."""
        account = {"id": "17841", "username": "testshow"}
        pages = [
            {"id": "p-1", "access_token": "EAA-other-page"},
            {"id": "p-2", "access_token": "EAA-show-page", "instagram_business_account": account},
        ]
        code, request, _ = self._login("EAA-user", pages=pages)

        self.assertEqual(code, 0)
        self.assertEqual(
            request.call_args_list[3].args[0],
            "https://graph.facebook.com/v26.0/me/accounts"
            "?fields=access_token,instagram_business_account%7Bid%2Cusername%7D",
        )
        saved_text = settings.instagram_token_path.read_text()
        self.assertEqual(json.loads(saved_text)["access_token"], "EAA-show-page")
        self.assertNotIn("EAA-user", saved_text)

    def test_login_never_guesses_the_account(self) -> None:
        """No match for INSTAGRAM_USERNAME, no reachable account, or several with none configured: nothing is saved."""
        two = [{"id": "1", "username": "a"}, {"id": "2", "username": "b"}]
        cases = [("testshow", two), ("testshow", []), (None, two), ("testshow", [{"id": "17841"}]), ("testshow", None)]
        for username, shared in cases:
            with self.subTest(username=username, shared=shared):
                settings.instagram_username = username
                code, _, stderr = self._login("EAA-token", shared)
                self.assertEqual(code, 1)
                self.assertIn("Nothing was saved", stderr)
                self.assertFalse(settings.instagram_token_path.exists())


if __name__ == "__main__":
    unittest.main()
