#!/usr/bin/env python3
"""Run from any working directory after unpacking the complete delivery bundle."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-output", type=Path, required=True)
    parser.add_argument("--video-output", type=Path)
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    subprocess.run([sys.executable, str(directory / "code" / "compare.py"), "--spec", str(directory / "spec.json"), "--output", str(args.analysis_output.resolve())], check=True)
    if args.video_output:
        subprocess.run([sys.executable, str(directory / "code" / "render.py"), str(args.analysis_output.resolve()), "--output", str(args.video_output.resolve())], check=True)


if __name__ == "__main__":
    main()
