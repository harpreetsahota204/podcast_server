"""Audience metrics: pluggable collectors, a local SQLite store, analysis, and an HTML dashboard.

Pipeline:
    collectors (OP3, Cloudflare, X, or your own)  →  metrics.db  →  analysis  →  dashboard / MCP tools

Run `uv run python -m metrics.collect` to fetch, then `uv run python -m metrics.report`
to write `$DATA_DIR/metrics/dashboard.html`. Every source is optional; a collector
runs only once its settings are present.
"""
