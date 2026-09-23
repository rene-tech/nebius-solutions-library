import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root / "lammps/runtime"), str(root / "gromacs/runtime")]
