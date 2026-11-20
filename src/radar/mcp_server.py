# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""MCP server: the knowledge base as read-only tools for any agent (Claude, LangChain, ...).

Run:  python -m radar.mcp_server            (stdio, e.g. for Claude Desktop / Claude Code)
      python -m radar.mcp_server --http     (streamable HTTP on :8765)
"""

from __future__ import annotations

import argparse
from functools import lru_cache
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from radar import kb
from radar.eligibility import HireProfile
from radar.store import Store

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

server = MCPServer(
    name="agevolazioni-radar",
    instructions=(
        "Knowledge base of Italian hiring incentives built from INPS circulars and messages. "
        "Every rule field carries the paragraph anchor and the verbatim quote it was read from. "
        "Use search_passages for open questions and always cite doc + anchor. check_eligibility is a "
        "pre-screening for a consulente del lavoro, not a decision."
    ),
)


@lru_cache(maxsize=1)
def _store() -> Store:
    return Store()


def store() -> Store:
    return _store().refresh()


@server.tool(annotations=READ_ONLY)
def search_passages(query: str, k: int = 5) -> list[dict[str, Any]]:
    """Find INPS paragraphs relevant to a question (BM25). Returns doc label, anchor, url and text."""
    return store().search(query, max(1, min(k, 10)))


@server.tool(annotations=READ_ONLY)
def list_incentives() -> list[dict[str, Any]]:
    """List the incentives extracted so far, with their source act and how many fields are verified."""
    return store().incentives()


@server.tool(annotations=READ_ONLY)
def get_incentive(incentive_id: str) -> dict[str, Any]:
    """Full rule of one incentive: each field has value, status (verified/unverified/absent) and citation."""
    found = store().rule(incentive_id)
    if not found:
        return {
            "error": f"unknown incentive_id {incentive_id!r}",
            "known": [i["incentive_id"] for i in store().incentives()],
        }
    rule, meta = found
    return {
        "rule": rule.model_dump(),
        "source": {
            "doc": f"{meta['doc_type']} n. {meta['number']}",
            "published_on": meta["published_on"],
            "url": meta["url"],
        },
        "sources": [x["label"] for x in meta["sources"]],
        "field_sources": {f: x["label"] for f, x in meta["field_sources"].items()},
        "differences_to_review": meta["differences"],
    }


@server.tool(annotations=READ_ONLY)
def check_eligibility(incentive_id: str, hire: HireProfile) -> dict[str, Any]:
    """Pre-screen one hire against one incentive. Each criterion: pass / fail / unknown, with citation."""
    out = store().check(incentive_id, hire)
    return out or {"error": f"unknown incentive_id {incentive_id!r}"}


@server.tool(annotations=READ_ONLY)
def diff_versions(doc_id: int) -> list[dict[str, Any]]:
    """Paragraph-level changes between successive versions of an INPS act (empty if never changed)."""
    with kb.connect(store().db_path, read_only=True) as con:
        return kb.diff_versions(con, doc_id)


@server.tool(annotations=READ_ONLY)
def kb_status() -> dict[str, Any]:
    """Freshness of the knowledge base: newest act seen, counts, days since the newest act."""
    return store().freshness


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true")
    args = ap.parse_args()
    if args.http:
        # No authentication: bind to loopback only; expose through an authenticating proxy if ever needed.
        server.run("streamable-http", host="127.0.0.1", port=8765)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
