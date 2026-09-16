from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
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
STACK = load_script("inference_stack", ROOT / "inference-stack")


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


class OperatorAccessHygieneTests(unittest.TestCase):
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
        consumers = resources["database_consumer"]
        self.assertIn("data_wo", consumers)
        self.assertIn(
            "data_wo_revision = var.credential_generations.database", consumers
        )
        self.assertIn("depends_on = [kubernetes_manifest.control_database]", consumers)
        self.assertIn("local.active_database_usernames", consumers)
        self.assertIn("local.active_database_passwords", consumers)

    def test_key_classes_rotate_independently_with_retained_v1_and_write_only_delivery(
        self,
    ) -> None:
        secrets = (ROOT / "stages/workloads/secrets.tf").read_text(encoding="utf-8")
        variables = (ROOT / "stages/workloads/variables.tf").read_text(encoding="utf-8")
        for key_class in ("payload", "ledger", "pepper", "attestor"):
            self.assertRegex(variables, rf"(?m)^\s*{key_class}\s+= optional\(object")
        self.assertNotIn("key_material = optional", variables)
        for resource, legacy_id in {
            "payload_keyring_versioned": "payload-v1",
            "ledger_keyring_versioned": "ledger-v1",
            "token_pepper_versioned": "pepper-v1",
            "route_attestors_versioned": "generation-1 public key",
        }.items():
            block = dict(hcl_blocks(secrets, "resource"))[resource]
            self.assertIn("data_wo", block)
            self.assertIn("data_wo_revision", block)
            self.assertIn(legacy_id, block)
        self.assertIn("var.keyring_generations.payload.retained", secrets)
        self.assertIn("var.keyring_generations.ledger.retained", secrets)
        self.assertIn("var.keyring_generations.pepper.retained", secrets)
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
        foundation = (ROOT / "stages/foundation/releases.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn('"fs2.nebius.ai/secret-rollout-generation"', foundation)

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
                    self.assertRaisesRegex(
                        STACK.DeploymentError, "server-enforced TTL"
                    ),
                ):
                    STACK.scoped_credential_handoff(
                        "terraform",
                        "kubectl",
                        Path("/private/run"),
                        {},
                        kind=kind,
                        expires_in_seconds=3600,
                        allow_privileged=True,
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
        for address in (
            "random_id.bootstrap_access_token_id",
            "random_id.scientific_access_token_id[0]",
            "random_password.bootstrap_access_token_secret",
            "random_password.scientific_access_token_secret[0]",
            "kubernetes_secret_v1.bootstrap_access",
            "kubernetes_secret_v1.scientific_access[0]",
            'random_password.database["runtime"]',
            'kubernetes_secret_v1.database_account["runtime"]',
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

    def test_wrapper_guards_both_generated_and_saved_plans(self) -> None:
        source = (ROOT / "inference-stack").read_text(encoding="utf-8")
        self.assertIn("fixed-v1-identity.receipt.json", source)
        self.assertIn(
            "guard_saved_plan(terraform, root, plan_path, environment)", source
        )
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

            def fake_run(command, *, env=None, capture=False, input_text=None):
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

    def test_retirement_canary_requires_no_plaintext_plan_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            base.chmod(0o700)
            root = base / "run"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            state = root / "workloads.tfstate"
            state.write_text("test metadata", encoding="utf-8")
            state.chmod(0o600)
            manifest = GUARD.write_artifact_manifest(
                root, base / "artifacts.receipt.json"
            )
            self.assertEqual(
                GUARD.inspect_run_root(root, retired=False)["plaintext_artifacts"], 1
            )
            with self.assertRaises(GUARD.GuardError):
                GUARD.inspect_run_root(root, retired=True, artifact_manifest=manifest)
            state.unlink()
            self.assertEqual(
                GUARD.inspect_run_root(root, retired=True, artifact_manifest=manifest)[
                    "plaintext_artifacts"
                ],
                0,
            )
            with self.assertRaisesRegex(GUARD.GuardError, "manifest"):
                GUARD.inspect_run_root(root, retired=True)

    def test_retirement_canary_catches_real_plan_cookie_and_scoped_export_names(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            base.chmod(0o700)
            root = base / "run"
            root.mkdir(mode=0o700)
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
                GUARD.inspect_run_root(root, retired=False)["plaintext_artifacts"], 3
            )
            self.assertEqual(
                GUARD.inspect_run_root(root, retired=False)["unknown_artifacts"], 1
            )
            manifest = GUARD.write_artifact_manifest(
                root, base / "artifacts.receipt.json"
            )
            self.assertEqual(len(manifest["artifacts"]), 4)
            with self.assertRaises(GUARD.GuardError):
                GUARD.inspect_run_root(root, retired=True, artifact_manifest=manifest)
            for path in root.iterdir():
                path.unlink()
            self.assertEqual(
                GUARD.inspect_run_root(root, retired=True, artifact_manifest=manifest)[
                    "total_artifacts"
                ],
                0,
            )

    def test_versioned_pat_ids_cannot_be_reused_across_generation_or_audience(
        self,
    ) -> None:
        token_a = "fs2_pat_" + ("a" * 32) + "_" + ("A" * 32)
        token_b = "fs2_pat_" + ("b" * 32) + "_" + ("B" * 32)
        STACK.validate_versioned_access_pat_ids(
            json.dumps({"2": token_a}), json.dumps({"2": token_b})
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
                    json.dumps(general), json.dumps(scientific)
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
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            tokens_path = root / "tokens.json"
            tokens_path.write_text(
                json.dumps(
                    {
                        "general": {"1": token_1, "2": token_2},
                        "scientific": {"1": scientific},
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
        ):
            with (
                self.subTest(document=document),
                self.assertRaisesRegex(PAT_GUARD.PatGuardError, expected),
            ):
                PAT_GUARD.parse_tokens(document)

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
                project_id="project-test",
                cluster_id="mk8scluster-test",
                expires_at=expiry,
                name="fs2-operator-handoff",
                predecessor_public_key_id="authpublickey-old",
                predecessor_service_account_id="serviceaccount-admin",
                predecessor_project_id="project-test",
            )

            def key_document(key_id, service_account_id, expires_at=None):
                return {
                    "metadata": {"id": key_id, "parent_id": "project-test"},
                    "spec": {
                        "account": {"service_account": {"id": service_account_id}},
                        "expires_at": expires_at,
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

            with mock.patch.object(HANDOFF, "run", side_effect=fake_run):
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
                project_id="project-test",
                cluster_id="mk8scluster-test",
                expires_at=expiry,
                name="fs2-operator-handoff",
                predecessor_public_key_id="authpublickey-old",
                predecessor_service_account_id="serviceaccount-admin",
                predecessor_project_id="project-test",
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
                        return subprocess.CompletedProcess(command, 1, stdout="")
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

            with mock.patch.object(HANDOFF, "run", side_effect=fake_run):
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
                self.assertRaisesRegex(HANDOFF.HandoffError, "already revoked"),
            ):
                HANDOFF.revoke_old(args)

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

            with mock.patch.object(HANDOFF, "run", side_effect=fake_run):
                result = HANDOFF.verify(args)
            self.assertEqual(result["status"], "verified")
            verified = json.loads((root / "receipt.json").read_text())
            self.assertTrue(verified["verification"]["inventory_allowed"])
            self.assertTrue(verified["verification"]["create_pods_denied"])
            self.assertTrue(verified["verification"]["read_secrets_denied"])
            self.assertEqual(verified["verification"]["negative_probe_count"], 9)
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


if __name__ == "__main__":
    unittest.main()
