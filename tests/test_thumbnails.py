"""Tests for YouTube thumbnails: prompt staging, headline rendering, the style file, and the setter.

Rendering uses Pillow's built-in font (no show fonts needed); YouTube is always mocked.

Run from the repo root:
    uv run python -m unittest discover -s tests -v
"""

# Settings are built when core.config is first imported, so the fake show's
# environment (see fake_show.py) must be set before the imports below.
import fake_show  # noqa: F401 - must precede every engine import

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from core.config import settings
from core.thumbnail import MAX_THUMBNAIL_BYTES, THUMB_SIZE, ThumbnailStyle, generate_youtube_thumbnail, load_style, render_thumbnail


class ThumbnailTestCase(unittest.TestCase):
    """Temp data dir with a black, non-16:9 base image."""

    def setUp(self) -> None:
        """Create the temp dir and point the data dir (templates, thumbnails, logs) at it."""
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = settings.data_dir
        settings.data_dir = self.tmp
        self.base = self.tmp / "art.png"
        Image.new("RGB", (900, 900), "black").save(self.base)

    def tearDown(self) -> None:
        """Restore settings and delete the temp dir."""
        settings.data_dir = self._saved
        shutil.rmtree(self.tmp)


class RenderTests(ThumbnailTestCase):
    """The headline is fitted into its box and the output meets YouTube's rules."""

    # No fade, so any non-black pixel is text.
    PLAIN = ThumbnailStyle(scrim_opacity=0)

    def test_text_stays_inside_its_box_and_off_the_badge_corner(self) -> None:
        """Output is 1280x720 and every drawn pixel sits in the text box, clear of the duration badge."""
        out = self.tmp / "out.png"
        result = render_thumbnail(self.base, ["Everything", "can be", "measured"], out, style=self.PLAIN)
        with Image.open(result["path"]) as image:
            self.assertEqual(image.size, THUMB_SIZE)
            left, top, right, bottom = image.convert("L").getbbox()
        box = [round(f * d) for f, d in zip(self.PLAIN.text_box, (*THUMB_SIZE, *THUMB_SIZE), strict=True)]
        self.assertGreaterEqual(left, box[0])
        self.assertGreaterEqual(top, box[1])
        self.assertLessEqual(right, box[2] + 1)
        self.assertLessEqual(bottom, box[3] + 1)
        self.assertEqual(result["lines"], ["EVERYTHING", "CAN BE", "MEASURED"])

    def test_longer_headlines_and_labels_get_smaller_type(self) -> None:
        """The fit shrinks the headline for wider text, and for a label when height is what binds."""
        # Four short lines are limited by the box's height, so a label must cost headline size.
        tall = ["It", "is", "all", "data"]
        sizes = [
            render_thumbnail(self.base, lines, self.tmp / f"o{i}.png", label=label, style=self.PLAIN)["headline_font_px"]
            for i, (lines, label) in enumerate([(tall, None), (tall, "EP 12"), (["Everything can be measured"], None)])
        ]
        self.assertGreater(sizes[0], sizes[1])
        self.assertGreater(sizes[0], sizes[2])

    def test_oversized_png_falls_back_to_jpeg_under_the_limit(self) -> None:
        """Noisy art compresses badly as PNG, so the output switches to JPEG."""
        noisy = self.tmp / "noise.png"
        Image.frombytes("RGB", THUMB_SIZE, os.urandom(THUMB_SIZE[0] * THUMB_SIZE[1] * 3)).save(noisy)
        result = render_thumbnail(noisy, ["Noise"], self.tmp / "noisy-final.png")
        self.assertTrue(result["path"].endswith(".jpg"))
        self.assertLessEqual(result["file_size_bytes"], MAX_THUMBNAIL_BYTES)
        self.assertFalse((self.tmp / "noisy-final.png").exists())

    def test_bad_inputs_are_refused(self) -> None:
        """Writing over the art, or an empty or too-long headline, fails before drawing."""
        with self.assertRaises(ValueError):
            render_thumbnail(self.base, ["X"], self.base)
        for lines in ([], ["  "], ["a", "b", "c", "d", "e"]):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                render_thumbnail(self.base, lines, self.tmp / "o.png")

    def test_style_file_paths_are_relative_to_it(self) -> None:
        """Font paths in thumbnail_style.json resolve against its folder; no file means defaults."""
        self.assertEqual(load_style(), ThumbnailStyle())
        settings.templates_dir.mkdir()
        (settings.templates_dir / "thumbnail_style.json").write_text(json.dumps(
            {"headline_font": "fonts/Display.ttf", "accent_color": "#FF6B00"}))
        style = load_style()
        self.assertEqual(style.headline_font, settings.templates_dir / "fonts" / "Display.ttf")
        self.assertEqual(style.accent_color, "#FF6B00")

    def test_prompt_reserves_space_and_forbids_lettering(self) -> None:
        """The staged art prompt leaves the right side empty and asks for no text."""
        staged = generate_youtube_thumbnail("dice bursting into curves", "ep-art")
        self.assertIn("right 40%", staged["prompt"])
        self.assertIn("no text", staged["prompt"])
        self.assertEqual(staged["local_path"], str(settings.thumbnails_dir / "ep-art.png"))


class SetThumbnailTests(ThumbnailTestCase):
    """Setting a thumbnail validates first, then logs the change for the metrics dashboard."""

    def test_invalid_requests_never_reach_youtube(self) -> None:
        """Bad ids, wrong formats, missing or oversized files are refused before any API call."""
        from exporters.youtube import set_video_thumbnail

        huge = self.tmp / "huge.png"
        huge.write_bytes(b"\x00" * (MAX_THUMBNAIL_BYTES + 1))
        cases = [("bad id!", str(self.base)), ("abcdefghijk", str(self.tmp / "x.gif")),
                 ("abcdefghijk", str(self.tmp / "missing.png")), ("abcdefghijk", str(huge))]
        with mock.patch("exporters.youtube.get_authenticated_youtube_service") as service:
            for video_id, path in cases:
                with self.subTest(path=path), self.assertRaises((ValueError, FileNotFoundError)):
                    set_video_thumbnail(video_id, path)
        service.assert_not_called()

    def test_change_is_logged_with_tracked_episode_and_marked_on_dashboard(self) -> None:
        """The episode comes from the tracked-video mapping, and the change becomes a dashboard event."""
        from exporters.youtube import set_video_thumbnail
        from metrics.analysis import events
        from metrics.episodes import EpisodeIndex

        settings.youtube_videos_file.write_text(json.dumps({"videos": [{"video_id": "abcdefghijk", "episode_slug": "ep-a"}]}))
        with mock.patch("exporters.youtube.get_authenticated_youtube_service") as service, \
                mock.patch("exporters.youtube.MediaFileUpload"):
            result = set_video_thumbnail("abcdefghijk", str(self.base))
        service.return_value.thumbnails.return_value.set.assert_called_once()
        self.assertEqual(result["episode_slug"], "ep-a")
        logged = json.loads(settings.youtube_thumbnail_log_path.read_text())
        self.assertEqual((logged["video_id"], logged["episode_slug"]), ("abcdefghijk", "ep-a"))

        index = EpisodeIndex([{"slug": "ep-a", "title": "Episode A", "published_at": "2020-01-01"}])
        marked = [e for e in events(index, "2000-01-01") if e["kind"] == "thumbnail"]
        self.assertEqual(marked[0]["label"], "New thumbnail: Episode A")


if __name__ == "__main__":
    unittest.main()
