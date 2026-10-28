#!/usr/bin/env bash
# Collects audience metrics from every configured source and rewrites the HTML dashboard.
# Meant for cron or a systemd timer (run `uv sync` once first). Extra args pass through,
# e.g. `collect-metrics.sh --source op3`. Exit code 1 means at least one source failed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m metrics.collect --report "$@"
