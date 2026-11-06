"""Tests for Hashnode cross-posting and the pieces every cross-post shares (FAQ section, footer).

Covers exporters.hashnode (configuration refusals, the CLI transport, tags, dates, the
copy's fields, duplicate checks, drafts, removal), the Hashnode metrics collector, the
blog FAQ frontmatter and section, and the cross-post footer on dev.to and Hashnode copies.
The Hashnode CLI is always mocked, so no test talks to Hashnode.

Run from the repo root:
    uv run python -m unittest discover -s tests -v
"""

# Settings are built when core.config is first imported, so the fake show's
# environment (see fake_show.py) must be set before the imports below.
import fake_show  # noqa: F401 - must precede every engine import

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from core.config import settings
from exporters import hashnode
from exporters.blog_post import create_blog_post, crosspost_source, validate_faq, with_footer

CANONICAL = "https://example.com/blog/tactile/"
FAQ = [{"question": "What is shear sensing?", "answer": "Measuring sideways force at the fingertip."}]


def completed(stdout: Any = None, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    """A finished CLI run whose stdout is `stdout` as JSON."""
    return subprocess.CompletedProcess([], returncode, json.dumps(stdout) if stdout is not None else "", stderr)


def posts_page(*posts: dict[str, Any]) -> dict[str, Any]:
    """The `data` of POSTS_QUERY with these posts on a single page."""
    edges = [{"node": post} for post in posts]
    return {"publication": {"id": "pub1", "posts": {"edges": edges, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}


class HashnodeTestCase(unittest.TestCase):
    """Temp website and data dir, Hashnode configured with a fake CLI binary, and one live blog post with an FAQ."""

    def setUp(self) -> None:
        """Point settings at temp dirs and write the 'tactile' post."""
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (
            settings.website_path, settings.data_dir, settings.blog_url_template,
            settings.hashnode_publication_host, settings.hashnode_cli_path,
        )
        settings.website_path, settings.data_dir = self.tmp / "site", self.tmp / "data"
        settings.blog_url_template = "https://example.com/blog/{slug}/"
        settings.hashnode_publication_host = "blog.example.com"
        settings.hashnode_cli_path = self.tmp / "hashnode"
        settings.hashnode_cli_path.write_text("#!/bin/sh\n")
        create_blog_post(
            title="Tactile Sensing", description="Why robot hands need touch.", slug="tactile",
            content_markdown="See [the episode](/podcast/ep-one/).", tags=["Hardware", "Robotics"],
            discovery_override="fixture for Hashnode tests", faq=FAQ,
        )

    def tearDown(self) -> None:
        """Restore settings and remove the temp dir."""
        (settings.website_path, settings.data_dir, settings.blog_url_template,
         settings.hashnode_publication_host, settings.hashnode_cli_path) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_footer(self, text: str = "Originally published at {original_url}.") -> None:
        """Save the show's cross-post footer template."""
        settings.crosspost_footer_path.parent.mkdir(parents=True, exist_ok=True)
        settings.crosspost_footer_path.write_text(text, encoding="utf-8")

    @staticmethod
    def cli(*replies: subprocess.CompletedProcess[str]) -> mock._patch:
        """Patch the CLI so each call returns the next reply."""
        return mock.patch("exporters.hashnode.subprocess.run", side_effect=list(replies))

    @staticmethod
    def sent(run: mock.MagicMock, call: int = -1) -> tuple[str, dict[str, Any]]:
        """The GraphQL query and variables of one CLI call."""
        args = run.call_args_list[call].args[0]
        return args[args.index("gql") + 1], json.loads(args[args.index("--vars") + 1])


class ConfigAndTransportTests(HashnodeTestCase):
    """What's refused before the CLI runs, and how CLI output and errors come back."""

    def test_missing_host_or_cli_refused_before_running(self) -> None:
        """No publication host, or no CLI binary, fails with the setting to fix and never runs anything."""
        with self.cli() as run:
            settings.hashnode_publication_host = None
            with self.assertRaisesRegex(hashnode.HashnodeError, "HASHNODE_PUBLICATION_HOST"):
                hashnode.gql("query { me { username } }")
            settings.hashnode_publication_host = "blog.example.com"
            settings.hashnode_cli_path = self.tmp / "missing"
            with self.assertRaisesRegex(hashnode.HashnodeError, "HASHNODE_CLI_PATH"):
                hashnode.gql("query { me { username } }")
        run.assert_not_called()

    def test_cli_error_message_is_passed_on(self) -> None:
        """Hashnode's own text (e.g. the Pro-plan refusal) reaches the agent without the CLI's 'error:' prefix."""
        message = "Publication does not have an active Pro plan."
        with self.cli(completed(returncode=1, stderr=f"error: {message}\n")), self.assertRaises(hashnode.HashnodeError) as caught:
            hashnode.whoami()
        self.assertEqual(str(caught.exception), f"Hashnode: {message}")

    def test_oversized_request_refused_before_running(self) -> None:
        """A request over Hashnode's 100 KB limit is refused locally."""
        with self.cli() as run, self.assertRaisesRegex(hashnode.HashnodeError, "100 KB"):
            hashnode.gql("mutation", {"input": {"contentMarkdown": "x" * hashnode.MAX_REQUEST_BYTES}})
        run.assert_not_called()


class FieldTests(HashnodeTestCase):
    """Tags and dates for a copy."""

    def test_tags_keep_site_names_and_validate_extras(self) -> None:
        """Site tags become slugs named as on the site; extra slugs are checked; more than 15 is refused."""
        tags = hashnode.tag_inputs(["Evaluation & Safety", "AI"], ["machine-learning", "ai"])
        self.assertEqual(tags, [
            {"slug": "evaluation-safety", "name": "Evaluation & Safety"},
            {"slug": "ai", "name": "AI"},
            {"slug": "machine-learning", "name": "machine-learning"},
        ])
        with self.assertRaises(ValueError):
            hashnode.tag_inputs([], ["Machine Learning"])
        with self.assertRaises(ValueError):
            hashnode.tag_inputs([f"tag {i}" for i in range(16)])

    def test_published_at_is_utc_and_never_in_the_future(self) -> None:
        """A date-only pubDate is midnight UTC, an offset is converted, and a future date is refused."""
        self.assertEqual(hashnode.published_at_iso(None, date(2026, 6, 26)), "2026-06-26T00:00:00+00:00")
        self.assertEqual(hashnode.published_at_iso("2026-06-26T19:38:02-05:00", None), "2026-06-27T00:38:02+00:00")
        self.assertIsNone(hashnode.published_at_iso(None, None))
        with self.assertRaises(ValueError):
            hashnode.published_at_iso((datetime.now(UTC) + timedelta(days=1)).isoformat(), None)
        with self.assertRaises(ValueError):
            hashnode.published_at_iso("last tuesday", None)


class CrosspostTests(HashnodeTestCase):
    """The copy Hashnode receives, duplicate checks, and drafts."""

    def test_dry_run_checks_everything_without_calling_hashnode(self) -> None:
        """A dry run reports the copy and never runs the CLI."""
        with self.cli() as run:
            result = hashnode.crosspost_to_hashnode("tactile", tags=["robotics"], dry_run=True, publish=True)
        run.assert_not_called()
        self.assertEqual((result["status"], result["canonical_url"]), ("dry_run", CANONICAL))
        self.assertEqual(result["tags"], ["hardware", "robotics"])

    def test_publish_sends_canonical_seo_fields_and_logs(self) -> None:
        """A live copy keeps the site canonical, reuses the post's SEO fields and date, and is logged."""
        self.write_footer()
        publication = {"publication": {"id": "pub1", "title": "T", "url": "https://blog.example.com", "aeoSettings": {}}}
        created = {"publishPost": {"post": {"id": "p1", "slug": "tactile", "url": "https://blog.example.com/tactile"}}}
        with self.cli(completed(posts_page()), completed(publication), completed(created)) as run:
            result = hashnode.crosspost_to_hashnode("tactile", publish=True, published_at="2026-06-26")
        query, variables = self.sent(run)
        sent = variables["input"]
        self.assertIn("publishPost", query)
        self.assertEqual(result["status"], "published")
        self.assertEqual(
            {k: sent[k] for k in ("publicationId", "originalArticleURL", "slug", "metaTitle", "metaDescription", "enableToc")},
            {"publicationId": "pub1", "originalArticleURL": CANONICAL, "slug": "tactile", "metaTitle": "Tactile Sensing",
             "metaDescription": "Why robot hands need touch.", "enableToc": True},
        )
        self.assertEqual(sent["publishedAt"], "2026-06-26T00:00:00+00:00")
        # Links work off-site, the FAQ is a section, and the footer points at the original.
        self.assertIn("(https://example.com/podcast/ep-one/)", sent["contentMarkdown"])
        self.assertIn("### What is shear sensing?", sent["contentMarkdown"])
        self.assertTrue(sent["contentMarkdown"].endswith(f"Originally published at {CANONICAL}."))
        logged = [json.loads(line) for line in settings.hashnode_post_log_path.read_text().splitlines()]
        self.assertEqual((logged[0]["post_id"], logged[0]["slug"]), ("p1", "tactile"))

    def test_existing_copy_is_returned_instead_of_creating_another(self) -> None:
        """A logged copy answers without any request; one found on the publication by canonical URL is returned too."""
        settings.hashnode_post_log_path.parent.mkdir(parents=True)
        settings.hashnode_post_log_path.write_text(json.dumps({"slug": "tactile", "post_id": "p1", "url": "u"}) + "\n")
        with self.cli() as run:
            self.assertEqual(hashnode.crosspost_to_hashnode("tactile", publish=True)["status"], "exists")
        run.assert_not_called()
        settings.hashnode_post_log_path.unlink()
        found = {"id": "p2", "slug": "other-slug", "url": "u2", "canonicalUrl": CANONICAL}
        with self.cli(completed(posts_page(found))) as run:
            result = hashnode.crosspost_to_hashnode("tactile", publish=True)
        self.assertEqual((result["status"], result["post_id"], run.call_count), ("exists", "p2", 1))

    def test_draft_nests_seo_and_table_of_contents(self) -> None:
        """Without publish, a draft is created with the draft input's nested metaTags and settings."""
        publication = {"publication": {"id": "pub1", "title": "T", "url": "u", "aeoSettings": {}}}
        created = {"createDraft": {"draft": {"id": "d1", "slug": "tactile"}}}
        with self.cli(completed(posts_page()), completed(publication), completed(created)) as run:
            result = hashnode.crosspost_to_hashnode("tactile", cover_image="https://cdn.example.com/c.png")
        query, variables = self.sent(run)
        sent = variables["input"]
        self.assertIn("createDraft", query)
        self.assertEqual((result["status"], result["draft_id"]), ("draft_created", "d1"))
        self.assertEqual(sent["metaTags"], {"title": "Tactile Sensing", "description": "Why robot hands need touch.",
                                            "image": "https://cdn.example.com/c.png"})
        self.assertEqual(sent["settings"], {"enableTableOfContent": True})
        self.assertEqual(sent["coverImageOptions"], {"coverImageURL": "https://cdn.example.com/c.png"})
        self.assertNotIn("enableToc", sent)


class DeleteAndMetricsTests(HashnodeTestCase):
    """Removal only of the publication's own posts, and views only for logged copies."""

    POST = {"id": "p1", "title": "Tactile Sensing", "slug": "tactile", "url": "u", "canonicalUrl": CANONICAL,
            "publishedAt": "2026-06-26T00:00:00Z", "views": 42}

    def test_delete_refuses_foreign_ids_then_removes_and_logs(self) -> None:
        """An id outside the publication is refused; a dry run removes nothing; a removal is logged."""
        with self.cli(completed(posts_page(self.POST))), self.assertRaises(hashnode.HashnodeError):
            hashnode.delete_post("not-ours")
        with self.cli(completed(posts_page(self.POST))) as run:
            self.assertEqual(hashnode.delete_post("p1", dry_run=True)["status"], "dry_run")
        self.assertEqual(run.call_count, 1)
        with self.cli(completed(posts_page(self.POST)), completed({"removePost": {"post": {"id": "p1"}}})) as run:
            self.assertEqual(hashnode.delete_post("p1")["status"], "removed")
        self.assertIn("removePost", self.sent(run)[0])
        self.assertIn('"post_id": "p1"', settings.hashnode_deletion_log_path.read_text())

    def test_update_refuses_foreign_ids_then_sends_the_fields(self) -> None:
        """An id outside the publication changes nothing; one inside gets exactly the given fields."""
        with self.cli(completed(posts_page(self.POST))) as run, self.assertRaises(hashnode.HashnodeError):
            hashnode.update_copy("not-ours", {"contentMarkdown": "x"})
        self.assertEqual(run.call_count, 1)
        updated = {"updatePost": {"post": {"id": "p1", "url": "u"}}}
        with self.cli(completed(posts_page(self.POST)), completed(updated)) as run:
            self.assertEqual(hashnode.update_copy("p1", {"contentMarkdown": "New."}), {"id": "p1", "url": "u"})
        query, variables = self.sent(run)
        self.assertIn("updatePost", query)
        self.assertEqual(variables["input"], {"contentMarkdown": "New.", "id": "p1"})

    def test_collector_measures_only_logged_posts(self) -> None:
        """Views are snapshotted for logged copies, credited to the matching episode; other posts are ignored."""
        from metrics.collectors.base import CollectionContext
        from metrics.collectors.hashnode import HashnodeCollector

        settings.hashnode_post_log_path.parent.mkdir(parents=True)
        settings.hashnode_post_log_path.write_text(json.dumps({"slug": "tactile", "post_id": "p1"}) + "\n")
        other = {**self.POST, "id": "p9", "slug": "written-on-hashnode"}
        ctx = mock.Mock(spec=CollectionContext, today=date(2026, 10, 30), episodes={"tactile": {"slug": "tactile"}})
        with self.cli(completed(posts_page(self.POST, other))):
            result = HashnodeCollector().collect(ctx)
        self.assertEqual([(o.entity_id, o.value, o.episode_slug) for o in result.observations], [("p1", 42, "tactile")])


class SharedCrosspostTests(HashnodeTestCase):
    """The FAQ frontmatter and section, and the footer, which every cross-post shares."""

    def test_faq_is_saved_in_frontmatter_and_becomes_a_section(self) -> None:
        """create_blog_post saves the FAQ; crosspost_source appends it with a heading per question; bad items are refused."""
        self.assertIn('faq: [{"question": "What is shear sensing?"', (settings.site_blog_dir / "tactile.md").read_text())
        body = crosspost_source("tactile").body_markdown
        self.assertIn("## Frequently asked questions\n\n### What is shear sensing?\n\nMeasuring sideways force", body)
        for bad in ([{"question": "Q"}], "not a list", [{"question": "Q", "answer": "A"}] * 11):
            with self.assertRaises(ValueError):
                validate_faq(bad)

    def test_preview_image_comes_from_frontmatter_or_template_and_must_be_live(self) -> None:
        """A post's own https `image` wins over the template; a broken or non-image URL is never used as a cover."""
        from exporters.blog_post import reachable_image

        self.assertIsNone(crosspost_source("tactile").image_url)
        with mock.patch.object(settings, "blog_image_url_template", "https://example.com/og/blog/{slug}.png"):
            self.assertEqual(crosspost_source("tactile").image_url, "https://example.com/og/blog/tactile.png")
            path = settings.site_blog_dir / "tactile.md"
            path.write_text(path.read_text().replace("draft: false", 'draft: false\nimage: "https://cdn.example.com/own.png"'))
            self.assertEqual(crosspost_source("tactile").image_url, "https://cdn.example.com/own.png")

        def head(content_type: str = "image/png", status: int = 200) -> mock.MagicMock:
            """A HEAD response with this status and content type."""
            response = mock.MagicMock(status=status, headers={"Content-Type": content_type})
            response.__enter__.return_value = response
            return response

        url = "https://example.com/og/blog/tactile.png"
        with mock.patch("exporters.blog_post.open_https", return_value=head()):
            self.assertEqual(reachable_image(url), url)
        with mock.patch("exporters.blog_post.open_https", return_value=head("text/html")):
            self.assertIsNone(reachable_image(url))
        with mock.patch("exporters.blog_post.open_https", side_effect=OSError("404")):
            self.assertIsNone(reachable_image(url))

    def test_live_preview_image_becomes_the_cover_on_devto_and_hashnode(self) -> None:
        """With the image live, dev.to gets it as main_image and Hashnode as coverImage and ogImage."""
        from exporters import devto

        url = "https://example.com/og/blog/tactile.png"
        with (
            mock.patch.object(settings, "blog_image_url_template", "https://example.com/og/blog/{slug}.png"),
            mock.patch("exporters.devto.reachable_image", return_value=url),
            mock.patch("exporters.hashnode.reachable_image", return_value=url),
            mock.patch.object(settings, "devto_api_key", "test-key"),
        ):
            self.assertEqual(devto.crosspost_to_devto("tactile", dry_run=True)["cover_image"], url)
            publication = {"publication": {"id": "pub1", "title": "T", "url": "u", "aeoSettings": {}}}
            created = {"publishPost": {"post": {"id": "p1", "slug": "tactile", "url": "u"}}}
            with self.cli(completed(posts_page()), completed(publication), completed(created)) as run:
                hashnode.crosspost_to_hashnode("tactile", publish=True)
        sent = self.sent(run)[1]["input"]
        self.assertEqual((sent["coverImage"], sent["ogImage"]), (url, url))

    def test_footer_fills_in_the_original_and_reaches_devto(self) -> None:
        """No template means no footer; with one, dev.to copies end with it and the original URL."""
        self.assertEqual(with_footer("Body.", CANONICAL), "Body.")
        self.write_footer()
        from exporters import devto

        with mock.patch.object(settings, "devto_api_key", "test-key"):
            result = devto.crosspost_to_devto("tactile", dry_run=True)
        self.assertEqual(result["body_chars"], len(with_footer(crosspost_source("tactile").body_markdown, CANONICAL)))
        self.assertTrue(with_footer("Body.", CANONICAL).endswith(f"Originally published at {CANONICAL}."))


if __name__ == "__main__":
    unittest.main()
