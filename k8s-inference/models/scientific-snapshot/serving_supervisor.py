#!/usr/bin/env python3
"""Serving-only backing-files preparation around the common supervisor.

The scientific bundle source identity remains independent of this optional
vLLM filesystem closure. Error logs are printed before a failed Pod's emptyDir
is reclaimed, so a failed restore remains diagnosable without host access.
"""

from pathlib import Path
import sys

from serving_filesystem import restore
import supervisor


def main():
    arguments = sys.argv[1:]
    try:
        if "restore" in arguments:
            source = Path(arguments[arguments.index("--source-directory") + 1])
            restore(source)
        supervisor.main()
    finally:
        log = Path("/tmp/fs2-checkpoint-work/restore.log")
        if log.is_file():
            for line in log.read_text(errors="replace").splitlines():
                if "Error" in line:
                    print("CRIU_RESTORE_ERROR " + line, flush=True)


if __name__ == "__main__":
    main()
