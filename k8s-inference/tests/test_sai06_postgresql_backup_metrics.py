from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT / "stages/workloads/scripts/postgresql_backup_metrics.py"
)
SPEC = importlib.util.spec_from_file_location("postgresql_backup_metrics", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
METRICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METRICS)


def test_version_inventory_counts_current_noncurrent_and_restore_receipt() -> None:
    pages = [
        {
            "Versions": [
                {
                    "Key": "postgresql/v1/base/one",
                    "Size": 100,
                    "IsLatest": True,
                    "LastModified": datetime(2026, 9, 16, 2, tzinfo=UTC),
                },
                {
                    "Key": "postgresql/v1/base/one",
                    "Size": 80,
                    "IsLatest": False,
                    "LastModified": datetime(2026, 9, 15, 2, tzinfo=UTC),
                },
                {
                    "Key": (
                        "postgresql/v1/restore-verification/success/"
                        "sai06-20260916T030000Z.json"
                    ),
                    "Size": 16,
                    "IsLatest": True,
                    "LastModified": datetime(2026, 9, 16, 3, tzinfo=UTC),
                },
            ],
            "DeleteMarkers": [
                {
                    "Key": "postgresql/v1/old",
                    "IsLatest": True,
                    "LastModified": datetime(2026, 9, 14, tzinfo=UTC),
                }
            ],
        }
    ]

    summary = METRICS.summarize_versions(
        pages,
        prefix="postgresql/v1/",
        capacity_bytes=400,
    )

    assert summary["usage_bytes"] == 196
    assert summary["current_bytes"] == 116
    assert summary["noncurrent_bytes"] == 80
    assert summary["version_count"] == 3
    assert summary["delete_marker_count"] == 1
    assert summary["usage_ratio"] == 0.49
    assert summary["restore_last_success_timestamp_seconds"] == datetime(
        2026, 9, 16, 3, tzinfo=UTC
    ).timestamp()


def test_metrics_output_is_numeric_and_never_contains_object_keys() -> None:
    summary = {
        "usage_bytes": 196,
        "current_bytes": 116,
        "noncurrent_bytes": 80,
        "capacity_bytes": 400,
        "usage_ratio": 0.49,
        "version_count": 3,
        "delete_marker_count": 1,
        "restore_last_success_timestamp_seconds": 1_789_527_600.0,
    }

    rendered = METRICS.render_metrics(
        summary,
        scrape_success=True,
        last_success_timestamp=1_789_527_601.0,
        errors_total=0,
    )

    assert "fs2_postgresql_backup_bucket_usage_bytes 196" in rendered
    assert "fs2_postgresql_backup_bucket_scrape_success 1" in rendered
    assert "fs2_postgresql_restore_last_success_timestamp_seconds" in rendered
    assert "postgresql/v1" not in rendered
    assert "sai06-20260916" not in rendered
