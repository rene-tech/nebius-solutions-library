from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml


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
                "paths": ["postgresql/v1/*"],
                "secret_delivery": "MYSTERY_BOX",
            },
        },
        "postgresql_backup_object_storage_access": {
            "key_id": "accesskey-postgresql-test",
            "access_key_id": "TESTPOSTGRESQLACCESSKEY",
            "secret_reference_id": "mysterybox-postgresql-test",
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
    assert 'paths    = ["postgresql/v1/*"]' in infrastructure
    assert 'roles    = ["storage.object-editor"]' in infrastructure
    assert 'output "postgresql_backup_storage_contract"' in outputs
    assert 'output "postgresql_backup_object_storage_access"' in outputs


def test_postgresql_backup_capacity_is_retention_aware_and_live_checked() -> None:
    root_variables = _text("variables.tf")
    root_locals = _text("locals.tf")
    infrastructure = _text("stages/infrastructure/postgresql_backup.tf")
    stack = _text("inference-stack")

    assert "estimated_daily_wal_gib" in root_variables
    assert "capacity_headroom_percent" in root_variables
    assert "postgresql_backup_required_capacity_gib" in root_locals
    assert "max_size_gib = optional(number, 6144)" in root_variables
    assert "var.postgresql_backup.object_storage.max_size_gib >=" in infrastructure
    assert "local.postgresql_backup_required_capacity_gib" in infrastructure
    assert "preflight_postgresql_backup_capacity" in stack
    assert '"storage.bucket.size.standard"' in stack
    assert '"sai06-postgresql-capacity-preflight.json"' in stack


def test_live_capacity_preflight_binds_usage_sizing_and_cost_review() -> None:
    contract = {
        "target": {"project_id": "project-test", "region": "eu-north1"},
        "stages": {
            "infrastructure": {
                "system_pool": {
                    "node_count": 3,
                    "three_node_ha_cost_review_acknowledged": True,
                },
                "postgresql_backup": {
                    "object_storage": {"max_size_gib": 6144},
                    "retention_days": 30,
                    "database_volume_size_gib": 100,
                    "estimated_daily_wal_gib": 32,
                    "capacity_headroom_percent": 25,
                    "required_capacity_gib": 5480,
                    "capacity_cost_review_acknowledged": True,
                },
            }
        },
    }
    payload = {
        "items": [
            {
                "metadata": {"name": "storage.bucket.size.standard"},
                "spec": {"region": "eu-north1"},
                "status": {
                    "usage": "239725098497",
                    "unit": "byte",
                    "usage_state": "USAGE_STATE_USED",
                },
            }
        ]
    }

    with mock.patch.object(
        STACK,
        "run",
        return_value=subprocess.CompletedProcess(
            ["nebius"], 0, stdout=json.dumps(payload), stderr=""
        ),
    ):
        evidence = STACK.preflight_postgresql_backup_capacity(
            SimpleNamespace(nebius="nebius", nebius_profile="sandbox"), contract
        )

    assert evidence["required_capacity_gib"] == 5480
    assert evidence["configured_bucket_max_gib"] == 6144
    assert evidence["observed_usage_bytes"] == 239725098497
    assert evidence["provider_explicit_limit_bytes"] is None
    assert evidence["projected_usage_if_fully_allocated_bytes"] > evidence[
        "observed_usage_bytes"
    ]
    assert (
        evidence["provider_limit_verdict"]
        == "provider-limit-not-exposed-manual-quota-review-required"
    )
    assert evidence["system_pool_nodes"] == 3
    assert evidence["cost_review_acknowledged"] is True

    contract["stages"]["infrastructure"]["system_pool"]["node_count"] = 1
    with (
        mock.patch.object(
            STACK,
            "run",
            return_value=subprocess.CompletedProcess(
                ["nebius"], 0, stdout=json.dumps(payload), stderr=""
            ),
        ),
        pytest.raises(STACK.DeploymentError, match="three nodes"),
    ):
        STACK.preflight_postgresql_backup_capacity(
            SimpleNamespace(nebius="nebius", nebius_profile="sandbox"), contract
        )

    contract["stages"]["infrastructure"]["system_pool"]["node_count"] = 3
    payload["items"][0]["status"]["limit"] = "1000000000000"
    with (
        mock.patch.object(
            STACK,
            "run",
            return_value=subprocess.CompletedProcess(
                ["nebius"], 0, stdout=json.dumps(payload), stderr=""
            ),
        ),
        pytest.raises(STACK.DeploymentError, match="exceeds the live provider limit"),
    ):
        STACK.preflight_postgresql_backup_capacity(
            SimpleNamespace(nebius="nebius", nebius_profile="sandbox"), contract
        )


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
    assert "credential|secret|token|session|operation|audit|payload|artifact" in database
    assert "pg_attribute" in database


def test_explicit_system_pool_requires_three_node_cost_acknowledgement() -> None:
    root_variables = _text("variables.tf")
    infrastructure_variables = _text("stages/infrastructure/variables.tf")
    example = _text("terraform.tfvars.example")

    for source in (root_variables, infrastructure_variables):
        assert "three_node_ha_cost_review_acknowledged" in source
        assert "node_count >= 3" in source
    assert re.search(
        r"three_node_ha_cost_review_acknowledged\s*=\s*true", example
    )


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
