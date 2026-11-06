"""Tests for the ``podcast-server`` command line: tool listing, the public-tool gate, argument parsing, workflow.

The CLI runs the same server as MCP in memory, so these tests use the fake show (never real data) and replace the
network-facing function behind ``podcast_post_to_x`` with a mock, so nothing is ever posted.

Run from the repo root:
    uv run python -m unittest discover -s tests -v
"""

# Settings are built when core.config is first imported, so the fake show's environment must be set first.
import fake_show  # noqa: F401 - must precede every engine import

import contextlib
import io
import json
import re
import unittest
from unittest import mock

from mcp_server import cli
from mcp_server.server import INSTRUCTIONS


def run_cli(argv: list[str], stdin: str = "") -> tuple[int, str, str]:
    """Run ``cli.main`` in-process; return its exit status, stdout and stderr."""
    out, err = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
        mock.patch("sys.stdin", io.StringIO(stdin)),
    ):
        status = cli.main(argv)
    return status, out.getvalue(), err.getvalue()


def public_tools_in_instructions() -> set[str]:
    """The tool names the server's instructions list as public (between "Public tools:" and "Removing content")."""
    section = INSTRUCTIONS.split("Public tools:")[1].split("- Removing")[0]
    return set(re.findall(r"podcast_\w+", section))


class ToolListingTests(unittest.TestCase):
    """`tools` and `describe` show what an MCP client would see."""

    def test_public_gate_matches_the_servers_public_list(self) -> None:
        """The CLI's idea of "public" (annotations) equals the list in the server instructions, so neither drifts."""
        status, out, _ = run_cli(["tools", "--public"])
        self.assertEqual(status, 0)
        listed = {line.split()[0] for line in out.splitlines()}
        self.assertEqual(listed, public_tools_in_instructions())

    def test_describe_accepts_the_short_name_and_lists_parameters(self) -> None:
        """`describe get_episode` finds podcast_get_episode and reports its required `slug` parameter."""
        status, out, _ = run_cli(["describe", "get_episode"])
        info = json.loads(out)
        self.assertEqual((status, info["tool"], info["kind"]), (0, "podcast_get_episode", "read"))
        self.assertEqual([(p["name"], p["required"]) for p in info["parameters"]], [("slug", True)])

    def test_unknown_tool_is_refused_with_suggestions(self) -> None:
        """A near miss names the closest tools; the exit status is 2."""
        status, _, err = run_cli(["run", "get_episod"])
        self.assertEqual(status, cli.REFUSED)
        self.assertIn("podcast_get_episode", err)


class RunTests(unittest.TestCase):
    """`run` calls tools through the server, with the public-tool gate in front."""

    def test_read_tool_runs_and_prints_json(self) -> None:
        """A read-only tool needs no flag and prints its result as JSON."""
        status, out, _ = run_cli(["run", "podcast_status"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out)["podcast_title"], "Test Show")

    def test_public_tool_is_refused_without_yes_and_not_called(self) -> None:
        """Without --yes (or dry_run=true) a public tool exits 2 and the function behind it never runs."""
        with mock.patch("mcp_server.server.post_to_x") as post:
            status, _, err = run_cli(["run", "post_to_x", "text=hello"])
        self.assertEqual(status, cli.REFUSED)
        self.assertIn("--yes", err)
        post.assert_not_called()

    def test_dry_run_passes_the_gate_without_yes(self) -> None:
        """dry_run=true is the tool's own safe mode, so it runs without --yes and reaches the tool as True."""
        with mock.patch("mcp_server.server.post_to_x", return_value={"status": "dry_run"}) as post:
            status, out, _ = run_cli(["run", "post_to_x", "text=hello", "dry_run=true"])
        self.assertEqual((status, json.loads(out)), (0, {"status": "dry_run"}))
        self.assertIs(post.call_args.kwargs["dry_run"], True)

    def test_yes_lets_a_public_tool_run(self) -> None:
        """With --yes the public tool is called with the parameters as typed."""
        with mock.patch("mcp_server.server.post_to_x", return_value={"status": "posted"}) as post:
            status, out, _ = run_cli(["run", "post_to_x", "text=hello", "--yes"])
        self.assertEqual((status, json.loads(out)), (0, {"status": "posted"}))
        self.assertEqual(post.call_args.kwargs["text"], "hello")

    def test_string_parameters_stay_text_and_others_parse_as_json(self) -> None:
        """`slug=2024` stays the string "2024"; `limit=5` becomes the number 5 (read from the tool's schema)."""
        listing = cli.asyncio.run(cli._list_tools())
        slug = listing["podcast_get_episode"].input_schema["properties"]["slug"]
        limit = listing["podcast_list_episodes"].input_schema["properties"]["limit"]
        self.assertEqual(cli._coerce("2024", slug), "2024")
        self.assertEqual(cli._coerce("5", limit), 5)

    def test_unknown_parameter_is_refused_with_the_valid_ones(self) -> None:
        """A misspelled parameter exits 2 and lists the tool's real parameters."""
        status, _, err = run_cli(["run", "get_episode", "slugg=x"])
        self.assertEqual(status, cli.REFUSED)
        self.assertIn("slug", err)

    def test_args_json_from_stdin(self) -> None:
        """`--args -` reads the parameters from stdin, which is how long show notes are passed."""
        with mock.patch("mcp_server.server.post_to_x", return_value={"status": "dry_run"}) as post:
            status, _, _ = run_cli(["run", "post_to_x", "--args", "-"], stdin='{"text": "long text", "dry_run": true}')
        self.assertEqual(status, 0)
        self.assertEqual(post.call_args.kwargs["text"], "long text")

    def test_bad_args_json_is_refused(self) -> None:
        """Invalid JSON for --args exits 2 instead of a traceback."""
        status, _, err = run_cli(["run", "podcast_status", "--args", "{nope"])
        self.assertEqual(status, cli.REFUSED)
        self.assertIn("--args", err)


class WorkflowTests(unittest.TestCase):
    """`workflow` is the safety rule plus the server's own instructions."""

    def test_workflow_starts_with_the_safety_rule_and_includes_the_instructions(self) -> None:
        """The CLI cannot show different rules from MCP, and the --yes rule comes first."""
        status, out, _ = run_cli(["workflow"])
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith(cli.CLI_SAFETY_RULE))
        self.assertIn(INSTRUCTIONS, out)


if __name__ == "__main__":
    unittest.main()
