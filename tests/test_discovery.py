"""Tests for core.discovery: brief validation and storage, channel checks, and the publish gate.

Run from the repo root:
    uv run python -m unittest discover -s tests -v
"""

# Settings are built when core.config is first imported, so the fake show's
# environment (see fake_show.py) must be set before the imports below.
import fake_show  # noqa: F401 - must precede every engine import

import shutil
import tempfile
import unittest
from pathlib import Path

from core import discovery
from core.config import settings
from core.formatting import parse_chapters

SAMPLE_BRIEF = {
    "primary_phrase": "zero trust architecture",
    "secondary_phrases": ["network segmentation", "identity verification", "cyberwarfare"],
    "audience": "Security engineers running production systems",
    "question": "How do you run zero trust in production?",
    "entities": ["Chase Cunningham"],
}
# A YouTube description that passes every check for SAMPLE_BRIEF.
GOOD_YOUTUBE_DESCRIPTION = "How zero trust architecture works in production.\n\n00:00 Intro\n02:10 Segmentation\n05:30 Identity"
GOOD_BLOG_DESCRIPTION = "What zero trust architecture looks like once it leaves the slide deck and meets production."


def stage_sample_brief(slug: str = "ep-one") -> discovery.DiscoveryBrief:
    """Save SAMPLE_BRIEF for `slug` in the current data dir (shared with other test modules)."""
    brief = discovery.make_brief(slug, **SAMPLE_BRIEF)
    discovery.save_brief(brief)
    return brief


class TempDataDirTestCase(unittest.TestCase):
    """Points DATA_DIR at a fresh temp directory for each test."""

    def setUp(self) -> None:
        """Swap in a temp data dir."""
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = settings.data_dir
        settings.data_dir = self.tmp

    def tearDown(self) -> None:
        """Restore the data dir and delete temp files."""
        settings.data_dir = self._saved
        shutil.rmtree(self.tmp)


class BriefTests(TempDataDirTestCase):
    """Making, saving, and loading briefs."""

    def test_round_trip(self) -> None:
        """A saved brief loads back identical, from DATA_DIR/discovery/<slug>.json."""
        brief = stage_sample_brief()
        self.assertTrue((self.tmp / "discovery" / "ep-one.json").is_file())
        self.assertEqual(discovery.load_brief("ep-one"), brief)

    def test_missing_brief_says_how_to_stage_one(self) -> None:
        """Loading a brief that was never staged names the tool to call."""
        with self.assertRaisesRegex(ValueError, "podcast_stage_discovery"):
            discovery.load_brief("ep-one")

    def test_rejects_bad_briefs(self) -> None:
        """Too few phrases, duplicate phrases, no entities, or an unsafe slug are refused."""
        cases = {
            "too few secondary": {"secondary_phrases": ["one phrase", "two phrase"]},
            "duplicate": {"secondary_phrases": ["Zero Trust Architecture", "segmentation", "identity"]},
            "no entities": {"entities": []},
            "blank audience": {"audience": "  "},
        }
        for name, change in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                discovery.make_brief("ep-one", **{**SAMPLE_BRIEF, **change})
        with self.assertRaises(ValueError):
            discovery.make_brief("../escape", **SAMPLE_BRIEF)


class ChannelCheckTests(unittest.TestCase):
    """Each channel's check finds the problems a search engine or feed would punish."""

    def setUp(self) -> None:
        """Build the sample brief without touching disk."""
        self.brief = discovery.make_brief("ep-one", **SAMPLE_BRIEF)

    def test_feed(self) -> None:
        """Titles name the topic without episode numbers or the show title; summaries use a phrase."""
        self.assertEqual(discovery.check_feed(self.brief, "Chase Cunningham on Zero Trust", "Why network segmentation fails."), [])
        problems = discovery.check_feed(self.brief, "Ep. 12: Test Show special", "A chat.")
        self.assertEqual(len(problems), 4)
        # "C#10" or a mid-word "#" isn't an episode number.
        self.assertEqual(discovery.check_feed(self.brief, "Zero trust architecture in C#10 services", "Network segmentation."), [])

    def test_blog(self) -> None:
        """Descriptions are search-snippet length and carry the primary phrase."""
        self.assertEqual(discovery.check_blog(self.brief, "Zero trust architecture in practice", GOOD_BLOG_DESCRIPTION), [])
        self.assertEqual(len(discovery.check_blog(self.brief, "Some thoughts", "Too short.")), 3)

    def test_youtube(self) -> None:
        """The hook, a tag, and valid chapters are required; Shorts skip chapters."""
        tags = ["zero trust architecture"]
        self.assertEqual(discovery.check_youtube(self.brief, "Chase Cunningham", GOOD_YOUTUBE_DESCRIPTION, tags, False), [])
        late_hook = "x" * 160 + " zero trust architecture"
        problems = discovery.check_youtube(self.brief, "Untitled", late_hook, ["misc"], is_short=False)
        self.assertEqual(len(problems), 4)
        self.assertEqual(discovery.check_youtube(self.brief, "Chase Cunningham", "zero trust architecture", tags, is_short=True), [])

    def test_youtube_chapter_rules(self) -> None:
        """Chapters must start at 00:00 and be at least 10 seconds apart."""
        tags = ["zero trust architecture"]
        late_start = "zero trust architecture\n00:05 a\n02:00 b\n03:00 c"
        crowded = "zero trust architecture\n00:00 a\n00:05 b\n03:00 c"
        for description in (late_start, crowded):
            with self.subTest(description=description):
                self.assertEqual(len(discovery.check_youtube(self.brief, "Chase Cunningham", description, tags, False)), 1)

    def test_instagram(self) -> None:
        """Alt text is required and a phrase must appear before Instagram's 'more' cut."""
        self.assertEqual(discovery.check_instagram(self.brief, "Network segmentation, explained.", "A shield over a grid"), [])
        problems = discovery.check_instagram(self.brief, "x" * 130 + " network segmentation", None)
        self.assertEqual(len(problems), 2)


class GateTests(TempDataDirTestCase):
    """The gate every publish path calls before going public."""

    def test_passes_and_refuses(self) -> None:
        """No problems returns None; any problem raises with the fix and the override option."""
        stage_sample_brief()
        self.assertIsNone(discovery.gate("blog", "ep-one", None, lambda b: []))
        with self.assertRaisesRegex(ValueError, "Nothing was published.*discovery_override"):
            discovery.gate("blog", "ep-one", None, lambda b: ["the title is vague"])

    def test_override_needs_a_reason(self) -> None:
        """An override skips the checks and is returned for the record; a blank one is refused."""
        self.assertEqual(discovery.gate("x", None, "  launch teaser  ", lambda b: ["never run"]), "launch teaser")
        with self.assertRaisesRegex(ValueError, "reason"):
            discovery.gate("x", None, " ", lambda b: [])

    def test_needs_a_slug_or_override(self) -> None:
        """Without a slug there's no brief to check, so the gate refuses."""
        with self.assertRaisesRegex(ValueError, "slug"):
            discovery.gate("youtube", None, None, lambda b: [])


class ParseChaptersTests(unittest.TestCase):
    """Chapter timestamps from show notes and YouTube descriptions."""

    def test_both_formats(self) -> None:
        """Show-notes bullets and plain description lines parse the same way; other lines are skipped."""
        text = "Intro text\n- **00:00** — Introduction\n06:40 Main topic\n1:02:03 - Wrap-up\nNot a chapter 12:00"
        self.assertEqual(
            parse_chapters(text),
            [
                {"seconds": 0.0, "title": "Introduction"},
                {"seconds": 400.0, "title": "Main topic"},
                {"seconds": 3723.0, "title": "Wrap-up"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
