# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""One place that talks to the Claude API: structured JSON output against a schema."""

from __future__ import annotations

import json
from typing import Any

_DROP = {"default", "maxLength", "minLength", "title", "examples"}


def strict_schema(schema: Any) -> Any:
    """Make a pydantic JSON schema acceptable for structured outputs.

    Objects get ``additionalProperties: false`` and all properties required (optional
    ones stay nullable), and keywords the validator does not support are dropped.
    """
    if isinstance(schema, list):
        return [strict_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: strict_schema(v) for k, v in schema.items() if k not in _DROP}
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def call_json(
    system: str, user: str, schema: dict[str, Any], model: str, max_tokens: int = 8000
) -> tuple[dict[str, Any], dict[str, int]]:
    import anthropic

    msg = anthropic.Anthropic().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": strict_schema(schema)}},
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(f"output truncated at {max_tokens} tokens")
    return json.loads(text), usage
