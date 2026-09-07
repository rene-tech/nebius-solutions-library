"""Propagate only the explicitly selected snapshot loop to spawned workers."""

import os

if os.environ.get("FS2_SNAPSHOT_ASYNCIO_LOOP") == "1":
    from serving_launcher import configure_loop

    configure_loop()
