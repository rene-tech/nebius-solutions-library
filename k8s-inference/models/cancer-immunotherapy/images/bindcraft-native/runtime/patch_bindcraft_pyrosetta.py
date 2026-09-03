#!/usr/bin/env python3
"""Apply the narrow PyRosetta 2026 DockingPartners API compatibility patch."""

from __future__ import annotations

import sys
from pathlib import Path


IMPORT = "from pyrosetta.rosetta.core.io import pose_from_pose"
PATCHED_IMPORT = IMPORT + "\nfrom pyrosetta.rosetta.core.pose import DockingPartners"
CALL = 'iam.set_interface("A_B")'
PATCHED_CALL = 'iam.set_interface(DockingPartners.docking_partners_from_string("A_B"))'


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_bindcraft_pyrosetta.py PYROSETTA_UTILS")
    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    if source.count(IMPORT) != 1 or source.count(CALL) != 1:
        raise SystemExit("pinned BindCraft PyRosetta interface no longer matches the compatibility patch")
    source = source.replace(IMPORT, PATCHED_IMPORT).replace(CALL, PATCHED_CALL)
    path.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
