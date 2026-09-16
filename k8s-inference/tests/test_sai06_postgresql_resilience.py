from __future__ import annotations

import importlib.machinery
import importlib.util
import base64
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from contextlib import redirect_stdout

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
STACK_PATH = ROOT / "inference-stack"
LOADER = importlib.machinery.SourceFileLoader("sai06_inference_stack", str(STACK_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
STACK = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = STACK
LOADER.exec_module(STACK)


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _assert_hcl_assignment(source: str, name: str, value: str) -> None:
    assert re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*{re.escape(value)}\s*$", source)


def _backup_handoff_inputs(run_root: Path) -> tuple[dict, dict]:
    contract = {
        "artifact_delivery": {"mode": "direct-source"},
        "stages": {
            "foundation": {
                "grafana_publication": {"enabled": False, "external_base_url": ""}
            },
            "workloads": {
                "postgresql_backup": {
                    "enabled": True,
                    "retention_days": 30,
                    "schedule": "0 0 2 * * *",
                    "credential_generation": 1,
                }
            },
        },
    }
    dynamic = {
        "run_root": str(run_root),
        "kubeconfig_path": str(run_root / "kubeconfig"),
        "run_id": "sai06test",
        "cluster_id": "mk8scluster-test",
        "cluster_name": "sai06-test",
        "kube_context": "sai06-test",
        "kube_system_uid": "11111111-2222-3333-4444-555555555555",
        "project_id": "project-test",
        "target_contract": {"schema": "target-test/v1"},
        "infrastructure_contract": {"schema": "infrastructure-test/v1"},
        "accelerator_pool_contract": {"schema": "accelerators-test/v1"},
        "public_edge_contract": {"mode": "internal-only"},
        "postgresql_backup_storage_contract": {
            "schema": "fs2-serve.nebius.ai/postgresql-backup-storage/v1",
            "project_id": "project-test",
            "region": "us-north1",
            "object_storage": {
                "id": "storagebucket-postgresql-test",
                "name": "postgresql-backup-test",
            },
            "writer": {
                "role": "storage.object-editor",
                "paths": ["postgresql/v1/fs2-control-db/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
            "inventory_reader": {
                "roles": ["storage.object-lister", "storage.object-viewer"],
                "paths": ["postgresql/v1/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
            "receipt_publisher": {
                "role": "storage.uploader",
                "paths": ["postgresql/v1/restore-verification/success/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
        },
        "postgresql_backup_object_storage_access": {
            "key_id": "accesskey-postgresql-test",
            "access_key_id": "TESTPOSTGRESQLACCESSKEY",
            "secret_reference_id": "mysterybox-postgresql-test",
            "resource_version": 1,
        },
        "postgresql_backup_inventory_object_storage_access": {
            "key_id": "accesskey-postgresql-inventory-test",
            "access_key_id": "TESTPOSTGRESQLINVENTORY",
            "secret_reference_id": "mysterybox-postgresql-inventory-test",
            "resource_version": 1,
        },
        "postgresql_backup_receipt_object_storage_access": {
            "key_id": "accesskey-postgresql-receipt-test",
            "access_key_id": "TESTPOSTGRESQLRECEIPT",
            "secret_reference_id": "mysterybox-postgresql-receipt-test",
            "resource_version": 1,
        },
        "postgresql_backup_lifecycle": {
            "retention_mode": "retain",
            "destroy_status": "blocked-retained",
        },
    }
    return contract, dynamic


def test_every_capacity_profile_has_three_system_nodes() -> None:
    profiles = json.loads(_text("catalog/profiles/capacity-profiles.json"))[
        "capacity_profiles"
    ]

    assert profiles
    assert all(profile["system_nodes"] >= 3 for profile in profiles.values())
    assert "var.deployment.cluster.system_pool.node_count >= 3" in _text("variables.tf")
    assert "var.system_pool.node_count >= 3" in _text(
        "stages/infrastructure/variables.tf"
    )


def test_postgresql_backup_is_versioned_retained_and_mysterybox_delivered() -> None:
    infrastructure = _text("stages/infrastructure/postgresql_backup.tf")
    outputs = _text("stages/infrastructure/outputs.tf")

    assert 'resource "nebius_storage_v1_bucket" "postgresql_backup"' in infrastructure
    assert 'versioning_policy     = "ENABLED"' in infrastructure
    assert "prevent_destroy = true" in infrastructure
    assert 'secret_delivery_mode = "MYSTERY_BOX"' in infrastructure
    assert "postgresql_backup_writer_path_scope" in infrastructure
    assert (
        'postgresql_backup_writer_role                 = "storage.object-editor"'
        in infrastructure
    )
    assert (
        'postgresql_backup_inventory_roles             = ["storage.object-lister", "storage.object-viewer"]'
        in infrastructure
    )
    assert (
        'postgresql_backup_receipt_role                = "storage.uploader"'
        in infrastructure
    )
    assert "postgresql_backup_inventory" in infrastructure
    assert "postgresql_restore_receipt" in infrastructure
    assert 'output "postgresql_backup_storage_contract"' in outputs
    assert 'output "postgresql_backup_object_storage_access"' in outputs
    assert 'output "postgresql_backup_inventory_object_storage_access"' in outputs
    assert 'output "postgresql_backup_receipt_object_storage_access"' in outputs


def test_postgresql_backup_capacity_is_retention_aware_and_live_checked() -> None:
    root_variables = _text("variables.tf")
    root_locals = _text("locals.tf")
    infrastructure = _text("stages/infrastructure/postgresql_backup.tf")
    stack = _text("inference-stack")

    assert "estimated_daily_wal_gib" in root_variables
    assert "capacity_headroom_percent" in root_variables
    assert "postgresql_backup_required_capacity_gib" in root_locals
    assert "max_size_gib = optional(number, 12288)" in root_variables
    assert "postgresql_backup_noncurrent_base_backup_days" in root_locals
    assert "postgresql_backup_noncurrent_wal_days" in root_locals
    assert 'schedule == "0 0 2 * * *"' in root_variables
    assert "var.postgresql_backup.object_storage.max_size_gib >=" in infrastructure
    assert "local.postgresql_backup_required_capacity_gib" in infrastructure
    assert "preflight_postgresql_backup_capacity" in stack
    assert '"sai06_capacity_plan_binding": sai06_capacity_plan_binding(' in stack
    assert '"storage.bucket.size.standard"' in stack
    assert '"sai06-postgresql-capacity-preflight.json"' in stack
    assert "SAI06_APPROVAL_REQUEST_NAME" in stack
    assert "infrastructure_plan_sha256" in stack
    assert "source_tree" in stack
    assert "Ed25519PublicKey" in stack


def _capacity_contract() -> dict:
    return {
        "target": {"project_id": "project-test", "region": "eu-north1"},
        "stages": {
            "infrastructure": {
                "system_pool": {
                    "node_count": 3,
                    "three_node_ha_cost_review_acknowledged": True,
                },
                "postgresql_backup": {
                    "object_storage": {"max_size_gib": 12288},
                    "schedule": "0 0 2 * * *",
                    "retention_days": 30,
                    "database_volume_size_gib": 100,
                    "estimated_daily_wal_gib": 32,
                    "capacity_headroom_percent": 25,
                    "required_capacity_gib": 11585,
                    "capacity_cost_review_acknowledged": True,
                },
            }
        },
    }


def _quota_payload(
    *, compute_limit: int | None = 30, storage_limit: int | None = 30 * 1024**4
) -> dict:
    def item(name: str, unit: str, usage: int, limit: int | None) -> dict:
        status = {
            "usage": str(usage),
            "unit": unit,
            "usage_state": "USAGE_STATE_USED",
        }
        if limit is not None:
            status["limit"] = str(limit)
        return {
            "metadata": {"name": name},
            "spec": {"region": "eu-north1"},
            "status": status,
        }

    return {
        "items": [
            item("compute.instance.count", "count", 15, compute_limit),
            item(
                "storage.bucket.size.standard", "byte", 239_725_098_497, storage_limit
            ),
        ]
    }


def _plan_document(
    *,
    node_before: int = 1,
    node_after: int = 3,
    storage_before: int = 0,
    storage_after: int = 12288 * 1024**3,
) -> dict:
    return {
        "resource_changes": [
            {
                "address": "nebius_mk8s_v1_node_group.system",
                "change": {
                    "before": {"fixed_node_count": node_before},
                    "after": {"fixed_node_count": node_after},
                    "actions": ["update"],
                },
            },
            {
                "address": "nebius_storage_v1_bucket.postgresql_backup[0]",
                "change": {
                    "before": None
                    if storage_before == 0
                    else {"max_size_bytes": storage_before},
                    "after": {"max_size_bytes": storage_after},
                    "actions": ["create"] if storage_before == 0 else ["no-op"],
                },
            },
        ]
    }


def _signed_approval(
    request: dict,
    *,
    compute_ceiling: int = 30,
    storage_ceiling: int = 30 * 1024**4,
    owner_overrides: list[dict] | None = None,
    now: datetime | None = None,
) -> tuple[dict, dict]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key_id = hashlib.sha256(public_key).hexdigest()
    captured = now or datetime.now(UTC)
    payload = {
        "schema": "fs2-serve.nebius.ai/sai06-capacity-approval/v2",
        "issuer": "platform-capacity-owner",
        "issued_at": captured.isoformat().replace("+00:00", "Z"),
        "valid_until": (captured + timedelta(hours=12))
        .isoformat()
        .replace("+00:00", "Z"),
        "nonce": request["nonce"],
        "request_sha256": STACK.canonical_sha256(request),
        "request": request,
        "approved_ceilings": [
            {
                "resource": "compute.instance.count",
                "unit": "count",
                "ceiling": compute_ceiling,
            },
            {
                "resource": "storage.bucket.size.standard",
                "unit": "byte",
                "ceiling": storage_ceiling,
            },
        ],
        "owner_overrides": owner_overrides or [],
    }
    signature = private_key.sign(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    )
    envelope = {
        "schema": "fs2-serve.nebius.ai/sai06-capacity-approval-envelope/v2",
        "algorithm": "Ed25519",
        "key_id": key_id,
        "payload": payload,
        "signature": base64.b64encode(signature).decode(),
    }
    trust = {
        "schema": "fs2-serve.nebius.ai/sai06-trusted-issuers/v1",
        "issuers": [
            {
                "issuer": "platform-capacity-owner",
                "key_id": key_id,
                "algorithm": "Ed25519",
                "public_key_base64": base64.b64encode(public_key).decode(),
                "roles": ["capacity-approver", "capacity-owner"],
                "enabled": True,
            }
        ],
    }
    return envelope, trust


def _approval_request(resources: list[dict]) -> dict:
    return {
        "schema": "fs2-serve.nebius.ai/sai06-capacity-approval-request/v2",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "nonce": "1" * 64,
        "project_id": "project-test",
        "region": "eu-north1",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "infrastructure_plan_sha256": "c" * 64,
        "infrastructure_plan_json_sha256": "d" * 64,
        "quota_evidence_sha256": "e" * 64,
        "capacity_plan_binding_sha256": "f" * 64,
        "resources": resources,
    }


def test_live_capacity_preflight_binds_compute_storage_and_incremental_deltas() -> None:
    contract = _capacity_contract()
    payload = _quota_payload()
    with mock.patch.object(
        STACK,
        "run",
        return_value=subprocess.CompletedProcess(
            ["nebius"], 0, stdout=json.dumps(payload), stderr=""
        ),
    ):
        evidence = STACK.preflight_postgresql_backup_capacity(
            SimpleNamespace(nebius="nebius", nebius_profile="sandbox"),
            contract,
            plan_document=_plan_document(),
        )

    resources = {item["resource"]: item for item in evidence["resources"]}
    assert resources["compute.instance.count"]["incremental_demand"] == 2
    assert resources["compute.instance.count"]["projected_usage"] == 17
    assert (
        resources["storage.bucket.size.standard"]["incremental_demand"]
        == 12288 * 1024**3
    )
    assert evidence["quota_evidence_sha256"] == STACK.canonical_sha256(payload)
    assert evidence["apply_authorized"] is False

    recurring = STACK.sai06_capacity_projections(
        payload,
        project_id="project-test",
        region="eu-north1",
        plan_document=_plan_document(
            node_before=3,
            node_after=3,
            storage_before=12288 * 1024**3,
            storage_after=12288 * 1024**3,
        ),
        fallback_system_nodes=3,
        fallback_storage_bytes=12288 * 1024**3,
    )
    recurring_index = {item["resource"]: item for item in recurring}
    assert recurring_index["compute.instance.count"]["incremental_demand"] == 0
    assert recurring_index["storage.bucket.size.standard"]["incremental_demand"] == 0


def test_capacity_plan_rejects_any_resource_replacement() -> None:
    accepted = _plan_document()
    STACK.require_sai06_no_replacements(accepted)

    replacement = _plan_document()
    replacement["resource_changes"].append(
        {
            "address": "nebius_mk8s_v1_cluster.this",
            "change": {"before": {}, "after": {}, "actions": ["delete", "create"]},
        }
    )
    with pytest.raises(STACK.DeploymentError, match="no-replacement.*cluster.this"):
        STACK.require_sai06_no_replacements(replacement)


def test_signed_approval_rejects_missing_limits_without_owner_override() -> None:
    resources = STACK.sai06_capacity_projections(
        _quota_payload(compute_limit=None, storage_limit=None),
        project_id="project-test",
        region="eu-north1",
        plan_document=_plan_document(),
        fallback_system_nodes=3,
        fallback_storage_bytes=12288 * 1024**3,
    )
    request = _approval_request(resources)
    envelope, trust = _signed_approval(request)
    with pytest.raises(STACK.DeploymentError, match="absent.*owner override"):
        STACK.validate_sai06_capacity_receipt(envelope, request, trust)

    overrides = [
        {
            "resource": item["resource"],
            "unit": item["unit"],
            "ceiling": 30 if item["unit"] == "count" else 30 * 1024**4,
            "reason": "Provider omitted a numeric limit; capacity owner reviewed the exact saved plan.",
            "evidence_sha256": "9" * 64,
        }
        for item in resources
    ]
    envelope, trust = _signed_approval(request, owner_overrides=overrides)
    summary = STACK.validate_sai06_capacity_receipt(envelope, request, trust)
    assert summary["owner_override_resources"] == [
        "compute.instance.count",
        "storage.bucket.size.standard",
    ]


def test_signed_approval_rejects_forgery_stale_scope_and_insufficient_limit() -> None:
    resources = STACK.sai06_capacity_projections(
        _quota_payload(),
        project_id="project-test",
        region="eu-north1",
        plan_document=_plan_document(),
        fallback_system_nodes=3,
        fallback_storage_bytes=12288 * 1024**3,
    )
    request = _approval_request(resources)
    envelope, trust = _signed_approval(request)
    forged = json.loads(json.dumps(envelope))
    forged["payload"]["request"]["project_id"] = "project-other"
    with pytest.raises(STACK.DeploymentError, match="different request|signature"):
        STACK.validate_sai06_capacity_receipt(forged, request, trust)

    stale, trust = _signed_approval(request, now=datetime.now(UTC) - timedelta(days=2))
    with pytest.raises(STACK.DeploymentError, match="stale|expired"):
        STACK.validate_sai06_capacity_receipt(stale, request, trust)

    insufficient, trust = _signed_approval(request, compute_ceiling=16)
    with pytest.raises(STACK.DeploymentError, match="exceeds the signed ceiling"):
        STACK.validate_sai06_capacity_receipt(insufficient, request, trust)

    extra_field = json.loads(json.dumps(request))
    extra_field["caller_note"] = "not part of the exact approval request"
    extra_envelope, trust = _signed_approval(extra_field)
    with pytest.raises(STACK.DeploymentError, match="request fields are not exact"):
        STACK.validate_sai06_capacity_receipt(extra_envelope, extra_field, trust)

    invalid_math = json.loads(json.dumps(request))
    invalid_math["resources"][0]["projected_usage"] += 1
    invalid_envelope, trust = _signed_approval(invalid_math)
    with pytest.raises(STACK.DeploymentError, match="projected capacity is malformed"):
        STACK.validate_sai06_capacity_receipt(invalid_envelope, invalid_math, trust)


def test_apply_loader_rejects_a_trusted_signature_for_the_wrong_project(
    tmp_path: Path,
) -> None:
    contract = _capacity_contract()
    plan_document = _plan_document()
    resources = STACK.sai06_capacity_projections(
        _quota_payload(),
        project_id="project-test",
        region="eu-north1",
        plan_document=plan_document,
        fallback_system_nodes=3,
        fallback_storage_bytes=12288 * 1024**3,
    )
    plan_path = tmp_path / "infrastructure-plan.tfplan"
    plan_path.write_bytes(b"exact-saved-plan")
    plan_path.chmod(0o600)
    STACK.private_json(tmp_path / "infrastructure-plan.plan.json", plan_document)
    request = _approval_request(resources)
    request.update(
        {
            "project_id": "project-other",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "infrastructure_plan_sha256": hashlib.sha256(
                b"exact-saved-plan"
            ).hexdigest(),
            "infrastructure_plan_json_sha256": STACK.canonical_sha256(plan_document),
            "quota_evidence_sha256": "e" * 64,
            "capacity_plan_binding_sha256": STACK.sai06_capacity_plan_binding(contract),
        }
    )
    envelope, trust = _signed_approval(request)
    STACK.private_json(tmp_path / STACK.SAI06_APPROVAL_REQUEST_NAME, request)
    receipt_path = tmp_path / "capacity-approval.json"
    trust_path = tmp_path / "trusted-issuers.json"
    STACK.private_json(receipt_path, envelope)
    STACK.private_json(trust_path, trust)

    with (
        mock.patch.object(STACK, "SAI06_TRUSTED_ISSUERS_PATH", trust_path),
        mock.patch.object(STACK, "source_tree", return_value="b" * 40),
        pytest.raises(STACK.DeploymentError, match="project_id no longer matches"),
    ):
        STACK.load_and_validate_sai06_capacity_approval(
            SimpleNamespace(
                sai06_capacity_receipt=receipt_path,
                nebius="nebius",
                nebius_profile="sandbox",
            ),
            tmp_path,
            contract,
            "a" * 40,
        )


def test_secure_approval_loader_rejects_symlink_hardlink_and_permissive_mode(
    tmp_path: Path,
) -> None:
    original = tmp_path / "approval.json"
    original.write_text("{}", encoding="utf-8")
    original.chmod(0o600)
    assert STACK._secure_json_file(original, label="test approval", private=True) == {}

    symlink = tmp_path / "approval-symlink.json"
    symlink.symlink_to(original)
    with pytest.raises(STACK.DeploymentError, match="opened safely"):
        STACK._secure_json_file(symlink, label="test approval", private=True)

    hardlink = tmp_path / "approval-hardlink.json"
    hardlink.hardlink_to(original)
    with pytest.raises(STACK.DeploymentError, match="singly linked"):
        STACK._secure_json_file(original, label="test approval", private=True)
    hardlink.unlink()
    original.chmod(0o640)
    with pytest.raises(STACK.DeploymentError, match="0600"):
        STACK._secure_json_file(original, label="test approval", private=True)

    plan = tmp_path / "plan.tfplan"
    plan.write_bytes(b"exact-saved-plan")
    plan.chmod(0o600)
    assert (
        STACK._secure_sha256_file(plan, label="test plan")
        == hashlib.sha256(b"exact-saved-plan").hexdigest()
    )
    plan_link = tmp_path / "plan-link.tfplan"
    plan_link.symlink_to(plan)
    with pytest.raises(STACK.DeploymentError, match="opened safely"):
        STACK._secure_sha256_file(plan_link, label="test plan")


def test_capacity_receipt_schema_is_strict_and_matches_runtime_resources() -> None:
    schema = json.loads(_text("docs/sai06-capacity-approval.schema.json"))

    assert schema["additionalProperties"] is False
    assert schema["properties"]["algorithm"]["const"] == "Ed25519"
    assert schema["$defs"]["request"]["additionalProperties"] is False
    assert schema["$defs"]["projection"]["additionalProperties"] is False
    assert set(schema["$defs"]["resource"]["enum"]) == {
        "compute.instance.count",
        "storage.bucket.size.standard",
    }


def test_destroy_preflights_all_plans_before_any_delete_and_retains_postgresql(
    tmp_path: Path,
) -> None:
    configuration = {
        "stages": {
            "infrastructure": {
                "postgresql_backup": {
                    "enabled": True,
                    "lifecycle": {"retention_mode": "retain"},
                }
            }
        }
    }
    dynamic = {
        "postgresql_backup_storage_contract": {
            "schema": "fs2-serve.nebius.ai/postgresql-backup-storage/v1",
            "object_storage": {
                "id": "storagebucket-test",
                "name": "postgresql-backup-test",
            },
        },
        "postgresql_backup_lifecycle": {
            "retention_mode": "retain",
            "adoption_status": "ids-exported-for-explicit-state-adoption",
        },
    }
    events: list[str] = []

    def plan(*_args: object, **kwargs: object) -> tuple[Path, dict, dict]:
        stage = str(kwargs["stage"])
        events.append(f"plan:{stage}")
        return tmp_path / f"{stage}-destroy.tfplan", {}, {}

    def apply(*args: object, **_kwargs: object) -> None:
        events.append(f"apply:{Path(str(args[1])).name}")

    with (
        mock.patch.object(STACK, "state_has_resources", return_value=True),
        mock.patch.object(STACK, "state_ready", return_value=True),
        mock.patch.object(STACK, "infrastructure_outputs", return_value=dynamic),
        mock.patch.object(
            STACK,
            "write_downstream_variables",
            return_value=(tmp_path / "foundation.json", tmp_path / "workloads.json"),
        ),
        mock.patch.object(STACK, "workload_endpoint_outputs", return_value={}),
        mock.patch.object(STACK, "plan_stage", side_effect=plan),
        mock.patch.object(STACK, "apply_plan", side_effect=apply),
        mock.patch.object(STACK, "write_infrastructure_variables") as write_infra,
        redirect_stdout(io.StringIO()),
    ):
        STACK.destroy_stack(
            SimpleNamespace(
                terraform="terraform",
                nebius="nebius",
                nebius_profile="sandbox",
                kubectl="kubectl",
            ),
            tmp_path,
            configuration,
            "a" * 40,
        )

    assert events == [
        "plan:workloads",
        "plan:foundation",
        "apply:workloads",
        "apply:foundation",
    ]
    write_infra.assert_not_called()
    receipt = json.loads((tmp_path / "postgresql-backup-retention.json").read_text())
    assert receipt["postgresql_backup"]["lifecycle"]["retention_mode"] == "retain"


def test_destroy_plan_failure_makes_zero_deletions(tmp_path: Path) -> None:
    configuration = {"stages": {"infrastructure": {}}}

    def plan(*_args: object, **kwargs: object) -> tuple[Path, dict, dict]:
        if kwargs["stage"] == "foundation":
            raise STACK.DeploymentError("foundation destroy plan rejected")
        return tmp_path / "workloads-destroy.tfplan", {}, {}

    with (
        mock.patch.object(
            STACK,
            "write_infrastructure_variables",
            return_value=tmp_path / "infra.json",
        ),
        mock.patch.object(STACK, "state_has_resources", return_value=True),
        mock.patch.object(STACK, "state_ready", return_value=False),
        mock.patch.object(
            STACK,
            "plan_stage",
            side_effect=plan,
        ),
        mock.patch.object(STACK, "apply_plan") as apply_plan,
        pytest.raises(STACK.DeploymentError, match="foundation destroy plan rejected"),
    ):
        # Cached downstream inputs make this a pure plan-order test.
        (tmp_path / "foundation.tfvars.json").write_text("{}")
        (tmp_path / "workloads.tfvars.json").write_text("{}")
        STACK.destroy_stack(
            SimpleNamespace(terraform="terraform", nebius_profile="sandbox"),
            tmp_path,
            configuration,
            "a" * 40,
        )
    apply_plan.assert_not_called()


def test_backup_handoff_is_exact_retained_and_secret_free(tmp_path: Path) -> None:
    contract, dynamic = _backup_handoff_inputs(tmp_path)
    STACK.private_directory(tmp_path)
    _foundation, workloads_path = STACK.write_downstream_variables(
        tmp_path, contract, dynamic
    )
    generated_text = workloads_path.read_text(encoding="utf-8")
    backup = json.loads(generated_text)["postgresql_backup"]

    assert backup["storage_contract"] == dynamic["postgresql_backup_storage_contract"]
    assert (
        backup["object_storage_access"]
        == dynamic["postgresql_backup_object_storage_access"]
    )
    assert (
        backup["inventory_object_storage_access"]
        == dynamic["postgresql_backup_inventory_object_storage_access"]
    )
    assert (
        backup["receipt_object_storage_access"]
        == dynamic["postgresql_backup_receipt_object_storage_access"]
    )
    assert "secret-access-key" not in generated_text
    assert "secret_value" not in generated_text


def test_backup_handoff_rejects_disposable_lifecycle(tmp_path: Path) -> None:
    contract, dynamic = _backup_handoff_inputs(tmp_path)
    dynamic["postgresql_backup_lifecycle"]["retention_mode"] = "disposable"
    STACK.private_directory(tmp_path)

    try:
        STACK.write_downstream_variables(tmp_path, contract, dynamic)
    except STACK.DeploymentError as error:
        assert "must remain retained" in str(error)
    else:
        raise AssertionError("disposable PostgreSQL backup lifecycle was accepted")


def test_cnpg_archives_wal_schedules_backups_and_requires_host_spread() -> None:
    database = _text("stages/workloads/database.tf")

    _assert_hcl_assignment(database, "podAntiAffinityType", '"required"')
    _assert_hcl_assignment(database, "topologyKey", '"kubernetes.io/hostname"')
    _assert_hcl_assignment(database, "instances", "3")
    assert "barmanObjectStore" in database
    assert 'compression = "gzip"' in database
    assert "maxParallel" in database
    _assert_hcl_assignment(database, "archive_timeout", '"60s"')
    assert "retentionPolicy" in database
    assert 'kind       = "ScheduledBackup"' in database
    assert 'method               = "barmanObjectStore"' in database
    assert "immediate            = true" in database
    assert 'backupOwnerReference = "self"' in database


def test_restore_verification_replays_wal_between_paired_markers() -> None:
    database = _text("stages/workloads/database.tf")

    _assert_hcl_assignment(database, "name", '"fs2-control-db-backup-source"')
    _assert_hcl_assignment(database, "source", '"fs2-control-db-backup-source"')
    assert "targetImmediate" not in database
    _assert_hcl_assignment(database, "targetTime", "var.database_restore_target_time")
    assert 'resource "kubernetes_job_v1" "database_pitr_marker"' in database
    assert "fs2_pitr_restore_markers" in database
    assert "FS2_PITR_TARGET_TIME=" in database
    assert "-a" in database
    assert "-b" in database
    assert "pg_current_wal_insert_lsn()" in database
    assert "pg_last_wal_replay_lsn()" in database
    assert "prepare_database_restore_marker_job" in database
    assert 'resource "kubernetes_job_v1" "database_pitr_marker_cleanup"' in database
    assert "safe_cleanup" in database
    assert "safe_prepare" in database
    assert "DROP TABLE public.fs2_pitr_restore_markers" in database
    assert 'resource "kubernetes_job_v1" "database_restore_verification"' in database
    assert (
        'resource "terraform_data" "postgresql_restore_verification_contract"'
        in database
    )
    assert "condition     = var.postgresql_backup.enabled" in database
    _assert_hcl_assignment(database, "name", '"fs2-control-db-restore-verifier"')
    _assert_hcl_assignment(
        database,
        "name",
        'kubernetes_secret_v1.database_account["restore_verifier"].metadata[0].name',
    )
    _assert_hcl_assignment(database, "automount_service_account_token", "false")
    assert "to_regclass('public.fs2_schema_migrations')" in database


def test_restore_verifier_is_marker_only_and_denied_sensitive_tables() -> None:
    database = _text("stages/workloads/database.tf")

    assert "GRANT SELECT ON TABLE public.fs2_pitr_restore_markers" in database
    assert "REVOKE CREATE ON SCHEMA public" in database
    for table in (
        "fs2_tokens",
        "fs2_operations",
        "fs2_operation_events",
        "fs2_audit_events",
        "fs2_request_debug",
        "fs2_scientific_artifacts",
        "fs2_scientific_uploads",
        "fs2_scientific_artifact_events",
        "fs2_scientific_run_results",
        "fs2_operator_sessions",
    ):
        assert table in database
    assert "has_any_column_privilege" in database
    assert (
        "credential|secret|token|session|operation|audit|payload|artifact" in database
    )
    assert "pg_attribute" in database


def test_effective_system_pool_requires_three_node_cost_acknowledgement() -> None:
    root_variables = _text("variables.tf")
    infrastructure_variables = _text("stages/infrastructure/variables.tf")
    example = _text("terraform.tfvars.example")

    assert "system_pool_cost_review_acknowledged" in root_variables
    assert "three_node_ha_cost_review_acknowledged" in infrastructure_variables
    for source in (root_variables, infrastructure_variables):
        assert "node_count >= 3" in source
    assert re.search(r"system_pool_cost_review_acknowledged\s*=\s*true", example)


def test_default_system_pool_requires_effective_node_and_backup_acknowledgements() -> (
    None
):
    root_variables = _text("variables.tf")
    root_locals = _text("locals.tf")
    stack = _text("inference-stack")

    assert "system_pool_cost_review_acknowledged" in root_variables
    assert "local.selected_capacity.system_nodes" in root_locals
    assert "three_node_ha_cost_review_acknowledged" in root_locals
    assert "not isinstance(system_pool, Mapping)" in stack
    assert 'backup.get("capacity_cost_review_acknowledged") is not True' in stack


def test_backup_observability_is_executable_and_covers_every_sai06_signal() -> None:
    monitoring = _text("stages/workloads/postgresql_backup_monitoring.tf")
    metrics = _text("stages/workloads/scripts/postgresql_backup_metrics.py")

    assert 'kind       = "ServiceMonitor"' in monitoring
    assert 'kind       = "PrometheusRule"' in monitoring
    for alert in (
        "Fs2PostgresqlBackupFailed",
        "Fs2PostgresqlBackupStale",
        "Fs2PostgresqlRecoverabilityPointMissing",
        "Fs2PostgresqlWalArchiveFailed",
        "Fs2PostgresqlWalArchiveStalled",
        "Fs2PostgresqlBackupBucketExporterUnavailable",
        "Fs2PostgresqlBackupBucketCapacityWarning",
        "Fs2PostgresqlBackupBucketCapacityCritical",
        "Fs2PostgresqlRestoreVerificationFailed",
        "Fs2PostgresqlRestoreVerificationStale",
    ):
        assert alert in monitoring
    assert "list_object_versions" in metrics
    assert "get_object" in metrics
    assert "validate_restore_receipt" in metrics
    assert "fs2_postgresql_backup_bucket_usage_bytes" in metrics
    assert "fs2_postgresql_backup_bucket_capacity_bytes" in metrics
    assert "fs2_postgresql_restore_last_success_timestamp_seconds" in metrics
    assert "restore-verification/success/" in monitoring


def test_backup_alert_promql_is_accepted_by_promtool(tmp_path: Path) -> None:
    monitoring = _text("stages/workloads/postgresql_backup_monitoring.tf")
    expressions = [
        json.loads(f'"{encoded}"')
        for encoded in re.findall(
            r'(?m)^\s*expr\s*=\s*"((?:\\.|[^"\\])*)"\s*$', monitoring
        )
    ]
    assert len(expressions) == 10
    rule_file = tmp_path / "sai06-rules.yaml"
    rule_file.write_text(
        yaml.safe_dump(
            {
                "groups": [
                    {
                        "name": "sai06",
                        "rules": [
                            {"alert": f"Sai06Syntax{index}", "expr": expression}
                            for index, expression in enumerate(expressions)
                        ],
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    promtool = shutil.which("promtool")
    assert promtool is not None
    result = subprocess.run(  # noqa: S603 - exact resolved test dependency
        [promtool, "check", "rules", str(rule_file)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_successful_restore_publishes_a_nonsensitive_durable_receipt() -> None:
    database = _text("stages/workloads/database.tf")

    assert (
        'resource "kubernetes_job_v1" "database_restore_verification_receipt"'
        in database
    )
    assert "restore-verification/success/" in database
    assert "boto3.client" in database
    assert '"fs2-serve.nebius.ai/postgresql-restore-verification/v2"' in database
    assert 'IfNoneMatch="*"' in database
    assert "verification_binding_sha256" in database
    assert "kubernetes_secret_v1.postgresql_restore_receipt" in database
    assert (
        "count = var.postgresql_backup.enabled && "
        "var.run_database_restore_verification_job ? 1 : 0"
    ) in database
    assert "kubernetes_secret_v1.postgresql_backup_inventory" in _text(
        "stages/workloads/postgresql_backup_monitoring.tf"
    )
    assert "depends_on = [kubernetes_job_v1.database_restore_verification]" in database
    assert "automount_service_account_token = false" in database


def test_public_envoy_has_two_replicas_required_spread_and_a_pdb() -> None:
    values = yaml.safe_load(
        _text("charts/control-plane/fs2-serve-control-plane/values.yaml")
    )
    schema = json.loads(
        _text("charts/control-plane/fs2-serve-control-plane/values.schema.json")
    )
    template = _text(
        "charts/control-plane/fs2-serve-control-plane/templates/envoyproxy.yaml"
    )

    gateway = values["publicGateway"]
    assert gateway["replicaCount"] >= 2
    assert gateway["podDisruptionBudget"] == {
        "enabled": True,
        "minAvailable": 1,
    }
    assert (
        schema["properties"]["publicGateway"]["properties"]["replicaCount"]["minimum"]
        == 2
    )
    assert "envoyDeployment:" in template
    assert "replicas: {{ .Values.publicGateway.replicaCount }}" in template
    assert "requiredDuringSchedulingIgnoredDuringExecution:" in template
    assert "topologyKey: kubernetes.io/hostname" in template
    assert "envoyPDB:" in template
    assert (
        "minAvailable: {{ .Values.publicGateway.podDisruptionBudget.minAvailable }}"
        in template
    )


def test_rollout_runbook_preserves_release_gate_and_rollback() -> None:
    runbook = _text("docs/SAI06_POSTGRESQL_RESILIENCE.md")

    assert "SAI-09" in runbook
    assert "do not apply" in runbook.lower()
    assert "firstRecoverabilityPoint" in runbook
    assert "rollback" in runbook.lower()
    assert "restore_verifier" in runbook
    assert "marker A" in runbook
    assert "marker B" in runbook
    assert "marker cleanup" in runbook.lower()
    assert "no replacement" in runbook.lower()
    assert "quota" in runbook.lower()
    assert "cost" in runbook.lower()
