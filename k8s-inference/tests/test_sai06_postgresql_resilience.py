from __future__ import annotations

import importlib.machinery
import importlib.util
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest import mock
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
                "service_account_id": "serviceaccount-postgresql-writer",
                "group_id": "group-postgresql-writer",
                "role": "storage.object-editor",
                "paths": ["postgresql/v1/fs2-control-db/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
            "restore_reader": {
                "service_account_id": "serviceaccount-postgresql-restore",
                "group_id": "group-postgresql-restore",
                "roles": ["storage.object-lister", "storage.object-viewer"],
                "paths": ["postgresql/v1/fs2-control-db/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
            "inventory_reader": {
                "service_account_id": "serviceaccount-postgresql-inventory",
                "group_id": "group-postgresql-inventory",
                "roles": ["storage.object-lister", "storage.object-viewer"],
                "paths": ["postgresql/v1/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
            "receipt_publisher": {
                "service_account_id": "serviceaccount-postgresql-receipt",
                "group_id": "group-postgresql-receipt",
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
        "postgresql_backup_restore_object_storage_access": {
            "key_id": "accesskey-postgresql-restore-test",
            "access_key_id": "TESTPOSTGRESQLRESTORE",
            "secret_reference_id": "mysterybox-postgresql-restore-test",
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


def _retained_destroy_contract() -> dict:
    return {
        "target": {"project_id": "project-test", "region": "eu-north1"},
        "stages": {
            "infrastructure": {
                "postgresql_backup": {
                    "enabled": True,
                    "retention_days": 30,
                    "object_storage": {
                        "bucket_name": "postgresql-backup-test",
                        "max_size_gib": 12288,
                    },
                    "lifecycle": {"retention_mode": "retain"},
                }
            },
            "workloads": {
                "postgresql_backup": {
                    "enabled": True,
                    "schedule": "0 0 2 * * *",
                }
            },
        },
    }


def _retained_destroy_dynamic(tmp_path: Path) -> dict:
    resource_ids = {
        "bucket": "storagebucket-postgresql-test",
        "service_account": "serviceaccount-postgresql-writer",
        "group": "group-postgresql-writer",
        "access_key": "accesskey-postgresql-writer",
        "restore_service_account": "serviceaccount-postgresql-restore",
        "restore_group": "group-postgresql-restore",
        "restore_access_key": "accesskey-postgresql-restore",
        "inventory_service_account": "serviceaccount-postgresql-inventory",
        "inventory_group": "group-postgresql-inventory",
        "inventory_access_key": "accesskey-postgresql-inventory",
        "receipt_service_account": "serviceaccount-postgresql-receipt",
        "receipt_group": "group-postgresql-receipt",
        "receipt_access_key": "accesskey-postgresql-receipt",
    }
    return {
        "run_id": "sai06test",
        "kubeconfig_path": str(tmp_path / "kubeconfig"),
        "kube_context": "sai06-test",
        "postgresql_backup_storage_contract": {
            "schema": "fs2-serve.nebius.ai/postgresql-backup-storage/v1",
            "project_id": "project-test",
            "region": "eu-north1",
            "object_storage": {
                "id": resource_ids["bucket"],
                "name": "postgresql-backup-test",
                "endpoint": "https://storage.eu-north1.nebius.cloud",
                "max_size_gib": 12288,
                "versioning_policy": "ENABLED",
                "storage_class": "STANDARD",
                "addressing_style": "path",
                "verify_tls": True,
            },
            "writer": {
                "service_account_id": resource_ids["service_account"],
                "group_id": resource_ids["group"],
                "role": "storage.object-editor",
                "paths": ["postgresql/v1/fs2-control-db/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
            "restore_reader": {
                "service_account_id": resource_ids["restore_service_account"],
                "group_id": resource_ids["restore_group"],
                "roles": ["storage.object-lister", "storage.object-viewer"],
                "paths": ["postgresql/v1/fs2-control-db/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
            "inventory_reader": {
                "service_account_id": resource_ids["inventory_service_account"],
                "group_id": resource_ids["inventory_group"],
                "roles": ["storage.object-lister", "storage.object-viewer"],
                "paths": ["postgresql/v1/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
            "receipt_publisher": {
                "service_account_id": resource_ids["receipt_service_account"],
                "group_id": resource_ids["receipt_group"],
                "role": "storage.uploader",
                "paths": ["postgresql/v1/restore-verification/success/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
            "layout": {
                "root": "postgresql/v1",
                "destination_path": "s3://postgresql-backup-test/postgresql/v1",
                "server_name": "fs2-control-db",
            },
            "retention": {
                "barman_retention_days": 30,
                "noncurrent_version_expiration_days": 37,
                "current_object_expiration": "cloudnative-pg-barman-owned",
                "lifecycle_rule_ids": [
                    "abort-incomplete-multipart-uploads",
                    "expire-noncurrent-versions-after-recovery-window",
                ],
            },
            "sizing": {
                "configured_capacity_gib": 12288,
                "required_capacity_gib": 11585,
                "capacity_cost_review_acknowledged": True,
                "live_capacity_preflight_required": True,
            },
            "lifecycle": {
                "retention_mode": "retain",
                "destroy_status": "blocked-retained",
                "destroy_completion": "full-stack-destroy-incomplete-postgresql-backup-retained",
                "adoption_status": "ids-exported-for-explicit-state-adoption",
                "retained_ids": {"bucket": resource_ids["bucket"]},
            },
        },
        "postgresql_backup_lifecycle": {
            "schema": "fs2-serve.nebius.ai/postgresql-backup-lifecycle/v1",
            "project_id": "project-test",
            "region": "eu-north1",
            "retention_mode": "retain",
            "status": "managed-retained",
            "destroy_status": "blocked-retained",
            "destroy_completion": "full-stack-destroy-incomplete-postgresql-backup-retained",
            "adoption_status": "ids-exported-for-explicit-state-adoption",
            "resource_ids": resource_ids,
        },
        "postgresql_backup_object_storage_access": {
            "key_id": resource_ids["access_key"]
        },
        "postgresql_backup_restore_object_storage_access": {
            "key_id": resource_ids["restore_access_key"]
        },
        "postgresql_backup_inventory_object_storage_access": {
            "key_id": resource_ids["inventory_access_key"]
        },
        "postgresql_backup_receipt_object_storage_access": {
            "key_id": resource_ids["receipt_access_key"]
        },
    }


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
        'postgresql_backup_restore_roles               = ["storage.object-lister", "storage.object-viewer"]'
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
    assert 'output "postgresql_backup_restore_object_storage_access"' in outputs
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


def test_capacity_plan_allows_only_stable_non_destructive_actions() -> None:
    accepted = _plan_document()
    STACK.require_sai06_no_replacements(accepted)

    for address, actions, previous_address in (
        ("nebius_mk8s_v1_cluster.this", ["delete", "create"], None),
        ("nebius_vpc_v1_security_group.workers", ["delete"], None),
        ("nebius_vpc_v1_security_group_rule.workers_egress", ["delete"], None),
        ("nebius_iam_v1_group_membership.node_pull", ["delete"], None),
        ("terraform_data.renamed", ["no-op"], "terraform_data.old"),
    ):
        document = _plan_document()
        document["resource_changes"].append(
            {
                "address": address,
                "previous_address": previous_address,
                "change": {"before": {}, "after": {}, "actions": actions},
            }
        )
        with pytest.raises(
            STACK.DeploymentError, match="only no-op.*stable addresses"
        ):
            STACK.require_sai06_no_replacements(document)

    unknown = _plan_document()
    unknown["resource_changes"][0]["change"]["actions"] = ["forget"]
    with pytest.raises(STACK.DeploymentError, match="unknown managed changes"):
        STACK.require_sai06_no_replacements(unknown)


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
    STACK.private_json(receipt_path, envelope)

    with (
        mock.patch.object(
            STACK, "verified_source_identity", return_value=("a" * 40, "b" * 40)
        ),
        mock.patch.object(STACK, "_git_json_blob", return_value=trust),
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
            plan_sha256=hashlib.sha256(b"exact-saved-plan").hexdigest(),
            plan_document=plan_document,
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


def test_secure_file_read_rejects_same_inode_same_size_mutation(
    tmp_path: Path,
) -> None:
    approval = tmp_path / "approval.json"
    approval.write_bytes(b'{"approved":true }')
    approval.chmod(0o600)
    original_read = STACK.os.read
    mutated = False

    def mutate_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        payload = original_read(descriptor, size)
        if not mutated and payload:
            mutated = True
            with approval.open("r+b", buffering=0) as stream:
                stream.seek(0)
                stream.write(b'{"approved":false}')
                os.fsync(stream.fileno())
        return payload

    with (
        mock.patch.object(STACK.os, "read", side_effect=mutate_after_first_read),
        pytest.raises(STACK.DeploymentError, match="changed while it was being read"),
    ):
        STACK._secure_file_bytes(approval, label="test approval", private=True)


def test_sealed_plan_survives_named_path_swap_and_rejects_writes(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "infrastructure-plan.tfplan"
    original = b"reviewed-plan-binary"
    plan.write_bytes(original)
    plan.chmod(0o600)

    with STACK._sealed_plan_snapshot(plan, label="test plan") as (descriptor, digest):
        replacement = tmp_path / "replacement.tfplan"
        replacement.write_bytes(b"attacker-plan-binary")
        replacement.chmod(0o600)
        replacement.replace(plan)
        os.lseek(descriptor, 0, os.SEEK_SET)
        assert os.read(descriptor, len(original) + 1) == original
        assert digest == hashlib.sha256(original).hexdigest()
        with pytest.raises(OSError):
            os.write(descriptor, b"x")


def test_reviewed_plan_uses_one_sealed_descriptor_and_rejects_json_divergence(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "workloads-plan.tfplan"
    plan.write_bytes(b"reviewed-workloads-plan")
    plan.chmod(0o600)
    reviewed = {"resource_changes": []}
    descriptors: list[int] = []

    def show(*args: object, **_kwargs: object) -> dict:
        descriptors.append(int(args[2]))
        return reviewed

    def apply(*args: object, **_kwargs: object) -> None:
        descriptors.append(int(args[2]))

    with (
        mock.patch.object(STACK, "show_sealed_plan", side_effect=show),
        mock.patch.object(STACK, "apply_sealed_plan", side_effect=apply),
    ):
        STACK.apply_reviewed_sai06_plan(
            "terraform",
            tmp_path,
            plan,
            reviewed,
            {},
        )
    assert len(descriptors) == 3
    assert len(set(descriptors)) == 1

    with (
        mock.patch.object(
            STACK,
            "show_sealed_plan",
            return_value={
                "resource_changes": [
                    {
                        "address": "terraform_data.unreviewed",
                        "change": {"actions": ["create"]},
                    }
                ]
            },
        ),
        mock.patch.object(STACK, "apply_sealed_plan") as apply_plan,
        pytest.raises(STACK.DeploymentError, match="no longer match"),
    ):
        STACK.apply_reviewed_sai06_plan(
            "terraform",
            tmp_path,
            plan,
            reviewed,
            {},
        )
    apply_plan.assert_not_called()


def test_destroy_apply_rederives_reviewed_json_from_the_same_sealed_plan(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "workloads-destroy.tfplan"
    plan.write_bytes(b"reviewed-destroy-plan")
    plan.chmod(0o600)
    reviewed = {
        "resource_changes": [
            {
                "address": "terraform_data.disposable",
                "change": {"actions": ["delete"]},
            }
        ]
    }
    descriptors: list[int] = []

    def show(*args: object, **_kwargs: object) -> dict:
        descriptors.append(int(args[2]))
        return reviewed

    def apply(*args: object, **_kwargs: object) -> None:
        descriptors.append(int(args[2]))

    with (
        mock.patch.object(STACK, "show_sealed_plan", side_effect=show),
        mock.patch.object(STACK, "apply_sealed_plan", side_effect=apply),
    ):
        STACK.apply_plan(
            "terraform",
            tmp_path,
            plan,
            {},
            reviewed,
        )

    assert len(descriptors) == 3
    assert len(set(descriptors)) == 1


def test_source_identity_rejects_dirty_tree_and_trust_comes_from_git_blob(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "sai06@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "SAI06 Test"],
        check=True,
    )
    trust_path = repository / "trusted.json"
    committed = {"schema": "trusted/v1", "issuers": [{"key": "reviewed"}]}
    trust_path.write_text(json.dumps(committed), encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "trusted.json"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "trusted issuer"],
        check=True,
    )
    with (
        mock.patch.object(STACK, "REPOSITORY_ROOT", repository),
        mock.patch.object(STACK, "SAI06_TRUSTED_ISSUERS_PATH", trust_path),
    ):
        commit, tree = STACK.verified_source_identity()
        assert re.fullmatch(r"[0-9a-f]{40}", tree)
        trust_path.write_text(
            json.dumps({"schema": "trusted/v1", "issuers": [{"key": "attacker"}]}),
            encoding="utf-8",
        )
        assert STACK._git_json_blob(commit, trust_path, label="trust") == committed
        with pytest.raises(STACK.DeploymentError, match="exact clean source"):
            STACK.verified_source_identity()
        subprocess.run(
            ["git", "-C", str(repository), "checkout", "--", "trusted.json"],
            check=True,
        )
        (repository / "untracked-key.json").write_text("{}", encoding="utf-8")
        with pytest.raises(STACK.DeploymentError, match="exact clean source"):
            STACK.verified_source_identity()


def test_sai06_planning_checks_source_before_plan_and_discards_changed_plan(
    tmp_path: Path,
) -> None:
    contract = {
        "stages": {
            "infrastructure": {"postgresql_backup": {"enabled": True}}
        }
    }
    commit = "a" * 40
    tree = "b" * 40
    plan = tmp_path / "infrastructure-plan.tfplan"

    with (
        mock.patch.object(
            STACK,
            "verified_source_identity",
            side_effect=STACK.DeploymentError("exact clean source required"),
        ),
        mock.patch.object(STACK, "plan_stage") as plan_stage,
        pytest.raises(STACK.DeploymentError, match="exact clean source"),
    ):
        STACK.plan_infrastructure_with_sai06(
            SimpleNamespace(terraform="terraform"),
            tmp_path,
            contract,
            commit,
            tmp_path / "infrastructure.tfvars.json",
        )
    plan_stage.assert_not_called()

    def generated_plan(*_args: object, **_kwargs: object) -> tuple[Path, dict, dict]:
        plan.write_bytes(b"untrusted plan")
        return plan, {"resource_changes": []}, {}

    @contextmanager
    def exact_snapshot(_commit: str, _run_root: Path):
        yield tmp_path / "snapshot" / "k8s-inference"

    with (
        mock.patch.object(
            STACK,
            "verified_source_identity",
            side_effect=[(commit, tree), (commit, "c" * 40)],
        ),
        mock.patch.object(
            STACK, "exact_git_source_snapshot", side_effect=exact_snapshot
        ),
        mock.patch.object(STACK, "plan_stage", side_effect=generated_plan),
        pytest.raises(STACK.DeploymentError, match="changed while Terraform was planning"),
    ):
        STACK.plan_infrastructure_with_sai06(
            SimpleNamespace(terraform="terraform"),
            tmp_path,
            contract,
            commit,
            tmp_path / "infrastructure.tfvars.json",
        )
    assert not plan.exists()


def test_sai06_plans_from_exact_git_object_not_mutable_worktree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "sai06@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "SAI06 Test"],
        check=True,
    )
    files = {
        "k8s-inference/main.tf": "reviewed-root\n",
        "k8s-inference/stages/infrastructure/main.tf": "reviewed-source\n",
        "k8s-inference/stages/foundation/main.tf": "reviewed-foundation\n",
        "k8s-inference/stages/workloads/main.tf": "reviewed-workloads\n",
        "k8s-inference/catalog/profiles/approved-targets.json": "{}\n",
        "modules/device-plugin/main.tf": "reviewed-device\n",
        "modules/gpu-operator/main.tf": "reviewed-gpu\n",
        "modules/network-operator/main.tf": "reviewed-network\n",
    }
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "reviewed source"],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    mutable = repository / "k8s-inference/stages/infrastructure/main.tf"
    mutable.write_text("attacker-worktree-source\n", encoding="utf-8")
    run_root = tmp_path / "run"

    with mock.patch.object(STACK, "REPOSITORY_ROOT", repository):
        with STACK.exact_git_source_snapshot(commit, run_root) as solution_root:
            extracted = solution_root / "stages/infrastructure/main.tf"
            assert extracted.read_text(encoding="utf-8") == "reviewed-source\n"
            assert extracted.stat().st_mode & 0o222 == 0
            snapshot_parent = solution_root.parent
        assert not snapshot_parent.exists()


def test_main_binds_root_and_every_stage_to_prevalidated_exact_source() -> None:
    stack = _text("inference-stack")

    source_index = stack.index("source_identity = verified_source_identity()")
    configuration_index = stack.index("contract, configuration_environment = validate_configuration(")
    assert source_index < configuration_index
    assert "root_override=source_root" in stack
    assert 'approved_roots = ("k8s-inference", "modules")' in stack
    assert 'source_root / "stages" / "foundation"' in stack
    assert 'source_root / "stages" / "workloads"' in stack
    assert "source_identity=(commit, source_tree)" in stack
    assert "source_tree=source_identity[1]" in stack


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
    configuration = _retained_destroy_contract()
    dynamic = _retained_destroy_dynamic(tmp_path)
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
        mock.patch.object(
            STACK,
            "validate_postgresql_live_recovery_boundary",
            return_value={"continuous_archiving": True},
        ) as live_recovery,
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
            source_root=ROOT,
            source_tree="b" * 40,
        )

    assert events == [
        "plan:workloads",
        "plan:foundation",
        "apply:workloads",
        "apply:foundation",
    ]
    assert live_recovery.call_count == 2
    write_infra.assert_not_called()
    receipt = json.loads((tmp_path / "postgresql-backup-retention.json").read_text())
    assert receipt["postgresql_backup"]["lifecycle"]["retention_mode"] == "retain"


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda dynamic: dynamic.update({"postgresql_backup_storage_contract": {}}),
            "outputs are incomplete",
        ),
        (
            lambda dynamic: dynamic.update({"postgresql_backup_lifecycle": {}}),
            "outputs are incomplete",
        ),
        (
            lambda dynamic: dynamic["postgresql_backup_storage_contract"][
                "object_storage"
            ].update({"versioning_policy": "DISABLED"}),
            "does not match the exact",
        ),
        (
            lambda dynamic: dynamic["postgresql_backup_lifecycle"].update(
                {"project_id": "project-attacker"}
            ),
            "does not match the exact",
        ),
        (
            lambda dynamic: dynamic["postgresql_backup_lifecycle"].update(
                {"destroy_status": "eligible"}
            ),
            "does not match the exact",
        ),
    ],
)
def test_destroy_rejects_invalid_retained_postgresql_boundary_before_any_plan(
    tmp_path: Path, mutator: Callable[[dict], None], message: str
) -> None:
    configuration = _retained_destroy_contract()
    dynamic = _retained_destroy_dynamic(tmp_path)
    mutator(dynamic)
    with (
        mock.patch.object(STACK, "state_has_resources", return_value=True),
        mock.patch.object(STACK, "state_ready", return_value=True),
        mock.patch.object(STACK, "infrastructure_outputs", return_value=dynamic),
        mock.patch.object(STACK, "plan_stage") as plan_stage,
        mock.patch.object(STACK, "apply_plan") as apply_plan,
        pytest.raises(STACK.DeploymentError, match=message),
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
            source_root=ROOT,
            source_tree="b" * 40,
        )
    plan_stage.assert_not_called()
    apply_plan.assert_not_called()


def _live_recovery_documents() -> list[dict]:
    cluster_uid = "11111111-2222-3333-4444-555555555555"
    return [
        {
            "metadata": {
                "name": "fs2-control-db",
                "namespace": "fs2-data",
                "uid": cluster_uid,
            },
            "status": {
                "firstRecoverabilityPoint": "2026-09-16T08:00:00Z",
                "lastSuccessfulBackup": "2026-09-16T08:10:00Z",
                "lastArchivedWal": "00000001000000000000000A",
                "conditions": [{"type": "ContinuousArchiving", "status": "True"}],
            },
        },
        {
            "metadata": {"name": "fs2-control-db", "namespace": "fs2-data"},
            "spec": {
                "schedule": "0 0 2 * * *",
                "suspend": False,
                "cluster": {"name": "fs2-control-db"},
            },
            "status": {"lastCheckTime": "2026-09-16T08:20:00Z"},
        },
        {
            "items": [
                {
                    "metadata": {
                        "name": "fs2-control-db-20260916020000",
                        "namespace": "fs2-data",
                        "uid": "66666666-7777-8888-9999-aaaaaaaaaaaa",
                        "ownerReferences": [
                            {
                                "apiVersion": "postgresql.cnpg.io/v1",
                                "kind": "Cluster",
                                "name": "fs2-control-db",
                                "uid": cluster_uid,
                                "controller": True,
                            }
                        ],
                    },
                    "spec": {"cluster": {"name": "fs2-control-db"}},
                    "status": {
                        "phase": "completed",
                        "stoppedAt": "2026-09-16T08:10:00Z",
                        "endWal": "000000010000000000000009",
                    },
                }
            ]
        },
    ]


def _provider_bucket_document() -> dict:
    return {
        "metadata": {
            "id": "storagebucket-postgresql-test",
            "parent_id": "project-test",
            "name": "postgresql-backup-test",
            "resource_version": 7,
        },
        "spec": {
            "versioning_policy": "ENABLED",
            "max_size_bytes": 12288 * 1024**3,
            "default_storage_class": "STANDARD",
            "lifecycle_configuration": {
                "rules": [
                    {
                        "id": "abort-incomplete-multipart-uploads",
                        "status": "ENABLED",
                        "abort_incomplete_multipart_upload": {
                            "days_after_initiation": 1
                        },
                    },
                    {
                        "id": "expire-noncurrent-versions-after-recovery-window",
                        "status": "ENABLED",
                        "noncurrent_version_expiration": {"noncurrent_days": 37},
                    },
                ]
            },
        },
        "status": {
            "state": "ACTIVE",
            "region": "eu-north1",
            "suspension_state": "NOT_SUSPENDED",
            "anonymous_access_enabled": False,
        },
    }


def _inventory_evidence() -> dict:
    return {
        "inventory_last_success": "2026-09-16T08:55:00Z",
        "restore_last_success": "2026-09-16T08:30:00Z",
        "usage_bytes": 1024,
        "object_versions": 2,
        "receipt_binding": {
            "backup_name": "fs2-control-db-20260916020000",
            "backup_uid": "66666666-7777-8888-9999-aaaaaaaaaaaa",
            "cluster_uid": "11111111-2222-3333-4444-555555555555",
            "run_id": "sai06test",
            "source_backup_wal": "000000010000000000000009",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "verified_wal": "00000001000000000000000A",
        },
        "metrics_sha256": "f" * 64,
    }


def test_live_destroy_gate_requires_backup_wal_and_recoverability_point(
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(
        kubectl="kubectl", nebius="nebius", nebius_profile="sandbox"
    )
    contract = _retained_destroy_contract()
    dynamic = _retained_destroy_dynamic(tmp_path)
    now = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)

    with (
        mock.patch.object(
            STACK,
            "_postgresql_provider_bucket",
            return_value={"bucket_id": "storagebucket-postgresql-test"},
        ),
        mock.patch.object(
            STACK, "_kubectl_json", side_effect=_live_recovery_documents()
        ),
        mock.patch.object(
            STACK, "_postgresql_inventory_evidence", return_value=_inventory_evidence()
        ) as inventory,
    ):
        result = STACK.validate_postgresql_live_recovery_boundary(
            args,
            contract,
            dynamic,
            source_commit="a" * 40,
            source_tree="b" * 40,
            now=now,
        )
    assert result["completed_backup_count"] == 1
    assert result["continuous_archiving"] is True
    assert result["selected_backup_uid"] == "66666666-7777-8888-9999-aaaaaaaaaaaa"
    inventory.assert_called_once()
    inventory_kwargs = inventory.call_args.kwargs
    assert inventory_kwargs["receipt_binding"] == {
        "backup_name": "fs2-control-db-20260916020000",
        "backup_uid": "66666666-7777-8888-9999-aaaaaaaaaaaa",
        "cluster_uid": "11111111-2222-3333-4444-555555555555",
        "run_id": "sai06test",
        "source_backup_wal": "000000010000000000000009",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
    }
    assert inventory_kwargs["current_wal"] == "00000001000000000000000A"

    for mutate in (
        lambda documents: documents[0]["metadata"].update({"uid": None}),
        lambda documents: documents[0]["status"].update(
            {"firstRecoverabilityPoint": "2001-01-01T00:00:00Z"}
        ),
        lambda documents: documents[0]["status"]["conditions"][0].update(
            {"status": "False"}
        ),
        lambda documents: documents[0]["status"].update(
            {"lastArchivedWal": "arbitrary-wal"}
        ),
        lambda documents: documents[0]["status"].update(
            {"lastArchivedWal": "000000000000000000000000"}
        ),
        lambda documents: documents[2]["items"][0]["status"].update(
            {"endWal": "00000001000000000000000A"}
        ),
        lambda documents: documents[2]["items"][0]["metadata"].update(
            {"uid": None}
        ),
        lambda documents: documents[2]["items"][0]["metadata"][
            "ownerReferences"
        ][0].update({"uid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}),
    ):
        documents = _live_recovery_documents()
        mutate(documents)
        with (
            mock.patch.object(
                STACK,
                "_postgresql_provider_bucket",
                return_value={"bucket_id": "storagebucket-postgresql-test"},
            ),
            mock.patch.object(STACK, "_kubectl_json", side_effect=documents),
            mock.patch.object(
                STACK,
                "_postgresql_inventory_evidence",
                return_value=_inventory_evidence(),
            ),
            pytest.raises(STACK.DeploymentError, match="no resource was changed"),
        ):
            STACK.validate_postgresql_live_recovery_boundary(
                args,
                contract,
                dynamic,
                source_commit="a" * 40,
                source_tree="b" * 40,
                now=now,
            )


def test_destroy_gate_gets_exact_provider_bucket_and_object_versions(
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(nebius="nebius", nebius_profile="sandbox")
    retained = STACK.validate_postgresql_retained_boundary(
        _retained_destroy_contract(), _retained_destroy_dynamic(tmp_path)
    )
    with mock.patch.object(
        STACK, "_nebius_json", return_value=_provider_bucket_document()
    ) as get_bucket:
        evidence = STACK._postgresql_provider_bucket(args, retained)
    assert evidence["resource_version"] == 7
    assert evidence["provider_document_sha256"] == STACK.canonical_sha256(
        _provider_bucket_document()
    )
    get_bucket.assert_called_once_with(
        args,
        [
            "storage",
            "bucket",
            "get",
            "--id",
            "storagebucket-postgresql-test",
        ],
        "object-storage bucket",
    )

    for mutate in (
        lambda document: document["metadata"].update({"parent_id": "project-other"}),
        lambda document: document["status"].update({"state": "UPDATING"}),
        lambda document: document["spec"].update(
            {"versioning_policy": "SUSPENDED"}
        ),
        lambda document: document["spec"]["lifecycle_configuration"]["rules"][
            1
        ]["noncurrent_version_expiration"].update({"noncurrent_days": 1}),
    ):
        document = _provider_bucket_document()
        mutate(document)
        with (
            mock.patch.object(STACK, "_nebius_json", return_value=document),
            pytest.raises(STACK.DeploymentError, match="no resource was changed"),
        ):
            STACK._postgresql_provider_bucket(args, retained)


def test_destroy_gate_requires_fresh_object_version_inventory(tmp_path: Path) -> None:
    now = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
    retained = STACK.validate_postgresql_retained_boundary(
        _retained_destroy_contract(), _retained_destroy_dynamic(tmp_path)
    )
    timestamps = {
        "inventory": datetime(2026, 9, 16, 8, 55, tzinfo=UTC).timestamp(),
        "restore": datetime(2026, 9, 16, 8, 30, tzinfo=UTC).timestamp(),
    }

    def metrics(
        *,
        versions: int = 2,
        inventory: float | None = None,
        restore: float | None = None,
    ) -> str:
        values = {
            "fs2_postgresql_backup_bucket_scrape_success": 1,
            "fs2_postgresql_backup_bucket_last_success_timestamp_seconds": (
                timestamps["inventory"] if inventory is None else inventory
            ),
            "fs2_postgresql_backup_bucket_usage_bytes": 1024,
            "fs2_postgresql_backup_bucket_current_bytes": 768,
            "fs2_postgresql_backup_bucket_noncurrent_bytes": 256,
            "fs2_postgresql_backup_bucket_capacity_bytes": 12288 * 1024**3,
            "fs2_postgresql_backup_bucket_object_versions": versions,
            "fs2_postgresql_restore_last_success_timestamp_seconds": timestamps[
                "restore"
            ] if restore is None else restore,
        }
        scalars = "".join(f"{name} {value}\n" for name, value in values.items())
        info = (
            'fs2_postgresql_restore_receipt_info{'
            'backup_name="fs2-control-db-20260916020000",'
            'backup_uid="66666666-7777-8888-9999-aaaaaaaaaaaa",'
            'cluster_uid="11111111-2222-3333-4444-555555555555",'
            'run_id="sai06test",'
            'source_backup_wal="000000010000000000000009",'
            'source_commit="' + "a" * 40 + '",'
            'source_tree="' + "b" * 40 + '",'
            'verified_wal="00000001000000000000000A"} 1\n'
        )
        return scalars + info

    receipt_binding = {
        "backup_name": "fs2-control-db-20260916020000",
        "backup_uid": "66666666-7777-8888-9999-aaaaaaaaaaaa",
        "cluster_uid": "11111111-2222-3333-4444-555555555555",
        "run_id": "sai06test",
        "source_backup_wal": "000000010000000000000009",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
    }

    with mock.patch.object(STACK, "_kubectl_raw", return_value=metrics()):
        result = STACK._postgresql_inventory_evidence(
            SimpleNamespace(),
            {},
            retained,
            now=now,
            receipt_binding=receipt_binding,
            current_wal="00000001000000000000000A",
        )
    assert result["object_versions"] == 2

    incomplete_binding = dict(receipt_binding)
    incomplete_binding.pop("source_tree")
    with (
        mock.patch.object(STACK, "_kubectl_raw", return_value=metrics()),
        pytest.raises(STACK.DeploymentError, match="no resource was changed"),
    ):
        STACK._postgresql_inventory_evidence(
            SimpleNamespace(),
            {},
            retained,
            now=now,
            receipt_binding=incomplete_binding,
            current_wal="00000001000000000000000A",
        )

    for invalid in (
        metrics(versions=0),
        metrics(inventory=datetime(2026, 9, 16, 8, 30, tzinfo=UTC).timestamp()),
        metrics(
            restore=(now - timedelta(hours=47)).timestamp(),
        ),
        metrics().replace('source_tree="' + "b" * 40, 'source_tree="' + "c" * 40),
        metrics().replace(
            'source_backup_wal="000000010000000000000009"',
            'source_backup_wal="000000000000000000000000"',
        ),
    ):
        with (
            mock.patch.object(STACK, "_kubectl_raw", return_value=invalid),
            pytest.raises(STACK.DeploymentError, match="no resource was changed"),
        ):
            STACK._postgresql_inventory_evidence(
                SimpleNamespace(),
                {},
                retained,
                now=now,
                receipt_binding=receipt_binding,
                current_wal="00000001000000000000000A",
            )


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
        tmp_path, contract, dynamic, source_identity=("a" * 40, "b" * 40)
    )
    generated_text = workloads_path.read_text(encoding="utf-8")
    backup = json.loads(generated_text)["postgresql_backup"]
    source_identity = json.loads(generated_text)["sai06_source_identity"]

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
        backup["restore_object_storage_access"]
        == dynamic["postgresql_backup_restore_object_storage_access"]
    )
    assert (
        backup["receipt_object_storage_access"]
        == dynamic["postgresql_backup_receipt_object_storage_access"]
    )
    assert "secret-access-key" not in generated_text
    assert "secret_value" not in generated_text
    assert source_identity == {"commit": "a" * 40, "tree": "b" * 40}


def test_backup_handoff_rejects_disposable_lifecycle(tmp_path: Path) -> None:
    contract, dynamic = _backup_handoff_inputs(tmp_path)
    dynamic["postgresql_backup_lifecycle"]["retention_mode"] = "disposable"
    STACK.private_directory(tmp_path)

    try:
        STACK.write_downstream_variables(
            tmp_path, contract, dynamic, source_identity=("a" * 40, "b" * 40)
        )
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
    assert "local.postgresql_restore_barman_object_store" in database
    assert "kubernetes_secret_v1.postgresql_backup_restore" in database
    assert 'credential-purpose" = "postgresql-backup-restore-reader"' in database
    assert "postgresql_backup_restore_credential_revision" in database


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
    database = _text("stages/workloads/database.tf")

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
    assert "fs2_postgresql_restore_receipt_info" in metrics
    assert "> 129600" in monitoring
    assert "within 36 hours" in monitoring
    assert "restore-verification/success/" in monitoring
    assert (
        '"fs2.nebius.ai/postgresql-inventory-credential-sha256" = '
        "local.postgresql_backup_inventory_credential_identity_sha256" in monitoring
    )
    for field in (
        "inventory_object_storage_access.key_id",
        "inventory_object_storage_access.access_key_id",
        "inventory_object_storage_access.secret_reference_id",
        "inventory_object_storage_access.resource_version",
    ):
        assert field in database
    assert (
        "parseint(substr(local.postgresql_backup_inventory_credential_identity_sha256, 0, 15), 16)"
        in database
    )
    assert (
        "pod_template_annotation    = local.postgresql_backup_inventory_credential_identity_sha256"
        in monitoring
    )


def test_inventory_rotation_identity_resists_the_reviewed_24_bit_collision() -> None:
    database = _text("stages/workloads/database.tf")
    assert "0, 6" not in database
    assert "0, 15" in database
    assert "tostring(var.postgresql_backup.credential_generation)" in database

    def digest(resource_version: int) -> str:
        identity = "|".join(
            (
                "1",
                "accesskey-postgresqlinventorytest",
                "AJE000POSTGRESQLINVENTORY",
                "mysteryboxsecret-postgresqlinventorytest",
                str(resource_version),
            )
        )
        return hashlib.sha256(identity.encode()).hexdigest()

    old = digest(1872)
    new = digest(2695)
    assert old != new
    assert old[:15] != new[:15]
    assert int(old[:15], 16) != int(new[:15], 16)


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
    for binding in (
        '"source_tree"',
        '"source_cluster_uid"',
        '"source_backup_uid"',
        '"source_backup_wal"',
        '"verified_wal"',
    ):
        assert binding in database
    assert "datetime.timedelta(hours=24)" in database
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
