#!/usr/bin/env python3
"""Run the secret-safe all-model public HTTP and MCP acceptance harness."""

from __future__ import annotations

import sys
from pathlib import Path

# Operator invocations run this source script directly. Prefer the adjacent
# checked-out packages over any older wheel installed in the selected Python
# environment so release identity and acceptance contracts cannot diverge.
_CONTROL_ROOT = Path(__file__).resolve().parents[1]
_SOLUTION_ROOT = _CONTROL_ROOT.parents[1]
sys.path[:0] = [str(_CONTROL_ROOT / "src"), str(_SOLUTION_ROOT / "catalog/runtime")]

from fs2_serve.live_acceptance import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
