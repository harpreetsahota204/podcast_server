"""Tests that go through the MCP protocol itself, using FastMCP's in-memory client.

They check what an agent actually sees: the advertised tool list (and that the docs
list the same tools), the safety annotations clients use for auto-approval, the
connection instructions, and the guards on the catalog-edit, deploy, and backup tools.

Run from the repo root:
    uv run python -m unittest discover -s tests -v
"""

# Settings are built when core.config is first imported, so the fake show's
# environment (see fake_show.py) must be set before the imports below.
import fake_show  # noqa: F401 - must precede every engine import

import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_engine import make_episode

from core import backup
from core.capabilities import CHANNELS
from core.config import PROJECT_ROOT, settings
from core.db import CatalogDB
from mcp_server.server import mcp


def _tool_names_in_agents_md() -> set[str]:
    """Tool names in AGENTS.md's tool table: the first column of each row."""
    text = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| `(podcast_\w+)`", text, flags=re.MULTILINE))


def _tool_names_in_readme() -> set[str]:
    """Tool names in the README's "MCP Tools" section, which holds only the tools table."""
    text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    section = text.split("\n## MCP Tools\n", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"`(podcast_\w+)`", section))


class ToolSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """What a connecting client is told about the tools."""

    async def asyncSetUp(self) -> None:
        """List the tools once, over the protocol, as a client would."""
        async with Client(mcp) as client:
            self.tools = {tool.name: tool for tool in await client.list_tools()}
            self.instructions = client.instructions or ""

    def test_docs_list_exactly_the_advertised_tools(self) -> None:
        """AGENTS.md and README can't drift from the server: a tool added or removed in code fails here."""
        self.assertEqual(_tool_names_in_agents_md(), set(self.tools))
        self.assertEqual(_tool_names_in_readme(), set(self.tools))

    def test_capabilities_name_only_real_tools(self) -> None:
        """podcast_capabilities can't point the agent at a tool that was renamed or removed."""
        named = {tool for _, _, tools in CHANNELS for tool in tools}
        self.assertEqual(named - set(self.tools), set())

    def test_every_tool_declares_whether_it_is_read_only(self) -> None:
        """Without an explicit hint, clients assume the worst (or the best) about a tool."""
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertIsNotNone(tool.annotations)
                self.assertIsNotNone(tool.annotations.read_only_hint)

    def test_instructions_name_every_public_destructive_tool(self) -> None:
        """The connection instructions list exactly the tools that change what the public sees."""
        public = {
            name for name, tool in self.tools.items()
            if tool.annotations.destructive_hint and tool.annotations.open_world_hint
        }
        listed = re.search(r"Public tools: ([^.]*)\.", self.instructions).group(1)
        self.assertEqual(set(re.findall(r"podcast_\w+", listed)), public)
        self.assertIn("PLAYBOOK.md", self.instructions)


class CatalogEditToolTests(unittest.IsolatedAsyncioTestCase):
    """podcast_update_episode and podcast_deploy_site, called over the protocol."""

    def setUp(self) -> None:
        """Point the catalog and website at temp paths and add one imported episode."""
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (settings.website_path, settings.database_path)
        settings.website_path = self.tmp / "site"
        settings.database_path = self.tmp / "catalog.db"
        with CatalogDB(settings.database_path) as db:
            db.insert_episode(make_episode(content_html="<p>imported</p>", content_markdown="old"))

    def tearDown(self) -> None:
        """Restore settings and delete temp files."""
        settings.website_path, settings.database_path = self._saved
        shutil.rmtree(self.tmp)

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool through an in-memory MCP client and return its structured result."""
        async with Client(mcp) as client:
            return (await client.call_tool(tool, arguments)).structured_content

    def _episode(self) -> dict[str, Any]:
        """The test episode as currently stored."""
        with CatalogDB(settings.database_path) as db:
            return db.get_episode_by_slug("ep-one")

    async def test_update_rewrites_notes_and_keeps_guid(self) -> None:
        """New notes replace stale imported HTML, the feed is rebuilt, and the GUID is untouched."""
        result = await self._call("podcast_update_episode", {"slug": "ep-one", "show_notes_markdown": "### New"})
        self.assertEqual(result["changed_fields"], ["content_markdown"])
        self.assertTrue(Path(result["rss_path"]).exists())
        episode = self._episode()
        self.assertEqual((episode["content_markdown"], episode["content_html"], episode["guid"]), ("### New", "", "guid-1"))

    async def test_update_cannot_touch_guid(self) -> None:
        """A guid argument is never applied, whether the schema rejects it or ignores it."""
        with self.assertRaises(ToolError):
            await self._call("podcast_update_episode", {"slug": "ep-one", "guid": "hijacked"})
        self.assertEqual(self._episode()["guid"], "guid-1")

    async def test_update_unknown_slug_fails(self) -> None:
        """Editing an episode that doesn't exist is an error, not a silent no-op."""
        with self.assertRaises(ToolError):
            await self._call("podcast_update_episode", {"slug": "nope", "title": "x"})

    async def test_deploy_refuses_missing_blog_post(self) -> None:
        """A mistyped blog slug fails loudly instead of deploying without the post."""
        with mock.patch("mcp_server.server.deploy_site") as deploy, self.assertRaises(ToolError):
            await self._call("podcast_deploy_site", {"message": "m", "blog_slugs": ["missing-post"]})
        deploy.assert_not_called()


class BackupJobTests(unittest.TestCase):
    """The background backup job: one run at a time, status read from its log."""

    def setUp(self) -> None:
        """Use a temp data dir (backup_dir lives under it) with backups enabled."""
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (settings.data_dir, settings.hf_backup_repo)
        settings.data_dir, settings.hf_backup_repo = self.tmp, "user/archive"

    def tearDown(self) -> None:
        """Restore settings and delete temp files."""
        settings.data_dir, settings.hf_backup_repo = self._saved
        shutil.rmtree(self.tmp)

    def test_refuses_without_backup_repo(self) -> None:
        """No HF_BACKUP_REPO means no backup process is ever started."""
        settings.hf_backup_repo = None
        with mock.patch("core.backup.subprocess.Popen") as popen, self.assertRaises(ValueError):
            backup.start_backup()
        popen.assert_not_called()

    def test_second_start_while_running_spawns_nothing(self) -> None:
        """While the lock is held, starting again reports "running" instead of launching a duplicate."""
        with backup.backup_lock(), mock.patch("core.backup.subprocess.Popen") as popen:
            self.assertEqual(backup.start_backup()["status"], "running")
        popen.assert_not_called()

    def test_status_reads_only_the_latest_run(self) -> None:
        """An earlier run's success doesn't mask a later failure."""
        self.assertEqual(backup.backup_status()["status"], "never_run")
        log = settings.backup_dir / backup.LOG_NAME
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"{backup.RUN_HEADER} 1 ===\n{backup.COMPLETE_MARKER}: url\n{backup.RUN_HEADER} 2 ===\nTraceback\n")
        status = backup.backup_status()
        self.assertEqual((status["status"], status["log_tail"][-1]), ("failed", "Traceback"))


if __name__ == "__main__":
    unittest.main()
