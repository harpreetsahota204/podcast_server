#!/usr/bin/env bash
# Starts the MCP server over stdio from this checkout's uv environment (run `uv sync` once first).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/mcp_server/server.py"
