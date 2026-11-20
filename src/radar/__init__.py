# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""agevolazioni-radar: INPS hiring incentives as a versioned, cited knowledge base."""

from __future__ import annotations

import os
from pathlib import Path


def _load_local_env(path: Path = Path(__file__).resolve().parents[2] / ".env.local") -> None:
    """Read KEY=VALUE lines from the git-ignored .env.local; never overrides the real environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()
