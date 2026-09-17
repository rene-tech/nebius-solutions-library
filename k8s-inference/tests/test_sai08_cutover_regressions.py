"""Source regressions for the SAI-08 non-destructive cutover protocol."""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).parents[1]
AUTHORITY = ROOT / "security/customer-storage-egress-authority"
sys.path.insert(0, str(AUTHORITY))
sys.path.insert(0, str(ROOT / "components/control-plane/src"))

import daemonset_fence_server  # noqa: E402
import storage_reconciler_cutover_server  # noqa: E402
from fs2_serve import storage_reconciler_activation  # noqa: E402
from fs2_serve.user_storage import UserStorageService  # noqa: E402
from fs2_serve.user_storage_nebius import _PROVIDER_OPERATION_TRACKER  # noqa: E402


def _review(*, dry_run: bool) -> dict[str, object]:
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "request": {
            "uid": "review-1",
            "dryRun": dry_run,
            "operation": "UPDATE",
            "resource": {"resource": "daemonsets"},
            "namespace": "kube-system",
            "name": "critical-agent",
            "userInfo": {"username": "owner", "uid": "owner-uid", "groups": []},
            "object": {"metadata": {"name": "critical-agent"}},
            "oldObject": {"metadata": {"name": "critical-agent"}},
        },
    }


@pytest.mark.parametrize(
    "module,record_name",
    [
        (daemonset_fence_server, "_record_decision"),
        (storage_reconciler_cutover_server, "_record"),
    ],
)
def test_admission_dry_run_never_persists_or_consumes(monkeypatch, module, record_name):
    recorded: list[object] = []
    monkeypatch.setattr(module, "load_verified_state", lambda: {"head_sha256": "a" * 64})
    monkeypatch.setattr(module, "allows", lambda *args, **kwargs: True)
    monkeypatch.setattr(module, record_name, lambda *args: recorded.append(args))

    assert module.evaluate(_review(dry_run=True)) == ("review-1", True)
    assert recorded == []
    assert module.evaluate(_review(dry_run=False)) == ("review-1", True)
    assert len(recorded) == 1


def _receipt(
    schema: str,
    purpose: str,
    detail: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    now = datetime.now(UTC)
    private_key = Ed25519PrivateKey.generate()
    public_key_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    adapter_sha256 = "d" * 64
    authority_id = f"fs2-storage-receipt-{purpose}:aaaaaaaaaaaa"
    authority = {
        "authority_id": authority_id,
        "purpose": purpose,
        "public_key_pem": public_key_pem,
        "public_key_sha256": hashlib.sha256(public_key_pem.encode()).hexdigest(),
        "receipt_schema": schema,
        "raw_observation_schema": storage_reconciler_activation.RECEIPT_CONTRACTS[
            purpose
        ][1],
        "observation_adapter_sha256": adapter_sha256,
    }
    object_identities = [
        {"kind": "Deployment", "id": "uid-1", "resource_version": "42", "sha256": "c" * 64}
    ]
    subject_generations = [
        "r20260917000000-aaaaaaaaaaaa",
        "r20260917000100-bbbbbbbbbbbb",
    ]
    observed_at = now.isoformat().replace("+00:00", "Z")
    raw_observation = {
        "schema": authority["raw_observation_schema"],
        "cluster_id": "cluster-1",
        "observed_at": observed_at,
        "subject_generations": subject_generations,
        "object_identities": object_identities,
        "facts": detail,
    }
    body: dict[str, object] = {
        "schema": schema,
        "purpose": purpose,
        "authority_id": authority_id,
        "issuer": {"username": "independent", "uid": "observer-uid", "groups": []},
        "observed_at": observed_at,
        "valid_until": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "cluster_id": "cluster-1",
        "subject_generations": subject_generations,
        "object_identities": object_identities,
        "observation_source": "independent-read-only-observer",
        "observation_adapter_sha256": adapter_sha256,
        "raw_observation": raw_observation,
        "raw_observation_sha256": hashlib.sha256(
            json.dumps(raw_observation, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "outcome": "PASS",
        "detail": detail,
    }
    body["receipt_id"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    envelope: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/storage-reconciler-signed-receipt/v1",
        "purpose": purpose,
        "authority_id": authority_id,
        "body": body,
        "payload_sha256": hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    envelope["signature"] = base64.b64encode(
        private_key.sign(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        )
    ).decode()
    return envelope, {purpose: authority}


@pytest.mark.parametrize(
    "schema,purpose,detail",
    [
        (
            "fs2-serve.nebius.ai/storage-reconciler-zero-inflight-observation/v1",
            "rollback-zero-inflight",
            {"nonterminal_provider_operations": 0},
        ),
        (
            "fs2-serve.nebius.ai/storage-reconciler-schema-compatibility/v1",
            "rollback-schema-compatibility",
            {"compatible": True},
        ),
        (
            "fs2-serve.nebius.ai/storage-reconciler-provider-continuity/v1",
            "rollback-provider-continuity",
            {"continuous": True},
        ),
    ],
)
def test_rollback_requires_independent_signed_raw_receipts(
    schema, purpose, detail
):
    receipt, authorities = _receipt(schema, purpose, detail)
    assert storage_reconciler_activation._bound_receipt(
        receipt,
        schema,
        "cluster-1",
        purpose=purpose,
        authorities=authorities,
        require_current=True,
    )
    forged = copy.deepcopy(receipt)
    forged["body"]["detail"] = {}
    assert not storage_reconciler_activation._bound_receipt(
        forged,
        schema,
        "cluster-1",
        purpose=purpose,
        authorities=authorities,
        require_current=True,
    )
    assert not storage_reconciler_activation._bound_receipt(
        "a" * 64,
        schema,
        "cluster-1",
        purpose=purpose,
        authorities=authorities,
        require_current=True,
    )


def test_outer_signer_cannot_substitute_an_unlisted_receipt_authority():
    schema = "fs2-serve.nebius.ai/storage-reconciler-schema-compatibility/v1"
    purpose = "rollback-schema-compatibility"
    trusted_receipt, trusted_authorities = _receipt(
        schema, purpose, {"compatible": True}
    )
    untrusted_receipt, _ = _receipt(schema, purpose, {"compatible": True})
    assert storage_reconciler_activation._bound_receipt(
        trusted_receipt,
        schema,
        "cluster-1",
        purpose=purpose,
        authorities=trusted_authorities,
        require_current=True,
    )
    assert not storage_reconciler_activation._bound_receipt(
        untrusted_receipt,
        schema,
        "cluster-1",
        purpose=purpose,
        authorities=trusted_authorities,
        require_current=True,
    )


def test_auth_refresh_is_append_only_and_semantically_inert_in_source_contract():
    source = (
        ROOT
        / "security/customer-storage-egress-authority/storage_reconciler_cutover_runtime.py"
    ).read_text()
    assert 'body.get("epoch_kind") not in {"TRANSITION", "AUTH_REFRESH"}' in source
    assert 'value != previous' in source
    assert 'AUTH_REFRESH changed storage cutover semantics' in source
    assert 'body.get("deployments") != retained_deployments' in source
    assert 'latest["valid_from"] != latest["security_owner_identity"]["valid_from"]' in source


def test_migration_drain_chart_and_admission_delete_contracts_are_source_bound():
    release = (ROOT / "components/control-plane/src/fs2_serve/postgresql_release.py").read_text()
    migration = (ROOT / "components/control-plane/migrations/0035_storage_reconciler_drain.sql").read_text()
    chart = (ROOT / "charts/security/customer-storage-reconciler-v2/templates/reconciler.yaml").read_text()
    schema = (ROOT / "charts/security/customer-storage-reconciler-v2/values.schema.json").read_text()
    fence = (ROOT / "security/customer-storage-daemonset-fence/main.tf").read_text()
    boundary = (ROOT / "security/customer-storage-egress-boundary/main.tf").read_text()
    cutover = (
        ROOT
        / "security/customer-storage-egress-authority/storage_reconciler_cutover_runtime.py"
    ).read_text()
    activation = (
        ROOT
        / "components/control-plane/src/fs2_serve/storage_reconciler_activation.py"
    ).read_text()

    assert '"0030_customer_storage_credentials.sql"' in release
    assert '"0030_mcp_semantic_outcomes.sql"' in release
    assert '"0035_storage_reconciler_drain.sql"' in release
    assert "fs2_mark_storage_provider_operation_indeterminate" in migration
    assert "requested_action IS NOT NULL" in migration
    assert "queued_user_actions" in migration
    assert '"database_receipt"' in cutover
    assert "_database_drain_receipt" in cutover
    assert 'digest(value["database_receipt"]["operations"])' in cutover
    assert "terminationGracePeriodSeconds" in chart
    assert "storage-reconciler-drain-wait" in chart
    assert "drainGraceSeconds" in schema
    assert 'body["phase"] in {"DRAINING", "QUIESCED"}' in activation
    assert "completed_shutdown_receipt" in activation
    assert 'rollback.get("zero_inflight_actions_receipt")' in activation
    assert "receipt-authorities.json" in chart
    assert "RECEIPT_CONTRACTS" in cutover
    assert "_independently_signed_receipt" in cutover
    assert '"receipt_authority_registry_sha256"' in cutover
    assert "_receipt_authorities" in activation
    assert fence.count('operations  = ["CREATE", "DELETE"]') >= 2
    assert "request.operation == 'DELETE' ?" in boundary
    assert "oldObject != null" in boundary


def test_epoch_executor_rereads_and_binds_bounded_credential_per_api_operation():
    source = (
        ROOT
        / "security/customer-storage-egress-authority/storage_reconciler_cutover_executor.py"
    ).read_text()
    assert "_safe_read(self.token_path)" in source
    assert '"fs2-storage-cutover"' in source
    assert 'hashlib.sha256(jti.encode()).hexdigest()' in source
    assert 'payload["exp"] > signed_until' in source
    assert "self.token =" not in source


class _Fence:
    async def admit_provider_operation(self):
        return {
            "reconciler_generation": "r20260917000000-aaaaaaaaaaaa",
            "activation_epoch": 7,
            "activation_state_head_sha256": "a" * 64,
            "transition_id": "b" * 64,
        }


class _Ledger:
    def __init__(self):
        self.finished: list[bool] = []
        self.indeterminate = 0

    async def begin_provider_operation(self, **kwargs):
        self.admission = kwargs

    async def provider_operation_submitted(self, operation_id, provider_operation_id):
        self.provider_operation_id = provider_operation_id

    async def provider_operation_terminal(
        self, operation_id, provider_operation_id, *, succeeded, code
    ):
        self.provider_terminal = (provider_operation_id, succeeded, code)

    async def finish_provider_operation(self, operation_id, *, succeeded):
        self.finished.append(succeeded)

    async def mark_provider_operation_indeterminate(self, operation_id):
        self.indeterminate += 1


@pytest.mark.asyncio
async def test_provider_success_is_not_terminal_until_dependent_database_commit():
    ledger = _Ledger()
    service = UserStorageService(ledger, None, None, activation_fence=_Fence())

    async def invoke():
        tracker = _PROVIDER_OPERATION_TRACKER.get()
        await tracker.submitted("provider-operation-1")
        await tracker.terminal("provider-operation-1", succeeded=True, code="OK")
        return "provider-result"

    async def failed_commit(result):
        assert result == "provider-result"
        raise RuntimeError("database CAS failed")

    with pytest.raises(RuntimeError, match="database CAS"):
        await service._provider_mutation(
            tenant="tenant-a",
            principal="alice",
            operation_kind="bucket-policy-reconcile",
            target_identity="bucket-a",
            invoke=invoke,
            commit=failed_commit,
        )
    assert ledger.indeterminate == 1
    assert ledger.finished == []


@pytest.mark.asyncio
async def test_provider_submission_loss_remains_nonterminal_for_cutover():
    ledger = _Ledger()
    service = UserStorageService(ledger, None, None, activation_fence=_Fence())

    async def lost_submission():
        tracker = _PROVIDER_OPERATION_TRACKER.get()
        await tracker.indeterminate()
        raise TimeoutError("provider submission result unknown")

    with pytest.raises(TimeoutError):
        await service._provider_mutation(
            tenant="tenant-a",
            principal="alice",
            operation_kind="credential-provision",
            target_identity="group-a",
            invoke=lost_submission,
        )
    assert ledger.indeterminate == 1
    assert ledger.finished == []
