from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "primary_isolated_runs", HERE / "primary_isolated_runs.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PrimaryIsolatedRunsTest(unittest.TestCase):
    def test_materialize_and_materialize_many_are_parsed(self) -> None:
        one = [
            "fs2-serve",
            "scientific-materialize",
            "--artifact-id",
            "artifact-a",
            "--expected-size-bytes",
            "5",
        ]
        self.assertEqual(
            MODULE.materializations(one),
            [["--artifact-id", "artifact-a", "--expected-size-bytes", "5"]],
        )
        commands = [["--artifact-id", "artifact-b", "--expected-size-bytes", "7"]]
        many = [
            "fs2-serve",
            "scientific-materialize-many",
            "--commands-json",
            json.dumps(commands),
        ]
        self.assertEqual(MODULE.materializations(many), commands)

    def test_load_jobs_accepts_list_and_individual_job_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.json").write_text(
                json.dumps({"kind": "List", "items": [{"kind": "Job", "id": "a"}]}),
                encoding="utf-8",
            )
            (root / "b.json").write_text(
                json.dumps({"kind": "Job", "id": "b"}), encoding="utf-8"
            )
            self.assertEqual(
                [item["id"] for item in MODULE.load_jobs(root)], ["a", "b"]
            )

    def test_seconds_uses_utc_boundaries(self) -> None:
        self.assertEqual(
            MODULE.seconds("2026-09-07T05:00:00Z", "2026-09-07T05:00:01.25Z"),
            1.25,
        )

    def test_marker_parser_ignores_nested_or_trailing_json(self) -> None:
        valid = {
            "phase": "model_ready",
            "model_id": "mosaic",
            "utc": "2026-09-07T05:00:00+00:00",
            "monotonic_seconds": 1.0,
        }
        log = (
            "2026-09-07T05:00:00Z progress FS2_STARTUP "
            + json.dumps(valid)
            + "\n"
            + 'report={"message":"FS2_STARTUP {\\"phase\\":\\"fake\\"}"}\n'
            + "FS2_STARTUP "
            + json.dumps(valid)
            + " trailing\n"
        )
        self.assertEqual(MODULE.parse_markers(log), [valid])


if __name__ == "__main__":
    unittest.main()
