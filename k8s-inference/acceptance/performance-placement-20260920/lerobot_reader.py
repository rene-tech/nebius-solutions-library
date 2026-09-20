"""Extract one bounded hash-checked source with the pinned LeRobot environment."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lerobot-customer-20260917"))
from dataset_io import extract_bounded

extract_bounded(Path(sys.argv[1]), Path(sys.argv[2]), sha256=sys.argv[3], max_bytes=256 * 1024**2)
