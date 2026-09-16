#!/usr/bin/env python3
"""Verify the signed, replay-resistant SAI-07 rollout receipt chain.

The program implements Terraform's external data-source protocol: one JSON
object is read from stdin and one string-valued JSON object is written to
stdout.  It deliberately does not modify a receipt ledger during planning.
Replay protection comes from the signed deployment nonce, exact cluster/run
and object UIDs, expiry, monotonic sequence, unique receipt IDs, and a hash
chain rooted at the fixed initial state.  Re-applying the same receipt to the
same state is idempotent; using it for a different run, object, phase, or later
transition is rejected.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA = "fs2-serve.nebius.ai/pod-security-rollout-receipts/v2"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
UID_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,126}[a-z0-9])?$")
DNS_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
MAX_RECEIPT_AGE = dt.timedelta(hours=24)
MAX_CLOCK_SKEW = dt.timedelta(minutes=5)
MAX_TRANSITIONS = 8

EDGES = {
    "unmanaged": "exception-ready",
    "exception-ready": "reference-data-ready",
    "reference-data-ready": "legacy-clean",
    "legacy-clean": "baseline-ready",
    "baseline-ready": "baseline-enforced",
    "baseline-enforced": "enforcement-removed",
    "enforcement-removed": "host-agents-restored",
    "host-agents-restored": "exception-removed",
}

PHASE_TERMINALS = {
    "prepare": None,
    # A phase consumes the signed terminal state produced by the preceding
    # phase.  This makes the sequence executable: prepare creates the unused
    # retained claim and dual-runs the host agents; only that receipt permits
    # the reference-data cutover.  The cutover receipt then permits cleanup.
    "migrate-reference-data": "exception-ready",
    "cleanup-legacy-resources": "reference-data-ready",
    "enforce": "baseline-ready",
    "rollback-remove-enforcement": "baseline-enforced",
    "rollback-restore-host-agents": "enforcement-removed",
    "rollback-remove-exception": "host-agents-restored",
}

EXPECTED_HOST_AGENTS = (
    "dcgm-exporter",
    "gpu-allocation-observer",
    "otel-node",
    "prometheus-node-exporter",
)


class ReceiptError(ValueError):
    """A receipt is untrusted or does not authorize the requested phase."""


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReceiptError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReceiptError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReceiptError(f"{label} must be an integer >= {minimum}")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ReceiptError(f"{label} fields differ from the canonical schema")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _instant(value: Any, label: str) -> dt.datetime:
    raw = _string(value, label)
    if not raw.endswith("Z"):
        raise ReceiptError(f"{label} must be an RFC3339 UTC instant")
    try:
        parsed = dt.datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as error:
        raise ReceiptError(f"{label} must be an RFC3339 UTC instant") from error
    if parsed.tzinfo != dt.timezone.utc:
        raise ReceiptError(f"{label} must use UTC")
    return parsed


def _load_json(path: Path) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ReceiptError(f"cannot safely open {path.name}: {error.strerror}") from error
    try:
        stat = os.fstat(descriptor)
        if not stat.st_mode & 0o100000:
            raise ReceiptError(f"{path.name} is not a regular file")
        if stat.st_size > 1_048_576:
            raise ReceiptError(f"{path.name} exceeds the 1 MiB receipt limit")
        payload = b""
        while len(payload) <= stat.st_size:
            chunk = os.read(descriptor, min(65536, stat.st_size - len(payload)))
            if not chunk:
                break
            payload += chunk
    finally:
        os.close(descriptor)
    try:
        return _object(json.loads(payload), "receipt bundle")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptError(f"{path.name} is not canonical JSON") from error


def _verify_signature(
    transition: dict[str, Any], public_key: Path, expected_key_id: str
) -> None:
    signature = _object(transition.get("signature"), "transition.signature")
    _exact_keys(signature, {"algorithm", "key_id", "value"}, "transition.signature")
    if signature["algorithm"] != "ed25519" or signature["key_id"] != expected_key_id:
        raise ReceiptError("transition signature authority differs")
    try:
        signature_bytes = base64.b64decode(signature["value"], validate=True)
    except (binascii.Error, TypeError) as error:
        raise ReceiptError("transition signature is not strict base64") from error
    if len(signature_bytes) != 64:
        raise ReceiptError("Ed25519 signature must be exactly 64 bytes")

    signed = dict(transition)
    del signed["signature"]
    with tempfile.TemporaryDirectory(prefix="fs2-psa-receipt-") as directory:
        root = Path(directory)
        message_path = root / "message.json"
        signature_path = root / "signature.bin"
        message_path.write_bytes(_canonical(signed))
        signature_path.write_bytes(signature_bytes)
        os.chmod(message_path, 0o600)
        os.chmod(signature_path, 0o600)
        completed = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(public_key),
                "-rawin",
                "-in",
                str(message_path),
                "-sigfile",
                str(signature_path),
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    if completed.returncode != 0:
        raise ReceiptError("transition signature verification failed")


def _validate_context(context: dict[str, Any], expected: dict[str, Any]) -> None:
    _exact_keys(
        context,
        {
            "cluster_id",
            "run_id",
            "kube_system_uid",
            "deployment_nonce",
            "exception_admission_sha256",
            "psa_version",
            "scientific_namespaces",
            "pvc",
            "dataset",
            "storage",
        },
        "context",
    )
    if context != expected:
        raise ReceiptError("receipt context differs from the exact deployment context")
    if not UID_RE.fullmatch(_string(context["cluster_id"], "context.cluster_id")):
        raise ReceiptError("context.cluster_id is malformed")
    if not DNS_RE.fullmatch(_string(context["run_id"], "context.run_id")):
        raise ReceiptError("context.run_id is malformed")
    if not UID_RE.fullmatch(_string(context["kube_system_uid"], "context.kube_system_uid")):
        raise ReceiptError("context.kube_system_uid is malformed")
    if not UID_RE.fullmatch(_string(context["deployment_nonce"], "context.deployment_nonce")):
        raise ReceiptError("context.deployment_nonce is malformed")
    if not SHA256_RE.fullmatch(
        _string(context["exception_admission_sha256"], "context.exception_admission_sha256")
    ):
        raise ReceiptError("context.exception_admission_sha256 is malformed")
    if not re.fullmatch(r"v1\.[0-9]{1,2}", _string(context["psa_version"], "context.psa_version")):
        raise ReceiptError("context.psa_version must pin one Kubernetes minor")
    namespaces = context["scientific_namespaces"]
    if (
        not isinstance(namespaces, list)
        or not namespaces
        or namespaces != sorted(set(namespaces))
        or not all(isinstance(item, str) and DNS_RE.fullmatch(item) for item in namespaces)
    ):
        raise ReceiptError("scientific namespace inventory must be non-empty, sorted, and unique")

    pvc = _object(context["pvc"], "context.pvc")
    _exact_keys(pvc, {"namespace", "name", "uid", "storage_class"}, "context.pvc")
    if pvc["namespace"] != "fs2-reference-data" or pvc["name"] != "fs2-reference-data-rwx":
        raise ReceiptError("receipt must bind the canonical reference-data claim")
    if not UID_RE.fullmatch(_string(pvc["uid"], "context.pvc.uid")):
        raise ReceiptError("context.pvc.uid is malformed")
    if pvc["storage_class"] != "fs2-reference-data-retained-sc":
        raise ReceiptError("receipt must bind the dedicated retained StorageClass")

    dataset = _object(context["dataset"], "context.dataset")
    _exact_keys(dataset, {"id", "revision", "tree_sha256"}, "context.dataset")
    for key in ("id", "revision"):
        _string(dataset[key], f"context.dataset.{key}")
    if not SHA256_RE.fullmatch(_string(dataset["tree_sha256"], "context.dataset.tree_sha256")):
        raise ReceiptError("context.dataset.tree_sha256 is malformed")

    storage = _object(context["storage"], "context.storage")
    _exact_keys(
        storage,
        {"filesystem_id", "capacity_gib", "claim_size_gib", "forbid_deletion", "retention_mode"},
        "context.storage",
    )
    _string(storage["filesystem_id"], "context.storage.filesystem_id")
    capacity = _integer(storage["capacity_gib"], "context.storage.capacity_gib", 1611)
    claim_size = _integer(storage["claim_size_gib"], "context.storage.claim_size_gib", 1611)
    if claim_size > capacity:
        raise ReceiptError("claim size exceeds the retained filesystem capacity")
    if storage["forbid_deletion"] is not True or storage["retention_mode"] != "retain":
        raise ReceiptError("reference-data storage is not durably retained")


def _validate_observations(state: str, observations: dict[str, Any], context: dict[str, Any]) -> None:
    if state == "exception-ready":
        _exact_keys(observations, {"host_agents", "legacy_agents_ready"}, "exception-ready observations")
        agents = observations["host_agents"]
        if not isinstance(agents, list) or tuple(sorted(agents)) != EXPECTED_HOST_AGENTS:
            raise ReceiptError("exception readiness must cover every exact host agent")
        if observations["legacy_agents_ready"] is not True:
            raise ReceiptError("prepare must retain ready legacy agents until replacements are ready")
    elif state == "reference-data-ready":
        _exact_keys(
            observations,
            {"pvc_bound", "status_ready", "read_only_probe_passed", "source_tree_sha256", "target_tree_sha256"},
            "reference-data-ready observations",
        )
        if not all(observations[key] is True for key in ("pvc_bound", "status_ready", "read_only_probe_passed")):
            raise ReceiptError("reference-data readiness probes did not all pass")
        tree = context["dataset"]["tree_sha256"]
        if observations["source_tree_sha256"] != tree or observations["target_tree_sha256"] != tree:
            raise ReceiptError("reference-data source/target tree identities differ")
    elif state == "legacy-clean":
        _exact_keys(
            observations,
            {"removed_uids", "remaining_network_policies", "remaining_service_accounts", "remaining_daemonsets"},
            "legacy-clean observations",
        )
        if not isinstance(observations["removed_uids"], list) or len(observations["removed_uids"]) > 128:
            raise ReceiptError("legacy cleanup UID inventory must be a bounded list")
        if len(observations["removed_uids"]) != len(set(observations["removed_uids"])):
            raise ReceiptError("legacy cleanup UID inventory contains duplicates")
        for field in ("remaining_network_policies", "remaining_service_accounts", "remaining_daemonsets"):
            if observations[field] != []:
                raise ReceiptError(f"legacy cleanup left {field}")
    elif state == "baseline-ready":
        _exact_keys(
            observations,
            {"scientific_namespaces", "inventory_sha256", "reference_host_paths", "baseline_incompatible_objects", "unauthorized_exception_objects"},
            "baseline-ready observations",
        )
        if observations["scientific_namespaces"] != context["scientific_namespaces"]:
            raise ReceiptError("baseline scan namespace inventory differs")
        if not SHA256_RE.fullmatch(_string(observations["inventory_sha256"], "baseline inventory digest")):
            raise ReceiptError("baseline inventory digest is malformed")
        for field in ("reference_host_paths", "baseline_incompatible_objects", "unauthorized_exception_objects"):
            if observations[field] != 0:
                raise ReceiptError(f"baseline readiness has nonzero {field}")
    elif state == "baseline-enforced":
        _exact_keys(
            observations,
            {"labels_match", "privileged_probe_rejected", "positive_smoke_passed"},
            "baseline-enforced observations",
        )
        if not all(observations.values()):
            raise ReceiptError("post-enforcement verification did not pass")
    elif state == "enforcement-removed":
        _exact_keys(observations, {"baseline_labels_removed", "exception_agents_ready"}, "enforcement-removed observations")
        if not all(observations.values()):
            raise ReceiptError("rollback did not remove labels before restoring host agents")
    elif state == "host-agents-restored":
        _exact_keys(observations, {"restored_agents", "exception_agents_ready"}, "host-agents-restored observations")
        if tuple(sorted(observations["restored_agents"])) != EXPECTED_HOST_AGENTS or observations["exception_agents_ready"] is not True:
            raise ReceiptError("rollback did not verify both restored and exception host agents")
    elif state == "exception-removed":
        _exact_keys(observations, {"exception_namespace_absent"}, "exception-removed observations")
        if observations["exception_namespace_absent"] is not True:
            raise ReceiptError("exception namespace removal is not verified")
    else:  # pragma: no cover - protected by the edge map
        raise ReceiptError(f"unsupported state {state}")


def verify(query: dict[str, Any]) -> dict[str, str]:
    _exact_keys(
        query,
        {"receipt_path", "public_key_path", "public_key_sha256", "expected_context", "expected_phase"},
        "query",
    )
    phase = _string(query["expected_phase"], "expected_phase")
    if phase not in PHASE_TERMINALS:
        raise ReceiptError("expected_phase is unsupported")
    if PHASE_TERMINALS[phase] is None:
        if query["receipt_path"]:
            raise ReceiptError("prepare must not consume an advance receipt")
        return {"valid": "true", "terminal_state": "unmanaged", "bundle_sha256": "", "transition_count": "0"}

    receipt_path = Path(_string(query["receipt_path"], "receipt_path"))
    public_key_path = Path(_string(query["public_key_path"], "public_key_path"))
    expected_key_sha256 = _string(query["public_key_sha256"], "public_key_sha256")
    if not SHA256_RE.fullmatch(expected_key_sha256):
        raise ReceiptError("public_key_sha256 is malformed")
    try:
        key_bytes = public_key_path.read_bytes()
    except OSError as error:
        raise ReceiptError(f"cannot read receipt public key: {error.strerror}") from error
    if _sha256(key_bytes) != expected_key_sha256:
        raise ReceiptError("receipt public key digest differs from the reviewed digest")

    try:
        expected_context = _object(json.loads(query["expected_context"]), "expected_context")
    except (TypeError, json.JSONDecodeError) as error:
        raise ReceiptError("expected_context is not JSON") from error
    bundle = _load_json(receipt_path)
    _exact_keys(bundle, {"schema", "key_id", "context", "transitions"}, "receipt bundle")
    if bundle["schema"] != SCHEMA:
        raise ReceiptError("receipt bundle schema is unsupported")
    key_id = _string(bundle["key_id"], "key_id")
    _validate_context(_object(bundle["context"], "context"), expected_context)

    transitions = bundle["transitions"]
    if not isinstance(transitions, list) or not 1 <= len(transitions) <= MAX_TRANSITIONS:
        raise ReceiptError("receipt transition chain length is invalid")
    now = dt.datetime.now(dt.timezone.utc)
    prior_state = "unmanaged"
    prior_digest: str | None = None
    receipt_ids: set[str] = set()
    for index, raw_transition in enumerate(transitions, start=1):
        transition = _object(raw_transition, f"transition {index}")
        _exact_keys(
            transition,
            {"receipt_id", "sequence", "from_state", "to_state", "prior_receipt_sha256", "issued_at", "expires_at", "observations", "signature"},
            f"transition {index}",
        )
        receipt_id = _string(transition["receipt_id"], "transition.receipt_id")
        if not UID_RE.fullmatch(receipt_id) or receipt_id in receipt_ids:
            raise ReceiptError("receipt IDs must be unique bounded identifiers")
        receipt_ids.add(receipt_id)
        if _integer(transition["sequence"], "transition.sequence", 1) != index:
            raise ReceiptError("receipt sequence is not contiguous")
        if transition["from_state"] != prior_state or transition["to_state"] != EDGES.get(prior_state):
            raise ReceiptError("receipt state chain skips or reverses a transition")
        if transition["prior_receipt_sha256"] != prior_digest:
            raise ReceiptError("receipt prior-state digest does not match the signed predecessor")
        issued = _instant(transition["issued_at"], "transition.issued_at")
        expires = _instant(transition["expires_at"], "transition.expires_at")
        if issued > now + MAX_CLOCK_SKEW or expires <= now or expires - issued > MAX_RECEIPT_AGE:
            raise ReceiptError("receipt is future-dated, expired, or valid for more than 24 hours")
        _validate_observations(
            transition["to_state"],
            _object(transition["observations"], "transition.observations"),
            expected_context,
        )
        _verify_signature(transition, public_key_path, key_id)
        prior_state = transition["to_state"]
        prior_digest = _sha256(_canonical(transition))

    expected_terminal = PHASE_TERMINALS[phase]
    if prior_state != expected_terminal:
        raise ReceiptError(f"phase {phase} requires terminal state {expected_terminal}, got {prior_state}")
    bundle_bytes = _canonical(bundle)
    return {
        "valid": "true",
        "terminal_state": prior_state,
        "bundle_sha256": _sha256(bundle_bytes),
        "transition_count": str(len(transitions)),
    }


def main() -> int:
    try:
        query = _object(json.load(sys.stdin), "query")
        result = verify(query)
    except (OSError, ReceiptError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"pod-security receipt verification failed: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
