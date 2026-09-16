from __future__ import annotations

import importlib.machinery
import importlib.util
import argparse
import json
import re
import stat
import subprocess
import tempfile
import unittest
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


GUARD = load_script("secret_migration_guard", ROOT / "scripts/secret_migration_guard.py")
HANDOFF = load_script("operator_handoff", ROOT / "scripts/operator_handoff.py")


def hcl_blocks(source: str, block_type: str) -> list[tuple[str, str]]:
    labels = r'"([^"]+)"\s+"([^"]+)"' if block_type in {"resource", "ephemeral"} else r'"([^"]+)"'
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
                    blocks.append((match.group(name_group), source[match.start() : index + 1]))
                    break
        else:
            raise AssertionError(f"unterminated {block_type} block")
    return blocks


class OperatorAccessHygieneTests(unittest.TestCase):
    def test_fixed_generation_one_resources_are_preserved_and_protected(self) -> None:
        secrets = (ROOT / "stages/workloads/secrets.tf").read_text(encoding="utf-8")
        bootstrap = (ROOT / "stages/workloads/bootstrap_access.tf").read_text(encoding="utf-8")
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
        self.assertIn("prevent_destroy = true", resources["database"])
        self.assertIn("prevent_destroy = true", resources["database_account"])

    def test_database_rotation_adds_logins_before_switching_write_only_consumers(self) -> None:
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
        self.assertIn("data_wo_revision = var.credential_generations.database", consumers)
        self.assertIn("depends_on = [kubernetes_manifest.control_database]", consumers)
        self.assertIn("local.active_database_usernames", consumers)
        self.assertIn("local.active_database_passwords", consumers)

    def test_key_classes_rotate_independently_with_retained_v1_and_write_only_delivery(self) -> None:
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
            for path in (ROOT / "charts/control-plane/fs2-serve-control-plane/templates").glob("*.yaml")
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
        for generation in ("database", "admin", "payload", "ledger", "pepper", "attestor"):
            self.assertIn(f'"{generation}" .Values.secretRollout.', runtime)
        bootstrap = templates["bootstrap-access-job.yaml"]
        for generation in ("database", "access", "payload", "ledger", "pepper"):
            self.assertIn(f'"{generation}" .Values.secretRollout.', bootstrap)
        workloads = (ROOT / "stages/workloads/control_plane.tf").read_text(encoding="utf-8")
        self.assertIn("atomic           = true", workloads)
        self.assertIn("wait             = true", workloads)
        self.assertIn("kubernetes_secret_v1.payload_keyring_versioned", workloads)
        foundation = (ROOT / "stages/foundation/releases.tf").read_text(encoding="utf-8")
        self.assertIn('"fs2.nebius.ai/secret-rollout-generation"', foundation)

    def test_control_plane_allowlist_accepts_only_canonical_hosts(self) -> None:
        for path in ("variables.tf", "stages/infrastructure/variables.tf"):
            source = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn('? 128 : 32}', source)
            self.assertIn("cidr ==", source)
        self.assertEqual(HANDOFF.host_cidrs(["192.0.2.8/32", "2001:db8::1/128"]), ["192.0.2.8/32", "2001:db8::1/128"])
        for bypass in (["0.0.0.0/0"], ["0.0.0.0/1", "128.0.0.0/1"], ["192.0.2.7/24"]):
            with self.subTest(bypass=bypass), self.assertRaises(HANDOFF.HandoffError):
                HANDOFF.host_cidrs(bypass)

    def test_sensitive_outputs_are_secret_references_only(self) -> None:
        outputs = (ROOT / "stages/workloads/outputs.tf").read_text(encoding="utf-8")
        for output_name in (
            "admin_token", "admin_bootstrap_token", "mcp_access_token", "inference_access_token",
            "scientific_access_token", "grafana_admin_username", "grafana_admin_password",
        ):
            self.assertNotIn(f'output "{output_name}"', outputs)
        access = dict(hcl_blocks(outputs, "output"))["access_bundle"]
        self.assertIn("access-bundle-contract/v2", access)
        self.assertIn("credential_secret_refs", access)
        self.assertNotIn("random_password", access)

    def test_plan_guard_rejects_fixed_generation_delete_or_replace(self) -> None:
        safe = {"resource_changes": [{"address": "random_password.admin_token", "change": {"actions": ["no-op"]}}]}
        self.assertEqual(GUARD.inspect_plan(safe)["protected_changes"], 0)
        for actions in (["delete"], ["delete", "create"], ["replace"]):
            with self.subTest(actions=actions), self.assertRaises(GUARD.GuardError):
                GUARD.inspect_plan({"resource_changes": [{"address": "random_password.admin_token", "change": {"actions": actions}}]})
        for address in (
            'random_password.database["runtime"]',
            'kubernetes_secret_v1.database_account["runtime"]',
        ):
            with self.subTest(address=address), self.assertRaises(GUARD.GuardError):
                GUARD.inspect_plan({"resource_changes": [{"address": address, "change": {"actions": ["delete", "create"]}}]})

    def test_retirement_canary_requires_no_plaintext_plan_or_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            state = root / "workloads.tfstate"
            state.write_text("test metadata", encoding="utf-8")
            state.chmod(0o600)
            self.assertEqual(GUARD.inspect_run_root(root, retired=False)["plaintext_artifacts"], 1)
            with self.assertRaises(GUARD.GuardError):
                GUARD.inspect_run_root(root, retired=True)
            state.unlink()
            self.assertEqual(GUARD.inspect_run_root(root, retired=True)["plaintext_artifacts"], 0)

    def test_operator_receipts_remain_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = HANDOFF.private_directory(Path(temporary) / "handoff")
            receipt = {"schema": "fs2-serve.nebius.ai/operator-handoff/v1"}
            HANDOFF.private_json(root / "receipt.json", receipt)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "receipt.json").stat().st_mode), 0o600)
            self.assertEqual(json.loads((root / "receipt.json").read_text()), receipt)

    def test_viewer_verification_requires_inventory_denials_and_exact_live_cidrs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = HANDOFF.private_directory(Path(temporary) / "handoff")
            (root / "private-key.pem").write_text("test-private-key", encoding="utf-8")
            (root / "private-key.pem").chmod(0o600)
            receipt = {
                "schema": "fs2-serve.nebius.ai/operator-handoff/v1",
                "service_account_id": "serviceaccount-test",
                "project_id": "project-test",
                "cluster_id": "mk8scluster-test",
                "public_key_id": "authpublickey-test",
                "expires_at": "2099-01-01T00:00:00Z",
                "delivery": {"recipient": "operator"},
                "verification": None,
            }
            HANDOFF.private_json(root / "receipt.json", receipt)
            args = argparse.Namespace(
                directory=root,
                nebius="nebius-test",
                kubectl="kubectl-test",
                approved_egress=["192.0.2.8/32"],
            )

            def fake_run(command, *, capture=False, check=True):
                if command[1:3] == ["profile", "create"]:
                    config = Path(command[command.index("--config") + 1])
                    config.write_text("test-config", encoding="utf-8")
                if "get-credentials" in command:
                    kubeconfig = Path(command[command.index("--kubeconfig") + 1])
                    kubeconfig.write_text("test-kubeconfig", encoding="utf-8")
                if command[0] == "kubectl-test" and "auth" in command:
                    self.assertFalse(check)
                    return subprocess.CompletedProcess(command, 1, stdout="no\n")
                if command[0] == "kubectl-test":
                    return subprocess.CompletedProcess(command, 0, stdout="namespace/default\n")
                if command[:5] == ["nebius-test", "mk8s", "v1", "cluster", "get"]:
                    payload = {"spec": {"control_plane": {"endpoints": {"public_endpoint": {"allowed_cidrs": ["192.0.2.8/32"]}}}}}
                    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload))
                return subprocess.CompletedProcess(command, 0, stdout="")

            with mock.patch.object(HANDOFF, "run", side_effect=fake_run):
                result = HANDOFF.verify(args)
            self.assertEqual(result["status"], "verified")
            verified = json.loads((root / "receipt.json").read_text())
            self.assertTrue(verified["verification"]["inventory_allowed"])
            self.assertTrue(verified["verification"]["create_pods_denied"])
            self.assertTrue(verified["verification"]["read_secrets_denied"])
            self.assertEqual(verified["verification"]["approved_egress_cidrs"], ["192.0.2.8/32"])

    def test_viewer_verification_rejects_live_cidr_set_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = HANDOFF.private_directory(Path(temporary) / "handoff")
            (root / "private-key.pem").write_text("test-private-key", encoding="utf-8")
            (root / "private-key.pem").chmod(0o600)
            HANDOFF.private_json(root / "receipt.json", {
                "schema": "fs2-serve.nebius.ai/operator-handoff/v1",
                "service_account_id": "serviceaccount-test", "project_id": "project-test",
                "cluster_id": "mk8scluster-test", "public_key_id": "authpublickey-test",
                "expires_at": "2099-01-01T00:00:00Z", "delivery": {"recipient": "operator"},
                "verification": None,
            })
            args = argparse.Namespace(directory=root, nebius="nebius-test", kubectl="kubectl-test", approved_egress=["192.0.2.8/32"])

            def fake_run(command, *, capture=False, check=True):
                if command[1:3] == ["profile", "create"]:
                    config = Path(command[command.index("--config") + 1])
                    config.write_text("test-config", encoding="utf-8")
                if "get-credentials" in command:
                    path = Path(command[command.index("--kubeconfig") + 1])
                    path.write_text("test-kubeconfig", encoding="utf-8")
                if command[0] == "kubectl-test" and "auth" in command:
                    self.assertFalse(check)
                    return subprocess.CompletedProcess(command, 1, stdout="no\n")
                if command[0] == "kubectl-test":
                    return subprocess.CompletedProcess(command, 0, stdout="namespace/default\n")
                if command[:5] == ["nebius-test", "mk8s", "v1", "cluster", "get"]:
                    payload = {"spec": {"control_plane": {"endpoints": {"public_endpoint": {"allowed_cidrs": ["192.0.2.9/32"]}}}}}
                    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload))
                return subprocess.CompletedProcess(command, 0, stdout="")

            with mock.patch.object(HANDOFF, "run", side_effect=fake_run), self.assertRaisesRegex(HANDOFF.HandoffError, "differ"):
                HANDOFF.verify(args)


if __name__ == "__main__":
    unittest.main()
