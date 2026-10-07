"""Owner-authorized recovery of the October 6 failed Lynx job, not a test.

Reuses the qualified October 6 public-API continuation/verification client.
There is deliberately no stop/cancel action. Qualification uses system/qa;
the existing customer credential is used only for this approved recovery.
"""

import argparse
import asyncio
import importlib.util
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "resume", "observe"))
    parser.add_argument("--owner-authorized", action="store_true")
    parser.add_argument("--minimum-segments", type=int, default=3)
    parser.add_argument("--minimum-delivered-ns-per-day", type=float, default=200.0)
    for name in ("key-file", "artifact-client", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    source = (
        Path(__file__).resolve().parent.parent
        / "gromacs-managed-resume-20261006"
        / "continue_customer.py"
    )
    spec = importlib.util.spec_from_file_location("customer_continuation", source)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.SOURCE = "5a6cba05-2de1-43e5-8dc7-a29d5a222d76"
    helper.IDEMPOTENCY_KEY = "lynx-authorized-reliability-recovery-20261007-5a6cba05"
    os.umask(0o077)
    asyncio.run(helper.run(args))


if __name__ == "__main__":
    main()
