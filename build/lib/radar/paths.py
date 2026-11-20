# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Project home: where registry/, examples/, evals/ and data/ live.

From a source checkout it is the repository root; an installed package (Docker) sets
RADAR_HOME, because the module then lives in site-packages, far from those folders.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("RADAR_HOME", Path(__file__).resolve().parents[2]))
