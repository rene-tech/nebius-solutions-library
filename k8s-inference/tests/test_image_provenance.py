"""Image-provenance admission policy and signing tooling contract (SAI-09).

Pins the ValidatingAdmissionPolicy that refuses unpinned, foreign-registry, or
non-allow-listed platform images in the platform namespaces, and the tooling
that renders the allow-list and constructs cosign commands.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import unittest
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_DIR = REPOSITORY_ROOT / "security" / "image-provenance"
POLICY_PATH = PROVENANCE_DIR / "policy.yaml"
TOOL_PATH = PROVENANCE_DIR / "provenance.py"

MODULE_NAME = "image_provenance_under_test"
LOADER = importlib.machinery.SourceFileLoader(MODULE_NAME, str(TOOL_PATH))
SPEC = importlib.util.spec_from_loader(MODULE_NAME, LOADER)
assert SPEC is not None
TOOL = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = TOOL
LOADER.exec_module(TOOL)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REGISTRY_PREFIX = "cr.example-region.example.invalid/registryid/"
PLATFORM_PREFIX = REGISTRY_PREFIX + "fs2-platform/"


def load_policy_documents() -> list[dict]:
    return [
        document
        for document in yaml.safe_load_all(POLICY_PATH.read_text(encoding="utf-8"))
        if document
    ]


class PolicyManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        documents = load_policy_documents()
        self.assertEqual(len(documents), 4)
        self.policy, self.binding, self.helm_policy, self.helm_binding = documents
        self.assertEqual(self.policy["kind"], "ValidatingAdmissionPolicy")
        self.assertEqual(self.binding["kind"], "ValidatingAdmissionPolicyBinding")
        self.assertEqual(self.helm_policy["kind"], "ValidatingAdmissionPolicy")
        self.assertEqual(
            self.helm_binding["kind"], "ValidatingAdmissionPolicyBinding"
        )

    def test_binding_denies_and_targets_only_platform_namespaces(self) -> None:
        spec = self.binding["spec"]
        self.assertEqual(spec["policyName"], self.policy["metadata"]["name"])
        self.assertIn("Deny", spec["validationActions"])
        expressions = spec["matchResources"]["namespaceSelector"]["matchExpressions"]
        self.assertEqual(len(expressions), 1)
        self.assertEqual(expressions[0]["key"], "kubernetes.io/metadata.name")
        self.assertEqual(expressions[0]["operator"], "In")
        self.assertEqual(
            sorted(expressions[0]["values"]), ["fs2-models", "fs2-system"]
        )

    def test_binding_param_ref_fails_closed_when_allowlist_is_missing(self) -> None:
        param_ref = self.binding["spec"]["paramRef"]
        self.assertEqual(param_ref["name"], TOOL.ALLOWLIST_NAME)
        self.assertEqual(param_ref["namespace"], TOOL.ALLOWLIST_NAMESPACE)
        self.assertEqual(param_ref["parameterNotFoundAction"], "Deny")

    def test_policy_matches_pods_and_direct_workload_deploys(self) -> None:
        rules = self.policy["spec"]["matchConstraints"]["resourceRules"]
        matched = {
            (rule["apiGroups"][0], resource)
            for rule in rules
            for resource in rule["resources"]
        }
        # Workload-controller matching makes a direct `helm upgrade` or
        # `kubectl apply` fail at the Deployment, not only at later Pod churn.
        self.assertEqual(
            matched,
            {
                ("", "pods"),
                ("apps", "deployments"),
                ("apps", "daemonsets"),
                ("apps", "statefulsets"),
                ("batch", "jobs"),
                ("batch", "cronjobs"),
            },
        )
        for rule in rules:
            self.assertEqual(sorted(rule["operations"]), ["CREATE", "UPDATE"])

    def test_policy_fails_closed_and_validates_all_container_kinds(self) -> None:
        spec = self.policy["spec"]
        self.assertEqual(spec["failurePolicy"], "Fail")
        variables = {v["name"]: v["expression"] for v in spec["variables"]}
        for field in ("containers", "initContainers", "ephemeralContainers"):
            self.assertIn(field, variables["images"])
        # Workload kinds contribute their Pod templates, including CronJobs.
        self.assertIn("jobTemplate.spec.template.spec", variables["podSpec"])
        self.assertIn("object.kind == 'Pod' ? object.spec", variables["podSpec"])

    def test_policy_reads_exactly_the_rendered_allowlist_keys(self) -> None:
        rendered_keys = set(
            TOOL.render_allowlist(
                [REGISTRY_PREFIX], PLATFORM_PREFIX, [DIGEST_A], ["deployer"]
            )["data"]
        )
        variable_expressions = " ".join(
            variable["expression"]
            for document in (self.policy, self.helm_policy)
            for variable in document["spec"]["variables"]
        )
        referenced_keys = {
            key
            for key in (
                "registry-prefixes",
                "platform-repository-prefix",
                "platform-digests",
                "deploy-principals",
            )
            if f"params.data['{key}']" in variable_expressions
        }
        self.assertEqual(referenced_keys, rendered_keys)
        # Missing keys must fail closed through guarded lookups, not error out.
        for key in rendered_keys:
            self.assertIn(f"'{key}' in params.data", variable_expressions)

    def test_helm_release_writes_are_restricted_to_deploy_principals(self) -> None:
        rules = self.helm_policy["spec"]["matchConstraints"]["resourceRules"]
        self.assertEqual(rules[0]["resources"], ["secrets"])
        validation = self.helm_policy["spec"]["validations"][0]
        self.assertIn("helm.sh/release.v1", validation["expression"])
        self.assertIn("request.userInfo.username", validation["expression"])
        self.assertIn("SAI-09", validation["message"])
        binding_spec = self.helm_binding["spec"]
        self.assertEqual(binding_spec["policyName"], self.helm_policy["metadata"]["name"])
        self.assertIn("Deny", binding_spec["validationActions"])
        self.assertEqual(
            binding_spec["paramRef"]["parameterNotFoundAction"], "Deny"
        )
        namespaces = binding_spec["matchResources"]["namespaceSelector"][
            "matchExpressions"
        ][0]["values"]
        self.assertEqual(namespaces, ["fs2-system"])

    def test_policy_enforces_digest_registry_and_platform_allowlist(self) -> None:
        expressions = [
            validation["expression"]
            for validation in self.policy["spec"]["validations"]
        ]
        self.assertEqual(len(expressions), 3)
        self.assertIn("@sha256:[0-9a-f]{64}", expressions[0])
        self.assertIn("registryPrefixes.exists", expressions[1])
        self.assertIn("platformDigests.exists", expressions[2])
        self.assertIn("!i.startsWith(variables.platformRepositoryPrefix)", expressions[2])
        for validation in self.policy["spec"]["validations"]:
            self.assertIn("SAI-09", validation["message"])
            self.assertEqual(validation["reason"], "Forbidden")


class AllowlistRenderingTest(unittest.TestCase):
    def test_renders_sorted_unique_digests(self) -> None:
        manifest = TOOL.render_allowlist(
            [REGISTRY_PREFIX],
            PLATFORM_PREFIX,
            [DIGEST_B, DIGEST_A, DIGEST_B],
            ["deployer"],
        )
        self.assertEqual(manifest["metadata"]["name"], TOOL.ALLOWLIST_NAME)
        self.assertEqual(manifest["metadata"]["namespace"], TOOL.ALLOWLIST_NAMESPACE)
        self.assertEqual(
            manifest["data"]["platform-digests"], f"{DIGEST_A}\n{DIGEST_B}"
        )
        self.assertEqual(manifest["data"]["registry-prefixes"], REGISTRY_PREFIX)
        self.assertEqual(manifest["data"]["deploy-principals"], "deployer")

    def test_rejects_malformed_digests_prefixes_and_principals(self) -> None:
        with self.assertRaises(TOOL.ProvenanceError):
            TOOL.render_allowlist(
                [REGISTRY_PREFIX], PLATFORM_PREFIX, ["sha256:short"], ["deployer"]
            )
        with self.assertRaises(TOOL.ProvenanceError):
            TOOL.render_allowlist(
                [REGISTRY_PREFIX], PLATFORM_PREFIX, ["latest"], ["deployer"]
            )
        with self.assertRaises(TOOL.ProvenanceError):
            TOOL.render_allowlist(
                ["cr.example.invalid/no-trailing-slash"],
                PLATFORM_PREFIX,
                [DIGEST_A],
                ["deployer"],
            )
        with self.assertRaises(TOOL.ProvenanceError):
            TOOL.render_allowlist([], PLATFORM_PREFIX, [DIGEST_A], ["deployer"])
        with self.assertRaises(TOOL.ProvenanceError):
            TOOL.render_allowlist([REGISTRY_PREFIX], PLATFORM_PREFIX, [], ["deployer"])
        with self.assertRaises(TOOL.ProvenanceError):
            TOOL.render_allowlist([REGISTRY_PREFIX], PLATFORM_PREFIX, [DIGEST_A], [])
        with self.assertRaises(TOOL.ProvenanceError):
            TOOL.render_allowlist(
                [REGISTRY_PREFIX], PLATFORM_PREFIX, [DIGEST_A], ["bad\nprincipal"]
            )


class CosignCommandTest(unittest.TestCase):
    REFERENCE = PLATFORM_PREFIX + "fs2-serve-control-plane@" + DIGEST_A

    def test_sign_disables_public_transparency_log(self) -> None:
        command = TOOL.cosign_sign_command("/private/cosign.key", self.REFERENCE)
        self.assertEqual(command[:2], ["cosign", "sign"])
        self.assertIn("--tlog-upload=false", command)
        self.assertIn("--use-signing-config=false", command)
        self.assertIn("--new-bundle-format=false", command)
        self.assertIn("--yes", command)
        self.assertEqual(command[-1], self.REFERENCE)

    def test_verify_uses_private_infrastructure_mode(self) -> None:
        command = TOOL.cosign_verify_command("cosign.pub", self.REFERENCE)
        self.assertEqual(command[:2], ["cosign", "verify"])
        self.assertIn("--private-infrastructure=true", command)
        self.assertEqual(command[-1], self.REFERENCE)

    def test_tag_only_references_are_refused(self) -> None:
        for constructor in (TOOL.cosign_sign_command, TOOL.cosign_verify_command):
            with self.assertRaises(TOOL.ProvenanceError):
                constructor("key", PLATFORM_PREFIX + "fs2-serve-control-plane:latest")


def git(repo: Path, *arguments: str) -> str:
    import subprocess as sp

    return sp.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def build_anchor_fixture(base: Path) -> dict:
    """Real repo, tagged commit, and bundle so bindings are actually verified."""
    import hashlib
    import subprocess as sp

    repo = base / "repo"
    repo.mkdir()
    sp.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "commit", "--allow-empty", "-m", "anchored release")
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    git(repo, "tag", "-a", "-m", "anchor", "deploy/fixture", head)
    tag_target = git(repo, "rev-parse", "refs/tags/deploy/fixture")
    bundle = base / "release-anchors" / "deploy-fixture.bundle"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "bundle", "create", str(bundle), "refs/tags/deploy/fixture")
    sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
    (base / "release-anchors.json").write_text(
        json.dumps(
            {
                "refs/tags/deploy/fixture": {
                    "commit": head,
                    "tag_target": tag_target,
                    "bundle_path": str(bundle),
                    "sha256": sha,
                    "restore_tested": True,
                }
            }
        ),
        encoding="utf-8",
    )
    return {
        "repo": repo,
        "head": head,
        "tree": tree,
        "tag_target": tag_target,
        "bundle": bundle,
        "bundle_sha256": sha,
    }


def write_signed_receipt_fixture(
    run_root: Path, reference: str, fixture: dict, **overrides
) -> Path:
    digest = reference.rsplit("@", 1)[1]
    receipt = {
        "schema": TOOL.RECEIPT_SCHEMA,
        "image": reference,
        "digest": digest,
        "source": {"commit": fixture["head"], "tree": fixture["tree"]},
        "anchor": {
            "mode": "bundle",
            "tag": "refs/tags/deploy/fixture",
            "tag_target": fixture["tag_target"],
            "commit": fixture["head"],
            "restored_commit": fixture["head"],
            "bundle_path": str(fixture["bundle"]),
            "bundle_sha256": fixture["bundle_sha256"],
        },
        "sbom": {
            "attestation_manifest_digest": "sha256:" + "f" * 64,
            "subject_manifest_digest": "sha256:" + "a" * 64,
            "spdx_layer_digest": "sha256:" + "e" * 64,
            "statement_sha256": "e" * 64,
            "slsa_layer_digest": None,
            "spdx_sha256": None,
            "spdx_subject_digest": None,
        },
    }
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        if field:
            receipt[section][field] = value
        else:
            receipt[section] = value
    path = TOOL.receipt_path(run_root, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    (path.parent / (path.name + ".sig")).write_text("fixture-signature\n")
    return path


NOOP_VERIFIER = lambda command: None  # noqa: E731 - injected in place of cosign


def write_inventory_fixture(
    base: Path,
    platform_images: list[str],
    live: list[str] | None = None,
    helm: list[str] | None = None,
    frozen: list[str] | None = None,
    drained: list[dict] | None = None,
    schema: str | None = None,
    sign: bool = True,
) -> Path:
    inventory = {
        "schema": schema or TOOL.INVENTORY_SCHEMA,
        "sources": {
            "live_workloads": {"refs": live if live is not None else platform_images},
            "helm_rollback_window": {"refs": helm or []},
            "frozen_scientific_bindings": {"refs": frozen or []},
        },
        "drained_removals": drained or [],
        "platform_images": platform_images,
    }
    path = base / "inventory.json"
    path.write_text(json.dumps(inventory), encoding="utf-8")
    if sign:
        (base / "inventory.json.sig").write_text("fixture-signature\n")
    return path


class VerifiedAllowlistTest(unittest.TestCase):
    """Allow-listing renders only the signed complete inventory, receipted."""

    REFERENCE_A = PLATFORM_PREFIX + "control-plane@" + DIGEST_A
    REFERENCE_B = PLATFORM_PREFIX + "admin-console@" + DIGEST_B

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False)
        self._tmp.write("-----BEGIN PUBLIC KEY-----\ntest\n-----END PUBLIC KEY-----\n")
        self._tmp.close()
        self.addCleanup(lambda: Path(self._tmp.name).unlink(missing_ok=True))
        self._run_root_holder = tempfile.TemporaryDirectory()
        self.addCleanup(self._run_root_holder.cleanup)
        self.run_root = Path(self._run_root_holder.name)
        self.fixture = build_anchor_fixture(self.run_root)
        write_signed_receipt_fixture(self.run_root, self.REFERENCE_A, self.fixture)
        write_signed_receipt_fixture(self.run_root, self.REFERENCE_B, self.fixture)
        self.inventory = write_inventory_fixture(
            self.run_root, [self.REFERENCE_A, self.REFERENCE_B]
        )

    def render(self, references=(), verifier=NOOP_VERIFIER, inventory=None):
        return TOOL.verified_allowlist(
            self._tmp.name,
            list(references),
            [REGISTRY_PREFIX],
            PLATFORM_PREFIX,
            self.run_root,
            inventory or self.inventory,
            deploy_principals=["deployer"],
            verifier=verifier,
        )

    def test_inventory_references_are_verified_before_rendering(self) -> None:
        verified: list[str] = []
        manifest = self.render(
            verifier=lambda command: verified.append(command[-1])
        )
        # Inventory signature, then per reference: receipt sig + image sig.
        self.assertEqual(len(verified), 5)
        self.assertEqual(
            manifest["data"]["platform-digests"], f"{DIGEST_A}\n{DIGEST_B}"
        )
        annotations = manifest["metadata"]["annotations"]
        self.assertIn("security.fs2.nebius.ai/verified-with-key-sha256", annotations)
        self.assertIn("security.fs2.nebius.ai/inventory-sha256", annotations)

    def test_explicit_references_must_equal_the_inventory_exactly(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "equal the signed inventory"):
            self.render(references=[self.REFERENCE_A])
        extra = PLATFORM_PREFIX + "website@sha256:" + "9" * 64
        with self.assertRaisesRegex(TOOL.ProvenanceError, "equal the signed inventory"):
            self.render(references=[self.REFERENCE_A, self.REFERENCE_B, extra])
        manifest = self.render(references=[self.REFERENCE_B, self.REFERENCE_A])
        self.assertEqual(
            manifest["data"]["platform-digests"], f"{DIGEST_A}\n{DIGEST_B}"
        )

    def test_unsigned_or_missing_inventory_is_refused(self) -> None:
        (self.run_root / "inventory.json.sig").unlink()
        with self.assertRaisesRegex(TOOL.ProvenanceError, "signed release inventory"):
            self.render()

    def test_inventory_signature_failure_is_refused(self) -> None:
        import subprocess

        def failing_verifier(command):
            raise subprocess.CalledProcessError(1, command)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "inventory signature"):
            self.render(verifier=failing_verifier)

    def test_inventory_set_arithmetic_is_enforced(self) -> None:
        # platform_images must equal union(sources) minus audited drains.
        silent_omission = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A],
            live=[self.REFERENCE_A, self.REFERENCE_B],
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "sources minus"):
            self.render(inventory=silent_omission)
        unlisted_drain = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A],
            live=[self.REFERENCE_A],
            drained=[{"image": self.REFERENCE_B, "reason": "never deployed"}],
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "no source lists"):
            self.render(inventory=unlisted_drain)
        undocumented_drain = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A],
            live=[self.REFERENCE_A, self.REFERENCE_B],
            drained=[{"image": self.REFERENCE_B, "reason": "  "}],
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "audited reason"):
            self.render(inventory=undocumented_drain)
        audited = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A],
            live=[self.REFERENCE_A, self.REFERENCE_B],
            drained=[
                {"image": self.REFERENCE_B, "reason": "change:CHG-42 drained"}
            ],
        )
        manifest = self.render(inventory=audited)
        self.assertEqual(manifest["data"]["platform-digests"], DIGEST_A)

    def test_unreceipted_inventory_reference_aborts_rendering(self) -> None:
        orphan = PLATFORM_PREFIX + "website@sha256:" + "9" * 64
        inventory = write_inventory_fixture(
            self.run_root, [self.REFERENCE_A, self.REFERENCE_B, orphan]
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "no signed release receipt"):
            self.render(inventory=inventory)

    def test_receipt_verification_failure_aborts_rendering(self) -> None:
        import subprocess

        def failing_after_inventory(command):
            if command[-1] == str(self.inventory):
                return None
            raise subprocess.CalledProcessError(1, command)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "not trustworthy"):
            self.render(verifier=failing_after_inventory)


class ReceiptBindingTest(unittest.TestCase):
    """Signing prerequisites: digest <-> source <-> anchor <-> SBOM binding."""

    REFERENCE = PLATFORM_PREFIX + "control-plane@" + DIGEST_A
    PUBLIC_KEY = "unused.pub"

    def setUp(self) -> None:
        import tempfile

        self._holder = tempfile.TemporaryDirectory()
        self.addCleanup(self._holder.cleanup)
        self.run_root = Path(self._holder.name)
        self.fixture = build_anchor_fixture(self.run_root)

    def load(self):
        return TOOL.load_bound_receipt(
            self.run_root, self.REFERENCE, self.PUBLIC_KEY, verifier=NOOP_VERIFIER
        )

    def create(self, capture, sbom=None):
        return TOOL.create_release_receipt(
            self.REFERENCE,
            self.run_root,
            self.fixture["repo"],
            "deploy/fixture",
            "release.key",
            "release.pub",
            sbom,
            capture=capture,
        )

    def test_bound_receipt_loads(self) -> None:
        write_signed_receipt_fixture(self.run_root, self.REFERENCE, self.fixture)
        self.assertEqual(self.load()["digest"], DIGEST_A)

    def test_receipt_signature_is_verified_before_anything_else(self) -> None:
        write_signed_receipt_fixture(self.run_root, self.REFERENCE, self.fixture)
        commands: list[list[str]] = []
        TOOL.load_bound_receipt(
            self.run_root,
            self.REFERENCE,
            "release.pub",
            verifier=lambda command: commands.append(list(command)),
        )
        self.assertEqual(commands[0][:2], ["cosign", "verify-blob"])
        self.assertIn("--insecure-ignore-tlog=true", commands[0])

    def test_missing_receipt_signature_is_refused(self) -> None:
        path = write_signed_receipt_fixture(
            self.run_root, self.REFERENCE, self.fixture
        )
        (path.parent / (path.name + ".sig")).unlink()
        with self.assertRaisesRegex(TOOL.ProvenanceError, "no signed release receipt"):
            self.load()

    def test_receipt_for_a_different_digest_is_refused(self) -> None:
        write_signed_receipt_fixture(
            self.run_root, self.REFERENCE, self.fixture, image="other@" + DIGEST_B
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "does not bind"):
            self.load()

    def test_restore_evidence_must_match_the_anchor_commit(self) -> None:
        write_signed_receipt_fixture(
            self.run_root,
            self.REFERENCE,
            self.fixture,
            **{"anchor.restored_commit": "9" * 40},
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "restore evidence"):
            self.load()

    def test_receipt_without_subject_bound_sbom_is_refused(self) -> None:
        write_signed_receipt_fixture(
            self.run_root,
            self.REFERENCE,
            self.fixture,
            sbom={
                "attestation_manifest_digest": None,
                "subject_manifest_digest": None,
                "spdx_layer_digest": None,
                "slsa_layer_digest": None,
                "spdx_sha256": "b" * 64,
                "spdx_subject_digest": "sha256:" + "9" * 64,
            },
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "subject-bound SBOM"):
            self.load()

    def test_receipt_with_attestation_but_no_spdx_layer_is_refused(self) -> None:
        write_signed_receipt_fixture(
            self.run_root,
            self.REFERENCE,
            self.fixture,
            **{"sbom.spdx_layer_digest": None},
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "subject-bound SBOM"):
            self.load()

    def test_missing_bundle_artifact_is_refused(self) -> None:
        write_signed_receipt_fixture(self.run_root, self.REFERENCE, self.fixture)
        self.fixture["bundle"].unlink()
        with self.assertRaisesRegex(TOOL.ProvenanceError, "missing"):
            self.load()

    def test_tampered_bundle_artifact_is_refused(self) -> None:
        write_signed_receipt_fixture(self.run_root, self.REFERENCE, self.fixture)
        self.fixture["bundle"].write_bytes(b"tampered")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "no longer matches"):
            self.load()

    def test_bundle_must_still_carry_the_anchor_tag_at_its_target(self) -> None:
        write_signed_receipt_fixture(
            self.run_root,
            self.REFERENCE,
            self.fixture,
            **{"anchor.tag_target": "8" * 40},
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "does not carry"):
            self.load()

    def fixture_statement(
        self, statement_subject_hex: str, subject_name: str = "pkg:docker/fixture-image"
    ) -> dict:
        statement = {
            "_type": "https://in-toto.io/Statement/v0.1",
            "predicateType": "https://spdx.dev/Document",
            "subject": [
                {"name": subject_name, "digest": {"sha256": statement_subject_hex}}
            ],
            "predicate": {
                "spdxVersion": "SPDX-2.3",
                "SPDXID": "SPDXRef-DOCUMENT",
                "name": "sbom",
                "documentNamespace": "https://example.invalid/spdxdocs/fixture",
                "packages": [
                    {"SPDXID": "SPDXRef-Package-fixture", "name": "fixture-root"}
                ],
                "relationships": [
                    {
                        "spdxElementId": "SPDXRef-DOCUMENT",
                        "relationshipType": "DESCRIBES",
                        "relatedSpdxElement": "SPDXRef-Package-fixture",
                    }
                ],
            },
        }
        return statement

    def crane_capture(
        self,
        revision: str,
        spdx_predicate: str = "https://spdx.dev/Document",
        statement_subject_hex: str | None = None,
        tree_label: str | object = "fixture-tree",
        statement_mutator=None,
        statement_text_override: str | None = None,
        statement_layer_digest: str | None = None,
        amd64_platforms: int = 1,
        attestation_subject: str | None = None,
        config_architecture: str = "amd64",
    ):
        """Build a deterministic multi-platform image fixture, bottom-up."""
        import hashlib

        def sha(text: str) -> str:
            return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

        if tree_label == "fixture-tree":
            tree_label = self.fixture["tree"]
        labels = {"org.opencontainers.image.revision": revision}
        if tree_label is not None:
            labels["ai.nebius.fs2-serve.source-tree"] = tree_label
        config_text = json.dumps(
            {
                "architecture": config_architecture,
                "os": "linux",
                "config": {"Labels": labels},
            }
        )
        config_digest = sha(config_text)
        amd64_manifest_text = json.dumps(
            {"schemaVersion": 2, "config": {"digest": config_digest}, "layers": []}
        )
        amd64_digest = sha(amd64_manifest_text)
        self.expected_amd64_digest = amd64_digest
        self.expected_config_digest = config_digest

        if statement_text_override is not None:
            statement_text = statement_text_override
        else:
            statement = self.fixture_statement(
                statement_subject_hex or amd64_digest.split(":", 1)[1]
            )
            if statement_mutator is not None:
                statement_mutator(statement)
            statement_text = json.dumps(statement)
        if statement_layer_digest is None:
            statement_layer_digest = sha(statement_text)
        self.expected_spdx_layer_digest = statement_layer_digest
        attestation_manifest_text = json.dumps(
            {
                "layers": [
                    {
                        "mediaType": "application/vnd.in-toto+json",
                        "digest": statement_layer_digest,
                        "annotations": {"in-toto.io/predicate-type": spdx_predicate},
                    },
                    {
                        "mediaType": "application/vnd.in-toto+json",
                        "digest": "sha256:" + "d" * 64,
                        "annotations": {
                            "in-toto.io/predicate-type": "https://slsa.dev/provenance/v0.2"
                        },
                    },
                ]
            }
        )
        attestation_digest = sha(attestation_manifest_text)
        arm64_digest = "sha256:" + "b" * 64
        entries = []
        for _ in range(amd64_platforms):
            entries.append(
                {
                    "digest": amd64_digest,
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            )
        entries.append(
            {
                "digest": arm64_digest,
                "platform": {"architecture": "arm64", "os": "linux"},
            }
        )
        entries.append(
            {
                "digest": attestation_digest,
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": attestation_subject or amd64_digest,
                },
            }
        )
        entries.append(
            {
                "digest": "sha256:" + "c" * 64,
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": arm64_digest,
                },
            }
        )
        index_text = json.dumps({"schemaVersion": 2, "manifests": entries})

        def capture(command):
            if command[:2] == ["crane", "manifest"]:
                target = command[2]
                if target == self.REFERENCE:
                    return index_text
                if target.endswith("@" + amd64_digest):
                    return amd64_manifest_text
                if target.endswith("@" + attestation_digest):
                    return attestation_manifest_text
                raise AssertionError(f"unexpected manifest fetch: {target}")
            if command[:2] == ["crane", "blob"]:
                target = command[2]
                if target.endswith("@" + config_digest):
                    return config_text
                if target.endswith("@" + statement_layer_digest):
                    return statement_text
                raise AssertionError(f"unexpected blob fetch: {target}")
            if command[:2] == ["cosign", "sign-blob"]:
                self.assertIn("--tlog-upload=false", command)
                self.assertIn("--use-signing-config=false", command)
                Path(command[command.index("--output-file") + 1]).write_text("sig\n")
                return ""
            if command[:2] == ["cosign", "verify-blob"]:
                self.assertIn("--insecure-ignore-tlog=true", command)
                return ""
            raise AssertionError(f"unexpected command: {command}")

        return capture

    def test_create_receipt_binds_source_anchor_sbom_and_signs_it(self) -> None:
        receipt = self.create(self.crane_capture(self.fixture["head"]))
        self.assertEqual(receipt["source"]["commit"], self.fixture["head"])
        self.assertEqual(receipt["source"]["tree"], self.fixture["tree"])
        self.assertEqual(receipt["anchor"]["tag_target"], self.fixture["tag_target"])
        self.assertEqual(
            receipt["image_manifest"]["amd64_manifest_digest"],
            self.expected_amd64_digest,
        )
        self.assertEqual(
            receipt["image_manifest"]["config_digest"], self.expected_config_digest
        )
        self.assertEqual(
            receipt["sbom"]["subject_manifest_digest"], self.expected_amd64_digest
        )
        self.assertEqual(
            receipt["sbom"]["spdx_layer_digest"], self.expected_spdx_layer_digest
        )
        self.assertEqual(
            "sha256:" + receipt["sbom"]["statement_sha256"],
            self.expected_spdx_layer_digest,
        )
        self.assertEqual(receipt["sbom"]["slsa_layer_digest"], "sha256:" + "d" * 64)
        loaded = self.load()
        self.assertEqual(loaded["anchor"]["tag"], "refs/tags/deploy/fixture")

    def test_receipt_recreation_is_idempotent_and_preserves_bytes(self) -> None:
        capture = self.crane_capture(self.fixture["head"])
        first = self.create(capture)
        path = TOOL.receipt_path(self.run_root, DIGEST_A)
        signature = path.parent / (path.name + ".sig")
        receipt_bytes = path.read_bytes()
        signature_bytes = signature.read_bytes()
        second = self.create(capture)
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(path.read_bytes(), receipt_bytes)
        self.assertEqual(signature.read_bytes(), signature_bytes)

    def test_idempotent_path_reverifies_the_existing_signature(self) -> None:
        import subprocess as sp

        self.create(self.crane_capture(self.fixture["head"]))
        base_capture = self.crane_capture(self.fixture["head"])

        def tamper_aware_capture(command):
            if command[:2] == ["cosign", "verify-blob"]:
                raise sp.CalledProcessError(1, command)
            return base_capture(command)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "fails verification"):
            self.create(tamper_aware_capture)

    def test_conflicting_receipt_recreation_is_refused_and_preserves_original(
        self,
    ) -> None:
        self.create(self.crane_capture(self.fixture["head"]))
        path = TOOL.receipt_path(self.run_root, DIGEST_A)
        original_bytes = path.read_bytes()

        def rename_subject(statement):
            statement["subject"][0]["name"] = "pkg:docker/replayed-image"

        with self.assertRaisesRegex(TOOL.ProvenanceError, "immutable"):
            self.create(
                self.crane_capture(
                    self.fixture["head"], statement_mutator=rename_subject
                )
            )
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_partial_receipt_evidence_is_never_overwritten(self) -> None:
        capture = self.crane_capture(self.fixture["head"])
        self.create(capture)
        path = TOOL.receipt_path(self.run_root, DIGEST_A)
        (path.parent / (path.name + ".sig")).unlink()
        with self.assertRaisesRegex(TOOL.ProvenanceError, "partial receipt"):
            self.create(capture)

    def test_failed_signing_leaves_no_partial_published_evidence(self) -> None:
        import subprocess as sp

        base_capture = self.crane_capture(self.fixture["head"])

        def crashing_capture(command):
            if command[:2] == ["cosign", "sign-blob"]:
                raise sp.CalledProcessError(1, command)
            return base_capture(command)

        with self.assertRaises(sp.CalledProcessError):
            self.create(crashing_capture)
        final_dir = TOOL.receipt_path(self.run_root, DIGEST_A).parent
        self.assertFalse(final_dir.exists())
        # Publication is recoverable: the same creation succeeds afterwards.
        receipt = self.create(self.crane_capture(self.fixture["head"]))
        self.assertEqual(receipt["digest"], DIGEST_A)
        self.assertTrue(final_dir.is_dir())

    def test_symlinked_receipt_path_is_refused(self) -> None:
        final_dir = TOOL.receipt_path(self.run_root, DIGEST_A).parent
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        target = self.run_root / "elsewhere"
        target.mkdir()
        final_dir.symlink_to(target)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "symlink"):
            self.create(self.crane_capture(self.fixture["head"]))

    def test_recreated_tag_object_at_same_commit_is_refused(self) -> None:
        # Same peeled commit, different annotated tag object: never receipted.
        git(self.fixture["repo"], "tag", "-d", "deploy/fixture")
        git(
            self.fixture["repo"],
            "tag",
            "-a",
            "-m",
            "recreated",
            "deploy/fixture",
            self.fixture["head"],
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "tag object changed"):
            self.create(self.crane_capture(self.fixture["head"]))

    def test_index_without_exactly_one_amd64_manifest_is_refused(self) -> None:
        for count in (0, 2):
            with self.assertRaisesRegex(
                TOOL.ProvenanceError, "exactly one linux/amd64"
            ):
                self.create(
                    self.crane_capture(self.fixture["head"], amd64_platforms=count)
                )

    def test_attestation_must_subject_the_amd64_manifest(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "none whose subject"):
            self.create(
                self.crane_capture(
                    self.fixture["head"],
                    attestation_subject="sha256:" + "b" * 64,
                )
            )

    def test_config_platform_must_be_linux_amd64(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "expected linux/amd64"):
            self.create(
                self.crane_capture(
                    self.fixture["head"], config_architecture="arm64"
                )
            )

    def test_create_receipt_refuses_unanchored_source(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "not reachable"):
            self.create(self.crane_capture("a" * 40))

    def test_create_receipt_requires_exact_source_tree_label(self) -> None:
        for bad_label in (None, "46a60fb"):
            with self.assertRaisesRegex(TOOL.ProvenanceError, "source-tree"):
                self.create(
                    self.crane_capture(self.fixture["head"], tree_label=bad_label)
                )

    def test_create_receipt_refuses_wrong_source_tree_label(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "does not match"):
            self.create(
                self.crane_capture(self.fixture["head"], tree_label="9" * 40)
            )

    def test_create_receipt_refuses_attestation_without_spdx_predicate(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "spdx.dev"):
            self.create(
                self.crane_capture(
                    self.fixture["head"],
                    spdx_predicate="https://example.invalid/other",
                )
            )

    def test_create_receipt_refuses_statement_subject_mismatch(self) -> None:
        with self.assertRaisesRegex(
            TOOL.ProvenanceError, "does not name the image manifest"
        ):
            self.create(
                self.crane_capture(
                    self.fixture["head"], statement_subject_hex="9" * 64
                )
            )

    def _refused_statement_text(self, statement_text: str, pattern: str) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, pattern):
            self.create(
                self.crane_capture(
                    self.fixture["head"], statement_text_override=statement_text
                )
            )

    def _refused_statement_mutation(self, mutator, pattern: str) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, pattern):
            self.create(
                self.crane_capture(self.fixture["head"], statement_mutator=mutator)
            )

    def test_create_receipt_refuses_non_statement_blob(self) -> None:
        # Annotation-only trust is not enough: the fetched blob must be a real
        # in-toto Statement, not any JSON that merely mentions a subject.
        self._refused_statement_text(
            json.dumps({"subject": [{"digest": {"sha256": "a" * 64}}]}), "in-toto"
        )

    def test_create_receipt_refuses_malformed_statement_blob(self) -> None:
        self._refused_statement_text("this is not json", "not valid JSON")

    def test_create_receipt_refuses_wrong_statement_predicate_type(self) -> None:
        def mutate(statement):
            statement["predicateType"] = "https://slsa.dev/provenance/v0.2"

        self._refused_statement_mutation(mutate, "predicateType")

    def test_create_receipt_refuses_empty_predicate(self) -> None:
        def mutate(statement):
            statement["predicate"] = {}

        self._refused_statement_mutation(mutate, "empty or non-object predicate")

    def test_create_receipt_refuses_unnamed_statement_subject(self) -> None:
        def mutate(statement):
            statement["subject"][0].pop("name")

        self._refused_statement_mutation(mutate, "no name")

    def test_create_receipt_refuses_non_spdx_shaped_predicate(self) -> None:
        for mutation, pattern in (
            ({"spdxVersion": "CycloneDX-1.5"}, "spdxVersion"),
            ({"SPDXID": "SPDXRef-Other"}, "SPDXRef-DOCUMENT"),
            ({"packages": []}, "no packages"),
            ({"relationships": []}, "DESCRIBES"),
            (
                {
                    "packages": [
                        {"SPDXID": "SPDXRef-Package-fixture", "name": "a"},
                        {"SPDXID": "SPDXRef-Package-fixture", "name": "b"},
                    ]
                },
                "duplicate package SPDXID",
            ),
            (
                {"packages": [{"SPDXID": "not a valid id", "name": "a"}]},
                "invalid SPDXID",
            ),
            (
                {
                    "relationships": [
                        {
                            "spdxElementId": "SPDXRef-DOCUMENT",
                            "relationshipType": "DESCRIBES",
                            "relatedSpdxElement": "SPDXRef-Nonexistent",
                        }
                    ]
                },
                "not a package in",
            ),
        ):
            def mutate(statement, mutation=mutation):
                statement["predicate"].update(mutation)

            self._refused_statement_mutation(mutate, pattern)

    def test_create_receipt_refuses_statement_hash_mismatch(self) -> None:
        # The blob must be the exact content the layer digest names.
        with self.assertRaisesRegex(TOOL.ProvenanceError, "layer digest"):
            self.create(
                self.crane_capture(
                    self.fixture["head"],
                    statement_layer_digest="sha256:" + "e" * 64,
                )
            )

    def spdx_document(self, digest_hex: str | None) -> dict:
        package = {
            "SPDXID": "SPDXRef-Package-image",
            "name": "fixture-image",
        }
        if digest_hex is not None:
            package["checksums"] = [
                {"algorithm": "SHA256", "checksumValue": digest_hex}
            ]
        return {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "fixture-sbom",
            "documentNamespace": "https://example.invalid/spdxdocs/fixture",
            "packages": [package],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": "SPDXRef-Package-image",
                }
            ],
        }

    def test_spdx_document_binding_requires_exact_package_identity(self) -> None:
        digest_hex = DIGEST_A.split(":", 1)[1]
        good = self.run_root / "sbom.spdx.json"
        good.write_text(json.dumps(self.spdx_document(digest_hex)), encoding="utf-8")
        evidence = TOOL._validated_spdx_document(DIGEST_A, good)
        self.assertEqual(evidence["spdx_subject_digest"], DIGEST_A)

        purl = self.spdx_document(None)
        purl["packages"][0]["externalRefs"] = [
            {
                "referenceType": "purl",
                "referenceLocator": f"pkg:oci/fixture-image@sha256:{digest_hex}?arch=amd64",
            }
        ]
        purl_path = self.run_root / "purl.spdx.json"
        purl_path.write_text(json.dumps(purl), encoding="utf-8")
        self.assertEqual(
            TOOL._validated_spdx_document(DIGEST_A, purl_path)["spdx_subject_digest"],
            DIGEST_A,
        )

        not_spdx = self.run_root / "not-sbom.json"
        not_spdx.write_text(json.dumps({"name": digest_hex}), encoding="utf-8")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "not an SPDX"):
            TOOL._validated_spdx_document(DIGEST_A, not_spdx)

        # Substring mentions anywhere — name, namespace, comments — never bind.
        for substring_doc in (
            {"name": f"fixture-image@sha256:{digest_hex}"},
            {"documentNamespace": f"https://example.invalid/{digest_hex}"},
            {"comment": f"mentions sha256:{digest_hex} only informally"},
        ):
            document = self.spdx_document(None)
            document.update(substring_doc)
            path = self.run_root / "substring.spdx.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(TOOL.ProvenanceError, "exact SHA256"):
                TOOL._validated_spdx_document(DIGEST_A, path)

        # The identity must sit on a DESCRIBED package, wrong algorithm or a
        # non-described package does not bind.
        wrong_algorithm = self.spdx_document(None)
        wrong_algorithm["packages"][0]["checksums"] = [
            {"algorithm": "SHA1", "checksumValue": digest_hex}
        ]
        path = self.run_root / "wrong-algorithm.spdx.json"
        path.write_text(json.dumps(wrong_algorithm), encoding="utf-8")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "exact SHA256"):
            TOOL._validated_spdx_document(DIGEST_A, path)

        undescribed = self.spdx_document(None)
        undescribed["packages"].append(
            {
                "SPDXID": "SPDXRef-Package-other",
                "name": "other",
                "checksums": [{"algorithm": "SHA256", "checksumValue": digest_hex}],
            }
        )
        path = self.run_root / "undescribed.spdx.json"
        path.write_text(json.dumps(undescribed), encoding="utf-8")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "exact SHA256"):
            TOOL._validated_spdx_document(DIGEST_A, path)

    def test_spdx_document_fallback_requires_document_shape(self) -> None:
        digest_hex = DIGEST_A.split(":", 1)[1]
        for mutation, pattern in (
            ({"packages": []}, "no packages"),
            ({"relationships": []}, "DESCRIBES"),
            ({"SPDXID": "SPDXRef-Other"}, "SPDXRef-DOCUMENT"),
            ({"documentNamespace": ""}, "documentNamespace"),
        ):
            document = self.spdx_document(digest_hex)
            document.update(mutation)
            path = self.run_root / "shape.spdx.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(TOOL.ProvenanceError, pattern):
                TOOL._validated_spdx_document(DIGEST_A, path)


class ReceiptSignatureRoundTripTest(unittest.TestCase):
    """Real cosign blob signature round trip when cosign is available."""

    def test_signed_receipt_verifies_and_tamper_fails(self) -> None:
        import os
        import shutil
        import subprocess as sp
        import tempfile

        if shutil.which("cosign") is None:
            self.skipTest("cosign is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            env = dict(os.environ, COSIGN_PASSWORD="")
            sp.run(
                ["cosign", "generate-key-pair"],
                cwd=base,
                env=env,
                check=True,
                capture_output=True,
            )
            payload = base / "receipt.json"
            payload.write_text('{"fixture": true}\n', encoding="utf-8")
            signature = base / "receipt.json.sig"
            sign_command = [
                "cosign",
                "sign-blob",
                "--key",
                str(base / "cosign.key"),
                "--use-signing-config=false",
                "--tlog-upload=false",
                "--yes",
                "--output-file",
                str(signature),
                str(payload),
            ]
            self.assertIn("--tlog-upload=false", sign_command)
            sp.run(sign_command, env=env, check=True, capture_output=True)
            verify = TOOL.receipt_verify_blob_command(
                str(base / "cosign.pub"), payload, signature
            )
            sp.run(verify, check=True, capture_output=True)
            payload.write_text('{"fixture": false}\n', encoding="utf-8")
            with self.assertRaises(sp.CalledProcessError):
                sp.run(verify, check=True, capture_output=True)


class PublicKeyPresenceTest(unittest.TestCase):
    def test_release_public_key_is_committed(self) -> None:
        key_path = PROVENANCE_DIR / "cosign.pub"
        self.assertTrue(key_path.is_file(), "cosign.pub must be committed")
        content = key_path.read_text(encoding="utf-8")
        self.assertIn("BEGIN PUBLIC KEY", content)
        self.assertNotIn("PRIVATE", content)


if __name__ == "__main__":
    unittest.main()
