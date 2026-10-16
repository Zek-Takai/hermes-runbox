"""A one-line per-turn note so the agent knows the box exists (plugin tools are deferred)."""
from __future__ import annotations

RULE = ("[runbox] To run, check (lint/syntax) or test Python code, use the `runbox` tool: an offline "
        "container for this chat; failures come back as file:line issues. If it is not directly "
        "callable, load it with tool_describe(names=[\"runbox\"]).")


def reminder(enabled: bool, **_kw):
    return {"context": RULE} if enabled else None
