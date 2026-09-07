import importlib.util
import shutil
import tempfile
from pathlib import Path

SOURCE = Path(__file__).with_name("validate_diffdock.py")
spec = importlib.util.spec_from_file_location("validate_diffdock_snapshot", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def summary():
    cases = [
        {
            "ok": True,
            "random_seed": seed,
            "request_sha256": str(seed) * 16,
            "response_sha256": str(seed + 1) * 16,
            "invariant": {
                "protein_sha256": "a" * 64,
                "pose": {"finite_coordinate_count": 13},
            },
        }
        for seed in module.SEEDS
    ]
    return {
        "validator": "diffdock-snapshot-semantic-v1",
        "native_validator_sha256": module.EXPECTED_VALIDATOR_SHA256,
        "fixture_sha256": module.EXPECTED_FIXTURE_SHA256,
        "ok": True,
        "status": "PASS",
        "passed_case_count": 2,
        "cases": cases,
    }


def test_archive_binds_exact_native_validator_and_fixture():
    assert len(module.validator_archive()) > module.FIXTURE.stat().st_size


def test_remote_bridge_imports_from_a_shallow_temporary_directory():
    with tempfile.TemporaryDirectory(
        prefix="fs2-diffdock-validator.", dir="/tmp"
    ) as directory:
        bridge = Path(directory) / "validate_diffdock_snapshot.py"
        shutil.copyfile(SOURCE, bridge)
        remote_spec = importlib.util.spec_from_file_location(
            "diffdock_snapshot_remote", bridge
        )
        remote = importlib.util.module_from_spec(remote_spec)
        remote_spec.loader.exec_module(remote)
        assert remote.ROOT is None


def test_projection_requires_two_distinct_accepted_public_seed_payloads():
    projected = module.project_summary(summary())
    assert projected["passed"]
    assert [item["random_seed"] for item in projected["requests"]] == [2370, 2371]

    changed = summary()
    changed["cases"][1]["request_sha256"] = changed["cases"][0]["request_sha256"]
    assert not module.project_summary(changed)["passed"]


def test_projection_rejects_changed_native_validator_identity():
    changed = summary()
    changed["native_validator_sha256"] = "0" * 64
    assert not module.project_summary(changed)["passed"]
