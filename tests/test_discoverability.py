"""Tests for what happens to a page's findability around a deploy: the site's blog rules and IndexNow pings.

Covers core.site_rules (tags, internal links, call to action, the evidence warning),
where those rules run (writing a post, deploying one written by hand), core.indexnow
(which pages a deploy produces, waiting until they're live, the submission and its
log), the deploy hook that starts it, and the doctor's checks for both. Network calls
and the detached process are always mocked.

Run from the repo root:
    uv run python -m unittest discover -s tests -v
"""

# Settings are built when core.config is first imported, so the fake show's
# environment (see fake_show.py) must be set before the imports below.
import fake_show  # noqa: F401 - must precede every engine import

import asyncio
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from fastmcp import Client
from fastmcp.exceptions import ToolError

from core import doctor, indexnow
from core.config import settings
from core.discovery import make_brief, save_brief
from core.site_rules import BlogRules, check_blog_rules, has_evidence, load_blog_rules, site_link_paths
from exporters.blog_post import check_existing_blog_post, create_blog_post
from mcp_server.server import mcp

# Rules a typical site would set: its taxonomy, one related link, and a services call to action.
RULES = {
    "blog": {
        "allowed_tags": ["Engineering", "Research"],
        "min_internal_links": 1,
        "internal_link_prefixes": ["/blog/", "/podcast/"],
        "cta_link_prefixes": ["/services/"],
        "warn_without_evidence": True,
    }
}
# The same rules plus the FAQ requirement, kept separate so tests of the other rules don't need FAQ items.
FAQ_RULES = {"blog": {**RULES["blog"], "min_faq_items": 3}}
# A body that meets every rule above.
GOOD_BODY = (
    "Fingertip sensors report shear at 100 Hz.\n\n"
    "Related: [the teleoperation episode](/podcast/dexterous-teleop/).\n\n"
    "Need help? See [Hacker-in-Residence](https://example.com/services/hacker-in-residence/)."
)
GOOD_DESCRIPTION = "Why tactile sensing matters for robot hands, and what shear data adds over cameras alone."


class TempShowTestCase(unittest.TestCase):
    """Temp data dir and website repo, with a blog URL template and a discovery brief for 'tactile'."""

    def setUp(self) -> None:
        """Point settings at temp dirs and stage a brief so discovery checks pass for the sample post."""
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (settings.data_dir, settings.website_path, settings.blog_url_template, settings.indexnow_key)
        settings.data_dir, settings.website_path = self.tmp / "data", self.tmp / "site"
        settings.blog_url_template = "https://example.com/blog/{slug}/"
        settings.indexnow_key = None
        save_brief(make_brief(
            "tactile", "tactile sensing", ["shear force", "robot hands", "incipient slip"],
            "Robotics engineers", "Why do robot hands need touch?", ["GelSight"],
        ))

    def tearDown(self) -> None:
        """Restore settings and delete temp files."""
        settings.data_dir, settings.website_path, settings.blog_url_template, settings.indexnow_key = self._saved
        shutil.rmtree(self.tmp)

    def write_rules(self, rules: dict[str, Any]) -> None:
        """Save a site rules file for the show."""
        settings.site_rules_path.parent.mkdir(parents=True, exist_ok=True)
        settings.site_rules_path.write_text(json.dumps(rules), encoding="utf-8")

    def write_post(self, body: str, tags: list[str] | None = None, draft: bool = False, **kwargs: Any) -> Path:
        """Write the sample 'tactile' post through the guarded path; extra kwargs go to create_blog_post."""
        return create_blog_post(
            title="Tactile Sensing for Robot Hands",
            description=GOOD_DESCRIPTION,
            content_markdown=body,
            slug="tactile",
            tags=tags or ["Research"],
            draft=draft,
            **kwargs,
        )


class SiteRulesTests(TempShowTestCase):
    """The rules themselves and where they're enforced."""

    def test_no_rules_file_checks_nothing(self) -> None:
        """A show without a rules file only gets the discovery checks it had before."""
        self.assertIsNone(load_blog_rules())
        self.assertTrue(self.write_post("No links at all.", tags=["Anything"]).exists())

    def test_good_post_passes_and_bad_post_lists_every_problem(self) -> None:
        """A post meeting every rule writes; one missing all of them is refused with each reason, before writing."""
        self.write_rules(RULES)
        self.assertTrue(self.write_post(GOOD_BODY).exists())
        settings.site_blog_dir.joinpath("tactile.md").unlink()
        with self.assertRaises(ValueError) as refused:
            self.write_post("Just prose.", tags=["Cooking"])
        message = str(refused.exception)
        for expected in ("taxonomy", "related page", "call-to-action"):
            self.assertIn(expected, message)
        self.assertFalse(settings.site_blog_dir.joinpath("tactile.md").exists())

    def test_drafts_skip_rules_and_override_skips_with_reason(self) -> None:
        """A draft can be saved mid-writing; a recorded human reason skips the rules for a live post."""
        self.write_rules(RULES)
        self.assertTrue(self.write_post("Just prose.", draft=True).exists())
        self.assertTrue(self.write_post("Just prose.", discovery_override="republished guide").exists())

    def test_link_counting(self) -> None:
        """Self-links, images, other hosts, and CTA pages don't count as related pages; www and absolute links do."""
        body = (
            "[self](/blog/tactile/) ![fig](/media/fig.png) [elsewhere](https://other.com/blog/x/) "
            "[services](/services/x/) [episode](https://www.example.com/podcast/ep/)"
        )
        self.assertEqual(
            site_link_paths(body), ["/blog/tactile/", "/services/x/", "/podcast/ep/"]
        )
        rules = BlogRules(**RULES["blog"])
        problems, _ = check_blog_rules(rules, "tactile", ["Research"], body)
        self.assertEqual(problems, [])
        problems, _ = check_blog_rules(rules, "tactile", ["Research"], "[self](/blog/tactile/) [s](/services/x/)")
        self.assertEqual(len(problems), 1)

    def test_evidence_only_warns(self) -> None:
        """Code, formulas, and measured numbers count as evidence; a post without any still passes, with a warning."""
        for body in ("```python\nx = 1\n```", "$$p^k$$", "an 84% failure rate", "16 DoF hands"):
            self.assertTrue(has_evidence(body), body)
        self.assertFalse(has_evidence("In 2024 we wrote 3 posts."))
        rules = BlogRules(**RULES["blog"])
        problems, warnings = check_blog_rules(rules, "tactile", ["Research"], GOOD_BODY.replace("100 Hz", "fast"))
        self.assertEqual((problems, len(warnings)), ([], 1))

    def test_faq_rule_counts_faq_items_not_body_headings(self) -> None:
        """A post needs `min_faq_items` question and answer pairs; a FAQ heading in the body doesn't count."""
        self.write_rules(FAQ_RULES)
        faq = [{"question": f"Question {n}?", "answer": f"Answer {n}."} for n in range(3)]

        # Refused with the count and the fix, nothing written; a body heading doesn't stand in for the field.
        for body in (GOOD_BODY, GOOD_BODY + "\n\n## FAQ\n\n### Q?\n\nA."):
            with self.assertRaises(ValueError) as refused:
                self.write_post(body, faq=faq[:2])
            self.assertIn("2 FAQ item(s)", str(refused.exception))
            self.assertFalse(settings.site_blog_dir.joinpath("tactile.md").exists())

        # Enough items write the post, with the FAQ in frontmatter where the site reads it.
        path = self.write_post(GOOD_BODY, faq=faq)
        self.assertIn('faq: [{"question": "Question 0?"', path.read_text(encoding="utf-8"))

        # Drafts skip the rule while being written; a recorded human reason skips it for a live post.
        path.unlink()
        self.assertTrue(self.write_post(GOOD_BODY, draft=True).exists())
        self.assertTrue(self.write_post(GOOD_BODY, discovery_override="republished guide").exists())

    def test_faq_rule_is_off_by_default_and_checked_on_hand_written_posts(self) -> None:
        """Without `min_faq_items` no FAQ is needed; with it, a post dropped into the blog directory is held to it."""
        rules = BlogRules(**RULES["blog"])
        self.assertEqual(check_blog_rules(rules, "tactile", ["Research"], GOOD_BODY)[0], [])
        self.assertEqual(len(check_blog_rules(BlogRules(**FAQ_RULES["blog"]), "tactile", ["Research"], GOOD_BODY)[0]), 1)

        self.write_rules(FAQ_RULES)
        settings.site_blog_dir.mkdir(parents=True)
        post = settings.site_blog_dir / "tactile.md"
        head = f'---\ntitle: "Tactile Sensing for Robot Hands"\ndescription: "{GOOD_DESCRIPTION}"\ntags: ["Research"]\n'
        post.write_text(f"{head}---\n\n{GOOD_BODY}\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            check_existing_blog_post("tactile")

        items = ", ".join(f'{{"question": "Q{n}?", "answer": "A{n}."}}' for n in range(3))
        post.write_text(f"{head}faq: [{items}]\n---\n\n{GOOD_BODY}\n", encoding="utf-8")
        self.assertEqual(check_existing_blog_post("tactile")["draft"], False)

    def test_misspelled_rule_is_refused(self) -> None:
        """A typo would silently turn its rule off, so an unknown key fails loudly."""
        self.write_rules({"blog": {"min_internal_link": 1}})
        with self.assertRaises(ValueError):
            load_blog_rules()

    def test_hand_written_post_is_checked_before_deploy(self) -> None:
        """A post dropped into the blog directory gets the same checks podcast_create_blog applies."""
        self.write_rules(RULES)
        settings.site_blog_dir.mkdir(parents=True)
        settings.site_blog_dir.joinpath("tactile.md").write_text(
            f'---\ntitle: "Tactile Sensing for Robot Hands"\ndescription: "{GOOD_DESCRIPTION}"\ntags: ["Research"]\n---\n\nProse only.\n',
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            check_existing_blog_post("tactile")
        # The override skips the refusals; the evidence warning is still passed on.
        self.assertEqual(len(check_existing_blog_post("tactile", "legacy post")["warnings"]), 1)


class DeployToolChecksTests(TempShowTestCase):
    """podcast_deploy_site checks named blog posts before pushing anything."""

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool through an in-memory MCP client and return its structured result."""
        async with Client(mcp) as client:
            return (await client.call_tool(tool, arguments)).structured_content

    def test_failing_post_blocks_the_deploy(self) -> None:
        """One post breaking the rules stops the whole deploy; nothing is pushed."""
        self.write_rules(RULES)
        self.write_post("Just prose.", draft=True)
        # Flip the saved draft to live, as a human would before deploying.
        path = settings.site_blog_dir / "tactile.md"
        path.write_text(path.read_text(encoding="utf-8").replace("draft: true", "draft: false"), encoding="utf-8")
        with mock.patch("mcp_server.server.deploy_site") as deploy, self.assertRaises(ToolError):
            asyncio.run(self._call("podcast_deploy_site", {"message": "m", "blog_slugs": ["tactile"]}))
        deploy.assert_not_called()


class IndexNowTests(TempShowTestCase):
    """Which URLs a deploy announces, the submission, and the background start."""

    def test_changed_files_map_to_public_pages(self) -> None:
        """Episodes add their page and the podcast landing page; live blog posts add theirs; drafts and the feed add none."""
        self.write_post(GOOD_BODY)
        create_blog_post(
            title="WIP", description="d", content_markdown="x", slug="wip", draft=True, discovery_override="fixture"
        )
        urls = indexnow.urls_for_changed_files([
            "src/content/podcast/ep-one.md",
            "src/content/blog/tactile.md",
            "src/content/blog/wip.md",
            "public/podcast/rss.xml",
        ])
        self.assertEqual(urls, [
            "https://example.com/podcast/ep-one/",
            "https://example.com/blog/tactile/",
            "https://example.com/podcast/",
        ])

    def test_only_the_sites_https_urls_are_accepted(self) -> None:
        """A foreign or plain-http URL would make engines refuse the whole batch, so it's refused first."""
        self.assertEqual(indexnow.validate_urls(["https://example.com/a/", "https://example.com/a/"]), ["https://example.com/a/"])
        for bad in ("https://other.com/a/", "http://example.com/a/"):
            with self.assertRaises(ValueError):
                indexnow.validate_urls([bad])

    def test_unconfigured_start_reports_off_without_spawning(self) -> None:
        """Without a key the deploy reports `off` with the fix, and no process starts."""
        with mock.patch("core.indexnow.subprocess.Popen") as popen:
            result = indexnow.start_announce(["https://example.com/a/"])
        self.assertEqual(result["status"], "off")
        popen.assert_not_called()

    def test_configured_start_spawns_a_detached_run(self) -> None:
        """With a key, a detached `python -m core.indexnow` run gets the URLs and its own session."""
        settings.indexnow_key = "testkey123"
        with mock.patch("core.indexnow.subprocess.Popen") as popen:
            popen.return_value.pid = 42
            result = indexnow.start_announce(["https://example.com/a/"])
        self.assertEqual((result["status"], result["pid"]), ("started", 42))
        args, kwargs = popen.call_args
        self.assertEqual(args[0][-3:], ["-m", "core.indexnow", "https://example.com/a/"])
        self.assertTrue(kwargs["start_new_session"])

    def test_submission_payload_and_log(self) -> None:
        """Live pages go out in one request with host, key, and key location, and the outcome is logged."""
        settings.indexnow_key = "testkey123"
        response = mock.MagicMock(status=202)
        response.__enter__.return_value = response
        with mock.patch("core.indexnow.open_https", return_value=response) as http:
            record = indexnow.announce(["https://example.com/a/"], wait=False)
        payload = json.loads(http.call_args.kwargs["data"])
        self.assertEqual(payload, {
            "host": "example.com",
            "key": "testkey123",
            "keyLocation": "https://example.com/testkey123.txt",
            "urlList": ["https://example.com/a/"],
        })
        self.assertEqual((record["status"], record["http_status"]), ("submitted", 202))
        self.assertEqual(indexnow.latest_announcement()["status"], "submitted")

    def test_pages_that_never_go_live_are_not_submitted(self) -> None:
        """A page still 404 at the timeout is left out; if none are live, nothing is sent."""
        settings.indexnow_key = "testkey123"
        with (
            mock.patch("core.indexnow.wait_until_live", return_value={"https://example.com/a/": 404}),
            mock.patch("core.indexnow.submit") as submit,
        ):
            record = indexnow.announce(["https://example.com/a/"])
        submit.assert_not_called()
        self.assertEqual((record["status"], record["not_live"]), ("nothing_live", {"https://example.com/a/": 404}))

    def test_wait_rechecks_until_live_or_timeout(self) -> None:
        """A page that 404s while the host builds is checked again until it answers 200, or until time runs out."""
        with (
            mock.patch("core.indexnow.time.sleep"),
            mock.patch("core.indexnow.page_status", side_effect=[404, 200]) as status,
        ):
            self.assertEqual(indexnow.wait_until_live(["https://example.com/a/"]), {"https://example.com/a/": 200})
        self.assertEqual(status.call_count, 2)
        # A clock that jumps past the deadline after the first check: one try, then give up.
        with (
            mock.patch("core.indexnow.time.sleep"),
            mock.patch("core.indexnow.time.monotonic", side_effect=[0.0, 10_000.0]),
            mock.patch("core.indexnow.page_status", return_value=404) as status,
        ):
            self.assertEqual(indexnow.wait_until_live(["https://example.com/a/"]), {"https://example.com/a/": 404})
        self.assertEqual(status.call_count, 1)


class DeployHookTests(unittest.TestCase):
    """deploy_paths starts the ping for exactly the files it committed."""

    def setUp(self) -> None:
        """A website repo cloned from a bare origin, with one committed file."""
        self.tmp = Path(tempfile.mkdtemp())
        origin, self.site = self.tmp / "origin.git", self.tmp / "site"
        git_env = ["-c", "init.defaultBranch=main"]
        subprocess.run(["git", *git_env, "init", "--bare", "-q", str(origin)], check=True)
        subprocess.run(["git", *git_env, "init", "-q", str(self.site)], check=True)
        for key, value in [("user.email", "t@example.com"), ("user.name", "Test")]:
            subprocess.run(["git", "config", key, value], cwd=self.site, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=self.site, check=True)
        (self.site / "README.md").write_text("site\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.site, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.site, check=True)
        self._saved = settings.website_path
        settings.website_path = self.site

    def tearDown(self) -> None:
        """Restore settings and delete the temp repos."""
        settings.website_path = self._saved
        shutil.rmtree(self.tmp)

    def test_push_announces_the_changed_episode(self) -> None:
        """Only the episode this deploy changed (plus the landing page) is announced, and a ping crash can't fail the push."""
        from core.site import deploy_paths

        settings.site_episodes_dir.mkdir(parents=True)
        (settings.site_episodes_dir / "ep-one.md").write_text("episode\n")
        with mock.patch("core.site.start_announce", return_value={"status": "started"}) as start:
            result = deploy_paths([settings.site_rss_path, settings.site_episodes_dir], "publish ep-one")
        self.assertEqual((result["status"], result["indexnow"]["status"]), ("pushed", "started"))
        start.assert_called_once_with(["https://example.com/podcast/ep-one/", "https://example.com/podcast/"])

        (settings.site_episodes_dir / "ep-one.md").write_text("episode, edited\n")
        with mock.patch("core.site.start_announce", side_effect=RuntimeError("boom")):
            result = deploy_paths([settings.site_episodes_dir], "edit ep-one")
        self.assertEqual((result["status"], result["indexnow"]["status"]), ("pushed", "failed"))


class DoctorChecksTests(TempShowTestCase):
    """The doctor's view of IndexNow and the site rules."""

    def test_indexnow_key_file_must_match(self) -> None:
        """Off without a key; FAIL when the served file holds something else; PASS when it holds the key."""
        self.assertEqual(doctor.check_indexnow()[0], "OFF")
        settings.indexnow_key = "testkey123"
        for served, expected in ((b"oldkey456", "FAIL"), (b"testkey123\n", "PASS")):
            with mock.patch("core.doctor.open_https", return_value=io.BytesIO(served)):
                self.assertEqual(doctor.check_indexnow()[0], expected)
        with mock.patch("core.doctor.open_https", side_effect=urllib.error.URLError("down")):
            self.assertEqual(doctor.check_indexnow()[0], "WARN")

    def test_site_rules_status(self) -> None:
        """Off without a file, PASS listing the active rules, FAIL for a malformed file."""
        self.assertEqual(doctor.check_site_rules()[0], "OFF")
        self.write_rules(RULES)
        status, detail = doctor.check_site_rules()
        self.assertEqual(status, "PASS")
        self.assertIn("call-to-action", detail)
        self.write_rules({"blog": {"typo": 1}})
        self.assertEqual(doctor.check_site_rules()[0], "FAIL")


if __name__ == "__main__":
    unittest.main()
