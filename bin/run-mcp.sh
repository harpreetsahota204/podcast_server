#!/usr/bin/env bash
# Starts the MCP server over stdio using this checkout's virtualenv; works from any clone location.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT"
exec "$ROOT/.venv/bin/python" "$ROOT/mcp_server/server.py"
