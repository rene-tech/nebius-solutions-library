from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_pod_security_receipts.py"

EDGES = (
    ("unmanaged", "exception-ready"),
    ("exception-ready", "reference-data-ready"),
    ("reference-data-ready", "legacy-clean"),
    ("legacy-clean", "baseline-ready"),
    ("baseline-ready", "baseline-enforced"),
    ("baseline-enforced", "enforcement-removed"),
    ("enforcement-removed", "host-agents-restored"),
)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def observations(state: str, context: dict[str, object]) -> dict[str, object]:
    if state == "exception-ready":
        return {
            "host_agents": [
                "dcgm-exporter",
                "gpu-allocation-observer",
                "otel-node",
                "prometheus-node-exporter",
            ],
            "legacy_agents_ready": True,
        }
    if state == "reference-data-ready":
        tree = context["dataset"]["tree_sha256"]  # type: ignore[index]
        return {
            "pvc_bound": True,
            "status_ready": True,
            "read_only_probe_passed": True,
            "source_tree_sha256": tree,
            "target_tree_sha256": tree,
        }
    if state == "legacy-clean":
        return {
            "removed_uids": ["legacy-object-uid"],
            "remaining_network_policies": [],
            "remaining_service_accounts": [],
            "remaining_daemonsets": [],
        }
    if state == "baseline-ready":
        return {
            "scientific_namespaces": context["scientific_namespaces"],
            "inventory_sha256": "a" * 64,
            "reference_host_paths": 0,
            "baseline_incompatible_objects": 0,
            "unauthorized_exception_objects": 0,
        }
    if state == "baseline-enforced":
        return {"labels_match": True, "privileged_probe_rejected": True, "positive_smoke_passed": True}
    if state == "enforcement-removed":
        return {"baseline_labels_removed": True, "exception_agents_ready": True}
    if state == "host-agents-restored":
        return {
            "restored_agents": [
                "dcgm-exporter",
                "gpu-allocation-observer",
                "otel-node",
                "prometheus-node-exporter",
            ],
            "exception_agents_ready": True,
        }
    raise AssertionError(state)


@pytest.fixture()
def authority(tmp_path: Path) -> tuple[Path, Path]:
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", private_key], check=True)
    subprocess.run(["openssl", "pkey", "-in", private_key, "-pubout", "-out", public_key], check=True)
    return private_key, public_key


@pytest.fixture()
def context() -> dict[str, object]:
    return {
        "cluster_id": "mk8scluster-test",
        "run_id": "sai07-test",
        "kube_system_uid": "kube-system-uid",
        "deployment_nonce": "sai07-test-20260916",
        "exception_admission_sha256": "d" * 64,
        "psa_version": "v1.35",
        "scientific_namespaces": [
            "fs2-bioir-boltz2",
            "fs2-bioir-coverage",
            "fs2-bioir-openfold",
            "fs2-bioir-protenix",
            "fs2-bioir-snapshot",
        ],
        "pvc": {
            "namespace": "fs2-reference-data",
            "name": "fs2-reference-data-rwx",
            "uid": "pvc-test-uid",
            "storage_class": "fs2-reference-data-retained-sc",
        },
        "dataset": {"id": "alphafold3-public-databases-v3.0", "revision": "20230311", "tree_sha256": "b" * 64},
        "storage": {
            "filesystem_id": "computefilesystem-test",
            "capacity_gib": 2048,
            "claim_size_gib": 1611,
            "forbid_deletion": True,
            "retention_mode": "retain",
        },
    }


def sign(private_key: Path, transition: dict[str, object], tmp_path: Path) -> dict[str, object]:
    message = tmp_path / f"message-{transition['sequence']}.json"
    signature = tmp_path / f"signature-{transition['sequence']}.bin"
    unsigned = dict(transition)
    message.write_bytes(canonical(unsigned))
    subprocess.run(
        ["openssl", "pkeyutl", "-sign", "-inkey", private_key, "-rawin", "-in", message, "-out", signature],
        check=True,
    )
    return {
        **transition,
        "signature": {"algorithm": "ed25519", "key_id": "test-authority", "value": base64.b64encode(signature.read_bytes()).decode()},
    }


def bundle_for(
    tmp_path: Path,
    private_key: Path,
    context: dict[str, object],
    terminal: str,
) -> Path:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    transitions: list[dict[str, object]] = []
    prior_digest: str | None = None
    for sequence, (from_state, to_state) in enumerate(EDGES, start=1):
        transition = sign(
            private_key,
            {
                "receipt_id": f"receipt-{sequence}",
                "sequence": sequence,
                "from_state": from_state,
                "to_state": to_state,
                "prior_receipt_sha256": prior_digest,
                "issued_at": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                "observations": observations(to_state, context),
            },
            tmp_path,
        )
        transitions.append(transition)
        prior_digest = hashlib.sha256(canonical(transition)).hexdigest()
        if to_state == terminal:
            break
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/pod-security-rollout-receipts/v2",
                "key_id": "test-authority",
                "context": context,
                "transitions": transitions,
            }
        )
    )
    return path


def verify(path: Path, public_key: Path, context: dict[str, object], phase: str) -> subprocess.CompletedProcess[str]:
    query = {
        "receipt_path": str(path),
        "public_key_path": str(public_key),
        "public_key_sha256": hashlib.sha256(public_key.read_bytes()).hexdigest(),
        "expected_context": json.dumps(context, sort_keys=True, separators=(",", ":")),
        "expected_phase": phase,
    }
    return subprocess.run(
        [sys.executable, VERIFIER],
        input=json.dumps(query),
        check=False,
        capture_output=True,
        text=True,
    )


def test_valid_chain_authorizes_only_its_exact_terminal_phase(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    private_key, public_key = authority
    path = bundle_for(tmp_path, private_key, context, "baseline-ready")
    accepted = verify(path, public_key, context, "enforce")
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["terminal_state"] == "baseline-ready"
    skipped = verify(path, public_key, context, "rollback-remove-exception")
    assert skipped.returncode != 0
    assert "requires terminal state host-agents-restored" in skipped.stderr


def test_each_phase_consumes_only_the_prior_completed_state(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    private_key, public_key = authority
    expected = {
        "migrate-reference-data": "exception-ready",
        "cleanup-legacy-resources": "reference-data-ready",
        "enforce": "baseline-ready",
        "rollback-remove-enforcement": "baseline-enforced",
        "rollback-restore-host-agents": "enforcement-removed",
        "rollback-remove-exception": "host-agents-restored",
    }
    for phase, terminal in expected.items():
        path = bundle_for(tmp_path, private_key, context, terminal)
        accepted = verify(path, public_key, context, phase)
        assert accepted.returncode == 0, f"{phase}: {accepted.stderr}"
        assert json.loads(accepted.stdout)["terminal_state"] == terminal


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["context"].update(run_id="replayed-run"),
        lambda value: value["context"]["pvc"].update(uid="other-pvc-uid"),
        lambda value: value["context"]["dataset"].update(tree_sha256="c" * 64),
        lambda value: value["transitions"][1].update(prior_receipt_sha256="d" * 64),
        lambda value: value["transitions"][1].update(from_state="unmanaged"),
    ],
)
def test_tamper_replay_and_phase_skip_are_rejected(
    tmp_path: Path,
    authority: tuple[Path, Path],
    context: dict[str, object],
    mutation,
) -> None:
    private_key, public_key = authority
    path = bundle_for(tmp_path, private_key, context, "baseline-ready")
    value = json.loads(path.read_text())
    mutation(value)
    path.write_text(json.dumps(value))
    rejected = verify(path, public_key, context, "enforce")
    assert rejected.returncode != 0


def test_arbitrary_hex_is_not_a_receipt(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    _, public_key = authority
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"sha256": "b" * 64}))
    rejected = verify(path, public_key, context, "enforce")
    assert rejected.returncode != 0
    assert "fields differ" in rejected.stderr


def test_broad_capacity_and_nonretained_storage_are_rejected(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    private_key, public_key = authority
    context["storage"]["capacity_gib"] = 128  # type: ignore[index]
    path = bundle_for(tmp_path, private_key, context, "exception-ready")
    rejected = verify(path, public_key, context, "migrate-reference-data")
    assert rejected.returncode != 0
    assert "integer >= 1611" in rejected.stderr


def test_baseline_gate_requires_zero_hostpaths_and_incompatible_objects(
    tmp_path: Path, authority: tuple[Path, Path], context: dict[str, object]
) -> None:
    private_key, public_key = authority
    path = bundle_for(tmp_path, private_key, context, "baseline-ready")
    value = json.loads(path.read_text())
    value["transitions"][-1]["observations"]["reference_host_paths"] = 103
    path.write_text(json.dumps(value))
    rejected = verify(path, public_key, context, "enforce")
    assert rejected.returncode != 0
    assert "nonzero reference_host_paths" in rejected.stderr
