#!/usr/bin/env python3
"""Payload-free aggregate of the preserved event snapshot (not original-event completeness)."""

from collections import Counter
import gzip
import json
from pathlib import Path
import re
import sys

archive = Path(sys.argv[1])
counts = Counter()
operations = Counter()
debug = Counter()
days = Counter()
for line in gzip.open(archive / "stockholm-database.jsonl.gz", "rt"):
    item = json.loads(line)
    if "table" not in item:
        continue
    table = item["table"]
    row = item["row"]
    counts[table] += 1
    if table == "fs2_operations":
        cohort = (
            "post-remediation-canary"
            if row["principal_id"].startswith("stockholm-canary-")
            else "event-team"
        )
        operations[
            (row["accepted_at"][:10], cohort, row["model_id"], row["status"])
        ] += 1
    elif table == "fs2_request_debug":
        endpoint = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "{id}", row["endpoint"])
        debug[(row["source"], row["method"], endpoint, str(row["http_status"]))] += 1
        days[row["started_at"][:10]] += 1
result = {
    "scope": "retained snapshot only; not a reconstructed complete event",
    "operations": [
        {"day": d, "cohort": c, "model": m, "status": s, "count": n}
        for (d, c, m, s), n in sorted(operations.items())
    ],
    "debug_days": dict(sorted(days.items())),
    "debug_top_paths": [
        {"source": s, "method": m, "endpoint": e, "http_status": code, "count": n}
        for (s, m, e, code), n in debug.most_common(20)
    ],
    "database_rows": dict(counts),
}
(archive / "aggregate.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
