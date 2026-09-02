"""Hermes Codex Limits plugin entrypoint.

The agent half intentionally registers no model tools. Its authenticated,
read-only HTTP route is mounted from dashboard/plugin_api.py.
"""


def register(ctx) -> None:
    return None
