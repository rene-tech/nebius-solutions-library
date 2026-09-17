#!/usr/bin/env python3
"""Wait for actual model readiness before starting an inference cohort."""
import json
import time
import urllib.request

started = time.monotonic()
while time.monotonic() - started < 300:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as reply:
            document = json.load(reply)
        if document.get("ready") is True:
            print(json.dumps({"readiness_wait_seconds": time.monotonic() - started,
                              "health": document}), flush=True)
            break
    except OSError:
        pass
    time.sleep(2)
else:
    raise SystemExit("model did not become ready within 300 seconds")
