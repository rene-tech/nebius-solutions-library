#!/usr/bin/env python3
"""Restore an OCI entrypoint's working directory and non-root identity."""

from __future__ import annotations

import argparse
import os


def launch(command: list[str], directory: str, uid: int, gid: int) -> None:
    if not command:
        raise ValueError("the exact original server command is required")
    if not os.path.isabs(directory):
        raise ValueError("the original working directory must be absolute")
    os.chdir(directory)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.execvp(command[0], command)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.uid <= 0 or args.gid <= 0:
        parser.error("the original runtime identity must be non-root")
    launch(command, args.directory, args.uid, args.gid)


if __name__ == "__main__":
    main()
