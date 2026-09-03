#!/usr/bin/env python3
"""Measure whole-bundle serialization against per-object claims.

Both modes run the same publisher. ``serialized`` reproduces the previous
behaviour by holding the bundle lock exclusively around each worker, which is
exactly what a pre-per-object worker does; ``per-object`` lets workers claim
individual objects. The comparison therefore isolates the locking scheme.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

REFERENCE_DATA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REFERENCE_DATA))

import reference_data  # noqa: E402


def build_bundle(work: Path, objects: int, megabytes: int) -> Path:
    sources = work / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    catalog_objects = []
    for index in range(objects):
        # Incompressible content, so decompression and hashing cost real time
        # and every object is the same size: that also exercises adoption
        # against a same-sized sibling blob.
        raw = os.urandom(megabytes * 1024 * 1024)
        path = sources / f"object-{index:03d}.gz"
        with gzip.open(path, "wb", compresslevel=1) as handle:
            handle.write(raw)
        payload = path.read_bytes()
        catalog_objects.append({
            "id": f"object-{index:03d}",
            "source": {"url": path.resolve().as_uri()},
            "target": f"database/object-{index:03d}.dat",
            "transform": "gzip",
            "source_bytes": len(payload),
            "source_integrity": {
                "algorithm": "sha256",
                "digest": hashlib.sha256(payload).hexdigest(),
                "cryptographic": True,
            },
            "license_component": "benchmark",
        })
    catalog = {
        "schema": reference_data.CATALOG_SCHEMA,
        "generated_at": "2026-09-03T00:00:00Z",
        "bundles": {
            "benchmark": {
                "id": "benchmark",
                "revision": "benchmark-2026-09-03",
                "description": "Synthetic multi-object staging benchmark.",
                "upstream": {
                    "project": "benchmark/reference-data",
                    "revision": "0" * 40,
                    "source_url": "https://example.invalid/source",
                    "source_sha256": "1" * 64,
                },
                "access": {
                    "state": "public",
                    "redistribution": "review-required",
                    "staging_policy": "automatic-public",
                    "terms": [{
                        "component": "benchmark",
                        "license": "test-only",
                        "url": "https://example.invalid/terms",
                        "verification": "upstream-terms-review-required",
                    }],
                },
                "sizing": {
                    "compressed_bytes": sum(int(item["source_bytes"]) for item in catalog_objects),
                    "expanded_bytes": sum(int(item["source_bytes"]) for item in catalog_objects),
                    "expanded_bytes_kind": "exact",
                },
                "update_policy": {
                    "cadence": "immutable benchmark fixture",
                    "mutable_aliases_allowed": False,
                    "promotion": "new-revision-after-offline-validation",
                },
                "objects": catalog_objects,
            }
        },
    }
    path = work / "catalog.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return path


def worker(catalog_path: str, root: str, serialized: bool, queue: object) -> None:
    started = time.monotonic()
    try:
        if serialized:
            # A separate mutex, so this emulation of "one worker owns the whole
            # bundle" never contends with the publisher's own bundle lock.
            lock_path = Path(root) / "benchmark-serialized.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                _manifest, digest = reference_data.stage_bundle(
                    Path(catalog_path), "benchmark", Path(root)
                )
        else:
            _manifest, digest = reference_data.stage_bundle(
                Path(catalog_path), "benchmark", Path(root)
            )
        queue.put({"ok": True, "digest": digest, "seconds": time.monotonic() - started,
                   "pid": os.getpid()})
    except BaseException as exc:  # noqa: BLE001 - reported to the parent
        queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "seconds": time.monotonic() - started, "pid": os.getpid()})


def run(catalog_path: Path, work: Path, workers: int, serialized: bool) -> dict[str, object]:
    root = work / ("serialized" if serialized else "per-object")
    if root.exists():
        shutil.rmtree(root)
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    processes = [
        context.Process(target=worker, args=(str(catalog_path), str(root), serialized, queue))
        for _ in range(workers)
    ]
    started = time.monotonic()
    for process in processes:
        process.start()
    # Drain before joining: a child blocked on a full pipe would never exit.
    results = [queue.get(timeout=1800) for _ in processes]
    for process in processes:
        process.join(timeout=120)
    elapsed = time.monotonic() - started
    telemetry = root / "telemetry" / "benchmark.localization.json"
    dispositions = (
        reference_data.load_json(telemetry)["dispositions"] if telemetry.is_file() else {}
    )
    return {
        "mode": "serialized" if serialized else "per-object",
        "workers": workers,
        "wall_clock_seconds": round(elapsed, 3),
        "worker_seconds": sorted(round(float(item["seconds"]), 3) for item in results),
        "all_ok": all(item["ok"] for item in results),
        "distinct_manifests": len({item.get("digest") for item in results}),
        "errors": [item.get("error") for item in results if not item["ok"]],
        "objects_downloaded": sum(1 for value in dispositions.values() if value == "downloaded"),
        "objects_total": len(dispositions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objects", type=int, default=8)
    parser.add_argument("--megabytes", type=int, default=24)
    parser.add_argument("--workers", type=int, default=2)
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fs2-staging-benchmark-") as temporary:
        work = Path(temporary)
        catalog_path = build_bundle(work, arguments.objects, arguments.megabytes)
        before = run(catalog_path, work, arguments.workers, serialized=True)
        after = run(catalog_path, work, arguments.workers, serialized=False)
    speedup = (
        round(float(before["wall_clock_seconds"]) / float(after["wall_clock_seconds"]), 2)
        if float(after["wall_clock_seconds"]) else None
    )
    print(json.dumps({
        "objects": arguments.objects,
        "megabytes_each": arguments.megabytes,
        "before": before,
        "after": after,
        "speedup": speedup,
    }, indent=2, sort_keys=True))
    return 0 if before["all_ok"] and after["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
