"""``podcast-server``: the command line twin of the MCP server, for humans, scripts and cron.

Every MCP tool is reachable from here without a hand-written subcommand per tool: the CLI talks to the same FastMCP
server in memory (``fastmcp.Client``), so arguments are validated, tools run and errors read exactly as they do for an
agent, and a tool added to ``mcp_server/server.py`` appears here with no CLI change.

    podcast-server tools [--public]                  list every tool with its kind (read, local, public)
    podcast-server describe podcast_get_episode      description, parameters and kind of one tool
    podcast-server run podcast_status                run a tool
    podcast-server run get_episode slug=my-episode   `podcast_` is optional; key=value (JSON values allowed)
    podcast-server run podcast_publish --args @pkg.json --yes     arguments from a file (or `-` for stdin)
    podcast-server workflow                          the agent rules (what the MCP server tells agents)
    podcast-server mcp                               run the MCP server over stdio

Public tools (the ones that change what the world sees: publish, post, delete, deploy, send) are refused unless you
pass ``--yes`` (or the tool's own ``dry_run=true``). ``--yes`` means a human approved the exact content.

Every command prints JSON. A refusal prints ``[error] message`` to stderr and exits with status 2.
"""

import argparse
import asyncio
import difflib
import json
import logging
import sys
import textwrap
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.exceptions import ToolError

# Said first by `workflow`: MCP clients prompt before a public action; the CLI has only the --yes flag.
CLI_SAFETY_RULE = (
    "CLI safety rule: a public tool (publish, post, delete, deploy, send; `podcast-server tools --public`) is refused\n"
    "unless you pass --yes, or the tool's own dry_run=true. Run dry_run=true first where the tool has it, show the\n"
    "human the exact content, and pass --yes only after they approved it."
)

# Exit status for a refusal or tool error (the same convention as the other engines' CLIs).
REFUSED = 2


def _server() -> Any:
    """The FastMCP server object, imported on demand so ``--help`` stays fast and a broken config only hurts tool use."""
    from mcp_server.server import mcp

    return mcp


def _kind(tool: Any) -> str:
    """Classify a tool from its MCP annotations: ``read``, ``local`` (writes files only) or ``public``.

    ``public`` is destructive *and* open-world: it changes something outside this machine that the public can see,
    which is exactly the set the server's instructions call "public tools". Anything read-only is ``read``.
    """
    hints = tool.annotations
    if hints is None:
        return "local"
    if hints.read_only_hint:
        return "read"
    return "public" if hints.destructive_hint and hints.open_world_hint else "local"


def _first_sentence(text: str) -> str:
    """The description's first sentence, trimmed to one line for the tool list."""
    one_line = " ".join((text or "").split())
    sentence = one_line.split(". ")[0].rstrip(".")
    return sentence if len(sentence) <= 110 else sentence[:107] + "..."


def _resolve_name(name: str, tools: dict[str, Any]) -> str:
    """The full tool name for ``name`` (the ``podcast_`` prefix is optional); a refusal with suggestions if unknown."""
    for candidate in (name, f"podcast_{name}"):
        if candidate in tools:
            return candidate
    close = difflib.get_close_matches(name, list(tools), n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else " Run `podcast-server tools` to list them."
    raise CliError(f"no tool named {name!r}.{hint}")


class CliError(Exception):
    """A refusal the user can act on; ``main`` prints it as ``[error] message`` and exits with status 2."""


def _schema_types(prop: dict[str, Any]) -> set[str]:
    """All JSON types a parameter accepts (``anyOf`` unions included), for deciding how to read ``key=value``."""
    types: set[str] = set()
    if "type" in prop:
        types.add(prop["type"])
    for option in prop.get("anyOf", []):
        types |= _schema_types(option)
    return types


def _coerce(raw: str, prop: dict[str, Any]) -> Any:
    """Turn the text after ``key=`` into the value the tool expects.

    A parameter that is only ever a string (``slug``, ``title``) keeps its text as typed, so ``slug=2024`` is not read
    as the number 2024. Every other parameter is parsed as JSON (``limit=5``, ``dry_run=true``, ``tags=["a","b"]``),
    falling back to the raw text if it is not valid JSON.
    """
    if _schema_types(prop) <= {"string", "null"} and "string" in _schema_types(prop):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _load_args_json(source: str | None) -> dict[str, Any]:
    """Read ``--args``: inline JSON, ``-`` for stdin, or ``@path`` for a JSON file; always an object."""
    if source is None:
        return {}
    try:
        if source == "-":
            text = sys.stdin.read()
        elif source.startswith("@"):
            text = Path(source[1:]).expanduser().read_text(encoding="utf-8")
        else:
            text = source
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as err:
        raise CliError(f"--args is not readable JSON: {err}") from err
    if not isinstance(data, dict):
        raise CliError("--args must be one JSON object of parameter names to values.")
    return data


def _build_arguments(tool: Any, args_json: str | None, pairs: list[str]) -> dict[str, Any]:
    """Merge ``--args`` and ``key=value`` pairs into the tool's argument dict (pairs win), refusing unknown names."""
    arguments = _load_args_json(args_json)
    properties = tool.input_schema.get("properties", {})
    for pair in pairs:
        key, separator, raw = pair.partition("=")
        if not separator:
            raise CliError(f"{pair!r} is not key=value. Parameters are passed as name=value (see `describe {tool.name}`).")
        if key not in properties:
            raise CliError(f"{tool.name} has no parameter {key!r}. Its parameters: {', '.join(properties) or 'none'}.")
        arguments[key] = _coerce(raw, properties[key])
    return arguments


def _is_dry_run(arguments: dict[str, Any]) -> bool:
    """Whether the call asks the tool itself for a dry run, which posts, deletes and publishes nothing."""
    return arguments.get("dry_run") is True


def _result_payload(result: Any) -> Any:
    """What a tool returned, as plain data: structured content, with MCP's ``{"result": x}`` wrapper removed."""
    payload = result.structured_content
    if payload is None:
        return result.data
    if isinstance(payload, dict) and list(payload) == ["result"]:
        return payload["result"]
    return payload


async def _list_tools() -> dict[str, Any]:
    """Every tool the server advertises, by name, as a client would see them."""
    async with Client(_server()) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def _call(name: str, arguments: dict[str, Any]) -> Any:
    """Call one tool through the in-memory client, so validation and errors match what agents get."""
    async with Client(_server()) as client:
        return await client.call_tool(name, arguments)


def _print(value: Any) -> None:
    """Print a result as JSON."""
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def cmd_tools(args: argparse.Namespace) -> int:
    """List every tool with its kind and a one-line summary (``--public`` keeps only the public ones)."""
    tools = asyncio.run(_list_tools())
    rows = [(name, _kind(tool), _first_sentence(tool.description or "")) for name, tool in tools.items()]
    if args.public:
        rows = [row for row in rows if row[1] == "public"]
    for name, kind, summary in rows:
        print(f"{name:<36} {kind:<7} {summary}")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    """Print one tool's kind, full description and parameters (type, required or default, description)."""
    tools = asyncio.run(_list_tools())
    tool = tools[_resolve_name(args.tool, tools)]
    schema = tool.input_schema
    required = set(schema.get("required", []))
    parameters = []
    for name, prop in schema.get("properties", {}).items():
        parameters.append(
            {
                "name": name,
                "type": " | ".join(sorted(_schema_types(prop))) or "any",
                "required": name in required,
                **({} if name in required else {"default": prop.get("default")}),
                **({"description": prop["description"]} if prop.get("description") else {}),
            }
        )
    _print({"tool": tool.name, "kind": _kind(tool), "description": tool.description, "parameters": parameters})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run one tool; public tools need ``--yes`` (or the tool's own ``dry_run=true``)."""
    tools = asyncio.run(_list_tools())
    tool = tools[_resolve_name(args.tool, tools)]
    arguments = _build_arguments(tool, args.args, args.params)
    if _kind(tool) == "public" and not args.yes and not _is_dry_run(arguments):
        raise CliError(
            f"{tool.name} is public (it changes what the world sees) and was not run. "
            "Show the human the exact content and get their approval, then re-run with --yes. "
            "Where the tool has dry_run, run it with dry_run=true first."
        )
    try:
        result = asyncio.run(_call(tool.name, arguments))
    except ToolError as err:
        raise CliError(str(err)) from err
    _print(_result_payload(result))
    return 0


def cmd_workflow(_: argparse.Namespace) -> int:
    """Print the agent rules for CLI-only use: the safety rule, the server's instructions, and the tool-name rule.

    The instructions come from the server module, so the CLI and the MCP server cannot disagree about the rules.
    """
    from mcp_server.server import INSTRUCTIONS

    print(
        f"{CLI_SAFETY_RULE}\n\n{INSTRUCTIONS}\n\n"
        "Using the CLI instead of MCP: the tool names above are the MCP names; run any of them with\n"
        "`podcast-server run <tool> [name=value ...]` (the `podcast_` prefix is optional; --args takes a JSON object,\n"
        "`-` for stdin or @file). `podcast-server tools` lists them, `podcast-server describe <tool>` shows the full\n"
        "description and parameters. Every command prints JSON."
    )
    return 0


def cmd_mcp(_: argparse.Namespace) -> int:
    """Serve the MCP server over stdio until the client disconnects (logs go to stderr, keeping stdout clean)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _server().run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """All subcommands and their arguments."""
    parser = argparse.ArgumentParser(
        prog="podcast-server",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__.split("\n\n", 1)[1]),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    tools = sub.add_parser("tools", help="list every tool with its kind (read, local, public)")
    tools.add_argument("--public", action="store_true", help="only the tools that change what the world sees")
    tools.set_defaults(handler=cmd_tools)
    describe = sub.add_parser("describe", help="a tool's description and parameters")
    describe.add_argument("tool")
    describe.set_defaults(handler=cmd_describe)
    run = sub.add_parser("run", help="run a tool (public tools need --yes)")
    run.add_argument("tool", help="tool name; the `podcast_` prefix is optional")
    run.add_argument("params", nargs="*", metavar="name=value", help="parameters; values may be JSON (limit=5, tags=[...])")
    run.add_argument("--args", help="parameters as a JSON object: inline, `-` for stdin, or @file.json")
    run.add_argument("--yes", action="store_true", help="a human approved this exact public action")
    run.set_defaults(handler=cmd_run)
    sub.add_parser("workflow", help="agent rules (the MCP instructions) and how to run tools").set_defaults(
        handler=cmd_workflow
    )
    sub.add_parser("mcp", help="run the MCP server over stdio").set_defaults(handler=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the command, and return its exit status (a refusal prints ``[error] ...`` and returns 2)."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except CliError as err:
        print(f"[error] {err}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    sys.exit(main())
