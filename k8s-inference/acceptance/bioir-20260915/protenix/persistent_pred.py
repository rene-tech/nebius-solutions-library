#!/usr/bin/env python3
"""Keep original stage validation; use the already qualified localhost bridge."""
import os
import sys

sys.path.insert(0, "/opt/fs2")
import run_protenix

if os.environ.get("FS2_PROTENIX_WORKER_URL") != "http://127.0.0.1:8000":
    raise RuntimeError("benchmark requires an explicit pod-local resident worker")
run_protenix.PROTENIX_CLI = "/mnt/fs2-scientific/worker/protenix_cli_proxy.py"
run_protenix.main(sys.argv[1:])
