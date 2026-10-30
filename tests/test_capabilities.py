"""Tests for core.capabilities: channel statuses built from the setup doctor's checks.

Run from the repo root:
    uv run python -m unittest discover -s tests -v
"""

# Settings are built when core.config is first imported, so the fake show's
# environment (see fake_show.py) must be set before the imports below.
import fake_show  # noqa: F401 - must precede every engine import

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import capabilities, doctor
from core.config import settings


def _check(status: str, detail: str) -> mock.Mock:
    """A stand-in doctor check that returns a fixed result and counts its calls."""
    return mock.Mock(return_value=(status, detail))


class ChannelStatusTests(unittest.TestCase):
    """How check results become one status per channel."""

    def test_worst_check_sets_status_and_detail(self) -> None:
        """A channel is as ready as its weakest check, and only the weakest checks' reasons are shown."""
        channels = [("Feed", [_check("PASS", "bucket ok"), _check("WARN", "wrong branch")], ["podcast_publish"])]
        with mock.patch.object(capabilities, "CHANNELS", channels):
            (feed,) = capabilities.capabilities()["channels"]
        self.assertEqual((feed["status"], feed["detail"]), ("needs_attention", "wrong branch"))

    def test_crashing_check_reports_broken(self) -> None:
        """A check that raises becomes `broken` with the error, instead of failing the whole call."""
        crash = mock.Mock(side_effect=RuntimeError("bad account id"))
        with mock.patch.object(capabilities, "CHANNELS", [("Feed", [crash], [])]):
            (feed,) = capabilities.capabilities()["channels"]
        self.assertEqual((feed["status"], feed["detail"]), ("broken", "RuntimeError: bad account id"))

    def test_shared_check_runs_once(self) -> None:
        """Channels that share a check (e.g. the website repo) don't repeat its network calls."""
        site = _check("PASS", "site ok")
        with mock.patch.object(capabilities, "CHANNELS", [("Feed", [site], []), ("Blog", [site], [])]):
            result = capabilities.capabilities()
        site.assert_called_once()
        self.assertEqual([c["status"] for c in result["channels"]], ["ready", "ready"])

    def test_blog_is_off_without_a_website(self) -> None:
        """SITE_MODE=none has nowhere to write blog posts, so the blog channel says so."""
        saved = settings.site_mode
        settings.site_mode = "none"
        try:
            status, _ = capabilities.check_blog()
        finally:
            settings.site_mode = saved
        self.assertEqual(status, "OFF")


class DoctorOffStatusTests(unittest.TestCase):
    """Optional integrations that aren't set up report OFF, not PASS."""

    def test_unset_integrations_are_off(self) -> None:
        """The fake show sets no X, Instagram, dev.to, or Substack settings."""
        for check in (doctor.check_x_posting, doctor.check_instagram, doctor.check_devto, doctor.check_substack):
            with self.subTest(check=check.__name__):
                self.assertEqual(check()[0], "OFF")

    def test_youtube_is_off_without_a_token(self) -> None:
        """No OAuth token file means YouTube is off, with the path that's missing."""
        saved = settings.youtube_token_path
        settings.youtube_token_path = Path(tempfile.mkdtemp()) / "missing.json"
        try:
            status, detail = doctor.check_youtube()
        finally:
            settings.youtube_token_path = saved
        self.assertEqual(status, "OFF")
        self.assertIn("missing.json", detail)


if __name__ == "__main__":
    unittest.main()
