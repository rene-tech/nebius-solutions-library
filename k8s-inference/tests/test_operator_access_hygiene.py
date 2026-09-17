from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


GUARD = load_script(
    "secret_migration_guard", ROOT / "scripts/secret_migration_guard.py"
)
HANDOFF = load_script("operator_handoff", ROOT / "scripts/operator_handoff.py")
PAT_GUARD = load_script("pat_rotation_guard", ROOT / "scripts/pat_rotation_guard.py")
ROTATION = load_script("credential_rotation", ROOT / "scripts/credential_rotation.py")
STACK = load_script("inference_stack", ROOT / "inference-stack")


def disposition_evidence(
    directory: Path,
    index: int,
    artifact: dict[str, str],
    *,
    action: str,
) -> dict[str, str]:
    evidence = {
        "provider": "encrypted-audit-store",
        "object_id": f"artifact-{index}",
        "version_id": f"version-{index}",
        "audit_event_id": f"audit-{index}",
        "verified_at": "2099-01-01T00:00:00Z",
        "verifier": "independent-security-review",
    }
    receipt_path = directory / f"provider-disposition-{index}.json"
    GUARD.write_private_json(
        receipt_path,
        {
            "schema": "fs2-serve.nebius.ai/artifact-disposition-provider-receipt/v1",
            **evidence,
            "action": action,
            "source_sha256": artifact["sha256"],
            "status": (
                "rewrapped-and-verified"
                if action == "encrypted-rewrap"
                else "securely-retired-and-verified"
            ),
        },
    )
    return {
        **evidence,
        "receipt_path": str(receipt_path.absolute()),
        "receipt_sha256": GUARD.file_sha256(receipt_path),
    }


def provider_inventory(readers: list[str]) -> dict[str, object]:
    return {
        "provider_version": "resource-version-17",
        "expires_at": "2099-01-01T00:00:00Z",
        "readers": readers,
    }


def hcl_blocks(source: str, block_type: str) -> list[tuple[str, str]]:
    labels = (
        r'"([^"]+)"\s+"([^"]+)"'
        if block_type in {"resource", "ephemeral"}
        else r'"([^"]+)"'
    )
    pattern = re.compile(rf"{re.escape(block_type)}\s+{labels}\s*\{{")
    blocks: list[tuple[str, str]] = []
    for match in pattern.finditer(source):
        depth = 0
        for index in range(match.end() - 1, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    name_group = 2 if block_type in {"resource", "ephemeral"} else 1
                    blocks.append(
                        (match.group(name_group), source[match.start() : index + 1])
                    )
                    break
        else:
            raise AssertionError(f"unterminated {block_type} block")
    return blocks


def hcl_resource_blocks(source: str) -> list[tuple[str, str, str]]:
    pattern = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"\s*\{')
    blocks: list[tuple[str, str, str]] = []
    for match in pattern.finditer(source):
        depth = 0
        for index in range(match.end() - 1, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(
                        (
                            match.group(1),
                            match.group(2),
                            source[match.start() : index + 1],
                        )
                    )
                    break
    return blocks


class OperatorAccessHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider_identity = {
            "realpath": "/usr/local/bin/provider",
            "device": 1,
            "inode": 2,
            "uid": 0,
            "sha256": "f" * 64,
            "arguments_sha256": hashlib.sha256(b"[]").hexdigest(),
            "argument_files": [],
        }
        patcher = mock.patch.object(
            ROTATION,
            "provider_command_identity",
            return_value=self.provider_identity,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fixed_generation_one_resources_are_preserved_and_protected(self) -> None:
        secrets = (ROOT / "stages/workloads/secrets.tf").read_text(encoding="utf-8")
        bootstrap = (ROOT / "stages/workloads/bootstrap_access.tf").read_text(
            encoding="utf-8"
        )
        resources = dict(hcl_blocks(secrets + "\n" + bootstrap, "resource"))
        self.assertIn("database", resources)
        self.assertIn("key_material", resources)
        self.assertIn("admin_token", resources)
        self.assertIn("bootstrap_access_token_secret", resources)
        for name, key_id in {
            "payload_keyring": "payload-v1",
            "ledger_keyring": "ledger-v1",
            "token_pepper": "pepper-v1",
        }.items():
            self.assertIn(key_id, resources[name])
            self.assertIn("prevent_destroy = true", resources[name])
            self.assertNotIn("data_wo", resources[name])
        self.assertIn("storage-v1", resources["storage_keyring"])
        self.assertIn("storage-name-v1", resources["storage_keyring"])
        self.assertIn("data_wo_revision = 1", resources["storage_keyring"])
        self.assertIn("prevent_destroy = true", resources["storage_keyring"])
        self.assertNotIn("ignore_changes", resources["storage_keyring"])
        self.assertIn("prevent_destroy = true", resources["route_attestors"])
        self.assertIn("prevent_destroy = true", resources["admin"])
        self.assertIn("prevent_destroy = true", resources["admin_token"])
        self.assertIn("prevent_destroy = true", resources["key_material"])
        self.assertIn("prevent_destroy = true", resources["database"])
        self.assertIn("prevent_destroy = true", resources["database_account"])
        for name in (
            "bootstrap_access_token_id",
            "bootstrap_access_token_secret",
            "scientific_access_token_id",
            "scientific_access_token_secret",
            "bootstrap_access",
            "scientific_access",
        ):
            self.assertIn("prevent_destroy = true", resources[name], name)

    def test_database_rotation_adds_logins_before_switching_write_only_consumers(
        self,
    ) -> None:
        secrets = (ROOT / "stages/workloads/secrets.tf").read_text(encoding="utf-8")
        database = (ROOT / "stages/workloads/database.tf").read_text(encoding="utf-8")
        variables = (ROOT / "stages/workloads/variables.tf").read_text(encoding="utf-8")
        wrapper = (ROOT / "inference-stack").read_text(encoding="utf-8")
        resources = dict(hcl_blocks(secrets + "\n" + database, "resource"))

        versioned = resources["database_account_versioned"]
        self.assertIn("var.credential_generation_history.database", secrets)
        self.assertIn("data_wo", versioned)
        self.assertIn("data_wo_revision", versioned)
        self.assertIn("prevent_destroy = true", versioned)
        self.assertIn("database_passwords", variables)
        self.assertIn("ephemeral   = true", variables)
        self.assertIn('"database_passwords": "FS2_DATABASE_PASSWORDS_JSON"', wrapper)

        cluster = resources["control_database"]
        self.assertIn("local.database_versioned_accounts", cluster)
        self.assertIn("kubernetes_secret_v1.database_account_versioned", cluster)
        self.assertIn("local.database_role_memberships[identity.account]", cluster)
        fixed_consumer = resources["database_consumer"]
        versioned_consumer = resources["database_consumer_versioned"]
        self.assertIn("data_wo_revision = 1", fixed_consumer)
        self.assertIn("random_password.database", fixed_consumer)
        self.assertIn("local.database_versioned_consumers", versioned_consumer)
        self.assertIn("data_wo_revision = each.value.generation", versioned_consumer)
        self.assertNotIn("ignore_changes", versioned_consumer)
        for consumer in (fixed_consumer, versioned_consumer):
            self.assertIn("kubernetes_manifest.control_database", consumer)
            self.assertIn("terraform_data.credential_migration_gate", consumer)

    def test_key_classes_rotate_independently_with_retained_v1_and_write_only_delivery(
        self,
    ) -> None:
        secrets = (ROOT / "stages/workloads/secrets.tf").read_text(encoding="utf-8")
        variables = (ROOT / "stages/workloads/variables.tf").read_text(encoding="utf-8")
        for key_class in (
            "payload",
            "ledger",
            "pepper",
            "attestor",
            "storage",
            "storage_name",
        ):
            self.assertRegex(variables, rf"(?m)^\s*{key_class}\s+= optional\(object")
        self.assertNotIn("key_material = optional", variables)
        for resource, legacy_id in {
            "payload_keyring_versioned": "payload-v1",
            "ledger_keyring_versioned": "ledger-v1",
            "token_pepper_versioned": "pepper-v1",
            "route_attestors_versioned": "generation-1 public key",
            "storage_keyring_versioned": "storage-v1",
            "storage_name_keyring_versioned": "storage-name-v1",
        }.items():
            block = dict(hcl_blocks(secrets, "resource"))[resource]
            self.assertIn("data_wo", block)
            self.assertIn("data_wo_revision", block)
            self.assertIn(legacy_id, block)
        self.assertIn("var.keyring_generations.payload.retained", secrets)
        self.assertIn("var.keyring_generations.ledger.retained", secrets)
        self.assertIn("var.keyring_generations.pepper.retained", secrets)
        self.assertIn("var.keyring_generations.storage.retained", secrets)
        self.assertIn("var.keyring_generations.storage_name.retained", secrets)
        self.assertIn('"storage", "storage_name"', secrets)
        self.assertIn(
            "FS2_STORAGE_KEYRINGS_JSON", (ROOT / "inference-stack").read_text()
        )
        self.assertIn(
            "FS2_STORAGE_NAME_KEYRINGS_JSON", (ROOT / "inference-stack").read_text()
        )
        self.assertGreaterEqual(secrets.count("prevent_destroy = true"), 9)

    def test_secret_consumers_have_nonsecret_generation_rollout_triggers(self) -> None:
        templates = {
            path.name: path.read_text(encoding="utf-8")
            for path in (
                ROOT / "charts/control-plane/fs2-serve-control-plane/templates"
            ).glob("*.yaml")
        }
        for name in (
            "deployment.yaml",
            "model-controller-deployment.yaml",
            "migration-job.yaml",
            "bootstrap-access-job.yaml",
            "bootstrap-scientific-access-job.yaml",
            "maintenance-cronjob.yaml",
        ):
            self.assertIn("fs2.nebius.ai/secret-rollout-sha256", templates[name], name)
        runtime = templates["deployment.yaml"]
        for generation in (
            "database",
            "admin",
            "payload",
            "ledger",
            "pepper",
            "attestor",
            "storage",
            "storageName",
            "artifactStore",
        ):
            self.assertIn(f'"{generation}" .Values.secretRollout.', runtime)
        bootstrap = templates["bootstrap-access-job.yaml"]
        for generation in ("database", "access", "payload", "ledger", "pepper"):
            self.assertIn(f'"{generation}" .Values.secretRollout.', bootstrap)
        workloads = (ROOT / "stages/workloads/control_plane.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn("atomic           = true", workloads)
        self.assertIn("wait             = true", workloads)
        self.assertIn("kubernetes_secret_v1.payload_keyring_versioned", workloads)
        self.assertIn(
            "artifactStoreGeneration = var.scientific_artifacts.credential_generation",
            workloads,
        )
        foundation = (ROOT / "stages/foundation/releases.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn('"fs2.nebius.ai/secret-rollout-generation"', foundation)

    def test_singleton_write_only_secrets_use_retained_generation_cutovers(
        self,
    ) -> None:
        sources = {
            "foundation": (ROOT / "stages/foundation/cluster_contract.tf").read_text(
                encoding="utf-8"
            ),
            "secrets": (ROOT / "stages/workloads/secrets.tf").read_text(
                encoding="utf-8"
            ),
            "database": (ROOT / "stages/workloads/database.tf").read_text(
                encoding="utf-8"
            ),
            "modelexpress": (ROOT / "stages/workloads/modelexpress.tf").read_text(
                encoding="utf-8"
            ),
            "artifacts": (ROOT / "stages/workloads/scientific_artifacts.tf").read_text(
                encoding="utf-8"
            ),
        }
        expected_pairs = {
            "foundation": ("grafana_admin", "grafana_admin_versioned"),
            "database": (
                "database_consumer",
                "database_consumer_versioned",
                "grafana_datasource",
                "grafana_datasource_versioned",
            ),
            "modelexpress": (
                "modelexpress_nvcrio",
                "modelexpress_nvcrio_versioned",
            ),
            "artifacts": (
                "scientific_artifact_store",
                "scientific_artifact_store_versioned",
            ),
            "secrets": (
                "storage_keyring",
                "storage_keyring_versioned",
                "storage_name_keyring_versioned",
                "ngc_api_key",
                "ngc_api_key_versioned",
                "nvcrio_cred",
                "nvcrio_cred_versioned",
                "dcgm_exporter_nvcrio",
                "dcgm_exporter_nvcrio_versioned",
            ),
        }
        for source_name, names in expected_pairs.items():
            blocks = dict(hcl_blocks(sources[source_name], "resource"))
            for name in names:
                with self.subTest(source=source_name, resource=name):
                    self.assertIn(name, blocks)
                    self.assertIn("data_wo_revision", blocks[name])
                    self.assertIn("prevent_destroy = true", blocks[name])
                    self.assertNotIn("ignore_changes", blocks[name])
                    self.assertIn(
                        "terraform_data.credential_migration_gate", blocks[name]
                    )

        control_plane = (ROOT / "stages/workloads/control_plane.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn("active_database_consumer_secret_names", control_plane)
        self.assertIn("scientific_artifact_store_versioned", control_plane)
        monitoring = (ROOT / "stages/workloads/observability.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn("retained_dcgm_nvcrio_secret_names", monitoring)
        releases = (ROOT / "stages/foundation/releases.tf").read_text(encoding="utf-8")
        self.assertIn("active_grafana_admin_secret_ref", releases)
        self.assertIn("grafana_admin_versioned", releases)

        helpers = (
            ROOT / "charts/control-plane/fs2-serve-control-plane/templates/_helpers.tpl"
        ).read_text(encoding="utf-8")
        settings = (
            ROOT / "components/control-plane/src/fs2_serve/settings.py"
        ).read_text(encoding="utf-8")
        for contract in (
            "fs2-serve.storageCryptoEnv",
            "fs2-serve.storageCryptoVolumes",
            "fs2-serve.storageCryptoVolumeMounts",
            "fs2-serve.storageCipherVolume",
            "fs2-serve.storageCipherVolumeMount",
        ):
            self.assertIn(f'define "{contract}"', helpers)
        self.assertIn(".Values.secrets.storageCipherKeyring.name", helpers)
        self.assertIn(".Values.secrets.storageNameKeyring.name", helpers)
        self.assertIn("FS2_USER_STORAGE_KEYRING_FILE", helpers)
        self.assertIn("FS2_USER_STORAGE_NAME_KEYRING_FILE", helpers)
        self.assertIn("user_storage_keyring_file", settings)
        self.assertIn("user_storage_name_keyring_file", settings)

    def test_reference_data_s3_uses_new_provider_ids_and_retained_secrets(
        self,
    ) -> None:
        infrastructure = (ROOT / "stages/infrastructure/storage.tf").read_text()
        module = (ROOT / "reference-data/terraform/main.tf").read_text()
        cloud = dict(hcl_blocks(infrastructure, "resource"))
        secrets = dict(hcl_blocks(module, "resource"))
        self.assertIn("ignore_changes  = all", cloud["reference_data"])
        self.assertIn("prevent_destroy = true", cloud["reference_data"])
        self.assertIn("reference_data_versioned", cloud)
        self.assertIn(
            "credential_generation_history", cloud["reference_data_versioned"]
        )
        self.assertNotIn("ignore_changes", cloud["reference_data_versioned"])
        for name in ("object_storage", "object_storage_versioned"):
            self.assertIn("prevent_destroy = true", secrets[name])
            self.assertIn("data_wo_revision", secrets[name])
            self.assertNotIn("ignore_changes", secrets[name])
        self.assertIn(
            "credentials_secret    = local.credentials_secrets[tostring(var.credential_generation)]",
            module,
        )
        self.assertIn('"current-write" : "retained-read"', module)

    def test_control_plane_allowlist_accepts_only_canonical_hosts(self) -> None:
        for path in ("variables.tf", "stages/infrastructure/variables.tf"):
            source = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn("? 128 : 32}", source)
            self.assertIn("cidr ==", source)
        self.assertEqual(
            HANDOFF.host_cidrs(["192.0.2.8/32", "2001:db8::1/128"]),
            ["192.0.2.8/32", "2001:db8::1/128"],
        )
        for bypass in (["0.0.0.0/0"], ["0.0.0.0/1", "128.0.0.0/1"], ["192.0.2.7/24"]):
            with self.subTest(bypass=bypass), self.assertRaises(HANDOFF.HandoffError):
                HANDOFF.host_cidrs(bypass)

    def test_sensitive_outputs_are_secret_references_only(self) -> None:
        outputs = (ROOT / "stages/workloads/outputs.tf").read_text(encoding="utf-8")
        for output_name in (
            "admin_token",
            "admin_bootstrap_token",
            "mcp_access_token",
            "inference_access_token",
            "scientific_access_token",
            "grafana_admin_username",
            "grafana_admin_password",
        ):
            self.assertNotIn(f'output "{output_name}"', outputs)
        access = dict(hcl_blocks(outputs, "output"))["access_bundle"]
        self.assertIn("access-bundle-contract/v2", access)
        self.assertIn("credential_secret_refs", access)
        self.assertNotIn("random_password", access)
        self.assertGreaterEqual(
            access.count("expires_at      = var.bootstrap_access_expires_at"), 2
        )

    def test_privileged_source_credentials_cannot_be_given_metadata_only_expiry(
        self,
    ) -> None:
        with mock.patch.object(STACK, "kubernetes_secret_values") as secret_values:
            for kind in ("admin", "grafana"):
                with (
                    self.subTest(kind=kind),
                    self.assertRaisesRegex(STACK.DeploymentError, "unsupported"),
                ):
                    STACK.scoped_credential_handoff(
                        "terraform",
                        "kubectl",
                        Path("/private/run"),
                        {},
                        kind=kind,
                        expires_in_seconds=3600,
                    )
            secret_values.assert_not_called()

    def test_plan_guard_rejects_fixed_generation_delete_or_replace(self) -> None:
        safe = {
            "resource_changes": [
                {
                    "address": "random_password.admin_token",
                    "change": {"actions": ["no-op"]},
                }
            ]
        }
        self.assertEqual(GUARD.inspect_plan(safe)["protected_changes"], 0)
        for actions in (["update"], ["delete"], ["delete", "create"], ["replace"]):
            with self.subTest(actions=actions), self.assertRaises(GUARD.GuardError):
                GUARD.inspect_plan(
                    {
                        "resource_changes": [
                            {
                                "address": "random_password.admin_token",
                                "change": {"actions": actions},
                            }
                        ]
                    }
                )

    def test_plan_guard_rejects_previous_address_escape_and_all_credential_classes(
        self,
    ) -> None:
        for previous, address in (
            ("kubernetes_secret_v1.bootstrap_access", "terraform_data.unprotected"),
            (
                "nebius_iam_v2_access_key.reference_data[0]",
                "nebius_iam_v2_access_key.unregistered[0]",
            ),
            (
                "kubernetes_secret_v1.grafana_admin[0]",
                "kubernetes_secret_v1.unregistered[0]",
            ),
        ):
            with (
                self.subTest(previous=previous),
                self.assertRaisesRegex(GUARD.GuardError, "move"),
            ):
                GUARD.inspect_plan(
                    {
                        "resource_changes": [
                            {
                                "address": address,
                                "previous_address": previous,
                                "change": {"actions": ["no-op"]},
                            }
                        ]
                    }
                )

        registry = GUARD.load_registry()
        required = {
            "kubernetes_secret_v1.postgresql_backup[0]",
            "kubernetes_secret_v1.scientific_artifact_store[0]",
            "kubernetes_secret_v1.ngc_api_key[0]",
            "kubernetes_secret_v1.nvcrio_cred[0]",
            "kubernetes_secret_v1.dcgm_exporter_nvcrio[0]",
            "kubernetes_secret_v1.modelexpress_nvcrio[0]",
            "kubernetes_secret_v1.grafana_admin[0]",
            "kubernetes_secret_v1.grafana_datasource",
            "nebius_iam_v2_access_key.postgresql_backup[0]",
            "nebius_iam_v2_access_key.scientific_artifacts[0]",
            "nebius_iam_v2_access_key.reference_data[0]",
            'random_password.key_material["storage"]',
            'random_password.key_material["storage_name"]',
            "kubernetes_secret_v1.storage_keyring",
            'kubernetes_secret_v1.storage_keyring_versioned["2"]',
            'kubernetes_secret_v1.storage_name_keyring_versioned["2"]',
        }
        self.assertTrue(
            all(
                GUARD.is_protected_address(address, registry=registry)
                for address in required
            )
        )

    def test_durable_registry_exactly_covers_current_terraform_credentials(
        self,
    ) -> None:
        registry = GUARD.load_registry()
        expected = {
            (item["root"], item["address"])
            for item in registry["terraform_resource_addresses"]
        }
        observed: set[tuple[str, str]] = set()
        source_roots = (
            ("foundation", "", ROOT / "stages/foundation"),
            ("infrastructure", "", ROOT / "stages/infrastructure"),
            ("workloads", "", ROOT / "stages/workloads"),
            (
                "workloads",
                "module.reference_data.",
                ROOT / "reference-data/terraform",
            ),
        )
        credential_types = {
            "random_password",
            "random_id",
            "kubernetes_secret_v1",
            "nebius_iam_v2_access_key",
        }
        for root_name, address_prefix, root in source_roots:
            source = "\n".join(
                path.read_text(encoding="utf-8") for path in sorted(root.glob("*.tf"))
            )
            for resource_type, resource_name, block in hcl_resource_blocks(source):
                if resource_type not in credential_types:
                    continue
                address = f"{address_prefix}{resource_type}.{resource_name}"
                observed.add((root_name, address))
                self.assertIn("prevent_destroy = true", block, address)
                if "data_wo" in block:
                    self.assertIn("data_wo_revision", block, address)
                    self.assertNotIn("ignore_changes", block, address)
                elif (
                    resource_type == "nebius_iam_v2_access_key"
                    and resource_name.endswith("_versioned")
                ):
                    self.assertNotIn("ignore_changes", block, address)
                else:
                    self.assertIn("ignore_changes  = all", block, address)
                self.assertIn(
                    "terraform_data.credential_migration_gate", block, address
                )
        self.assertEqual(observed, expected)

        for root in dict.fromkeys(item[2] for item in source_roots):
            gate = (root / "credential_migration_gate.tf").read_text(encoding="utf-8")
            self.assertIn('data "external" "credential_migration_gate"', gate)
            self.assertIn("secret_migration_guard.py", gate)
            if root == ROOT / "reference-data/terraform":
                self.assertNotIn(
                    'resource "terraform_data" "credential_apply_gate_generation"',
                    gate,
                )
                self.assertNotIn("apply-saved-plan-gate", gate)
            else:
                self.assertIn(
                    "for_each = setunion(var.credential_migration_gate_history, toset([var.credential_migration_gate_receipt_sha256]))",
                    gate,
                )
                self.assertIn("apply-saved-plan-gate", gate)
            self.assertIn("prevent_destroy = true", gate)

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(GUARD.GuardError, "exact saved-plan receipt"),
        ):
            GUARD.validate_saved_plan_gate_from_environment(
                terraform_configuration=ROOT / "stages/workloads",
                terraform_root="workloads",
                source_commit="a" * 40,
            )

    def test_fixed_v1_identity_receipt_is_value_free_and_exact(self) -> None:
        secret_value = "must-not-appear-in-receipt"
        values = {"id": "legacy-id", "result": secret_value}
        state = {
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "address": "random_password.bootstrap_access_token_secret",
                            "values": values,
                        }
                    ]
                }
            }
        }
        plan = {
            "prior_state": state,
            "resource_changes": [
                {
                    "address": "random_password.bootstrap_access_token_secret",
                    "change": {"before": values, "after": values, "actions": ["no-op"]},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "fixed-v1-identity.receipt.json"
            receipt = GUARD.write_identity_receipt(
                state,
                path,
                source_commit="a" * 40,
            )
            self.assertNotIn(secret_value, path.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            result = GUARD.inspect_plan(plan, identity_receipt=receipt)
            self.assertEqual(result["verified_identities"], 1)
            with self.assertRaisesRegex(GUARD.GuardError, "require.*identity receipt"):
                GUARD.inspect_plan(plan)

            drifted = json.loads(json.dumps(plan))
            drifted["prior_state"]["values"]["root_module"]["resources"][0]["values"][
                "result"
            ] = "different"
            with self.assertRaisesRegex(GUARD.GuardError, "differ"):
                GUARD.inspect_plan(drifted, identity_receipt=receipt)

            updated = json.loads(json.dumps(plan))
            updated["resource_changes"][0]["change"]["actions"] = ["update"]
            with self.assertRaisesRegex(GUARD.GuardError, "update"):
                GUARD.inspect_plan(updated, identity_receipt=receipt)

            omitted = json.loads(json.dumps(plan))
            omitted["resource_changes"] = []
            with self.assertRaisesRegex(GUARD.GuardError, "omitted"):
                GUARD.inspect_plan(omitted, identity_receipt=receipt)

            with self.assertRaisesRegex(GUARD.GuardError, "change inventory"):
                GUARD.inspect_plan({"prior_state": state}, identity_receipt=receipt)
        for address in (
            "random_id.bootstrap_access_token_id",
            "random_id.scientific_access_token_id[0]",
            "random_password.bootstrap_access_token_secret",
            "random_password.scientific_access_token_secret[0]",
            "kubernetes_secret_v1.bootstrap_access",
            "kubernetes_secret_v1.scientific_access[0]",
            'random_password.database["runtime"]',
            'kubernetes_secret_v1.database_account["runtime"]',
            'random_password.key_material["storage"]',
            'random_password.key_material["storage_name"]',
            "kubernetes_secret_v1.storage_keyring",
            'kubernetes_secret_v1.storage_keyring_versioned["2"]',
            'kubernetes_secret_v1.storage_name_keyring_versioned["2"]',
        ):
            with self.subTest(address=address), self.assertRaises(GUARD.GuardError):
                GUARD.inspect_plan(
                    {
                        "resource_changes": [
                            {
                                "address": address,
                                "change": {"actions": ["delete", "create"]},
                            }
                        ]
                    }
                )

    def test_storage_credentials_cannot_escape_guard_through_move_or_target(
        self,
    ) -> None:
        moved = {
            "resource_changes": [
                {
                    "address": "terraform_data.unprotected",
                    "previous_address": 'random_password.key_material["storage"]',
                    "change": {"actions": ["no-op"]},
                }
            ]
        }
        with self.assertRaisesRegex(GUARD.GuardError, "move"):
            GUARD.inspect_plan(moved)

        protected = {
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "address": 'random_password.key_material["storage"]',
                            "values": {"id": "none", "result": "not-recorded"},
                        },
                        {
                            "address": 'random_password.key_material["storage_name"]',
                            "values": {"id": "none", "result": "not-recorded"},
                        },
                    ]
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            receipt = GUARD.write_identity_receipt(
                protected,
                directory / "identity.json",
                source_commit="a" * 40,
            )
            with self.assertRaisesRegex(GUARD.GuardError, "omitted"):
                GUARD.inspect_plan(
                    {
                        "prior_state": protected,
                        "resource_changes": [
                            {
                                "address": 'random_password.key_material["storage"]',
                                "change": {"actions": ["no-op"]},
                            }
                        ],
                    },
                    identity_receipt=receipt,
                )

    def test_identity_receipt_binds_live_secret_uid_resource_version_and_content(
        self,
    ) -> None:
        address = "kubernetes_secret_v1.bootstrap_access"
        state = {
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "address": address,
                            "values": {
                                "metadata": [
                                    {
                                        "namespace": "fs2-system",
                                        "name": "fs2-serve-bootstrap-access",
                                        "uid": "uid-1",
                                        "resource_version": "17",
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        }
        live = {
            "items": [
                {
                    "metadata": {
                        "namespace": "fs2-system",
                        "name": "fs2-serve-bootstrap-access",
                        "uid": "uid-1",
                        "resourceVersion": "17",
                    },
                    "data": {"token": "not-written-to-receipt"},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            receipt_path = root / "identity.json"
            receipt = GUARD.write_identity_receipt(
                state,
                receipt_path,
                source_commit="a" * 40,
                terraform_root="workloads",
                live_secret_document=live,
            )
            binding = receipt["live_secret_bindings"][address]
            self.assertEqual(binding["uid"], "uid-1")
            self.assertEqual(binding["resource_version"], "17")
            encoded = receipt_path.read_text(encoding="utf-8")
            self.assertNotIn("not-written-to-receipt", encoded)
            self.assertEqual(
                binding["content_sha256"],
                GUARD.canonical_sha256(live["items"][0]["data"]),
            )
            loaded = GUARD.load_identity_receipt(
                receipt_path, terraform_root="workloads"
            )
            self.assertEqual(
                loaded["live_secret_bindings"], receipt["live_secret_bindings"]
            )
            receipt_path.unlink()
            receipt["live_secret_bindings"] = {}
            GUARD.write_private_json(receipt_path, receipt)
            with self.assertRaisesRegex(GUARD.GuardError, "every protected"):
                GUARD.load_identity_receipt(receipt_path, terraform_root="workloads")
            changed = json.loads(json.dumps(live))
            changed["items"][0]["metadata"]["uid"] = "uid-2"
            with self.assertRaisesRegex(GUARD.GuardError, "UID/resourceVersion"):
                GUARD.live_secret_bindings(
                    state,
                    changed,
                    registry=GUARD.load_registry(),
                    terraform_root="workloads",
                )

    def test_wrapper_guards_both_generated_and_saved_plans(self) -> None:
        source = (ROOT / "inference-stack").read_text(encoding="utf-8")
        self.assertIn("fixed-v1-identity.receipt.json", source)
        self.assertIn('Path(f"/proc/self/fd/{plan_descriptor}")', source)
        self.assertIn("pass_fds=(plan_descriptor,)", source)
        unsafe = json.dumps(
            {
                "resource_changes": [
                    {
                        "address": "kubernetes_secret_v1.bootstrap_access",
                        "change": {"actions": ["delete", "create"]},
                    }
                ]
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            plan = Path(temporary) / "workloads-plan.tfplan"
            plan.write_text("opaque", encoding="utf-8")
            plan.chmod(0o600)
            calls: list[list[str]] = []

            def fake_run(
                command,
                *,
                env=None,
                capture=False,
                input_text=None,
                pass_fds=(),
            ):
                calls.append(list(command))
                if "show" in command:
                    return subprocess.CompletedProcess(command, 0, stdout=unsafe)
                if (
                    "secret_migration_guard.py" in " ".join(command)
                    and "plan" in command
                ):
                    GUARD.inspect_plan(json.loads(input_text))
                return subprocess.CompletedProcess(command, 0, stdout="")

            with (
                mock.patch.object(STACK, "run", side_effect=fake_run),
                self.assertRaises(GUARD.GuardError),
            ):
                STACK.apply_plan("terraform", ROOT / "stages/workloads", plan, {})
            self.assertFalse(any("apply" in call for call in calls))

    def test_native_terraform_gate_is_short_lived_source_bound_and_move_aware(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            owner.chmod(0o700)
            configuration = owner / "workloads"
            receipts = owner / "receipts"
            configuration.mkdir(mode=0o700)
            receipts.mkdir(mode=0o700)
            (configuration / "main.tf").write_text(
                'resource "terraform_data" "safe" {}\n', encoding="utf-8"
            )
            root_patch = mock.patch.dict(
                GUARD.SUPPORTED_TERRAFORM_CONFIGURATION_ROOTS,
                {"workloads": configuration},
            )
            root_patch.start()
            self.addCleanup(root_patch.stop)
            receipt_path = receipts / "gate.json"
            raw_state = {
                "version": 4,
                "terraform_version": "1.13.3",
                "serial": 7,
                "lineage": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "resources": [{"mode": "managed", "type": "terraform_data"}],
            }
            GUARD.write_apply_gate_receipt(
                state_document={"values": {"root_module": {"resources": []}}},
                raw_state_document=raw_state,
                identity_receipt=None,
                terraform_configuration=configuration,
                terraform_root="workloads",
                source_commit="a" * 40,
                path=receipt_path,
            )
            query = {
                "receipt_path": str(receipt_path),
                "terraform_configuration": str(configuration),
                "terraform_root": "workloads",
                "source_commit": "a" * 40,
            }
            self.assertEqual(
                GUARD.validate_native_gate(
                    query, authoritative_state_document=raw_state
                )["status"],
                "pass",
            )
            with (
                mock.patch.object(
                    GUARD,
                    "command_json",
                    side_effect=AssertionError(
                        "forwarded receipt verification must not read Terraform state"
                    ),
                ),
                mock.patch.object(
                    GUARD,
                    "authority_json",
                    side_effect=AssertionError(
                        "forwarded receipt verification must not contact the provider authority"
                    ),
                ),
            ):
                self.assertEqual(
                    GUARD.validate_forwarded_native_gate(query)["status"],
                    "pass",
                )
            hostile = owner / "hostile-embedding"
            hostile.mkdir(mode=0o700)
            (hostile / "main.tf").write_text(
                (configuration / "main.tf").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                GUARD.GuardError, "registered workloads root"
            ):
                GUARD.validate_native_gate(
                    {**query, "terraform_configuration": str(hostile)},
                    authoritative_state_document=raw_state,
                )
            with self.assertRaisesRegex(
                GUARD.GuardError, "registered workloads root"
            ):
                GUARD.validate_forwarded_native_gate(
                    {**query, "terraform_configuration": str(hostile)}
                )
            with self.assertRaises(GUARD.GuardError):
                GUARD.validate_native_gate(
                    {**query, "receipt_path": ""},
                    authoritative_state_document=raw_state,
                )
            drifted_state = {**raw_state, "serial": 8}
            with self.assertRaisesRegex(GUARD.GuardError, "authoritative"):
                GUARD.validate_native_gate(
                    query, authoritative_state_document=drifted_state
                )
            (configuration / "main.tf").write_text(
                'resource "terraform_data" "changed" {}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(GUARD.GuardError, "bind"):
                GUARD.validate_native_gate(
                    query, authoritative_state_document=raw_state
                )

            for fabricated in ({}, {**raw_state, "resources": []}):
                with (
                    self.subTest(fabricated=fabricated),
                    self.assertRaisesRegex(GUARD.GuardError, "non-empty"),
                ):
                    GUARD.terraform_state_identity(fabricated)

            moved = owner / "moved"
            moved.mkdir(mode=0o700)
            (moved / "main.tf").write_text(
                "moved {\n"
                "  from = kubernetes_secret_v1.bootstrap_access\n"
                "  to = terraform_data.unprotected\n"
                "}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GUARD.GuardError, "moves"):
                GUARD.write_apply_gate_receipt(
                    state_document={"values": {"root_module": {"resources": []}}},
                    raw_state_document=raw_state,
                    identity_receipt=None,
                    terraform_configuration=moved,
                    terraform_root="workloads",
                    source_commit="a" * 40,
                    path=receipts / "moved.json",
                )

    def test_reference_data_standalone_has_no_wrapper_or_guard_cli_route(
        self,
    ) -> None:
        standalone = ROOT / "reference-data/terraform"
        with self.assertRaisesRegex(STACK.DeploymentError, "not registered"):
            STACK.terraform_root_name(standalone)
        with self.assertRaises(SystemExit):
            GUARD.parse_args(
                ["plan", "-", "--terraform-root", "reference-data"]
            )

    def test_retirement_canary_requires_no_plaintext_plan_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            base.chmod(0o700)
            scope = base / "state"
            scope.mkdir(mode=0o700)
            root = scope / GUARD.AUTHORITATIVE_STATE_ROOT_NAMES[0]
            root.mkdir(mode=0o700)
            authority = mock.patch.object(
                GUARD, "authoritative_operator_state_root", return_value=scope
            )
            authority.start()
            self.addCleanup(authority.stop)
            state = root / "workloads.tfstate"
            state.write_text("test metadata", encoding="utf-8")
            state.chmod(0o600)
            manifest = GUARD.write_artifact_manifest(
                scope, base / "artifacts.receipt.json"
            )
            disposition = GUARD.write_disposition_receipt(
                manifest,
                {
                    "schema": "fs2-serve.nebius.ai/global-artifact-disposition-input/v3",
                    "artifacts": [
                        {
                            "path": item["path"],
                            "sha256": item["sha256"],
                            "action": "encrypted-rewrap",
                            "evidence": disposition_evidence(
                                base,
                                index,
                                item,
                                action="encrypted-rewrap",
                            ),
                        }
                        for index, item in enumerate(manifest["artifacts"])
                    ],
                },
                base / "dispositions.receipt.json",
            )
            self.assertEqual(
                GUARD.inspect_run_root(scope, retired=False)["plaintext_artifacts"], 1
            )
            with self.assertRaises(GUARD.GuardError):
                GUARD.inspect_run_root(
                    scope,
                    retired=True,
                    artifact_manifest=manifest,
                    disposition_receipt=disposition,
                )
            state.unlink()
            with self.assertRaisesRegex(GUARD.GuardError, "disposition"):
                GUARD.inspect_run_root(scope, retired=True, artifact_manifest=manifest)
            self.assertEqual(
                GUARD.inspect_run_root(
                    scope,
                    retired=True,
                    artifact_manifest=manifest,
                    disposition_receipt=disposition,
                )["plaintext_artifacts"],
                0,
            )
            with self.assertRaisesRegex(GUARD.GuardError, "manifest"):
                GUARD.inspect_run_root(scope, retired=True)

    def test_saved_plan_gate_binds_plan_state_and_live_secret_at_apply(self) -> None:
        values = {
            "metadata": [
                {
                    "name": "fs2-serve-admin",
                    "namespace": "fs2-system",
                    "uid": "uid-admin",
                    "resource_version": "17",
                }
            ],
            "data": {"token": "state-redacted"},
        }
        state = {
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "address": "kubernetes_secret_v1.admin",
                            "values": values,
                        }
                    ]
                }
            }
        }
        plan = {
            "prior_state": state,
            "resource_changes": [
                {
                    "address": "kubernetes_secret_v1.admin",
                    "change": {"actions": ["no-op"], "before": values, "after": values},
                }
            ],
        }
        raw_state = {
            "version": 4,
            "terraform_version": "1.13.3",
            "serial": 91,
            "lineage": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "resources": [{"mode": "managed", "type": "kubernetes_secret"}],
        }
        live = {
            "items": [
                {
                    "metadata": {
                        "name": "fs2-serve-admin",
                        "namespace": "fs2-system",
                        "uid": "uid-admin",
                        "resourceVersion": "17",
                    },
                    "data": {"token": "bGl2ZS10b2tlbg=="},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            owner.chmod(0o700)
            configuration = owner / "workloads"
            configuration.mkdir(mode=0o700)
            (configuration / "main.tf").write_text(
                'resource "terraform_data" "safe" {}\n', encoding="utf-8"
            )
            root_patch = mock.patch.dict(
                GUARD.SUPPORTED_TERRAFORM_CONFIGURATION_ROOTS,
                {"workloads": configuration},
            )
            root_patch.start()
            self.addCleanup(root_patch.stop)
            identity_path = owner / "identity.json"
            identity = GUARD.write_identity_receipt(
                state,
                identity_path,
                source_commit="a" * 40,
                live_secret_document=live,
            )
            planning_path = owner / "planning.json"
            GUARD.write_apply_gate_receipt(
                state_document=state,
                raw_state_document=raw_state,
                identity_receipt=identity,
                terraform_configuration=configuration,
                terraform_root="workloads",
                source_commit="a" * 40,
                path=planning_path,
            )
            saved_plan = owner / "workloads.tfplan"
            saved_plan.write_bytes(b"opaque exact saved plan")
            saved_plan.chmod(0o600)
            apply_path = owner / "apply.json"
            GUARD.write_saved_plan_gate_receipt(
                plan_document=plan,
                raw_state_document=raw_state,
                saved_plan=saved_plan,
                planning_receipt_path=planning_path,
                identity_receipt=identity,
                live_secret_document=live,
                terraform_configuration=configuration,
                terraform_root="workloads",
                source_commit="a" * 40,
                path=apply_path,
            )
            self.assertEqual(
                GUARD.validate_saved_plan_gate(
                    receipt_path=apply_path,
                    plan_document=plan,
                    saved_plan=saved_plan,
                    live_secret_document=live,
                    raw_state_document=raw_state,
                    terraform_configuration=configuration,
                    terraform_root="workloads",
                    source_commit="a" * 40,
                )["status"],
                "pass",
            )

            changed_live = json.loads(json.dumps(live))
            changed_live["items"][0]["metadata"]["resourceVersion"] = "18"
            with self.assertRaisesRegex(GUARD.GuardError, "live Secret"):
                GUARD.validate_saved_plan_gate(
                    receipt_path=apply_path,
                    plan_document=plan,
                    saved_plan=saved_plan,
                    live_secret_document=changed_live,
                    raw_state_document=raw_state,
                    terraform_configuration=configuration,
                    terraform_root="workloads",
                    source_commit="a" * 40,
                )

            with self.assertRaisesRegex(GUARD.GuardError, "backend state"):
                GUARD.validate_saved_plan_gate(
                    receipt_path=apply_path,
                    plan_document=plan,
                    saved_plan=saved_plan,
                    live_secret_document=live,
                    raw_state_document={**raw_state, "serial": 92},
                    terraform_configuration=configuration,
                    terraform_root="workloads",
                    source_commit="a" * 40,
                )

            saved_plan.write_bytes(b"different plan")
            with self.assertRaisesRegex(GUARD.GuardError, "execution input"):
                GUARD.validate_saved_plan_gate(
                    receipt_path=apply_path,
                    plan_document=plan,
                    saved_plan=saved_plan,
                    live_secret_document=live,
                    raw_state_document=raw_state,
                    terraform_configuration=configuration,
                    terraform_root="workloads",
                    source_commit="a" * 40,
                )

    def test_retirement_canary_catches_real_plan_cookie_and_scoped_export_names(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            base.chmod(0o700)
            scope = base / "state"
            scope.mkdir(mode=0o700)
            root = scope / GUARD.AUTHORITATIVE_STATE_ROOT_NAMES[0]
            root.mkdir(mode=0o700)
            authority = mock.patch.object(
                GUARD, "authoritative_operator_state_root", return_value=scope
            )
            authority.start()
            self.addCleanup(authority.stop)
            for name in (
                "configuration.plan.json",
                "admin-cookie.acceptance.txt",
                "general-access.json",
                "unclassified-sensitive.bin",
            ):
                artifact = root / name
                artifact.write_text("opaque", encoding="utf-8")
                artifact.chmod(0o600)
            self.assertEqual(
                GUARD.inspect_run_root(scope, retired=False)["plaintext_artifacts"], 3
            )
            self.assertEqual(
                GUARD.inspect_run_root(scope, retired=False)["unknown_artifacts"], 1
            )
            manifest = GUARD.write_artifact_manifest(
                scope, base / "artifacts.receipt.json"
            )
            self.assertEqual(len(manifest["artifacts"]), 4)
            disposition_input = {
                "schema": "fs2-serve.nebius.ai/global-artifact-disposition-input/v3",
                "artifacts": [
                    {
                        "path": item["path"],
                        "sha256": item["sha256"],
                        "action": "secure-retire",
                        "evidence": disposition_evidence(
                            base,
                            index,
                            item,
                            action="secure-retire",
                        ),
                    }
                    for index, item in enumerate(manifest["artifacts"])
                ],
            }
            disposition = GUARD.write_disposition_receipt(
                manifest,
                disposition_input,
                base / "dispositions.receipt.json",
            )
            with self.assertRaisesRegex(GUARD.GuardError, "not retired"):
                GUARD.inspect_run_root(
                    scope,
                    retired=True,
                    artifact_manifest=manifest,
                    disposition_receipt=disposition,
                )
            for path in root.iterdir():
                path.unlink()
            self.assertEqual(
                GUARD.inspect_run_root(
                    scope,
                    retired=True,
                    artifact_manifest=manifest,
                    disposition_receipt=disposition,
                )["total_artifacts"],
                0,
            )
            incomplete = json.loads(json.dumps(disposition_input))
            incomplete["artifacts"].pop()
            with self.assertRaisesRegex(GUARD.GuardError, "exactly cover"):
                GUARD.write_disposition_receipt(
                    manifest,
                    incomplete,
                    base / "incomplete.receipt.json",
                )

    def test_global_retirement_scope_cannot_omit_sibling_roots_or_use_opaque_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            owner.chmod(0o700)
            scope = owner / "all-operator-state"
            receipts = owner / "receipts"
            scope.mkdir(mode=0o700)
            receipts.mkdir(mode=0o700)
            authority = mock.patch.object(
                GUARD, "authoritative_operator_state_root", return_value=scope
            )
            authority.start()
            self.addCleanup(authority.stop)
            for relative in (
                f"{GUARD.AUTHORITATIVE_STATE_ROOT_NAMES[0]}/workloads.tfstate",
                f"{GUARD.AUTHORITATIVE_STATE_ROOT_NAMES[1]}/configuration.plan.json",
            ):
                path = scope / relative
                path.parent.mkdir(mode=0o700)
                path.write_text("opaque", encoding="utf-8")
                path.chmod(0o600)
            with self.assertRaisesRegex(GUARD.GuardError, "authoritative"):
                GUARD.write_artifact_manifest(
                    scope / GUARD.AUTHORITATIVE_STATE_ROOT_NAMES[0],
                    receipts / "partial-manifest.json",
                )
            manifest = GUARD.write_artifact_manifest(
                scope, receipts / "global-manifest.json"
            )
            self.assertEqual(
                {item["path"] for item in manifest["artifacts"]},
                {
                    f"{GUARD.AUTHORITATIVE_STATE_ROOT_NAMES[0]}/workloads.tfstate",
                    f"{GUARD.AUTHORITATIVE_STATE_ROOT_NAMES[1]}/configuration.plan.json",
                },
            )
            opaque = {
                "schema": "fs2-serve.nebius.ai/global-artifact-disposition-input/v3",
                "artifacts": [
                    {
                        "path": item["path"],
                        "sha256": item["sha256"],
                        "action": "secure-retire",
                        "evidence": {"evidence_id": "self-asserted"},
                    }
                    for item in manifest["artifacts"]
                ],
            }
            with self.assertRaisesRegex(GUARD.GuardError, "invalid evidence"):
                GUARD.write_disposition_receipt(
                    manifest, opaque, receipts / "opaque.json"
                )
            disposition = GUARD.write_disposition_receipt(
                manifest,
                {
                    "schema": "fs2-serve.nebius.ai/global-artifact-disposition-input/v3",
                    "artifacts": [
                        {
                            "path": item["path"],
                            "sha256": item["sha256"],
                            "action": "secure-retire",
                            "evidence": disposition_evidence(
                                receipts,
                                index,
                                item,
                                action="secure-retire",
                            ),
                        }
                        for index, item in enumerate(manifest["artifacts"])
                    ],
                },
                receipts / "disposition.json",
            )
            (
                scope / GUARD.AUTHORITATIVE_STATE_ROOT_NAMES[0] / "workloads.tfstate"
            ).unlink()
            with self.assertRaisesRegex(GUARD.GuardError, "not retired"):
                GUARD.inspect_run_root(
                    scope,
                    retired=True,
                    artifact_manifest=manifest,
                    disposition_receipt=disposition,
                )

    def test_global_retirement_inventory_rejects_symlinks_and_midread_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary)
            owner.chmod(0o700)
            scope = owner / "all-operator-state"
            scope.mkdir(mode=0o700)
            authority = mock.patch.object(
                GUARD, "authoritative_operator_state_root", return_value=scope
            )
            authority.start()
            self.addCleanup(authority.stop)

            outside = owner / "outside.tfstate"
            outside.write_text("outside", encoding="utf-8")
            outside.chmod(0o600)
            (scope / "linked.tfstate").symlink_to(outside)
            with self.assertRaisesRegex(GUARD.GuardError, "safely open"):
                GUARD.inventory_run_root(scope)
            (scope / "linked.tfstate").unlink()

            artifact = scope / "workloads.tfstate"
            artifact.write_bytes(b"a" * (1024 * 1024 + 1))
            artifact.chmod(0o600)
            real_read = os.read
            changed = False

            def mutate_after_first_read(descriptor: int, size: int) -> bytes:
                nonlocal changed
                chunk = real_read(descriptor, size)
                if not changed:
                    changed = True
                    with artifact.open("ab") as stream:
                        stream.write(b"changed")
                return chunk

            with (
                mock.patch.object(
                    GUARD.os, "read", side_effect=mutate_after_first_read
                ),
                self.assertRaisesRegex(GUARD.GuardError, "changed while inventoried"),
            ):
                GUARD.inventory_run_root(scope)

    def test_versioned_pat_ids_cannot_be_reused_across_generation_or_audience(
        self,
    ) -> None:
        token_a = "fs2_pat_" + ("a" * 32) + "_" + ("A" * 32)
        token_b = "fs2_pat_" + ("b" * 32) + "_" + ("B" * 32)
        STACK.validate_versioned_access_pat_ids(
            json.dumps({"2": token_a}), json.dumps({"2": token_b}), None
        )
        for general, scientific in (
            ({"2": token_a, "3": token_a}, {}),
            ({"2": token_a}, {"2": token_a}),
        ):
            with (
                self.subTest(general=general, scientific=scientific),
                self.assertRaisesRegex(STACK.DeploymentError, "unique"),
            ):
                STACK.validate_versioned_access_pat_ids(
                    json.dumps(general), json.dumps(scientific), None
                )
        bootstrap = (ROOT / "stages/workloads/bootstrap_access.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "length(local.all_versioned_access_pat_ids) == length(toset(local.all_versioned_access_pat_ids))",
            bootstrap,
        )
        self.assertIn("random_id.bootstrap_access_token_id.hex", bootstrap)
        self.assertIn("random_id.scientific_access_token_id[0].hex", bootstrap)

    def test_pat_mapping_receipt_is_value_free_unique_and_live_provable(self) -> None:
        token_1 = "fs2_pat_" + ("1" * 32) + "_" + ("A" * 32)
        token_2 = "fs2_pat_" + ("2" * 32) + "_" + ("B" * 32)
        scientific = "fs2_pat_" + ("3" * 32) + "_" + ("C" * 32)
        website = "fs2_pat_" + ("4" * 32) + "_" + ("D" * 32)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            tokens_path = root / "tokens.json"
            tokens_path.write_text(
                json.dumps(
                    {
                        "general": {"1": token_1, "2": token_2},
                        "scientific": {"1": scientific},
                        "website": {"1": website},
                    }
                ),
                encoding="utf-8",
            )
            tokens_path.chmod(0o600)
            mapping_path = root / "mapping.receipt.json"
            mapping = PAT_GUARD.mapping_receipt(tokens_path, mapping_path)
            encoded = mapping_path.read_text(encoding="utf-8")
            self.assertNotIn(token_1, encoded)
            self.assertNotIn(token_2, encoded)
            self.assertEqual(mapping["audiences"]["general"]["1"]["token_id"], "1" * 32)
            self.assertEqual(mapping["audiences"]["website"]["1"]["token_id"], "4" * 32)

            overlap_path = root / "overlap.receipt.json"
            overlap_args = argparse.Namespace(
                tokens_file=tokens_path,
                mapping_receipt=mapping_path,
                audience="general",
                old_generation=1,
                new_generation=2,
                endpoint="https://inference.example.invalid/v1/models",
                timeout_seconds=10,
                receipt=overlap_path,
            )
            successes = [
                {"status": 200, "body_sha256": "a" * 64, "semantic": True},
                {"status": 200, "body_sha256": "b" * 64, "semantic": True},
            ]
            with mock.patch.object(PAT_GUARD, "request_token", side_effect=successes):
                overlap = PAT_GUARD.prove(overlap_args, revoked=False)
            self.assertEqual(overlap["old"]["token_id"], "1" * 32)
            self.assertEqual(overlap["new"]["token_id"], "2" * 32)
            self.assertNotIn(token_1, overlap_path.read_text(encoding="utf-8"))

            revocation_args = argparse.Namespace(
                **{**vars(overlap_args), "receipt": root / "revocation.receipt.json"}
            )
            results = [
                {"status": 401, "body_sha256": "c" * 64, "semantic": False},
                {"status": 200, "body_sha256": "d" * 64, "semantic": True},
            ]
            with mock.patch.object(PAT_GUARD, "request_token", side_effect=results):
                revocation = PAT_GUARD.prove(revocation_args, revoked=True)
            self.assertEqual(revocation["old"]["status"], 401)
            self.assertEqual(revocation["new"]["status"], 200)

    def test_pat_mapping_rejects_reused_id_or_fingerprint(self) -> None:
        token_a = "fs2_pat_" + ("a" * 32) + "_" + ("A" * 32)
        same_id = "fs2_pat_" + ("a" * 32) + "_" + ("B" * 32)
        for document, expected in (
            ({"general": {"1": token_a, "2": same_id}}, "IDs"),
            ({"general": {"1": token_a}, "scientific": {"1": token_a}}, "IDs"),
            ({"general": {"1": token_a}, "website": {"1": token_a}}, "IDs"),
        ):
            with (
                self.subTest(document=document),
                self.assertRaisesRegex(PAT_GUARD.PatGuardError, expected),
            ):
                PAT_GUARD.parse_tokens(document)

    def test_every_credential_class_uses_disable_before_delete_state_machine(
        self,
    ) -> None:
        registry = ROTATION.load_registry(
            ROOT / "security/durable-credential-registry.json"
        )
        self.assertTrue(registry["credentials"])
        for item in registry["credentials"]:
            self.assertEqual(
                item["rotation"],
                {
                    "strategy": "dual-read-current-write",
                    "disable_before_delete": True,
                },
                item["id"],
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = ROTATION.private_directory(Path(temporary) / "rotation")
            journal = {
                "schema": "fs2-serve.nebius.ai/credential-rotation/v2",
                "operation_id": "operation-test",
                "credential_class": "registry-credentials",
                "owner_id": "owner-test",
                "project_id": "project-test",
                "purpose": "NVIDIA registry pulls and API access",
                "readers": ["model-runtimes"],
                "registry_sha256": "a" * 64,
                "provider_command": self.provider_identity,
                "predecessor": {
                    "id": "credential-old",
                    "credential_class": "registry-credentials",
                    "owner_id": "owner-test",
                    "project_id": "project-test",
                    "purpose": "NVIDIA registry pulls and API access",
                    "generation": 1,
                    "fingerprint": "1" * 64,
                    "status": "source-observed",
                    **provider_inventory(["model-runtimes"]),
                },
                "successor_generation": 2,
                "successor_fingerprint": "2" * 64,
                "successor": {
                    "id": "credential-new",
                    "credential_class": "registry-credentials",
                    "owner_id": "owner-test",
                    "project_id": "project-test",
                    "purpose": "NVIDIA registry pulls and API access",
                    "generation": 2,
                    "fingerprint": "2" * 64,
                    "status": "active",
                    **provider_inventory(["model-runtimes"]),
                },
                "phase": "current-write",
                "created_at": "2026-09-16T00:00:00Z",
                "events": [],
            }
            ROTATION.atomic_private_json(root / "journal.json", journal)
            args = argparse.Namespace(
                directory=root,
                provider_command=["provider"],
                command="delete-old",
                confirm_predecessor_id="credential-old",
            )
            with (
                mock.patch.object(
                    ROTATION,
                    "provider_call",
                    side_effect=AssertionError("delete must not run"),
                ),
                self.assertRaisesRegex(ROTATION.RotationError, "disabled"),
            ):
                ROTATION.transition(args)

            args.command = "disable-old"
            disabled = {**journal["predecessor"], "status": "disabled"}
            with mock.patch.object(ROTATION, "provider_call", return_value=disabled):
                result = ROTATION.transition(args)
            self.assertEqual(result["status"], "predecessor-disabled")

            args.command = "delete-old"
            responses = [
                {"reader_references": 0, "current_write_id": "credential-new"},
                {"deleted": True},
                {
                    "get_absent": True,
                    "list_absent": True,
                    "successor_active": True,
                },
            ]
            with mock.patch.object(ROTATION, "provider_call", side_effect=responses):
                result = ROTATION.transition(args)
            self.assertEqual(result["status"], "predecessor-deleted")

    def test_rotation_rejects_reused_predecessor_fingerprint_before_create(
        self,
    ) -> None:
        predecessor = {
            "id": "storage-v1",
            "credential_class": "customer-storage-cipher-keyring",
            "owner_id": "owner-test",
            "project_id": "project-test",
            "purpose": "customer-storage access-key envelope encryption",
            "generation": 1,
            "fingerprint": "1" * 64,
            "status": "active",
            **provider_inventory(
                [
                    "customer-storage-reconciler",
                    "customer-storage-disclosure",
                    "migration-job",
                ]
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(
                directory=Path(temporary) / "rotation",
                registry=ROOT / "security/durable-credential-registry.json",
                provider_command=["provider"],
                credential_class="customer-storage-cipher-keyring",
                owner_id="owner-test",
                project_id="project-test",
                predecessor_id="storage-v1",
                predecessor_generation=1,
                successor_fingerprint="1" * 64,
            )
            with (
                mock.patch.object(
                    ROTATION, "provider_call", return_value=predecessor
                ) as provider,
                self.assertRaisesRegex(ROTATION.RotationError, "must differ"),
            ):
                ROTATION.create(args)
            provider.assert_called_once()

        with self.assertRaisesRegex(ROTATION.RotationError, "authority"):
            ROTATION.require_provider_command(
                {"provider_command": {**self.provider_identity, "inode": 99}},
                ["provider"],
            )
        with self.assertRaisesRegex(ROTATION.RotationError, "incomplete"):
            ROTATION.exact_identity(
                {key: value for key, value in predecessor.items() if key != "readers"}
            )

    def test_provider_inventory_covers_every_class_with_expiry_and_readers(
        self,
    ) -> None:
        registry = ROTATION.load_registry(
            ROOT / "security/durable-credential-registry.json"
        )
        items = []
        for index, policy in enumerate(registry["credentials"], start=1):
            items.append(
                {
                    "id": f"credential-{index}",
                    "credential_class": policy["id"],
                    "owner_id": f"owner-{index}",
                    "project_id": "project-test",
                    "purpose": policy["purpose"],
                    "generation": 1,
                    "fingerprint": f"{index:064x}",
                    "status": "active",
                    "provider_version": f"version-{index}",
                    "expires_at": (
                        "2099-01-01T00:00:00Z" if policy["expiry"]["required"] else None
                    ),
                    "readers": policy["readers"],
                }
            )
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(
                directory=Path(temporary) / "inventory",
                registry=ROOT / "security/durable-credential-registry.json",
                provider_command=["provider"],
                project_id="project-test",
            )
            inventory_response = {
                "items": items,
                "required_classes": registry["credential_presence"]["required"],
                "feature_gated_classes": sorted(
                    registry["credential_presence"]["feature_gated"]
                ),
                "enabled_classes": sorted(
                    item["id"] for item in registry["credentials"]
                ),
                "absent_feature_classes": [],
                "absent_optional_classes": [],
                "externalEvidence": {},
                "authorityObservation": {},
            }
            with mock.patch.object(
                ROTATION, "provider_call", return_value=inventory_response
            ):
                result = ROTATION.audit_inventory(args)
            self.assertEqual(result["credential_classes"], len(registry["credentials"]))
            receipt = json.loads(Path(result["receipt"]).read_text())
            self.assertEqual(
                set(receipt["classes"]),
                {item["id"] for item in registry["credentials"]},
            )
            self.assertNotIn("credential_value", json.dumps(receipt).lower())

        with tempfile.TemporaryDirectory() as temporary:
            args.directory = Path(temporary) / "inventory"
            with (
                mock.patch.object(
                    ROTATION,
                    "provider_call",
                    return_value={**inventory_response, "items": items[:-1]},
                ),
                self.assertRaisesRegex(ROTATION.RotationError, "omits"),
            ):
                ROTATION.audit_inventory(args)

    def test_rotation_interruption_is_journaled_before_cutover_and_reconciled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = ROTATION.private_directory(Path(temporary) / "rotation")
            journal = {
                "schema": "fs2-serve.nebius.ai/credential-rotation/v2",
                "operation_id": "operation-test",
                "credential_class": "registry-credentials",
                "owner_id": "owner-test",
                "project_id": "project-test",
                "purpose": "NVIDIA registry pulls and API access",
                "readers": ["model-runtimes"],
                "registry_sha256": "a" * 64,
                "provider_command": self.provider_identity,
                "predecessor": {
                    "id": "registry-v1",
                    "credential_class": "registry-credentials",
                    "owner_id": "owner-test",
                    "project_id": "project-test",
                    "purpose": "NVIDIA registry pulls and API access",
                    "generation": 1,
                    "fingerprint": "1" * 64,
                    "status": "active",
                    **provider_inventory(["model-runtimes"]),
                },
                "successor_generation": 2,
                "successor_fingerprint": "2" * 64,
                "successor": {
                    "id": "registry-v2",
                    "credential_class": "registry-credentials",
                    "owner_id": "owner-test",
                    "project_id": "project-test",
                    "purpose": "NVIDIA registry pulls and API access",
                    "generation": 2,
                    "fingerprint": "2" * 64,
                    "status": "active",
                    **provider_inventory(["model-runtimes"]),
                },
                "phase": "dual-read",
                "created_at": "2026-09-16T00:00:00Z",
                "events": [],
            }
            ROTATION.atomic_private_json(root / "journal.json", journal)
            transition_args = argparse.Namespace(
                directory=root,
                provider_command=["provider"],
                command="switch-write",
            )
            with (
                mock.patch.object(
                    ROTATION, "provider_call", side_effect=KeyboardInterrupt
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                ROTATION.transition(transition_args)
            interrupted = ROTATION.load_journal(root / "journal.json")
            self.assertEqual(interrupted["phase"], "switch-write-pending")
            self.assertEqual(interrupted["events"][-1]["event"], "switch-write-intent")

            reconcile_args = argparse.Namespace(
                directory=root,
                provider_command=["provider"],
            )
            with mock.patch.object(
                ROTATION,
                "provider_call",
                return_value={
                    "dual_read": True,
                    "current_write_id": "registry-v2",
                },
            ):
                result = ROTATION.reconcile(reconcile_args)
            self.assertEqual(result["status"], "current-write")
            self.assertEqual(
                ROTATION.load_journal(root / "journal.json")["phase"],
                "current-write",
            )

    def test_payload_predecessor_disable_requires_all_deployed_ciphertext_cohorts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = ROTATION.private_directory(Path(temporary) / "rotation")
            base_identity = {
                "credential_class": "payload-keyring",
                "owner_id": "owner-test",
                "project_id": "project-test",
                "purpose": "customer payload, request-debug and customer-storage envelope encryption",
            }
            journal = {
                "schema": "fs2-serve.nebius.ai/credential-rotation/v2",
                "operation_id": "operation-test",
                **base_identity,
                "readers": ["control-plane"],
                "registry_sha256": "a" * 64,
                "provider_command": self.provider_identity,
                "predecessor": {
                    "id": "payload-v1",
                    **base_identity,
                    "generation": 1,
                    "fingerprint": "1" * 64,
                    "status": "active",
                    **provider_inventory(["control-plane"]),
                },
                "successor_generation": 2,
                "successor_fingerprint": "2" * 64,
                "successor": {
                    "id": "payload-v2",
                    **base_identity,
                    "generation": 2,
                    "fingerprint": "2" * 64,
                    "status": "active",
                    **provider_inventory(["control-plane"]),
                },
                "phase": "current-write",
                "created_at": "2026-09-16T00:00:00Z",
                "events": [],
            }
            ROTATION.atomic_private_json(root / "journal.json", journal)
            args = argparse.Namespace(
                directory=root,
                provider_command=["provider"],
                command="disable-old",
            )
            incomplete = {
                "aad_contract": "fs2-serve.nebius.ai/payload-aad/v1",
                "old_key_references": 0,
                "current_write_id": "payload-v2",
                "operation_ciphertexts": 1,
                "request_debug_ciphertexts": 1,
                "customer_storage_ciphertexts": 0,
            }
            with (
                mock.patch.object(ROTATION, "provider_call", return_value=incomplete),
                self.assertRaisesRegex(ROTATION.RotationError, "customer-storage"),
            ):
                ROTATION.transition(args)

    def test_storage_key_retirement_requires_usage_zero_and_rotation_canaries(
        self,
    ) -> None:
        cases = {
            "customer-storage-cipher-keyring": {
                "purpose": "customer-storage access-key envelope encryption",
                "operation": "prove-customer-storage-cipher-migration",
                "evidence_key": "storage_cipher_migration_evidence_sha256",
                "proof": {
                    "aad_contract": "fs2.user-storage/v1",
                    "old_key_references": 0,
                    "current_write_id": "credential-new",
                    "preexisting_ciphertexts": 2,
                    "successor_ciphertexts": 1,
                    "old_generation_decrypt_verified": True,
                    "new_generation_decrypt_verified": True,
                    "rollback_decrypt_verified": True,
                    "tenant_principal_binding_verified": True,
                },
            },
            "customer-storage-name-keyring": {
                "purpose": "customer-storage deterministic resource-name derivation",
                "operation": "prove-customer-storage-name-migration",
                "evidence_key": "storage_name_migration_evidence_sha256",
                "proof": {
                    "old_key_references": 0,
                    "current_write_id": "credential-new",
                    "preexisting_names": 2,
                    "successor_names": 1,
                    "old_generation_verified": True,
                    "new_generation_verified": True,
                    "rollback_verified": True,
                    "collision_free": True,
                },
            },
        }
        for credential_class, case in cases.items():
            with (
                self.subTest(credential_class=credential_class),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = ROTATION.private_directory(Path(temporary) / "rotation")
                base_identity = {
                    "credential_class": credential_class,
                    "owner_id": "owner-test",
                    "project_id": "project-test",
                    "purpose": case["purpose"],
                }
                journal = {
                    "schema": "fs2-serve.nebius.ai/credential-rotation/v2",
                    "operation_id": "operation-test",
                    **base_identity,
                    "readers": ["migration-job"],
                    "registry_sha256": "a" * 64,
                    "provider_command": self.provider_identity,
                    "predecessor": {
                        "id": "credential-old",
                        **base_identity,
                        "generation": 1,
                        "fingerprint": "1" * 64,
                        "status": "active",
                        **provider_inventory(["migration-job"]),
                    },
                    "successor_generation": 2,
                    "successor_fingerprint": "2" * 64,
                    "successor": {
                        "id": "credential-new",
                        **base_identity,
                        "generation": 2,
                        "fingerprint": "2" * 64,
                        "status": "active",
                        **provider_inventory(["migration-job"]),
                    },
                    "phase": "current-write",
                    "created_at": "2026-09-16T00:00:00Z",
                    "events": [],
                }
                ROTATION.atomic_private_json(root / "journal.json", journal)
                args = argparse.Namespace(
                    directory=root,
                    provider_command=["provider"],
                    command="disable-old",
                )
                incomplete = {**case["proof"], "old_key_references": 1}
                with (
                    mock.patch.object(
                        ROTATION, "provider_call", return_value=incomplete
                    ),
                    self.assertRaisesRegex(ROTATION.RotationError, "zero old-key"),
                ):
                    ROTATION.transition(args)

                disabled = {**journal["predecessor"], "status": "disabled"}
                with mock.patch.object(
                    ROTATION,
                    "provider_call",
                    side_effect=[case["proof"], disabled],
                ) as provider:
                    result = ROTATION.transition(args)
                self.assertEqual(result["status"], "predecessor-disabled")
                self.assertEqual(
                    provider.call_args_list[0].args[1]["operation"], case["operation"]
                )
                recorded = ROTATION.load_journal(root / "journal.json")
                self.assertEqual(
                    recorded[case["evidence_key"]],
                    ROTATION.canonical_sha256(case["proof"]),
                )

    def test_operator_receipts_remain_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = HANDOFF.private_directory(Path(temporary) / "handoff")
            receipt = {"schema": "fs2-serve.nebius.ai/operator-handoff/v2"}
            HANDOFF.private_json(root / "receipt.json", receipt)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((root / "receipt.json").stat().st_mode), 0o600
            )
            self.assertEqual(json.loads((root / "receipt.json").read_text()), receipt)

    def test_provider_derived_handoff_lineage_requires_exact_membership_and_role(
        self,
    ) -> None:
        args = argparse.Namespace(nebius="nebius", admin_profile="sandbox")
        account = {
            "metadata": {
                "id": "serviceaccount-viewer",
                "parent_id": "project-test",
                "labels": {
                    "purpose": "operator-handoff-viewer",
                    "handoff_lineage": "lineage-test",
                    "handoff_generation": "2",
                },
            }
        }
        inventory = [
            account,
            [{"member_id": "serviceaccount-viewer"}],
            [
                {
                    "metadata": {"parent_id": "group-viewer"},
                    "member_id": "serviceaccount-viewer",
                }
            ],
            [{"resource_id": "project-test", "role": "viewer"}],
        ]
        with mock.patch.object(HANDOFF, "provider_json", side_effect=inventory):
            result = HANDOFF.service_account_lineage(
                args,
                service_account_id="serviceaccount-viewer",
                group_id="group-viewer",
                project_id="project-test",
                lineage_id="lineage-test",
                generation=2,
                required_role="viewer",
            )
        self.assertEqual(result["role"], "viewer")

        predecessor_admin = {
            "metadata": {
                "id": "serviceaccount-admin",
                "parent_id": "project-test",
                "labels": {
                    "purpose": "operator-handoff-admin",
                    "handoff_lineage": "lineage-test",
                    "handoff_generation": "1",
                },
            }
        }
        admin_inventory = [
            predecessor_admin,
            [{"member_id": "serviceaccount-admin"}],
            [
                {
                    "metadata": {"parent_id": "group-admin"},
                    "member_id": "serviceaccount-admin",
                }
            ],
            [{"resource_id": "project-test", "role": "admin"}],
        ]
        with mock.patch.object(HANDOFF, "provider_json", side_effect=admin_inventory):
            result = HANDOFF.service_account_lineage(
                args,
                service_account_id="serviceaccount-admin",
                group_id="group-admin",
                project_id="project-test",
                lineage_id="lineage-test",
                generation=1,
                required_role="admin",
            )
        self.assertEqual(result["role"], "admin")

        wrong_role = [
            account,
            [{"member_id": "serviceaccount-viewer"}],
            [
                {
                    "metadata": {"parent_id": "group-viewer"},
                    "member_id": "serviceaccount-viewer",
                }
            ],
            [{"resource_id": "project-test", "role": "admin"}],
        ]
        with (
            mock.patch.object(HANDOFF, "provider_json", side_effect=wrong_role),
            self.assertRaisesRegex(HANDOFF.HandoffError, "exactly the required"),
        ):
            HANDOFF.service_account_lineage(
                args,
                service_account_id="serviceaccount-viewer",
                group_id="group-viewer",
                project_id="project-test",
                lineage_id="lineage-test",
                generation=2,
                required_role="viewer",
            )

        extra_group = [
            account,
            [{"member_id": "serviceaccount-viewer"}],
            [
                {
                    "metadata": {"parent_id": "group-viewer"},
                    "member_id": "serviceaccount-viewer",
                },
                {
                    "metadata": {"parent_id": "group-admin"},
                    "member_id": "serviceaccount-viewer",
                },
            ],
        ]
        with (
            mock.patch.object(HANDOFF, "provider_json", side_effect=extra_group),
            self.assertRaisesRegex(HANDOFF.HandoffError, "unreviewed"),
        ):
            HANDOFF.service_account_lineage(
                args,
                service_account_id="serviceaccount-viewer",
                group_id="group-viewer",
                project_id="project-test",
                lineage_id="lineage-test",
                generation=2,
                required_role="viewer",
            )

    def test_keyboard_interrupt_after_handoff_create_is_journaled_and_reconciled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "handoff"
            expiry = (
                (HANDOFF.utc_now() + timedelta(days=1))
                .isoformat()
                .replace("+00:00", "Z")
            )
            args = argparse.Namespace(
                directory=root,
                nebius="nebius-test",
                openssl="openssl-test",
                admin_profile="sandbox",
                service_account_id="serviceaccount-viewer",
                viewer_group_id="group-viewer",
                project_id="project-test",
                cluster_id="cluster-test",
                expires_at=expiry,
                name="fs2-operator-handoff-v2",
                lineage_id="lineage-test",
                generation=2,
                predecessor_public_key_id="authpublickey-old",
                predecessor_service_account_id="serviceaccount-admin",
                predecessor_group_id="group-admin",
                predecessor_project_id="project-test",
                predecessor_generation=1,
                predecessor_role="admin",
            )
            reconciled = {
                "public_key_id": "authpublickey-created",
                "service_account_id": "serviceaccount-viewer",
                "project_id": "project-test",
                "expires_at": expiry,
                "name": "fs2-operator-handoff-v2",
                "lineage_id": "lineage-test",
                "generation": "2",
                "public_key_sha256": hashlib.sha256(b"test-key").hexdigest(),
            }

            def fake_run(command, *, capture=False, check=True):
                if command[0] == "openssl-test":
                    Path(command[command.index("-out") + 1]).write_text(
                        "test-key", encoding="ascii"
                    )
                    return subprocess.CompletedProcess(command, 0, stdout="")
                if command[1:4] == ["iam", "auth-public-key", "get"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps(
                            {
                                "metadata": {
                                    "id": "authpublickey-old",
                                    "parent_id": "project-test",
                                },
                                "spec": {
                                    "account": {
                                        "service_account": {
                                            "id": "serviceaccount-admin"
                                        }
                                    }
                                },
                            }
                        ),
                    )
                if command[1:4] == ["iam", "auth-public-key", "create"]:
                    raise KeyboardInterrupt
                raise AssertionError(command)

            with (
                mock.patch.object(HANDOFF, "run", side_effect=fake_run),
                mock.patch.object(
                    HANDOFF,
                    "service_account_lineage",
                    side_effect=lambda _args, **kwargs: {
                        "service_account_id": kwargs["service_account_id"],
                        "group_id": kwargs["group_id"],
                        "project_id": kwargs["project_id"],
                        "lineage_id": kwargs["lineage_id"],
                        "generation": kwargs["generation"],
                        "role": kwargs["required_role"],
                    },
                ),
                mock.patch.object(
                    HANDOFF, "reconcile_issued_key", return_value=reconciled
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                HANDOFF.issue(args)
            journal = json.loads(
                (root / "issuance.journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(journal["phase"], "provider-reconciled")
            self.assertEqual(journal["public_key_id"], "authpublickey-created")
            self.assertEqual(
                stat.S_IMODE((root / "issuance.journal.json").stat().st_mode), 0o600
            )

    def test_handoff_issue_binds_predecessor_and_provider_enforced_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "handoff"
            expiry = (
                (HANDOFF.utc_now() + timedelta(days=1))
                .isoformat()
                .replace("+00:00", "Z")
            )
            args = argparse.Namespace(
                directory=root,
                nebius="nebius-test",
                openssl="openssl-test",
                admin_profile="sandbox",
                service_account_id="serviceaccount-viewer",
                viewer_group_id="group-viewer",
                project_id="project-test",
                cluster_id="mk8scluster-test",
                expires_at=expiry,
                name="fs2-operator-handoff",
                lineage_id="lineage-test",
                generation=2,
                predecessor_public_key_id="authpublickey-old",
                predecessor_service_account_id="serviceaccount-admin",
                predecessor_group_id="group-admin",
                predecessor_project_id="project-test",
                predecessor_generation=1,
                predecessor_role="admin",
            )

            def key_document(key_id, service_account_id, expires_at=None):
                return {
                    "metadata": {
                        "id": key_id,
                        "parent_id": "project-test",
                        "name": "fs2-operator-handoff"
                        if key_id.endswith("new")
                        else "old",
                        "labels": {
                            "handoff_lineage": "lineage-test",
                            "handoff_generation": "2"
                            if key_id.endswith("new")
                            else "1",
                        },
                    },
                    "spec": {
                        "account": {"service_account": {"id": service_account_id}},
                        "expires_at": expires_at,
                        "data": "test-key" if key_id.endswith("new") else "old-key",
                    },
                }

            def fake_run(command, *, capture=False, check=True):
                if command[0] == "openssl-test":
                    output = Path(command[command.index("-out") + 1])
                    output.write_text("test-key", encoding="ascii")
                    return subprocess.CompletedProcess(command, 0, stdout="")
                if command[1:4] == ["iam", "auth-public-key", "get"]:
                    key_id = command[command.index("--id") + 1]
                    payload = (
                        key_document(key_id, "serviceaccount-admin")
                        if key_id == "authpublickey-old"
                        else key_document(key_id, "serviceaccount-viewer", expiry)
                    )
                    return subprocess.CompletedProcess(
                        command, 0, stdout=json.dumps(payload)
                    )
                if command[1:4] == ["iam", "auth-public-key", "create"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps({"metadata": {"id": "authpublickey-new"}}),
                    )
                return subprocess.CompletedProcess(command, 0, stdout="")

            expected_new = HANDOFF.auth_key_binding(
                key_document("authpublickey-new", "serviceaccount-viewer", expiry)
            )

            def lineage(_args, **kwargs):
                return {
                    "service_account_id": kwargs["service_account_id"],
                    "group_id": kwargs["group_id"],
                    "project_id": kwargs["project_id"],
                    "lineage_id": kwargs["lineage_id"],
                    "generation": kwargs["generation"],
                    "role": kwargs["required_role"],
                }

            with (
                mock.patch.object(HANDOFF, "run", side_effect=fake_run),
                mock.patch.object(
                    HANDOFF, "service_account_lineage", side_effect=lineage
                ),
                mock.patch.object(
                    HANDOFF, "reconcile_issued_key", return_value=expected_new
                ),
            ):
                result = HANDOFF.issue(args)
            self.assertEqual(result["public_key_id"], "authpublickey-new")
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(
                receipt["predecessor"]["public_key_id"], "authpublickey-old"
            )
            self.assertEqual(
                receipt["predecessor"]["service_account_id"], "serviceaccount-admin"
            )
            self.assertEqual(receipt["expires_at"], expiry)
            self.assertIsNotNone(receipt["provider_expiry_verified_at"])

    def test_handoff_issue_revokes_new_key_when_provider_does_not_enforce_expiry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "handoff"
            expiry = (
                (HANDOFF.utc_now() + timedelta(days=1))
                .isoformat()
                .replace("+00:00", "Z")
            )
            args = argparse.Namespace(
                directory=root,
                nebius="nebius-test",
                openssl="openssl-test",
                admin_profile="sandbox",
                service_account_id="serviceaccount-viewer",
                viewer_group_id="group-viewer",
                project_id="project-test",
                cluster_id="mk8scluster-test",
                expires_at=expiry,
                name="fs2-operator-handoff",
                lineage_id="lineage-test",
                generation=2,
                predecessor_public_key_id="authpublickey-old",
                predecessor_service_account_id="serviceaccount-admin",
                predecessor_group_id="group-admin",
                predecessor_project_id="project-test",
                predecessor_generation=1,
                predecessor_role="admin",
            )
            deleted: list[str] = []

            def fake_run(command, *, capture=False, check=True):
                if command[0] == "openssl-test":
                    Path(command[command.index("-out") + 1]).write_text(
                        "test-key", encoding="ascii"
                    )
                    return subprocess.CompletedProcess(command, 0, stdout="")
                if command[1:4] == ["iam", "auth-public-key", "create"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps({"metadata": {"id": "authpublickey-new"}}),
                    )
                if command[1:4] == ["iam", "auth-public-key", "get"]:
                    key_id = command[command.index("--id") + 1]
                    account = (
                        "serviceaccount-admin"
                        if key_id == "authpublickey-old"
                        else "serviceaccount-viewer"
                    )
                    payload = {
                        "metadata": {"id": key_id, "parent_id": "project-test"},
                        "spec": {
                            "account": {"service_account": {"id": account}},
                            "expires_at": None,
                        },
                    }
                    return subprocess.CompletedProcess(
                        command, 0, stdout=json.dumps(payload)
                    )
                if command[1:4] == ["iam", "auth-public-key", "delete"]:
                    deleted.append(command[command.index("--id") + 1])
                if command[1:4] == ["iam", "auth-public-key", "list"]:
                    return subprocess.CompletedProcess(command, 0, stdout="[]")
                return subprocess.CompletedProcess(command, 0, stdout="")

            with (
                mock.patch.object(HANDOFF, "run", side_effect=fake_run),
                mock.patch.object(
                    HANDOFF,
                    "service_account_lineage",
                    side_effect=lambda _args, **kwargs: {
                        "service_account_id": kwargs["service_account_id"],
                        "group_id": kwargs["group_id"],
                        "project_id": kwargs["project_id"],
                        "lineage_id": kwargs["lineage_id"],
                        "generation": kwargs["generation"],
                        "role": kwargs["required_role"],
                    },
                ),
                self.assertRaisesRegex(
                    HANDOFF.HandoffError, "provider-enforced expiry"
                ),
            ):
                HANDOFF.issue(args)
            self.assertEqual(deleted, ["authpublickey-new"])
            self.assertFalse((root / "receipt.json").exists())
            self.assertFalse((root / "private-key.pem").exists())
            self.assertFalse((root / "public-key.pem").exists())

    def test_revoke_old_uses_bound_predecessor_and_proves_inventory_absence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = HANDOFF.private_directory(Path(temporary) / "handoff")
            HANDOFF.private_json(
                root / "receipt.json",
                {
                    "schema": "fs2-serve.nebius.ai/operator-handoff/v2",
                    "service_account_id": "serviceaccount-viewer",
                    "project_id": "project-test",
                    "cluster_id": "mk8scluster-test",
                    "public_key_id": "authpublickey-new",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "delivery": {"recipient": "operator"},
                    "verification": {"verified_at": "2026-09-16T00:00:00Z"},
                    "predecessor": {
                        "public_key_id": "authpublickey-old",
                        "service_account_id": "serviceaccount-admin",
                        "project_id": "project-test",
                        "expires_at": None,
                    },
                    "revoked_old_key": None,
                },
            )
            args = argparse.Namespace(
                directory=root,
                nebius="nebius-test",
                admin_profile="sandbox",
                confirm_predecessor_public_key_id="authpublickey-old",
            )
            deleted: list[str] = []

            def fake_run(command, *, capture=False, check=True):
                action = command[3]
                if action == "get":
                    if deleted:
                        self.assertFalse(check)
                        return subprocess.CompletedProcess(
                            command, 1, stdout="", stderr="resource not found"
                        )
                    payload = {
                        "metadata": {
                            "id": "authpublickey-old",
                            "parent_id": "project-test",
                        },
                        "spec": {
                            "account": {
                                "service_account": {"id": "serviceaccount-admin"}
                            }
                        },
                    }
                    return subprocess.CompletedProcess(
                        command, 0, stdout=json.dumps(payload)
                    )
                if action == "delete":
                    deleted.append(command[command.index("--id") + 1])
                    return subprocess.CompletedProcess(command, 0, stdout="")
                if action == "list":
                    inventory = [{"metadata": {"id": "authpublickey-new"}}]
                    return subprocess.CompletedProcess(
                        command, 0, stdout=json.dumps(inventory)
                    )
                raise AssertionError(command)

            with (
                mock.patch.object(HANDOFF, "run", side_effect=fake_run),
                mock.patch.object(HANDOFF, "prove_receipt_lineage"),
            ):
                result = HANDOFF.revoke_old(args)
            self.assertEqual(result["public_key_id"], "authpublickey-old")
            self.assertEqual(deleted, ["authpublickey-old"])
            receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
            self.assertTrue(receipt["revoked_old_key"]["post_delete_absence_verified"])
            self.assertTrue(receipt["revoked_old_key"]["authoritative_get_absent"])
            with (
                mock.patch.object(
                    HANDOFF, "run", side_effect=AssertionError("must not run")
                ),
                mock.patch.object(HANDOFF, "prove_receipt_lineage"),
                self.assertRaisesRegex(HANDOFF.HandoffError, "already revoked"),
            ):
                HANDOFF.revoke_old(args)

    def test_revoke_old_does_not_treat_provider_failure_as_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = HANDOFF.private_directory(Path(temporary) / "handoff")
            HANDOFF.private_json(
                root / "receipt.json",
                {
                    "schema": "fs2-serve.nebius.ai/operator-handoff/v2",
                    "service_account_id": "serviceaccount-viewer",
                    "project_id": "project-test",
                    "cluster_id": "mk8scluster-test",
                    "public_key_id": "authpublickey-new",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "delivery": {"recipient": "operator"},
                    "verification": {"verified_at": "2026-09-16T00:00:00Z"},
                    "predecessor": {
                        "public_key_id": "authpublickey-old",
                        "service_account_id": "serviceaccount-admin",
                        "project_id": "project-test",
                        "expires_at": None,
                    },
                    "revocation_attempt": {
                        "public_key_id": "authpublickey-old",
                        "service_account_id": "serviceaccount-admin",
                        "project_id": "project-test",
                        "initiated_at": "2026-09-16T00:00:00Z",
                    },
                    "revoked_old_key": None,
                },
            )
            args = argparse.Namespace(
                directory=root,
                nebius="nebius-test",
                admin_profile="sandbox",
                confirm_predecessor_public_key_id="authpublickey-old",
            )

            def fake_run(command, *, capture=False, check=True):
                action = command[3]
                if action == "list":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps([{"metadata": {"id": "authpublickey-new"}}]),
                    )
                if action == "get":
                    self.assertFalse(check)
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        stdout="",
                        stderr="permission denied",
                    )
                raise AssertionError(command)

            with (
                mock.patch.object(HANDOFF, "run", side_effect=fake_run),
                mock.patch.object(HANDOFF, "prove_receipt_lineage"),
                self.assertRaisesRegex(HANDOFF.HandoffError, "absence was not proven"),
            ):
                HANDOFF.revoke_old(args)
            receipt = json.loads((root / "receipt.json").read_text())
            self.assertIsNone(receipt["revoked_old_key"])
            self.assertEqual(
                receipt["revocation_attempt"]["public_key_id"],
                "authpublickey-old",
            )

    def test_revoke_old_rejects_unbound_provider_identity_before_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = HANDOFF.private_directory(Path(temporary) / "handoff")
            HANDOFF.private_json(
                root / "receipt.json",
                {
                    "schema": "fs2-serve.nebius.ai/operator-handoff/v2",
                    "service_account_id": "serviceaccount-viewer",
                    "project_id": "project-test",
                    "cluster_id": "mk8scluster-test",
                    "public_key_id": "authpublickey-new",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "delivery": {"recipient": "operator"},
                    "verification": {"verified_at": "2026-09-16T00:00:00Z"},
                    "predecessor": {
                        "public_key_id": "authpublickey-old",
                        "service_account_id": "serviceaccount-admin",
                        "project_id": "project-test",
                        "expires_at": None,
                    },
                    "revoked_old_key": None,
                },
            )
            args = argparse.Namespace(
                directory=root,
                nebius="nebius-test",
                admin_profile="sandbox",
                confirm_predecessor_public_key_id="authpublickey-old",
            )
            commands: list[list[str]] = []
            unbound = {
                "metadata": {"id": "authpublickey-old", "parent_id": "project-other"},
                "spec": {
                    "account": {"service_account": {"id": "serviceaccount-other"}}
                },
            }

            def fake_run(command, *, capture=False, check=True):
                commands.append(list(command))
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(unbound)
                )

            with (
                mock.patch.object(HANDOFF, "run", side_effect=fake_run),
                mock.patch.object(HANDOFF, "prove_receipt_lineage"),
                self.assertRaisesRegex(HANDOFF.HandoffError, "bound receipt"),
            ):
                HANDOFF.revoke_old(args)
            self.assertFalse(any(command[3] == "delete" for command in commands))

    def test_revoke_old_rejects_different_confirmation_without_provider_action(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = HANDOFF.private_directory(Path(temporary) / "handoff")
            HANDOFF.private_json(
                root / "receipt.json",
                {
                    "schema": "fs2-serve.nebius.ai/operator-handoff/v2",
                    "service_account_id": "serviceaccount-viewer",
                    "project_id": "project-test",
                    "cluster_id": "mk8scluster-test",
                    "public_key_id": "authpublickey-new",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "delivery": {"recipient": "operator"},
                    "verification": {"verified_at": "2026-09-16T00:00:00Z"},
                    "predecessor": {
                        "public_key_id": "authpublickey-old",
                        "service_account_id": "serviceaccount-admin",
                        "project_id": "project-test",
                        "expires_at": None,
                    },
                    "revoked_old_key": None,
                },
            )
            args = argparse.Namespace(
                directory=root,
                nebius="nebius-test",
                admin_profile="sandbox",
                confirm_predecessor_public_key_id="authpublickey-arbitrary",
            )
            with (
                mock.patch.object(
                    HANDOFF, "run", side_effect=AssertionError("must not run")
                ),
                mock.patch.object(HANDOFF, "prove_receipt_lineage"),
                self.assertRaisesRegex(HANDOFF.HandoffError, "confirmation"),
            ):
                HANDOFF.revoke_old(args)

    def test_viewer_verification_requires_inventory_denials_and_exact_live_cidrs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = HANDOFF.private_directory(Path(temporary) / "handoff")
            (root / "private-key.pem").write_text("test-private-key", encoding="utf-8")
            (root / "private-key.pem").chmod(0o600)
            receipt = {
                "schema": "fs2-serve.nebius.ai/operator-handoff/v2",
                "service_account_id": "serviceaccount-test",
                "project_id": "project-test",
                "cluster_id": "mk8scluster-test",
                "public_key_id": "authpublickey-test",
                "expires_at": "2099-01-01T00:00:00Z",
                "delivery": {"recipient": "operator"},
                "verification": None,
                "provider_expiry_verified_at": "2026-09-16T00:00:00Z",
                "predecessor": {
                    "public_key_id": "authpublickey-old",
                    "service_account_id": "serviceaccount-old",
                    "project_id": "project-test",
                    "expires_at": None,
                },
            }
            HANDOFF.private_json(root / "receipt.json", receipt)
            args = argparse.Namespace(
                directory=root,
                nebius="nebius-test",
                kubectl="kubectl-test",
                approved_egress=["192.0.2.8/32"],
                admin_profile="sandbox",
            )

            def fake_run(command, *, capture=False, check=True):
                if command[1:3] == ["profile", "create"]:
                    config = Path(command[command.index("--config") + 1])
                    config.write_text("test-config", encoding="utf-8")
                if "get-credentials" in command:
                    kubeconfig = Path(command[command.index("--kubeconfig") + 1])
                    kubeconfig.write_text("test-kubeconfig", encoding="utf-8")
                if command[0] == "kubectl-test" and "auth" in command:
                    if "--list" in command:
                        self.assertTrue(check)
                        rules = (
                            "namespaces [] [] [get list]\npods [] [] [get list watch]\n"
                        )
                        return subprocess.CompletedProcess(command, 0, stdout=rules)
                    self.assertFalse(check)
                    return subprocess.CompletedProcess(command, 1, stdout="no\n")
                if command[0] == "kubectl-test":
                    return subprocess.CompletedProcess(
                        command, 0, stdout="namespace/default\n"
                    )
                if command[1:4] == ["iam", "auth-public-key", "get"]:
                    payload = {
                        "metadata": {
                            "id": "authpublickey-test",
                            "parent_id": "project-test",
                        },
                        "spec": {
                            "account": {
                                "service_account": {"id": "serviceaccount-test"}
                            },
                            "expires_at": "2099-01-01T00:00:00Z",
                        },
                    }
                    return subprocess.CompletedProcess(
                        command, 0, stdout=json.dumps(payload)
                    )
                if command[:5] == ["nebius-test", "mk8s", "v1", "cluster", "get"]:
                    payload = {
                        "spec": {
                            "control_plane": {
                                "endpoints": {
                                    "public_endpoint": {
                                        "allowed_cidrs": ["192.0.2.8/32"]
                                    }
                                }
                            }
                        }
                    }
                    return subprocess.CompletedProcess(
                        command, 0, stdout=json.dumps(payload)
                    )
                return subprocess.CompletedProcess(command, 0, stdout="")

            with (
                mock.patch.object(HANDOFF, "run", side_effect=fake_run),
                mock.patch.object(HANDOFF, "prove_receipt_lineage"),
            ):
                result = HANDOFF.verify(args)
            self.assertEqual(result["status"], "verified")
            verified = json.loads((root / "receipt.json").read_text())
            self.assertTrue(verified["verification"]["inventory_allowed"])
            self.assertTrue(verified["verification"]["create_pods_denied"])
            self.assertTrue(verified["verification"]["read_secrets_denied"])
            self.assertEqual(
                verified["verification"]["negative_probe_count"],
                len(HANDOFF.negative_authorization_probes()),
            )
            self.assertEqual(verified["verification"]["authorization_rule_count"], 2)
            self.assertEqual(
                verified["verification"]["approved_egress_cidrs"], ["192.0.2.8/32"]
            )

    def test_viewer_verification_rejects_live_cidr_set_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = HANDOFF.private_directory(Path(temporary) / "handoff")
            (root / "private-key.pem").write_text("test-private-key", encoding="utf-8")
            (root / "private-key.pem").chmod(0o600)
            HANDOFF.private_json(
                root / "receipt.json",
                {
                    "schema": "fs2-serve.nebius.ai/operator-handoff/v2",
                    "service_account_id": "serviceaccount-test",
                    "project_id": "project-test",
                    "cluster_id": "mk8scluster-test",
                    "public_key_id": "authpublickey-test",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "delivery": {"recipient": "operator"},
                    "verification": None,
                    "provider_expiry_verified_at": "2026-09-16T00:00:00Z",
                    "predecessor": {
                        "public_key_id": "authpublickey-old",
                        "service_account_id": "serviceaccount-old",
                        "project_id": "project-test",
                        "expires_at": None,
                    },
                },
            )
            args = argparse.Namespace(
                directory=root,
                nebius="nebius-test",
                kubectl="kubectl-test",
                approved_egress=["192.0.2.8/32"],
                admin_profile="sandbox",
            )

            def fake_run(command, *, capture=False, check=True):
                if command[1:3] == ["profile", "create"]:
                    config = Path(command[command.index("--config") + 1])
                    config.write_text("test-config", encoding="utf-8")
                if "get-credentials" in command:
                    path = Path(command[command.index("--kubeconfig") + 1])
                    path.write_text("test-kubeconfig", encoding="utf-8")
                if command[0] == "kubectl-test" and "auth" in command:
                    if "--list" in command:
                        rules = (
                            "namespaces [] [] [get list]\npods [] [] [get list watch]\n"
                        )
                        return subprocess.CompletedProcess(command, 0, stdout=rules)
                    self.assertFalse(check)
                    return subprocess.CompletedProcess(command, 1, stdout="no\n")
                if command[0] == "kubectl-test":
                    return subprocess.CompletedProcess(
                        command, 0, stdout="namespace/default\n"
                    )
                if command[1:4] == ["iam", "auth-public-key", "get"]:
                    payload = {
                        "metadata": {
                            "id": "authpublickey-test",
                            "parent_id": "project-test",
                        },
                        "spec": {
                            "account": {
                                "service_account": {"id": "serviceaccount-test"}
                            },
                            "expires_at": "2099-01-01T00:00:00Z",
                        },
                    }
                    return subprocess.CompletedProcess(
                        command, 0, stdout=json.dumps(payload)
                    )
                if command[:5] == ["nebius-test", "mk8s", "v1", "cluster", "get"]:
                    payload = {
                        "spec": {
                            "control_plane": {
                                "endpoints": {
                                    "public_endpoint": {
                                        "allowed_cidrs": ["192.0.2.9/32"]
                                    }
                                }
                            }
                        }
                    }
                    return subprocess.CompletedProcess(
                        command, 0, stdout=json.dumps(payload)
                    )
                return subprocess.CompletedProcess(command, 0, stdout="")

            with (
                mock.patch.object(HANDOFF, "run", side_effect=fake_run),
                mock.patch.object(HANDOFF, "prove_receipt_lineage"),
                self.assertRaisesRegex(HANDOFF.HandoffError, "differ"),
            ):
                HANDOFF.verify(args)

    def test_viewer_rule_inventory_rejects_any_mutation_or_secret_read(self) -> None:
        for rules, expected in (
            ("pods [] [] [get list create]\n", "mutation"),
            ("secrets [] [] [get]\n", "Secret read"),
            ("*.* [] [] [*]\n", "mutation"),
        ):
            with (
                self.subTest(rules=rules),
                self.assertRaisesRegex(HANDOFF.HandoffError, expected),
            ):
                HANDOFF.require_viewer_rules(HANDOFF.authorization_rules(rules))

        HANDOFF.require_viewer_rules(
            HANDOFF.authorization_rules(
                "selfsubjectaccessreviews.authorization.k8s.io [] [] [create]\n"
                "selfsubjectrulesreviews.authorization.k8s.io [] [] [create]\n"
                "selfsubjectreviews.authentication.k8s.io [] [] [create]\n"
                "pods [] [] [get list watch]\n"
            )
        )
        with self.assertRaisesRegex(HANDOFF.HandoffError, "mutation"):
            HANDOFF.require_viewer_rules(
                HANDOFF.authorization_rules(
                    "selfsubjectaccessreviews.authorization.k8s.io [] [] [create update]\n"
                )
            )


if __name__ == "__main__":
    unittest.main()
