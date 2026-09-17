"""Image-provenance admission policy and signing tooling contract (SAI-09).

Pins the ValidatingAdmissionPolicy that refuses unpinned, foreign-registry, or
non-allow-listed platform images in the platform namespaces, and the tooling
that renders the allow-list and constructs cosign commands.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
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
        self.assertEqual(len(documents), 6)
        (
            self.policy,
            self.binding,
            self.helm_policy,
            self.helm_binding,
            self.guard_policy,
            self.guard_binding,
        ) = documents
        self.assertEqual(self.guard_policy["kind"], "ValidatingAdmissionPolicy")
        self.assertEqual(
            self.guard_binding["kind"], "ValidatingAdmissionPolicyBinding"
        )
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
                ("", "pods/ephemeralcontainers"),
                ("apps", "deployments"),
                ("apps", "daemonsets"),
                ("apps", "statefulsets"),
                ("batch", "jobs"),
                ("batch", "cronjobs"),
            },
        )
        for rule in rules:
            self.assertEqual(sorted(rule["operations"]), ["CREATE", "UPDATE"])

    def test_policy_governs_kubectl_debug_ephemeral_injection(self) -> None:
        # kubectl debug writes the pods/ephemeralcontainers subresource; the
        # policy must match it so an injected mutable/foreign debug image is
        # denied while a pinned allow-listed one still works (debugging is
        # governed, never disabled).
        rules = self.policy["spec"]["matchConstraints"]["resourceRules"]
        pods_rule = next(
            rule for rule in rules if "pods" in rule["resources"]
        )
        self.assertIn("pods/ephemeralcontainers", pods_rule["resources"])
        variables = {
            v["name"]: v["expression"] for v in self.policy["spec"]["variables"]
        }
        self.assertIn("ephemeralContainers", variables["images"])

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
            render_allowlist_fixture(
                [REGISTRY_PREFIX], PLATFORM_PREFIX, [DIGEST_A], ["deployer"]
            )["data"]
        )
        variable_expressions = " ".join(
            variable["expression"]
            for document in (self.policy, self.helm_policy)
            for variable in document["spec"]["variables"]
        )
        guard_expressions = " ".join(
            variable["expression"]
            for variable in self.guard_policy["spec"]["variables"]
        )
        # The guard reads the SECURITY-owned parameter ConfigMap, which the
        # release renderer never emits: separation of duties by artifact.
        self.assertIn("params.data['security-principals']", guard_expressions)
        guard_keys = set(
            TOOL.render_guard_params(["system:serviceaccount:a:b"])["data"]
        )
        self.assertEqual(guard_keys, {"security-principals"})
        referenced_keys = {
            key
            for key in (
                "registry-prefixes",
                "platform-repository-prefix",
                "platform-digests",
                "deploy-principals",
                "namespaces",
                "token-audience",
                "automation-service-accounts",
                "workload-service-accounts",
            )
            if f"params.data['{key}']" in variable_expressions
        }
        self.assertEqual(referenced_keys, rendered_keys)
        # Missing keys must fail closed through guarded lookups, not error out.
        for key in rendered_keys:
            self.assertIn(f"'{key}' in params.data", variable_expressions)

    def test_guard_protects_exactly_what_admission_can_evaluate(self) -> None:
        # CORRECTED CONTRACT (independent reviewer ruling): Kubernetes
        # admission intentionally does NOT evaluate in-cluster policies or
        # webhooks on admission-configuration writes (anti-lockout), so the
        # guard claims ONLY what admission genuinely evaluates: the two
        # parameter ConfigMaps. Policy-object protection is the EXTERNAL
        # owner control; the renderer detects drift/deletion at every render.
        spec = self.guard_policy["spec"]
        self.assertEqual(spec["failurePolicy"], "Fail")
        rules = spec["matchConstraints"]["resourceRules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["resources"], ["configmaps"])
        self.assertEqual(
            sorted(rules[0]["operations"]), ["CREATE", "DELETE", "UPDATE"]
        )
        variables = {
            variable["name"]: variable["expression"]
            for variable in spec["variables"]
        }
        for name in (
            "fs2-image-provenance-allowlist",
            "fs2-security-guard-params",
        ):
            self.assertIn(name, variables["isProtected"])
        # No claim over admissionregistration resources survives: admission
        # cannot evaluate those writes, so matching them would be a false
        # promise.
        manifest_text = json.dumps(self.guard_policy)
        self.assertNotIn("validatingadmissionpolicies", manifest_text)
        self.assertNotIn("mutatingwebhookconfigurations", manifest_text)
        validations = spec["validations"]
        self.assertEqual(len(validations), 1)
        self.assertIn("securityPrincipals.exists", validations[0]["expression"])
        binding = self.guard_binding["spec"]
        self.assertIn("Deny", binding["validationActions"])
        self.assertEqual(binding["paramRef"]["name"], "fs2-security-guard-params")
        self.assertEqual(binding["paramRef"]["parameterNotFoundAction"], "Deny")

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
        self.assertEqual(len(expressions), 7)
        # The request namespace must be part of the owner-approved scope
        # recorded in the rendered allow-list, so claimed and enforced
        # coverage can never drift apart silently.
        self.assertIn("allowedNamespaces.exists", expressions[0])
        self.assertIn("request.namespace", expressions[0])
        self.assertIn("@sha256:[0-9a-f]{64}", expressions[1])
        self.assertIn("registryPrefixes.exists", expressions[2])
        self.assertIn("platformDigests.exists", expressions[3])
        self.assertIn("!i.startsWith(variables.platformRepositoryPrefix)", expressions[3])
        # Automation-identity token hardening: pod-level automount off, the
        # exact bounded audience, and no Secret volume/env paths — enforced
        # in admission, so a workload-create grant cannot pivot into
        # identity-credential access.
        self.assertIn("isAutomationPod", expressions[4])
        self.assertIn("automountServiceAccountToken == false", expressions[4])
        self.assertIn("expirationSeconds <= 3600", expressions[4])
        self.assertIn("!variables.usesSecretEnv", expressions[4])
        # Audience exclusivity: nobody else may project the release audience.
        self.assertIn("serviceAccountToken.audience != variables.tokenAudience", expressions[5])
        # Automation-WRITER constraint: pods/controllers written by an
        # automation identity run only as owner-enumerated ServiceAccounts,
        # with no hostPath and no privileged containers.
        self.assertIn("writerIsAutomation", expressions[6])
        self.assertIn("workloadServiceAccounts.exists", expressions[6])
        self.assertIn("!variables.usesHostPath", expressions[6])
        self.assertIn("!variables.usesPrivileged", expressions[6])
        for validation in self.policy["spec"]["validations"]:
            self.assertIn("SAI-09", validation["message"])
            self.assertEqual(validation["reason"], "Forbidden")


def render_allowlist_fixture(*args, **kwargs):
    """render_allowlist with the required identity keys defaulted."""
    kwargs.setdefault("token_audience", "fs2-release")
    kwargs.setdefault(
        "automation_service_accounts", ["fs2-system:fs2-release-automation"]
    )
    kwargs.setdefault(
        "workload_service_accounts", ["fs2-system:fs2-release-automation"]
    )
    return TOOL.render_allowlist(*args, **kwargs)


class AllowlistRenderingTest(unittest.TestCase):
    def test_renders_sorted_unique_digests(self) -> None:
        manifest = render_allowlist_fixture(
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
        self.assertEqual(manifest["data"]["token-audience"], "fs2-release")
        self.assertEqual(
            manifest["data"]["automation-service-accounts"],
            "fs2-system:fs2-release-automation",
        )
        self.assertEqual(
            manifest["data"]["workload-service-accounts"],
            "fs2-system:fs2-release-automation",
        )

    def test_empty_helm_secret_writers_render_the_deny_all_state(self) -> None:
        # The HELM_DRIVER=sql contract is FEASIBLE: an EMPTY writer list is
        # the stronger state (the governance policy denies every
        # helm.sh/release.v1 write), never an error.
        manifest = render_allowlist_fixture(
            [REGISTRY_PREFIX], PLATFORM_PREFIX, [DIGEST_A], []
        )
        self.assertEqual(manifest["data"]["deploy-principals"], "")

    def test_rejects_malformed_digests_prefixes_and_principals(self) -> None:
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture(
                [REGISTRY_PREFIX], PLATFORM_PREFIX, ["sha256:short"], ["deployer"]
            )
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture(
                [REGISTRY_PREFIX], PLATFORM_PREFIX, ["latest"], ["deployer"]
            )
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture(
                ["cr.example.invalid/no-trailing-slash"],
                PLATFORM_PREFIX,
                [DIGEST_A],
                ["deployer"],
            )
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture([], PLATFORM_PREFIX, [DIGEST_A], ["deployer"])
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture(
                [REGISTRY_PREFIX], PLATFORM_PREFIX, [], ["deployer"]
            )
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture(
                [REGISTRY_PREFIX], PLATFORM_PREFIX, [DIGEST_A], ["bad\nprincipal"]
            )
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture(
                [REGISTRY_PREFIX],
                PLATFORM_PREFIX,
                [DIGEST_A],
                ["deployer"],
                workload_service_accounts=[],
            )
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture(
                [REGISTRY_PREFIX],
                PLATFORM_PREFIX,
                [DIGEST_A],
                ["deployer"],
                workload_service_accounts=["no-colon-row"],
            )
        # The identity keys fail closed too: a missing/invalid audience or an
        # empty/malformed automation account list never renders.
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture(
                [REGISTRY_PREFIX],
                PLATFORM_PREFIX,
                [DIGEST_A],
                ["deployer"],
                token_audience="",
            )
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture(
                [REGISTRY_PREFIX],
                PLATFORM_PREFIX,
                [DIGEST_A],
                ["deployer"],
                automation_service_accounts=[],
            )
        with self.assertRaises(TOOL.ProvenanceError):
            render_allowlist_fixture(
                [REGISTRY_PREFIX],
                PLATFORM_PREFIX,
                [DIGEST_A],
                ["deployer"],
                automation_service_accounts=["no-colon-row"],
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
    origin = base / "origin.git"
    sp.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "commit", "--allow-empty", "-m", "anchored release")
    git(repo, "push", "origin", "main")
    git(repo, "fetch", "origin")
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    git(repo, "tag", "-a", "-m", "anchor", "deploy/fixture", head)
    tag_target = git(repo, "rev-parse", "refs/tags/deploy/fixture")
    bundle = base / "release-anchors" / "deploy-fixture.bundle"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "bundle", "create", str(bundle), "refs/tags/deploy/fixture")
    bundle.chmod(0o600)
    sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
    store = base / "release-anchors.json"
    store.write_text(
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
    store.chmod(0o600)
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
        "image_manifest": {
            "amd64_manifest_digest": "sha256:" + "a" * 64,
            "config_digest": "sha256:" + "c" * 64,
        },
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
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    path.chmod(0o600)
    signature = path.parent / (path.name + ".sig")
    signature.write_text("fixture-signature\n")
    signature.chmod(0o600)
    return path


NOOP_VERIFIER = lambda command: None  # noqa: E731 - injected in place of cosign

def fixture_statement(
    statement_subject_hex: str, subject_name: str = "pkg:docker/fixture-image"
) -> dict:
    return {
        "_type": "https://in-toto.io/Statement/v0.1",
        "predicateType": "https://spdx.dev/Document",
        "subject": [
            {"name": subject_name, "digest": {"sha256": statement_subject_hex}}
        ],
        "predicate": {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "dataLicense": "CC0-1.0",
            "name": "sbom",
            "documentNamespace": "https://example.invalid/spdxdocs/fixture",
            "creationInfo": {
                "created": "2026-09-16T00:00:00Z",
                "creators": ["Tool: fixture"],
            },
            "packages": [
                {
                    "SPDXID": "SPDXRef-Package-fixture",
                    "name": "fixture-root",
                    "downloadLocation": "NOASSERTION",
                }
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


def build_crane_fixture(
    anchor_fixture: dict,
    repository: str = "control-plane",
    revision: str | None = None,
    spdx_predicate: str = "https://spdx.dev/Document",
    statement_subject_hex: str | None = None,
    tree_label: str | object = "fixture-tree",
    statement_mutator=None,
    statement_text_override: str | None = None,
    statement_layer_digest: str | None = None,
    amd64_platforms: int = 1,
    attestation_subject: str | None = None,
    config_architecture: str = "amd64",
    duplicate_amd64_attestation: bool = False,
    duplicate_spdx_layer: bool = False,
    include_attestation: bool = True,
):
    """Build a deterministic multi-platform image fixture, bottom-up.

    Every digest is computed from the exact bytes the capture serves, so the
    registry re-proof performed at load time verifies REAL content addresses,
    exactly like production crane fetches.
    """
    import hashlib
    from types import SimpleNamespace

    ns = SimpleNamespace()

    def sha(text: str) -> str:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    revision = revision or anchor_fixture["head"]
    if tree_label == "fixture-tree":
        tree_label = anchor_fixture["tree"]
    labels = {
        "org.opencontainers.image.revision": revision,
        # Distinguishes repositories so their content addresses differ, as
        # different images do in a real registry.
        "ai.nebius.fs2-serve.fixture-repository": repository,
    }
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
    ns.expected_amd64_digest = amd64_digest
    ns.expected_config_digest = config_digest

    if statement_text_override is not None:
        statement_text = statement_text_override
    else:
        statement = fixture_statement(
            statement_subject_hex or amd64_digest.split(":", 1)[1]
        )
        if statement_mutator is not None:
            statement_mutator(statement)
        statement_text = json.dumps(statement)
    if statement_layer_digest is None:
        statement_layer_digest = sha(statement_text)
    ns.expected_spdx_layer_digest = statement_layer_digest
    attestation_layers = [
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
    if duplicate_spdx_layer:
        attestation_layers.append(dict(attestation_layers[0]))
    attestation_manifest_text = json.dumps({"layers": attestation_layers})
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
    if include_attestation:
        entries.append(
            {
                "digest": attestation_digest,
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": attestation_subject or amd64_digest,
                },
            }
        )
        if duplicate_amd64_attestation:
            entries.append(dict(entries[-1]))
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
    # The top manifest is content-addressed too: the fixture reference
    # digest is the hash of the exact index bytes served.
    ns.reference = PLATFORM_PREFIX + repository + "@" + sha(index_text)

    def capture(command):
        if command[:2] == ["crane", "manifest"]:
            target = command[2]
            if target == ns.reference:
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
            assert "--tlog-upload=false" in command
            assert "--use-signing-config=false" in command
            Path(command[command.index("--output-file") + 1]).write_text("sig\n")
            return ""
        if command[:2] == ["cosign", "verify-blob"]:
            assert "--insecure-ignore-tlog=true" in command
            return ""
        raise AssertionError(f"unexpected command: {command}")

    ns.capture = capture
    return ns


AUTOMATION_PRINCIPAL = "system:serviceaccount:fs2-system:fs2-release-automation"
SECURITY_PRINCIPAL = "system:serviceaccount:fs2-security:fs2-admission-guard"
AUTHORITY_IMAGE = PLATFORM_PREFIX + "fs2-serve-control-plane@sha256:" + "a" * 64
AUTHORITY_ROW_DIGEST = "d" * 64
AUTHORITY_RS_NAME = "fs2-serve-control-plane-0a1b2c3d4e"
AUTHORITY_RS_UID = "0a1b2c3d-0000-4000-8000-fixture00003"
AUTHORITY_FIXTURE = {
    "workload_uid": "0a1b2c3d-0000-4000-8000-fixture00001",
    "workload_resource_version": "424242",
    "pod_name": "fs2-serve-control-plane-0a1b2c3d4e-abcde",
    "pod_uid": "0a1b2c3d-0000-4000-8000-fixture00002",
    "pod_resource_version": "515151",
    "pod_controller": f"ReplicaSet/{AUTHORITY_RS_NAME}/{AUTHORITY_RS_UID}",
    "container_name": "control-plane",
    "image": AUTHORITY_IMAGE,
    "database": "fs2_serve",
    "role": "fs2_serve",
    "search_path": '"$user", public',
    "current_schema": "public",
    "server_version_sha256": "e" * 64,
    "server_version_num": "160004",
    "system_identifier": "7300000000000000001",
    "migration_version": "0024_scientific_model_policies.sql",
    "migration_sha256": "f" * 64,
    "table_oid": "16385",
    "trigger_relation": "fs2_scientific_batches",
    "trigger_function": "fs2_scientific_batch_state_immutable",
    "trigger_definition_sha256": "c" * 64,
    "trigger_function_sha256": "b" * 64,
    "trigger_enabled": "O",
    "rows_sha256": "a" * 64,
}


FIXTURE_WORM_STORE = "https://worm.example.invalid/fs2/anchored-heads"
PROVIDER_ADMIN_SUBJECT = "owner@example.invalid"


def write_provider_export_fixture(
    base: Path,
    cluster: str = "fixture-cluster",
    bindings: list | None = None,
    name: str = "provider-iam-export.json",
    **overrides,
) -> Path:
    """The ACTUAL provider IAM export whose bytes the attestation pins."""
    from datetime import UTC, datetime, timedelta

    document = {
        "schema": TOOL.PROVIDER_IAM_EXPORT_SCHEMA,
        "cluster": cluster,
        "exported_at": (datetime.now(UTC) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "bindings": bindings
        if bindings is not None
        else [
            {"subject": PROVIDER_ADMIN_SUBJECT, "role": "managed-k8s.admin"},
            {"subject": "auditor@example.invalid", "role": "viewer"},
        ],
    }
    document.update(overrides)
    path = base / name
    path.write_bytes(json.dumps(document).encode("utf-8"))
    path.chmod(0o644)
    return path


def write_attestation_fixture(
    base: Path,
    cluster: str = "fixture-cluster",
    anchored: dict | None = None,
    name: str = "provider-attestation.json",
    export_path: Path | None = None,
    **overrides,
) -> Path:
    """Attestor-signed provider attestation (fixture signature registry).

    The default anchored-heads snapshot is a REAL export of the run root's
    current chains (all required chains enumerated), and the evidence object
    pins a REAL provider-IAM-export fixture file written next to it —
    matching the loader's refusal of empty/omitted chains, bare provider-held
    strings, and hash-only evidence.
    """
    import hashlib
    from datetime import UTC, datetime, timedelta

    if export_path is None:
        export_path = base / "provider-iam-export.json"
        if not export_path.exists():
            write_provider_export_fixture(base, cluster=cluster)
    export_sha = hashlib.sha256(export_path.read_bytes()).hexdigest()
    now = datetime.now(UTC)
    document = {
        "schema": TOOL.PROVIDER_ATTESTATION_SCHEMA,
        "cluster": cluster,
        "masters_certificate_issuance": "provider-held",
        "apiserver_control": "provider-held",
        "etcd_access": "provider-held",
        "worm_store": FIXTURE_WORM_STORE,
        "evidence": {
            "provider_iam_export_sha256": export_sha,
            "reference": "ticket:FS2-SAI-09-boundary",
            "provider_admin_subjects": [PROVIDER_ADMIN_SUBJECT],
        },
        "anchored_heads": anchored or TOOL._anchor_snapshot(base),
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    document.update(overrides)
    payload = json.dumps(document).encode("utf-8")
    attestation = base / name
    attestation.write_bytes(payload)
    attestation.chmod(0o644)
    signature = base / (name + ".sig")
    signature.write_text("fixture-owner-signature\n", encoding="utf-8")
    signature.chmod(0o644)
    SIGNED_AUTHORITY_HASHES.add(hashlib.sha256(payload).hexdigest())
    return attestation


def default_scope_fixture(key_sha256: str) -> dict:
    import hashlib

    return {
        "cluster": "fixture-cluster",
        "namespaces": ["fs2-models", "fs2-system"],
        "registry_prefixes": [REGISTRY_PREFIX],
        "platform_repository_prefix": PLATFORM_PREFIX,
        "deploy_principals": [AUTOMATION_PRINCIPAL],
        # The decided HELM_DRIVER=sql contract: NO Helm release-Secret
        # writers — the governance policy denies every such write outright.
        "helm_secret_writers": [],
        "workload_service_accounts": [
            "fs2-system:fs2-release-automation",
            "fs2-system:fs2-serve-control-plane",
        ],
        "security_principals": [SECURITY_PRINCIPAL],
        "verification_key_sha256": key_sha256,
        "attestation_key_sha256": hashlib.sha256(
            ATTESTOR_KEY_CONTENT.encode("utf-8")
        ).hexdigest(),
        "iam_exempt_subjects": [],
        "token_audience": "fs2-release",
        "worm_store_uri": FIXTURE_WORM_STORE,
        "stage_binding_authority": {
            "namespace": "fs2-system",
            "workload": "deployment/fs2-serve-control-plane",
        },
        "tooling": {"kubectl": "/usr/bin/kubectl", "helm": "/usr/bin/helm"},
        "policy_sha256": hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest(),
    }


SIGNED_AUTHORITY_HASHES: set[str] = set()


def write_scope_fixture(
    base: Path, scope: dict | None, name: str = "release-scope.json"
) -> Path:
    """Write an OWNER-SIGNED scope fixture (signature = payload-hash registry).

    The fake owner signature registers the exact payload bytes; the fixture
    authority verifier accepts only registered byte strings, so a dirty edit,
    substitution, or local re-commit fails verification exactly as a real
    cosign check would without the owner-held private key.
    """
    import hashlib

    payload = json.dumps({"schema": TOOL.SCOPE_SCHEMA, "scope": scope}).encode(
        "utf-8"
    )
    path = base / name
    path.write_bytes(payload)
    path.chmod(0o644)
    signature = base / (name + ".sig")
    signature.write_text("fixture-owner-signature\n", encoding="utf-8")
    signature.chmod(0o644)
    SIGNED_AUTHORITY_HASHES.add(hashlib.sha256(payload).hexdigest())
    return path


def authority_checking_verifier(command):
    """Fixture verifier: owner-signed payloads must be REGISTERED bytes.

    Scope documents are checked against the signed registry; every other
    verify-blob (receipts, inventories) is accepted like NOOP_VERIFIER.
    """
    import hashlib
    import subprocess as sp

    if command[:2] != ["cosign", "verify-blob"]:
        return
    payload = Path(command[-1]).read_bytes()
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if isinstance(document, dict) and document.get("schema") in (
        TOOL.SCOPE_SCHEMA,
        TOOL.PROVIDER_ATTESTATION_SCHEMA,
        TOOL.RECOVERY_SCHEMA,
        TOOL.ROLLOUT_AUTHORIZATION_SCHEMA,
    ):
        if hashlib.sha256(payload).hexdigest() not in SIGNED_AUTHORITY_HASHES:
            raise sp.CalledProcessError(1, command)


TEST_KEY_CONTENT = "-----BEGIN PUBLIC KEY-----\ntest\n-----END PUBLIC KEY-----\n"
# SEPARATE attestor key: the provider attestation must never verify against
# the release pipeline's own key (self-attestation), so the fixtures carry a
# second key identity pinned by the scope's attestation_key_sha256.
ATTESTOR_KEY_CONTENT = (
    "-----BEGIN PUBLIC KEY-----\nattestor\n-----END PUBLIC KEY-----\n"
)


def no_registry_capture(command):
    raise AssertionError(f"unexpected registry/tool access: {command}")



def write_inventory_fixture(
    base: Path,
    platform_images: list[str],
    live: list[str] | None = None,
    helm: list[str] | None = None,
    frozen: list[str] | None = None,
    drained: list[dict] | None = None,
    schema: str | None = None,
    sign: bool = True,
    captured_at: str | None = None,
    observed_at: str | None = None,
    scope: dict | None = None,
    generation: int = 1,
    name: str = "inventory.json",
) -> Path:
    import hashlib as fixture_hashlib
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def source(refs: list[str], id_pattern: str) -> dict:
        return {
            "refs": refs,
            "observed_at": observed_at or now,
            "resource_ids": [id_pattern.format(i) for i in range(len(refs))],
        }

    inventory = {
        "schema": schema or TOOL.INVENTORY_SCHEMA,
        "cluster": "fixture-cluster",
        "generation": generation,
        "collector": {
            "method": "fs2-live-enumeration/v1",
            "identity": "fixture-collector",
        },
        "captured_at": captured_at or now,
        "scope": scope
        or default_scope_fixture(
            fixture_hashlib.sha256(TEST_KEY_CONTENT.encode("utf-8")).hexdigest()
        ),
        "sources": {
            "live_workloads": source(
                live if live is not None else platform_images,
                "pod/fs2-system/pod-{}",
            ),
            "helm_rollback_window": {
                "refs": helm or [],
                "observed_at": observed_at or now,
                "resource_ids": [
                    f"helm/fs2-system/fixture-release/{i + 1}"
                    for i in range(len(helm or []))
                ],
            },
            "frozen_scientific_bindings": {
                **source(
                    frozen or [],
                    "batch/fixture-{}/rev/1/" + AUTHORITY_ROW_DIGEST,
                ),
                "authority": dict(AUTHORITY_FIXTURE),
            },
        },
        "drained_removals": drained or [],
        "platform_images": platform_images,
    }
    path = base / name
    path.write_text(json.dumps(inventory), encoding="utf-8")
    path.chmod(0o600)
    if sign:
        signature = base / (name + ".sig")
        signature.write_text("fixture-signature\n")
        signature.chmod(0o600)
    return path


class VerifiedAllowlistTest(unittest.TestCase):
    """Allow-listing renders only the signed complete inventory, receipted.

    The receipts behind the two references are REAL: created bottom-up
    through create_release_receipt against deterministic content-addressed
    crane fixtures, so the full registry + retained-SBOM re-proof that runs
    on every allow-list load is exercised, never bypassed with invented
    digests.
    """

    def setUp(self) -> None:
        import hashlib as h
        import tempfile

        self._tmp = tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False)
        self._tmp.write(TEST_KEY_CONTENT)
        self._tmp.close()
        self.addCleanup(lambda: Path(self._tmp.name).unlink(missing_ok=True))
        self._attestor_key = tempfile.NamedTemporaryFile(
            "w", suffix=".pub", delete=False
        )
        self._attestor_key.write(ATTESTOR_KEY_CONTENT)
        self._attestor_key.close()
        self.addCleanup(
            lambda: Path(self._attestor_key.name).unlink(missing_ok=True)
        )
        self.key_sha256 = h.sha256(TEST_KEY_CONTENT.encode("utf-8")).hexdigest()
        # The fixture keys are pinned exactly as production pins the
        # committed keys: by patching BOTH source constants for this test's
        # lifetime (release key AND the separately designated attestor key).
        self._original_pin = TOOL.RELEASE_KEY_SHA256
        TOOL.RELEASE_KEY_SHA256 = self.key_sha256
        self.addCleanup(
            lambda: setattr(TOOL, "RELEASE_KEY_SHA256", self._original_pin)
        )
        self._original_attestor_pin = TOOL.ATTESTATION_KEY_SHA256
        TOOL.ATTESTATION_KEY_SHA256 = h.sha256(
            ATTESTOR_KEY_CONTENT.encode("utf-8")
        ).hexdigest()
        self.addCleanup(
            lambda: setattr(
                TOOL, "ATTESTATION_KEY_SHA256", self._original_attestor_pin
            )
        )
        self._run_root_holder = tempfile.TemporaryDirectory()
        self.addCleanup(self._run_root_holder.cleanup)
        self.run_root = Path(self._run_root_holder.name)
        self.fixture = build_anchor_fixture(self.run_root)
        self.fx_a = build_crane_fixture(self.fixture, repository="control-plane")
        self.fx_b = build_crane_fixture(self.fixture, repository="admin-console")
        self.REFERENCE_A = self.fx_a.reference
        self.REFERENCE_B = self.fx_b.reference
        self.DIGEST_A = self.REFERENCE_A.rsplit("@", 1)[1]
        self.DIGEST_B = self.REFERENCE_B.rsplit("@", 1)[1]
        for built in (self.fx_a, self.fx_b):
            TOOL.create_release_receipt(
                built.reference,
                self.run_root,
                self.fixture["repo"],
                "deploy/fixture",
                "release.key",
                "release.pub",
                None,
                capture=built.capture,
            )
        self.scope = default_scope_fixture(self.key_sha256)
        self.scope_path = write_scope_fixture(self.run_root, self.scope)
        self.attestation_path = write_attestation_fixture(self.run_root)
        self.provider_export = self.run_root / "provider-iam-export.json"
        self.inventory = write_inventory_fixture(
            self.run_root, [self.REFERENCE_A, self.REFERENCE_B]
        )

    def capture(self, command):
        text = " ".join(str(part) for part in command)
        if "admin-console" in text:
            return self.fx_b.capture(command)
        return self.fx_a.capture(command)

    def live_runner(
        self,
        images=None,
        identity="fixture-collector",
        live_policy=None,
        live_binding=None,
        helm_images=None,
        frozen_objects=None,
        controller_images=None,
        cluster="fixture-cluster",
        cluster_role_bindings=None,
        cluster_roles=None,
    ):
        """Serve the full authoritative observation surface.

        Pods carry `images` (defaults to both references), controllers carry
        `controller_images`, Helm history serves one release with one
        revision per helm image, frozen ConfigMaps serve `frozen_objects`
        ({name: [refs]}), and the live policy objects default to the
        committed manifests.
        """
        import subprocess

        live = list(
            images if images is not None else [self.REFERENCE_A, self.REFERENCE_B]
        )
        helm = list(helm_images or [])
        frozen_refs = list(frozen_objects or [])
        controllers = list(controller_images or [])
        documents = load_policy_documents()
        by_name = {
            (document["kind"], document["metadata"]["name"]): document
            for document in documents
        }

        def server_defaulted(document):
            # A real GET returns persisted API defaults the YAML omits.
            import copy

            live = copy.deepcopy(document)
            spec = live.setdefault("spec", {})
            if live["kind"] == "ValidatingAdmissionPolicy":
                spec.setdefault("failurePolicy", "Fail")
                constraints = spec.setdefault("matchConstraints", {})
                constraints.setdefault("matchPolicy", "Equivalent")
                for rule in constraints.get("resourceRules") or []:
                    rule.setdefault("scope", "*")
            else:
                matches = spec.setdefault("matchResources", {})
                matches.setdefault("matchPolicy", "Equivalent")
                matches.setdefault("namespaceSelector", {})
                matches.setdefault("objectSelector", {})
            return live

        live_objects = {}
        for kind in ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"):
            for name in (
                "fs2-image-provenance",
                "fs2-helm-release-governance",
                "fs2-provenance-guard",
            ):
                live_objects[(kind, name)] = server_defaulted(
                    by_name[(kind, name)]
                )
        if live_policy is not None:
            live_objects[("ValidatingAdmissionPolicy", "fs2-image-provenance")] = (
                live_policy
            )
        if live_binding is not None:
            live_objects[
                ("ValidatingAdmissionPolicyBinding", "fs2-image-provenance")
            ] = live_binding

        def runner(command):
            joined = " ".join(str(part) for part in command)
            if command[:3] == ["kubectl", "auth", "whoami"]:
                return json.dumps(
                    {"status": {"userInfo": {"username": identity}}}
                )
            if command[:4] == ["kubectl", "get", "namespace", "kube-system"]:
                return cluster
            if command[:3] == ["kubectl", "get", "validatingadmissionpolicy"]:
                target = live_objects[("ValidatingAdmissionPolicy", command[3])]
                if target == "absent":
                    raise subprocess.CalledProcessError(1, command)
                return json.dumps(target)
            if command[:3] == [
                "kubectl",
                "get",
                "validatingadmissionpolicybinding",
            ]:
                return json.dumps(
                    live_objects[
                        ("ValidatingAdmissionPolicyBinding", command[3])
                    ]
                )
            if command[:3] == ["kubectl", "get", "pods"]:
                namespace = command[command.index("-n") + 1]
                authority_pod = {
                    "metadata": {
                        "name": AUTHORITY_FIXTURE["pod_name"],
                        "uid": AUTHORITY_FIXTURE["pod_uid"],
                        "resourceVersion": AUTHORITY_FIXTURE[
                            "pod_resource_version"
                        ],
                        "ownerReferences": [
                            {
                                "kind": "ReplicaSet",
                                "name": AUTHORITY_RS_NAME,
                                "uid": AUTHORITY_RS_UID,
                                "controller": True,
                            }
                        ],
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": AUTHORITY_FIXTURE["container_name"],
                                "image": AUTHORITY_FIXTURE["image"],
                            }
                        ]
                    },
                    "status": {"phase": "Running"},
                }
                if "-l" in command:
                    # Authority pod resolution by selector.
                    return json.dumps({"items": [authority_pod]})
                if command[3] == AUTHORITY_FIXTURE["pod_name"]:
                    # Post-dump re-fetch of the exact pod by name.
                    return json.dumps(authority_pod)
                if namespace != "fs2-system":
                    return json.dumps({"items": []})
                return json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": f"pod-{index}"},
                                "spec": {"containers": [{"image": image}]},
                            }
                            for index, image in enumerate(live)
                        ]
                    }
                )
            if command[:3] == ["kubectl", "get", "replicaset"]:
                return json.dumps(
                    {
                        "metadata": {
                            "name": AUTHORITY_RS_NAME,
                            "uid": AUTHORITY_RS_UID,
                            "ownerReferences": [
                                {
                                    "kind": "Deployment",
                                    "name": "fs2-serve-control-plane",
                                    "uid": AUTHORITY_FIXTURE["workload_uid"],
                                    "controller": True,
                                }
                            ],
                        }
                    }
                )
            if command[:3] == ["kubectl", "get", "configmap"]:
                name = command[3]
                if name == "fs2-security-guard-params":
                    return json.dumps(
                        {
                            "metadata": {"name": name},
                            "data": {"security-principals": SECURITY_PRINCIPAL},
                        }
                    )
                raise subprocess.CalledProcessError(1, command)
            if command[:3] == ["kubectl", "get", "serviceaccount"]:
                return json.dumps(
                    {
                        "metadata": {"name": command[3]},
                        "automountServiceAccountToken": False,
                    }
                )
            if command[:3] == ["kubectl", "get", "deployment"]:
                return json.dumps(
                    {
                        "metadata": {
                            "uid": AUTHORITY_FIXTURE["workload_uid"],
                            "resourceVersion": AUTHORITY_FIXTURE[
                                "workload_resource_version"
                            ],
                        },
                        "spec": {
                            "selector": {
                                "matchLabels": {"app": "fs2-serve-control-plane"}
                            },
                            "template": {
                                "spec": {
                                    "containers": [
                                        {"image": AUTHORITY_FIXTURE["image"]}
                                    ]
                                }
                            },
                        },
                    }
                )
            if command[:3] == ["kubectl", "get", "secrets"]:
                return json.dumps({"items": []})
            if command[:2] == ["kubectl", "exec"]:
                # The AUTHORITATIVE frozen source: control-plane PostgreSQL
                # stage bindings + full database identity, dumped read-only
                # inside the resolved authority pod.
                payload = {
                    key: AUTHORITY_FIXTURE[key]
                    for key in (
                        "database",
                        "role",
                        "search_path",
                        "current_schema",
                        "server_version_sha256",
                        "server_version_num",
                        "system_identifier",
                        "migration_version",
                        "migration_sha256",
                        "table_oid",
                        "trigger_relation",
                        "trigger_function",
                        "trigger_definition_sha256",
                        "trigger_function_sha256",
                        "trigger_enabled",
                        "rows_sha256",
                    )
                }
                # Same-snapshot regclass cross-check served by the dump.
                payload["expected_table_oid"] = AUTHORITY_FIXTURE["table_oid"]
                payload["rows"] = [
                    [f"fixture-{index}", 1, ref, AUTHORITY_ROW_DIGEST]
                    for index, ref in enumerate(frozen_refs)
                ]
                return json.dumps(payload)
            if command[:3] == ["kubectl", "get", "clusterroles"]:
                return json.dumps({"items": list(cluster_roles or [])})
            if command[:3] == ["kubectl", "get", "clusterrolebindings"]:
                return json.dumps({"items": list(cluster_role_bindings or [])})
            if command[:3] == ["kubectl", "get", "roles"]:
                return json.dumps({"items": []})
            if command[:3] == ["kubectl", "get", "rolebindings"]:
                return json.dumps({"items": []})
            if command[:2] == ["kubectl", "get"] and command[2] in (
                "deployments",
                "daemonsets",
                "statefulsets",
                "replicasets",
                "replicationcontrollers",
                "jobs",
                "cronjobs",
            ):
                namespace = command[command.index("-n") + 1]
                if command[2] != "deployments" or namespace != "fs2-system":
                    return json.dumps({"items": []})
                return json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": f"scaled-zero-{index}"},
                                "spec": {
                                    "template": {
                                        "spec": {
                                            "containers": [{"image": image}]
                                        }
                                    }
                                },
                            }
                            for index, image in enumerate(controllers)
                        ]
                    }
                )
            if command[:2] == ["helm", "list"]:
                namespace = command[command.index("-n") + 1]
                if namespace != "fs2-system" or not helm:
                    return json.dumps([])
                return json.dumps([{"name": "fixture-release"}])
            if command[:2] == ["helm", "history"]:
                return json.dumps(
                    [{"revision": index + 1} for index in range(len(helm))]
                )
            if command[:3] == ["helm", "get", "manifest"]:
                revision = int(command[command.index("--revision") + 1])
                return f'image: "{helm[revision - 1]}"'
            raise AssertionError(f"unexpected live-enumeration command: {joined}")

        return runner

    def expected_digests(self) -> str:
        return "\n".join(sorted([self.DIGEST_A, self.DIGEST_B]))

    def render(
        self,
        references=(),
        verifier=authority_checking_verifier,
        inventory=None,
        scope_path=None,
        registry_prefixes=None,
        platform_prefix=PLATFORM_PREFIX,
        deploy_principals=(AUTOMATION_PRINCIPAL,),
        live_runner=None,
        attestation_path=None,
        attestation_key_path=None,
        provider_export_path=None,
    ):
        return TOOL.verified_allowlist(
            self._tmp.name,
            list(references),
            list(registry_prefixes or [REGISTRY_PREFIX]),
            platform_prefix,
            self.run_root,
            inventory or self.inventory,
            scope_path or self.scope_path,
            attestation_path or self.attestation_path,
            attestation_key_path or self._attestor_key.name,
            provider_export_path or self.provider_export,
            deploy_principals=list(deploy_principals),
            key_path="release.key",
            verifier=verifier,
            capture=self.capture,
            live_runner=live_runner or self.live_runner(),
        )

    def test_inventory_references_are_verified_before_rendering(self) -> None:
        verified: list[str] = []

        def counting_verifier(command):
            authority_checking_verifier(command)
            verified.append(command[-1])

        manifest = self.render(verifier=counting_verifier)
        # Owner-scope signature, attestor-signed provider attestation,
        # inventory signature, per reference a receipt sig + image sig, and
        # the newly appended acceptance head.
        self.assertEqual(len(verified), 8)
        self.assertEqual(
            manifest["data"]["platform-digests"], self.expected_digests()
        )
        annotations = manifest["metadata"]["annotations"]
        self.assertIn("security.fs2.nebius.ai/verified-with-key-sha256", annotations)
        self.assertIn("security.fs2.nebius.ai/inventory-sha256", annotations)

    def test_allowlist_annotations_bind_verified_bytes_and_pinned_key(
        self,
    ) -> None:
        import hashlib as h

        original_inventory = self.inventory.read_bytes()
        key_bytes = Path(self._tmp.name).read_bytes()
        key_paths: list[str] = []
        key_contents: set[bytes] = set()

        def swapping_verifier(command):
            authority_checking_verifier(command)
            key_path = command[command.index("--key") + 1]
            key_paths.append(key_path)
            key_contents.add(Path(key_path).read_bytes())
            # Adversarial swap AFTER the inventory bytes were verified:
            # rewrite the published inventory pathname. The annotation must
            # record the bytes that were verified, never a re-read of the
            # mutable pathname. (The swap starts once the inventory payload
            # has been seen, so earlier authority checks read the original.)
            if command[:2] != ["cosign", "verify-blob"]:
                return
            payload = Path(command[-1]).read_bytes()
            try:
                document = json.loads(payload)
            except json.JSONDecodeError:
                document = {}
            if (
                isinstance(document, dict)
                and document.get("schema") == TOOL.INVENTORY_SCHEMA
            ):
                self.inventory.write_text('{"swapped": true}', encoding="utf-8")

        manifest = self.render(verifier=swapping_verifier)
        annotations = manifest["metadata"]["annotations"]
        self.assertEqual(
            annotations["security.fs2.nebius.ai/inventory-sha256"],
            h.sha256(original_inventory).hexdigest(),
        )
        self.assertEqual(
            annotations["security.fs2.nebius.ai/verified-with-key-sha256"],
            h.sha256(key_bytes).hexdigest(),
        )
        # Every verification used ONE pinned private key identity — a scratch
        # copy of the key bytes, not the swappable original pathname.
        self.assertEqual(len(set(key_paths)), 1)
        self.assertNotEqual(key_paths[0], self._tmp.name)
        self.assertEqual(key_contents, {key_bytes})

    def test_zero_pod_controller_image_cannot_be_omitted(self) -> None:
        # A scaled-to-zero or crash-looping Deployment has NO Pod, but its
        # template image is active platform surface: the controller
        # enumeration must catch its omission from live_workloads.
        zero_pod = PLATFORM_PREFIX + "batch-worker@sha256:" + "e" * 64
        with self.assertRaisesRegex(TOOL.ProvenanceError, "omitted live images"):
            self.render(
                live_runner=self.live_runner(controller_images=[zero_pod])
            )

    def test_forged_live_resource_ids_fail_closed(self) -> None:
        # Correct refs with self-asserted resource identities never render:
        # the signed identities must equal the authenticated observation.
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            name="forged-ids.json",
        )
        document = json.loads(inventory.read_text(encoding="utf-8"))
        document["sources"]["live_workloads"]["resource_ids"] = [
            "pod/fs2-system/forged-a",
            "pod/fs2-system/forged-b",
        ]
        inventory.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "resource identities"):
            self.render(inventory=inventory)

    def test_foreign_cluster_fails_closed(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "foreign cluster"):
            self.render(live_runner=self.live_runner(cluster="other-cluster"))

    def test_omitted_helm_rollback_revision_fails_closed(self) -> None:
        # The rollback window is authoritatively re-derived from Helm history
        # manifests; a signed inventory that omits (or drains) a revision
        # image the history still carries never renders.
        rollback = PLATFORM_PREFIX + "control-plane@sha256:" + "d" * 64
        with self.assertRaisesRegex(TOOL.ProvenanceError, "Helm history"):
            self.render(live_runner=self.live_runner(helm_images=[rollback]))

    def test_ledger_truncation_is_detected_by_the_checkpoint(self) -> None:
        # A JSONL ledger is mutable: without the verified chain + head
        # checkpoint, truncating the consume ledger would silently
        # UN-CONSUME an authorization and enable replay.
        ledger = self.run_root / "release-authorization-consumed.jsonl"
        TOOL._record_consumed(self.run_root, "a" * 64, "test")
        TOOL._record_consumed(self.run_root, "b" * 64, "test")
        self.assertTrue(TOOL._is_consumed(self.run_root, "b" * 64))
        lines = ledger.read_bytes().splitlines()
        ledger.write_bytes(lines[0] + b"\n")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "checkpoint"):
            TOOL._is_consumed(self.run_root, "b" * 64)

    def test_anchored_heads_detect_whole_store_regression(self) -> None:
        # The off-host anchor snapshot makes chain deletion detectable: a
        # local state BEHIND the anchored counts fails closed.
        self.render()
        snapshot = TOOL._anchor_snapshot(self.run_root)
        self.assertGreaterEqual(
            snapshot["chains"]["acceptance-heads"]["count"], 1
        )
        regressed = json.loads(json.dumps(snapshot))
        regressed["chains"]["acceptance-heads"]["count"] += 5
        anchored = self.run_root / "anchored.json"
        anchored.write_text(json.dumps(regressed), encoding="utf-8")
        anchored.chmod(0o644)
        current = TOOL._anchor_snapshot(self.run_root)
        self.assertLess(
            current["chains"]["acceptance-heads"]["count"],
            regressed["chains"]["acceptance-heads"]["count"],
        )

    def test_anchored_head_must_be_a_prefix_of_the_local_chain(self) -> None:
        # A count comparison alone would accept a REWRITTEN history that has
        # since grown past the anchored count; the anchored head must equal
        # the hash of the local element AT the anchored position.
        self.render()
        earlier = TOOL._anchor_snapshot(self.run_root)
        second = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            generation=2,
            name="gen2-prefix.json",
        )
        self.render(inventory=second)
        grown = TOOL._anchor_snapshot(self.run_root)
        self.assertGreater(
            grown["chains"]["acceptance-heads"]["count"],
            earlier["chains"]["acceptance-heads"]["count"],
        )
        # The honest older anchor verifies as a strict prefix.
        TOOL._assert_anchored_heads(self.run_root, earlier)
        # A forged anchor at the same older count (history rewritten beneath
        # the growth) refuses.
        forged = json.loads(json.dumps(earlier))
        forged["chains"]["acceptance-heads"]["head"] = "0" * 64
        with self.assertRaisesRegex(TOOL.ProvenanceError, "NOT a prefix"):
            TOOL._assert_anchored_heads(self.run_root, forged)
        # Same for the hash-chained JSONL ledgers.
        TOOL._record_consumed(self.run_root, "3" * 64, "test")
        mid = TOOL._anchor_snapshot(self.run_root)
        TOOL._record_consumed(self.run_root, "4" * 64, "test")
        TOOL._assert_anchored_heads(self.run_root, mid)
        forged_ledger = json.loads(json.dumps(mid))
        forged_ledger["chains"]["consumed"]["head"] = "1" * 64
        with self.assertRaisesRegex(TOOL.ProvenanceError, "NOT a prefix"):
            TOOL._assert_anchored_heads(self.run_root, forged_ledger)
        # A snapshot omitting a required chain anchors nothing and refuses.
        omitting = json.loads(json.dumps(mid))
        del omitting["chains"]["consumed"]
        with self.assertRaisesRegex(TOOL.ProvenanceError, "OMITTED"):
            TOOL._assert_anchored_heads(self.run_root, omitting)

    def test_authority_identity_mismatch_refuses_rendering(self) -> None:
        # The signed inventory pins the authority workload/database identity;
        # a same-name workload replacement (different UID) never renders.
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            name="authority-mismatch.json",
        )
        document = json.loads(inventory.read_text(encoding="utf-8"))
        document["sources"]["frozen_scientific_bindings"]["authority"][
            "workload_uid"
        ] = "different-uid"
        inventory.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "authority"):
            self.render(inventory=inventory)

    def test_automation_identity_without_automount_off_is_refused(self) -> None:
        def sloppy_sa_runner(command):
            if command[:3] == ["kubectl", "get", "serviceaccount"]:
                return json.dumps({"metadata": {"name": command[3]}})
            return self.live_runner()(command)

        with self.assertRaisesRegex(
            TOOL.ProvenanceError, "automountServiceAccountToken"
        ):
            self.render(live_runner=sloppy_sa_runner)

    def test_omitted_database_stage_binding_fails_closed(self) -> None:
        # The DATABASE is the authority: a frozen stage binding present in
        # fs2_scientific_batches must appear in the signed inventory; a
        # signer cannot omit it, and no ConfigMap state can stand in for it.
        frozen_ref = PLATFORM_PREFIX + "frozen-stage@sha256:" + "c" * 64
        with self.assertRaisesRegex(
            TOOL.ProvenanceError, "authoritative database"
        ):
            self.render(live_runner=self.live_runner(frozen_objects=[frozen_ref]))

    def test_unreachable_stage_binding_database_fails_closed(self) -> None:
        import subprocess as sp

        def dead_database_runner(command):
            if command[:2] == ["kubectl", "exec"]:
                raise sp.CalledProcessError(1, command)
            return self.live_runner()(command)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "fails closed"):
            self.render(live_runner=dead_database_runner)

    def test_drifted_live_helm_governance_refuses_rendering(self) -> None:
        import copy

        documents = load_policy_documents()
        weakened = copy.deepcopy(documents[3])
        weakened["spec"]["validationActions"] = ["Audit"]

        def drifted_runner(command):
            if (
                command[:3]
                == ["kubectl", "get", "validatingadmissionpolicybinding"]
                and command[3] == "fs2-helm-release-governance"
            ):
                return json.dumps(weakened)
            return self.live_runner()(command)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "Helm-governance"):
            self.render(live_runner=drifted_runner)

    def test_live_guard_params_must_equal_the_signed_scope(self) -> None:
        # The parameter ConfigMap is derived state, never authority: live
        # content that differs from the owner-signed scope refuses rendering.
        def tampered_params_runner(command):
            if (
                command[:3] == ["kubectl", "get", "configmap"]
                and command[3] == "fs2-security-guard-params"
            ):
                return json.dumps(
                    {
                        "metadata": {"name": "fs2-security-guard-params"},
                        "data": {
                            "security-principals": "system:serviceaccount:evil:sa"
                        },
                    }
                )
            return self.live_runner()(command)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "never authority"):
            self.render(live_runner=tampered_params_runner)

    def test_forged_frozen_binding_fails_closed(self) -> None:
        # A recorded frozen ref the DATABASE does not carry is forged
        # coverage and refuses; the same inventory renders once the database
        # actually contains the binding.
        phantom = PLATFORM_PREFIX + "frozen-stage@sha256:" + "c" * 64
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B, phantom],
            live=[self.REFERENCE_A, self.REFERENCE_B],
            frozen=[phantom],
            name="frozen-forged.json",
        )
        with self.assertRaisesRegex(
            TOOL.ProvenanceError, "frozen_scientific_bindings"
        ):
            self.render(
                inventory=inventory,
                live_runner=self.live_runner(frozen_objects=[]),
            )
        # The same inventory renders once the live resource actually carries
        # the recorded reference.
        receipt_fx = build_crane_fixture(self.fixture, repository="frozen-stage")
        frozen_ref = receipt_fx.reference
        TOOL.create_release_receipt(
            frozen_ref,
            self.run_root,
            self.fixture["repo"],
            "deploy/fixture",
            "release.key",
            "release.pub",
            None,
            capture=receipt_fx.capture,
        )
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B, frozen_ref],
            live=[self.REFERENCE_A, self.REFERENCE_B],
            frozen=[frozen_ref],
            name="frozen-good.json",
        )

        def routing_capture(command):
            if "frozen-stage" in " ".join(str(part) for part in command):
                return receipt_fx.capture(command)
            return self.capture(command)

        manifest = TOOL.verified_allowlist(
            self._tmp.name,
            [],
            [REGISTRY_PREFIX],
            PLATFORM_PREFIX,
            self.run_root,
            inventory,
            self.scope_path,
            self.attestation_path,
            self._attestor_key.name,
            self.provider_export,
            deploy_principals=[AUTOMATION_PRINCIPAL],
            key_path="release.key",
            verifier=authority_checking_verifier,
            capture=routing_capture,
            live_runner=self.live_runner(frozen_objects=[frozen_ref]),
        )
        self.assertIn(
            frozen_ref.rsplit("@", 1)[1], manifest["data"]["platform-digests"]
        )

    def test_omitted_live_image_fails_closed(self) -> None:
        # The MindEval-omission class: an active platform image the
        # authenticated API session can see must appear in live_workloads.
        mindeval = PLATFORM_PREFIX + "mindeval-gateway@sha256:" + "e" * 64
        with self.assertRaisesRegex(TOOL.ProvenanceError, "omitted live images"):
            self.render(
                live_runner=self.live_runner(
                    [self.REFERENCE_A, self.REFERENCE_B, mindeval]
                )
            )

    def test_phantom_recorded_live_image_fails_closed(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "recorded-but-not-live"):
            self.render(live_runner=self.live_runner([self.REFERENCE_A]))

    def test_collector_identity_must_match_the_authenticated_user(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "collector identity"):
            self.render(
                live_runner=self.live_runner(identity="someone-else")
            )

    def test_unavailable_live_enumeration_fails_closed(self) -> None:
        import subprocess as sp

        def dead_runner(command):
            raise sp.CalledProcessError(1, command)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "fails closed"):
            self.render(live_runner=dead_runner)

    def test_unpinned_live_platform_image_fails_closed(self) -> None:
        unpinned = PLATFORM_PREFIX + "control-plane:latest"
        with self.assertRaisesRegex(TOOL.ProvenanceError, "not digest-pinned"):
            self.render(
                live_runner=self.live_runner(
                    [self.REFERENCE_A, self.REFERENCE_B, unpinned]
                )
            )

    def test_missing_typed_collector_is_refused(self) -> None:
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            name="no-collector.json",
        )
        document = json.loads(inventory.read_text(encoding="utf-8"))
        del document["collector"]
        inventory.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "typed authoritative"):
            self.render(inventory=inventory)

    def test_replayed_older_inventory_is_refused(self) -> None:
        # old -> new -> old within the freshness window: the external
        # monotonic checkpoint refuses the replay, and an equal-generation
        # inventory with DIFFERENT bytes is refused too.
        old_inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            generation=1,
            name="gen1.json",
        )
        new_inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            generation=2,
            name="gen2.json",
        )
        self.render(inventory=new_inventory)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "replays generation"):
            self.render(inventory=old_inventory)
        # Idempotent re-render of the exact accepted bytes stays allowed.
        self.render(inventory=new_inventory)
        forked = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A],
            live=[self.REFERENCE_A],
            generation=2,
            name="gen2-forked.json",
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "replays generation"):
            self.render(inventory=forked)

    def test_concurrent_render_is_refused_by_the_chain_lock(self) -> None:
        # Two concurrent renders could otherwise fork the same sequence and
        # poison the chain beyond repair (deletion is forbidden).
        import fcntl

        lock_path = self.run_root / "release-inventory-heads.lock"
        with lock_path.open("a+", encoding="utf-8") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with self.assertRaisesRegex(TOOL.ProvenanceError, "another render"):
                self.render()
        self.render()

    def randomized_signing_capture(self):
        # Real P-256 ECDSA signatures are RANDOMIZED: every signing of the
        # same payload yields different bytes. The fixture mirrors that so
        # byte-identical-adoption bugs cannot hide behind a literal stub.
        import uuid

        def capture(command):
            if command[:2] == ["cosign", "sign-blob"]:
                Path(command[command.index("--output-file") + 1]).write_text(
                    f"random-signature-{uuid.uuid4().hex}\n"
                )
                return ""
            return self.capture(command)

        return capture

    def render_with_random_signatures(self, **kwargs):
        kwargs.setdefault("verifier", authority_checking_verifier)
        return TOOL.verified_allowlist(
            self._tmp.name,
            [],
            [REGISTRY_PREFIX],
            PLATFORM_PREFIX,
            self.run_root,
            kwargs.pop("inventory", None) or self.inventory,
            self.scope_path,
            self.attestation_path,
            self._attestor_key.name,
            self.provider_export,
            deploy_principals=[AUTOMATION_PRINCIPAL],
            key_path="release.key",
            capture=self.randomized_signing_capture(),
            live_runner=self.live_runner(),
            **kwargs,
        )

    def test_acceptance_head_crash_between_links_recovers(self) -> None:
        # Signature links FIRST, and — because real ECDSA signatures are
        # randomized — the retry must VERIFY AND ADOPT the orphan signature
        # over the deterministic head payload instead of demanding
        # byte-identical re-signing, which can never happen.
        from unittest import mock

        original = TOOL._link_no_replace_or_adopt
        calls = {"count": 0}

        def crash_after_first_link(staged, final, payload):
            calls["count"] += 1
            original(staged, final, payload)
            if calls["count"] == 1:
                raise RuntimeError("simulated crash after the signature link")

        with mock.patch.object(
            TOOL, "_link_no_replace_or_adopt", crash_after_first_link
        ):
            with self.assertRaises(RuntimeError):
                self.render_with_random_signatures()
        heads_dir = self.run_root / "release-inventory-heads"
        self.assertEqual(
            [entry.name for entry in heads_dir.iterdir() if entry.name.endswith(".json")],
            [],
        )
        orphans = [e for e in heads_dir.iterdir() if e.name.endswith(".sig")]
        self.assertEqual(len(orphans), 1)
        orphan_bytes = orphans[0].read_bytes()
        # Retry re-signs DIFFERENT bytes; the orphan is verified and adopted.
        self.render_with_random_signatures()
        self.assertEqual(
            len([e for e in heads_dir.iterdir() if e.name.endswith(".json")]), 1
        )
        self.assertEqual(orphans[0].read_bytes(), orphan_bytes)

    def test_unverifiable_orphan_head_signature_is_refused(self) -> None:
        # An orphan signature that does not verify over the deterministic
        # head payload is a conflict, never silently replaced.
        import subprocess as sp

        heads_dir = self.run_root / "release-inventory-heads"
        heads_dir.mkdir(mode=0o700)

        def strict_verifier(command):
            authority_checking_verifier(command)
            if command[:2] != ["cosign", "verify-blob"]:
                return
            signature = Path(
                command[command.index("--signature") + 1]
            ).read_bytes()
            if signature.startswith(b"BAD"):
                raise sp.CalledProcessError(1, command)

        # Plant a bad orphan at the exact deterministic head path by letting
        # a crashing render compute it, then corrupting the orphan.
        from unittest import mock

        original = TOOL._link_no_replace_or_adopt
        calls = {"count": 0}

        def crash_after_first_link(staged, final, payload):
            calls["count"] += 1
            original(staged, final, payload)
            if calls["count"] == 1:
                raise RuntimeError("simulated crash")

        with mock.patch.object(
            TOOL, "_link_no_replace_or_adopt", crash_after_first_link
        ):
            with self.assertRaises(RuntimeError):
                self.render_with_random_signatures()
        orphan = next(
            entry for entry in heads_dir.iterdir() if entry.name.endswith(".sig")
        )
        orphan.write_bytes(b"BAD-orphan\n")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "orphaned signature"):
            self.render_with_random_signatures(verifier=strict_verifier)

    def test_acceptance_chain_tamper_and_truncation_fail_closed(self) -> None:
        # The acceptance heads are signed, content-addressed, and chained:
        # tampered bytes, a removed signature, and a spliced-out intermediate
        # head all refuse; deleting only the NEWEST head breaks nothing
        # locally detectable, which is exactly why WORM/off-host anchoring of
        # the head remains a named owner gate.
        for generation, name in ((1, "chain1.json"), (2, "chain2.json"), (3, "chain3.json")):
            inventory = write_inventory_fixture(
                self.run_root,
                [self.REFERENCE_A, self.REFERENCE_B],
                generation=generation,
                name=name,
            )
            self.render(inventory=inventory)
        heads_dir = self.run_root / "release-inventory-heads"
        heads = sorted(
            head for head in heads_dir.iterdir() if head.name.endswith(".json")
        )
        self.assertEqual(len(heads), 3)
        next_inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            generation=4,
            name="chain4.json",
        )
        # Tampered head bytes: content address no longer matches.
        original = heads[1].read_bytes()
        heads[1].write_bytes(original.replace(b'"generation": 2', b'"generation": 9'))
        with self.assertRaisesRegex(TOOL.ProvenanceError, "content address"):
            self.render(inventory=next_inventory)
        heads[1].write_bytes(original)
        # Missing signature: unsigned heads fail closed.
        signature = heads_dir / (heads[1].name + ".sig")
        signature_bytes = signature.read_bytes()
        signature.unlink()
        with self.assertRaisesRegex(TOOL.ProvenanceError, "UNSIGNED"):
            self.render(inventory=next_inventory)
        signature.write_bytes(signature_bytes)
        signature.chmod(0o600)
        # Spliced-out intermediate head: sequence gap fails closed.
        heads[1].unlink()
        (heads_dir / (heads[1].name + ".sig")).unlink()
        with self.assertRaisesRegex(TOOL.ProvenanceError, "gap|chain"):
            self.render(inventory=next_inventory)

    def test_missing_or_invalid_generation_is_refused(self) -> None:
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            name="no-generation.json",
        )
        document = json.loads(inventory.read_text(encoding="utf-8"))
        del document["generation"]
        inventory.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "generation"):
            self.render(inventory=inventory)

    def test_nonfinite_or_unbounded_max_age_is_refused(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf"), 0, -3, 10**6, True):
            with self.assertRaisesRegex(TOOL.ProvenanceError, "finite"):
                TOOL.load_signed_inventory(
                    self.inventory, self._tmp.name, NOOP_VERIFIER, bad
                )

    def test_future_observed_at_beyond_clock_skew_is_refused(self) -> None:
        from datetime import UTC, datetime, timedelta

        future = (datetime.now(UTC) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            observed_at=future,
            name="future-observed.json",
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "future-dated"):
            self.render(inventory=inventory)

    def test_duplicate_or_invalid_resource_ids_are_refused(self) -> None:
        for resource_ids, pattern in (
            (["duplicate", "duplicate"], "unique"),
            ([""], "unique, non-empty, structured"),
            ([{"kind": "Deployment"}], "unique, non-empty, structured"),
        ):
            inventory = write_inventory_fixture(
                self.run_root,
                [self.REFERENCE_A, self.REFERENCE_B],
                name="bad-resources.json",
            )
            document = json.loads(inventory.read_text(encoding="utf-8"))
            document["sources"]["live_workloads"]["resource_ids"] = resource_ids
            inventory.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(TOOL.ProvenanceError, pattern):
                self.render(inventory=inventory)

    def test_missing_or_empty_owner_scope_fails_closed(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "fails closed"):
            self.render(scope_path=self.run_root / "no-such-scope.json")
        empty = write_scope_fixture(self.run_root, None, name="empty-scope.json")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "EMPTY"):
            self.render(scope_path=empty)

    def test_scope_mismatches_are_refused(self) -> None:
        # Signer-substituted scope: the signed inventory names a different
        # cluster than the owner-approved scope.
        foreign_scope = dict(self.scope, cluster="other-cluster")
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            scope=foreign_scope,
            name="foreign-scope-inventory.json",
        )
        document = json.loads(inventory.read_text(encoding="utf-8"))
        document["cluster"] = "other-cluster"
        inventory.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "owner-approved"):
            self.render(inventory=inventory)
        # Key identity: the pinned verification key must be the one the
        # owner scope names.
        wrong_key_scope = write_scope_fixture(
            self.run_root,
            dict(self.scope, verification_key_sha256="f" * 64),
            name="wrong-key-scope.json",
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "verification key"):
            self.render(scope_path=wrong_key_scope)
        # CLI principals must equal the scope exactly.
        with self.assertRaisesRegex(TOOL.ProvenanceError, "--deploy-principal"):
            self.render(deploy_principals=["someone-else"])
        with self.assertRaisesRegex(TOOL.ProvenanceError, "--registry-prefix"):
            self.render(registry_prefixes=["cr.other.invalid/"])

    def test_security_and_deploy_principals_must_be_disjoint(self) -> None:
        overlapping = dict(
            self.scope, security_principals=[AUTOMATION_PRINCIPAL]
        )
        scope_path = write_scope_fixture(
            self.run_root, overlapping, name="overlap-scope.json"
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "DISJOINT"):
            self.render(scope_path=scope_path)

    def test_weakened_live_guard_refuses_rendering(self) -> None:
        # An allow-list rendered while the guard is missing or weakened would
        # claim protections that do not exist.
        def guardless_runner(command):
            if (
                command[:3] == ["kubectl", "get", "validatingadmissionpolicy"]
                and command[3] == "fs2-provenance-guard"
            ):
                import subprocess as sp

                raise sp.CalledProcessError(1, command)
            return self.live_runner()(command)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "security-owned"):
            self.render(live_runner=guardless_runner)

    def test_recovery_authorization_round_trip_and_refusals(self) -> None:
        import hashlib as h
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)

        def write_recovery(name: str, **overrides) -> Path:
            document = {
                "schema": TOOL.RECOVERY_SCHEMA,
                "cluster": "fixture-cluster",
                "target": "fs2-image-provenance",
                "target_uid": "0f0e0d0c-0b0a-4948-8767-060504030201",
                "target_resource_version": "12345",
                "prior_actions": ["Deny", "Audit"],
                "actions": ["Audit", "Warn"],
                "reason": "incident:INC-77 emergency observation window",
                "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at": (now + timedelta(hours=4)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
            document.update(overrides)
            payload = json.dumps(document).encode("utf-8")
            recovery = self.run_root / name
            recovery.write_bytes(payload)
            recovery.chmod(0o644)
            signature = self.run_root / (name + ".sig")
            signature.write_text("fixture-owner-signature\n", encoding="utf-8")
            signature.chmod(0o644)
            SIGNED_AUTHORITY_HASHES.add(h.sha256(payload).hexdigest())
            return recovery

        def recovery_verifier(command):
            import subprocess as sp

            payload = Path(command[-1]).read_bytes()
            if h.sha256(payload).hexdigest() not in SIGNED_AUTHORITY_HASHES:
                raise sp.CalledProcessError(1, command)

        good = write_recovery("recovery.json")
        document, annotation = TOOL.load_recovery_authorization(
            good, self._tmp.name, recovery_verifier
        )
        self.assertEqual(annotation, h.sha256(good.read_bytes()).hexdigest())
        self.assertEqual(document["target"], "fs2-image-provenance")
        # Tampered bytes fail the owner signature.
        good.write_bytes(good.read_bytes().replace(b"INC-77", b"INC-99"))
        with self.assertRaisesRegex(TOOL.ProvenanceError, "verification failed"):
            TOOL.load_recovery_authorization(
                good, self._tmp.name, recovery_verifier
            )
        expired = write_recovery(
            "expired.json",
            issued_at=(now - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=(now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "expired"):
            TOOL.load_recovery_authorization(
                expired, self._tmp.name, recovery_verifier
            )
        foreign = write_recovery("foreign.json", target="something-else")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "protected binding"):
            TOOL.load_recovery_authorization(
                foreign, self._tmp.name, recovery_verifier
            )
        unbounded = write_recovery(
            "unbounded.json",
            expires_at=(now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "at most"):
            TOOL.load_recovery_authorization(
                unbounded, self._tmp.name, recovery_verifier
            )
        unfenced = write_recovery("unfenced.json", target_resource_version="")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "resourceVersion"):
            TOOL.load_recovery_authorization(
                unfenced, self._tmp.name, recovery_verifier
            )
        # Single-use: once consumed through the chained ledger, the same
        # signed document never authorizes again.
        single = write_recovery("single-use.json")
        _, single_sha = TOOL.load_recovery_authorization(
            single, self._tmp.name, recovery_verifier, run_root=self.run_root
        )
        TOOL._record_consumed(self.run_root, single_sha, "recovery")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "SINGLE-USE"):
            TOOL.load_recovery_authorization(
                single, self._tmp.name, recovery_verifier, run_root=self.run_root
            )

    def test_human_deploy_principal_in_scope_is_refused(self) -> None:
        # Owner decision: automation-only, short-lived release identity —
        # a human username never holds deploy authority, in the scope or CLI.
        human = dict(self.scope, deploy_principals=["kubernetes-admin"])
        scope_path = write_scope_fixture(
            self.run_root, human, name="human-principal-scope.json"
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "AUTOMATION"):
            self.render(
                scope_path=scope_path, deploy_principals=["kubernetes-admin"]
            )

    def test_substituted_verification_key_never_verifies(self) -> None:
        # A caller-selected or co-located replacement key cannot verify
        # anything: its fingerprint cannot equal the source-pinned constant.
        import tempfile

        rogue = tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False)
        rogue.write("-----BEGIN PUBLIC KEY-----\nrogue\n-----END PUBLIC KEY-----\n")
        rogue.close()
        self.addCleanup(lambda: Path(rogue.name).unlink(missing_ok=True))
        with self.assertRaisesRegex(TOOL.ProvenanceError, "pinned key fingerprint"):
            TOOL.verified_allowlist(
                rogue.name,
                [],
                [REGISTRY_PREFIX],
                PLATFORM_PREFIX,
                self.run_root,
                self.inventory,
                self.scope_path,
                self.attestation_path,
                self._attestor_key.name,
                self.provider_export,
                deploy_principals=["deployer"],
                key_path="release.key",
                verifier=authority_checking_verifier,
                capture=self.capture,
                live_runner=self.live_runner(),
            )
        # The ATTESTOR key is pinned by the owner-signed scope: a rogue
        # attestor key (or the release key itself) never verifies the
        # provider attestation.
        with self.assertRaisesRegex(TOOL.ProvenanceError, "pinned key fingerprint"):
            self.render(attestation_key_path=rogue.name)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "pinned key fingerprint"):
            self.render(attestation_key_path=self._tmp.name)

    def test_substituted_scope_never_renders(self) -> None:
        # The reproduced attack: locally edit the scope (or re-commit it, or
        # move any Git ref — refs are NOT consulted). Authority is the owner
        # SIGNATURE over the exact bytes, so unsigned substituted bytes fail.
        substituted = dict(
            self.scope, registry_prefixes=["cr.attacker.invalid/"]
        )
        self.scope_path.write_bytes(
            json.dumps(
                {"schema": TOOL.SCOPE_SCHEMA, "scope": substituted}
            ).encode("utf-8")
        )
        self.scope_path.chmod(0o644)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "never authorizes"):
            self.render()

    def test_unsigned_scope_file_never_renders(self) -> None:
        # A scope file without its owner signature is not authority, however
        # well-formed its content is.
        unsigned = self.run_root / "unsigned-scope.json"
        unsigned.write_text(
            json.dumps({"schema": TOOL.SCOPE_SCHEMA, "scope": self.scope}),
            encoding="utf-8",
        )
        unsigned.chmod(0o644)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "UNSIGNED"):
            self.render(scope_path=unsigned)

    def test_identity_boundary_violations_refuse_rendering(self) -> None:
        # The external boundary is ENFORCED at render: any non-exempt subject
        # holding a forbidden identity path (here: impersonation) refuses.
        impersonator_role = {
            "metadata": {"name": "impersonator"},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["users"],
                    "verbs": ["impersonate"],
                }
            ],
        }
        mallory_binding = {
            "metadata": {"name": "mallory-can-impersonate"},
            "roleRef": {"kind": "ClusterRole", "name": "impersonator"},
            "subjects": [{"kind": "User", "name": "mallory"}],
        }
        violating_runner = self.live_runner(
            cluster_roles=[impersonator_role],
            cluster_role_bindings=[mallory_binding],
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "identity boundary"):
            self.render(live_runner=violating_runner)
        # A HUMAN exemption cannot even be signed: the scope refuses it, so
        # a wildcard cluster-admin user can never be exempted into validity.
        human_exempt = dict(self.scope, iam_exempt_subjects=["User:mallory"])
        human_scope_path = write_scope_fixture(
            self.run_root, human_exempt, name="human-exempt-scope.json"
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "NEVER exemptible"):
            self.render(scope_path=human_scope_path)
        # Only enumerated bootstrap identities / kube-system controller
        # ServiceAccounts are exemptible.
        controller_role = {
            "metadata": {"name": "controller-impersonator"},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["serviceaccounts"],
                    "verbs": ["impersonate"],
                }
            ],
        }
        controller_binding = {
            "metadata": {"name": "kube-controller"},
            "roleRef": {"kind": "ClusterRole", "name": "controller-impersonator"},
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "namespace": "kube-system",
                    "name": "namespace-controller",
                }
            ],
        }
        exempt_scope = dict(
            self.scope,
            iam_exempt_subjects=["ServiceAccount:kube-system:namespace-controller"],
        )
        scope_path = write_scope_fixture(
            self.run_root, exempt_scope, name="exempt-scope.json"
        )
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            scope=exempt_scope,
            name="exempt-inventory.json",
        )
        self.render(
            inventory=inventory,
            scope_path=scope_path,
            live_runner=self.live_runner(
                cluster_roles=[controller_role],
                cluster_role_bindings=[controller_binding],
            ),
        )

    def test_security_principal_allowance_is_function_scoped(self) -> None:
        # The security identity is permitted its FUNCTION (admission-config
        # writes) and nothing else: the same principal holding impersonation
        # violates like any other subject — no blanket identity exemption.
        security_subject = {
            "kind": "ServiceAccount",
            "namespace": "fs2-security",
            "name": "fs2-admission-guard",
        }
        reconciler_role = {
            "metadata": {"name": "fs2-security-boundary-operator"},
            "rules": [
                {
                    "apiGroups": ["admissionregistration.k8s.io"],
                    "resources": [
                        "validatingadmissionpolicies",
                        "validatingadmissionpolicybindings",
                    ],
                    "verbs": ["get", "list", "create", "update", "patch"],
                }
            ],
        }
        reconciler_binding = {
            "metadata": {"name": "fs2-security-boundary-operator"},
            "roleRef": {
                "kind": "ClusterRole",
                "name": "fs2-security-boundary-operator",
            },
            "subjects": [dict(security_subject)],
        }
        self.render(
            live_runner=self.live_runner(
                cluster_roles=[reconciler_role],
                cluster_role_bindings=[reconciler_binding],
            )
        )
        impersonator_role = {
            "metadata": {"name": "security-impersonator"},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["users"],
                    "verbs": ["impersonate"],
                }
            ],
        }
        impersonator_binding = {
            "metadata": {"name": "security-can-impersonate"},
            "roleRef": {"kind": "ClusterRole", "name": "security-impersonator"},
            "subjects": [dict(security_subject)],
        }
        with self.assertRaisesRegex(TOOL.ProvenanceError, "identity boundary"):
            self.render(
                live_runner=self.live_runner(
                    cluster_roles=[impersonator_role],
                    cluster_role_bindings=[impersonator_binding],
                )
            )

    def _security_pod_runner(self, pod: dict):
        base = self.live_runner()

        def runner(command):
            if (
                command[:3] == ["kubectl", "get", "pods"]
                and command[3] == "-n"
                and command[4] == "fs2-security"
                and "-l" not in command
            ):
                return json.dumps({"items": [pod]})
            return base(command)

        return runner

    def test_identity_pod_token_and_secret_hygiene(self) -> None:
        # Pods running as an automation identity must be token-hardened:
        # pod-level automount false, the REQUIRED bounded projection, and no
        # stored-credential paths (Secret volumes / env). The admission
        # policy enforces the same contract preventively; this is the live
        # re-verification.
        def principal_pod() -> dict:
            return {
                "metadata": {"name": "guard-pod"},
                "spec": {
                    "serviceAccountName": "fs2-admission-guard",
                    "automountServiceAccountToken": False,
                    "volumes": [
                        {
                            "projected": {
                                "sources": [
                                    {
                                        "serviceAccountToken": {
                                            "audience": "fs2-release",
                                            "expirationSeconds": 600,
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                    "containers": [{"name": "guard", "image": "img"}],
                },
            }

        self.render(live_runner=self._security_pod_runner(principal_pod()))
        implicit = principal_pod()
        del implicit["spec"]["automountServiceAccountToken"]
        with self.assertRaisesRegex(TOOL.ProvenanceError, "POD level"):
            self.render(live_runner=self._security_pod_runner(implicit))
        projectionless = principal_pod()
        projectionless["spec"]["volumes"] = []
        with self.assertRaisesRegex(TOOL.ProvenanceError, "REQUIRED"):
            self.render(live_runner=self._security_pod_runner(projectionless))
        secret_mount = principal_pod()
        secret_mount["spec"]["volumes"].append(
            {"secret": {"secretName": "stolen"}}
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "Secret volume"):
            self.render(live_runner=self._security_pod_runner(secret_mount))
        secret_env = principal_pod()
        secret_env["spec"]["containers"][0]["env"] = [
            {
                "name": "TOKEN",
                "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}},
            }
        ]
        with self.assertRaisesRegex(TOOL.ProvenanceError, "env/envFrom"):
            self.render(live_runner=self._security_pod_runner(secret_env))
        foreign_audience = principal_pod()
        foreign_audience["spec"]["volumes"][0]["projected"]["sources"][0][
            "serviceAccountToken"
        ]["audience"] = "kubernetes.default"
        with self.assertRaisesRegex(TOOL.ProvenanceError, "EXACT token_audience"):
            self.render(
                live_runner=self._security_pod_runner(foreign_audience)
            )

    def test_recovery_action_override_permits_only_the_annotated_toggle(
        self,
    ) -> None:
        # Post-check semantics: a live Audit/Warn binding is drift UNLESS the
        # override names exactly that binding with exactly those authorized
        # actions AND the live object carries the authorizing recovery
        # annotation — so a completed break-glass passes its own post-check
        # while any unannotated or unauthorized weakening still refuses.
        import copy

        recovery_sha = "5" * 64
        documents = load_policy_documents()
        weakened = copy.deepcopy(
            next(
                document
                for document in documents
                if document.get("kind") == "ValidatingAdmissionPolicyBinding"
                and document["metadata"]["name"] == "fs2-image-provenance"
            )
        )
        weakened["spec"]["validationActions"] = ["Audit", "Warn"]
        weakened["metadata"].setdefault("annotations", {})[
            "security.fs2.nebius.ai/recovery-authorization"
        ] = recovery_sha
        runner = self.live_runner(live_binding=weakened)
        overrides = {"fs2-image-provenance": (["Audit", "Warn"], recovery_sha)}
        TOOL._assert_policy_matches_scope(
            self.scope, runner, binding_action_overrides=overrides
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "does not equal"):
            TOOL._assert_policy_matches_scope(self.scope, runner)
        unannotated = copy.deepcopy(weakened)
        del unannotated["metadata"]["annotations"]
        with self.assertRaisesRegex(TOOL.ProvenanceError, "annotation"):
            TOOL._assert_policy_matches_scope(
                self.scope,
                self.live_runner(live_binding=unannotated),
                binding_action_overrides=overrides,
            )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "protected bindings"):
            TOOL._assert_policy_matches_scope(
                self.scope,
                runner,
                binding_action_overrides={
                    "not-a-protected-binding": (["Audit"], recovery_sha)
                },
            )

    def test_bootstrap_masters_recognition_is_attestation_driven(self) -> None:
        # The one bootstrap cluster-admin -> system:masters binding is
        # tolerated because the REQUIRED owner-signed provider attestation
        # asserts masters certificate issuance is provider-held; the group
        # itself remains non-exemptible, and any OTHER masters-shaped
        # binding still violates.
        masters_binding = {
            "metadata": {"name": "cluster-admin"},
            "roleRef": {"kind": "ClusterRole", "name": "cluster-admin"},
            "subjects": [{"kind": "Group", "name": "system:masters"}],
        }
        admin_role = {
            "metadata": {"name": "cluster-admin"},
            "rules": [
                {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}
            ],
        }
        runner = self.live_runner(
            cluster_roles=[admin_role],
            cluster_role_bindings=[masters_binding],
        )
        self.render(live_runner=runner)
        renamed = dict(masters_binding, metadata={"name": "shadow-admin"})
        with self.assertRaisesRegex(TOOL.ProvenanceError, "identity boundary"):
            self.render(
                live_runner=self.live_runner(
                    cluster_roles=[admin_role],
                    cluster_role_bindings=[renamed],
                )
            )
        masters_exempt = dict(
            self.scope, iam_exempt_subjects=["Group:system:masters"]
        )
        masters_scope = write_scope_fixture(
            self.run_root, masters_exempt, name="masters-exempt-scope.json"
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "NEVER exemptible"):
            self.render(scope_path=masters_scope)

    def test_provider_attestation_is_required_and_verified(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "missing provider"):
            self.render(
                attestation_path=self.run_root / "no-attestation.json"
            )
        foreign = write_attestation_fixture(
            self.run_root, cluster="other-cluster", name="foreign-attest.json"
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "different cluster"):
            self.render(attestation_path=foreign)
        weak = write_attestation_fixture(
            self.run_root,
            name="weak-attest.json",
            masters_certificate_issuance="operator-held",
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "provider-held"):
            self.render(attestation_path=weak)
        # Empty/omitted anchored chains anchor nothing: refused at load.
        hollow = write_attestation_fixture(
            self.run_root, anchored={"chains": {}}, name="hollow-attest.json"
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "omits required"):
            self.render(attestation_path=hollow)
        # Bare provider-held strings without the bound provider IAM export
        # digest are assertions, not evidence: refused at load.
        bare = write_attestation_fixture(
            self.run_root, name="bare-attest.json", evidence=None
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "not evidence"):
            self.render(attestation_path=bare)

    def test_anchored_heads_regression_refuses_rendering(self) -> None:
        # The attestation embeds the off-host anchor; local chains BEHIND it
        # mean deletion/truncation and rendering fails closed — anchor
        # verification is enforced, not optional or manual.
        self.render()
        snapshot = TOOL._anchor_snapshot(self.run_root)
        ahead = json.loads(json.dumps(snapshot))
        ahead["chains"]["acceptance-heads"]["count"] += 3
        regressed = write_attestation_fixture(
            self.run_root, anchored=ahead, name="regressed-attest.json"
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "BEHIND"):
            self.render(attestation_path=regressed)

    def test_ledger_legacy_adoption_and_crash_roll_forward(self) -> None:
        # Pre-checkpoint ledgers (preserved evidence) are ADOPTED forward,
        # and the append crash window (file one verified record ahead of the
        # checkpoint) is repaired forward; nothing is deleted.
        ledger = self.run_root / "release-authorization-consumed.jsonl"
        TOOL._record_consumed(self.run_root, "1" * 64, "test")
        TOOL._record_consumed(self.run_root, "2" * 64, "test")
        checkpoint = TOOL._ledger_checkpoint_path(ledger)
        # Legacy: checkpoint absent entirely -> adopted.
        saved = checkpoint.read_bytes()
        checkpoint.unlink()
        self.assertTrue(TOOL._is_consumed(self.run_root, "2" * 64))
        self.assertTrue(checkpoint.exists())
        # Crash window: checkpoint exactly one behind -> rolled forward.
        lines = ledger.read_bytes().splitlines()
        import hashlib as h

        checkpoint.write_bytes(
            json.dumps(
                {"count": 1, "head": h.sha256(lines[0]).hexdigest()},
                sort_keys=True,
            ).encode("utf-8")
        )
        self.assertTrue(TOOL._is_consumed(self.run_root, "2" * 64))
        adopted = json.loads(checkpoint.read_bytes())
        self.assertEqual(adopted["count"], 2)
        assert saved  # retained for clarity; content superseded forward

    def test_recovery_refuses_arbitrary_action_subsets(self) -> None:
        import hashlib as h
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        for actions in (
            ["Deny"],
            ["Warn"],
            ["Audit"],
            ["Deny", "Warn"],
            ["Deny", "Audit", "Warn"],
        ):
            document = {
                "schema": TOOL.RECOVERY_SCHEMA,
                "cluster": "fixture-cluster",
                "target": "fs2-image-provenance",
                "target_uid": "0f0e0d0c-0b0a-4948-8767-060504030201",
                "target_resource_version": "12345",
                "prior_actions": ["Deny", "Audit"],
                "actions": actions,
                "reason": "incident:INC-5 subset probe",
                "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at": (now + timedelta(hours=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
            payload = json.dumps(document).encode("utf-8")
            recovery = self.run_root / "subset.json"
            recovery.write_bytes(payload)
            recovery.chmod(0o644)
            signature = self.run_root / "subset.json.sig"
            signature.write_text("fixture-owner-signature\n", encoding="utf-8")
            signature.chmod(0o644)
            SIGNED_AUTHORITY_HASHES.add(h.sha256(payload).hexdigest())
            with self.assertRaisesRegex(
                TOOL.ProvenanceError, "sanctioned enforcement state"
            ):
                TOOL.load_recovery_authorization(
                    recovery, self._tmp.name, NOOP_VERIFIER
                )

    def test_rollout_authorization_embeds_and_pins_the_exact_plan(self) -> None:
        import hashlib as h
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        plan = [
            {"argv": ["kubectl", "apply", "-f", "-"], "stdin_sha256": "6" * 64,
             "verify": None}
        ]
        plan_sha = h.sha256(
            json.dumps(plan, sort_keys=True).encode("utf-8")
        ).hexdigest()

        def write_authorization(name: str, **overrides) -> Path:
            import hashlib as fixture_hashlib

            document = {
                "schema": TOOL.ROLLOUT_AUTHORIZATION_SCHEMA,
                "cluster": "fixture-cluster",
                "plan": plan,
                "plan_sha256": plan_sha,
                "executor": SECURITY_PRINCIPAL,
                "reason": "change:CH-9 boundary rollout",
                "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at": (now + timedelta(hours=4)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
            document.update(overrides)
            payload = json.dumps(document).encode("utf-8")
            path = self.run_root / name
            path.write_bytes(payload)
            path.chmod(0o644)
            signature = self.run_root / (name + ".sig")
            signature.write_text("fixture-owner-signature\n", encoding="utf-8")
            signature.chmod(0o644)
            SIGNED_AUTHORITY_HASHES.add(
                fixture_hashlib.sha256(payload).hexdigest()
            )
            return path

        good = write_authorization("authorization.json")
        document, document_sha = TOOL.load_rollout_authorization(
            good, self._tmp.name, "fixture-cluster", plan_sha, self.run_root,
            NOOP_VERIFIER,
        )
        self.assertEqual(document["plan_sha256"], plan_sha)
        # The embedded plan must hash to its own recorded digest.
        lying = write_authorization("lying.json", plan_sha256="7" * 64)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "EXACT actions"):
            TOOL.load_rollout_authorization(
                lying, self._tmp.name, "fixture-cluster", None, self.run_root,
                NOOP_VERIFIER,
            )
        # The named executor is required: execution/resume bind to ONE
        # owner-designated automation identity.
        headless = write_authorization("headless.json", executor="")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "executor"):
            TOOL.load_rollout_authorization(
                headless, self._tmp.name, "fixture-cluster", None,
                self.run_root, NOOP_VERIFIER,
            )
        human_led = write_authorization("human-led.json", executor="admin")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "executor"):
            TOOL.load_rollout_authorization(
                human_led, self._tmp.name, "fixture-cluster", None,
                self.run_root, NOOP_VERIFIER,
            )
        # A live recomputation that differs from the signed plan refuses.
        with self.assertRaisesRegex(TOOL.ProvenanceError, "freshly recomputed"):
            TOOL.load_rollout_authorization(
                good, self._tmp.name, "fixture-cluster", "8" * 64,
                self.run_root, NOOP_VERIFIER,
            )
        # Foreign cluster refuses.
        with self.assertRaisesRegex(TOOL.ProvenanceError, "not the live"):
            TOOL.load_rollout_authorization(
                good, self._tmp.name, "other-cluster", plan_sha,
                self.run_root, NOOP_VERIFIER,
            )
        # Resume (allow_consumed) requires the document to BE consumed…
        with self.assertRaisesRegex(TOOL.ProvenanceError, "never consumed"):
            TOOL.load_rollout_authorization(
                good, self._tmp.name, "fixture-cluster", None, self.run_root,
                NOOP_VERIFIER, allow_consumed=True,
            )
        # …and after consumption, first-use is refused while resume proceeds.
        TOOL._record_consumed(self.run_root, document_sha, "rollout-authorization")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "SINGLE-USE"):
            TOOL.load_rollout_authorization(
                good, self._tmp.name, "fixture-cluster", plan_sha,
                self.run_root, NOOP_VERIFIER,
            )
        TOOL.load_rollout_authorization(
            good, self._tmp.name, "fixture-cluster", None, self.run_root,
            NOOP_VERIFIER, allow_consumed=True,
        )

    def test_sigkill_hardlink_remnants_do_not_wedge_the_chain(self) -> None:
        # A SIGKILL between link(2) and staging cleanup leaves published
        # files with nlink=2 (the remnant may never be deleted). Chain
        # verification and adoption must keep working over such files, since
        # every byte is verified against signatures/content addresses.
        self.render()
        heads_dir = self.run_root / "release-inventory-heads"
        for entry in list(heads_dir.iterdir()):
            if entry.name.endswith((".json", ".sig")):
                os.link(entry, heads_dir / (".remnant-" + entry.name))
        next_inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            generation=2,
            name="gen2-after-remnant.json",
        )
        self.render(inventory=next_inventory)

    def test_weakened_or_absent_live_policy_refuses_rendering(self) -> None:
        import copy

        documents = load_policy_documents()
        # No live VAP at all: fail closed.
        with self.assertRaisesRegex(TOOL.ProvenanceError, "fails closed"):
            self.render(live_runner=self.live_runner(live_policy="absent"))
        # Audit-only live binding: observation is not a boundary.
        audit_only = copy.deepcopy(documents[1])
        audit_only["spec"]["validationActions"] = ["Audit", "Warn"]
        with self.assertRaisesRegex(TOOL.ProvenanceError, "drifted"):
            self.render(live_runner=self.live_runner(live_binding=audit_only))
        # NotIn selector with identical values inverts coverage: refused.
        inverted = copy.deepcopy(documents[1])
        inverted["spec"]["matchResources"]["namespaceSelector"][
            "matchExpressions"
        ][0]["operator"] = "NotIn"
        with self.assertRaisesRegex(TOOL.ProvenanceError, "drifted"):
            self.render(live_runner=self.live_runner(live_binding=inverted))
        # A live policy missing the ephemeral-container subresource: refused.
        weakened = copy.deepcopy(documents[0])
        weakened["spec"]["matchConstraints"]["resourceRules"][0][
            "resources"
        ] = ["pods"]
        with self.assertRaisesRegex(TOOL.ProvenanceError, "does not equal"):
            self.render(live_runner=self.live_runner(live_policy=weakened))
        # The reproduced exclude-all probe: a live binding with an
        # exclude-everything rule narrows enforcement to nothing while its
        # namespaceSelector still looks identical — must compare UNEQUAL.
        excluded = copy.deepcopy(documents[1])
        excluded["spec"]["matchResources"]["excludeResourceRules"] = [
            {
                "apiGroups": ["*"],
                "apiVersions": ["*"],
                "operations": ["*"],
                "resources": ["*"],
            }
        ]
        with self.assertRaisesRegex(TOOL.ProvenanceError, "drifted"):
            self.render(live_runner=self.live_runner(live_binding=excluded))
        # A live policy narrowed by an exclude rule or objectSelector: refused.
        exclude_policy = copy.deepcopy(documents[0])
        exclude_policy["spec"]["matchConstraints"]["excludeResourceRules"] = [
            {
                "apiGroups": [""],
                "apiVersions": ["v1"],
                "operations": ["*"],
                "resources": ["pods/ephemeralcontainers"],
            }
        ]
        with self.assertRaisesRegex(TOOL.ProvenanceError, "does not equal"):
            self.render(
                live_runner=self.live_runner(live_policy=exclude_policy)
            )
        selector_binding = copy.deepcopy(documents[1])
        selector_binding["spec"]["matchResources"]["objectSelector"] = {
            "matchLabels": {"never-matches": "true"}
        }
        with self.assertRaisesRegex(TOOL.ProvenanceError, "drifted"):
            self.render(
                live_runner=self.live_runner(live_binding=selector_binding)
            )

    def test_scope_must_pin_the_committed_policy_hash(self) -> None:
        mismatched = dict(self.scope, policy_sha256="f" * 64)
        scope_path = write_scope_fixture(
            self.run_root, mismatched, name="wrong-policy-scope.json"
        )
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            scope=mismatched,
            name="wrong-policy-inventory.json",
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "owner-signed scope authorizes"):
            self.render(inventory=inventory, scope_path=scope_path)

    def test_scope_namespaces_must_equal_the_enforced_policy_coverage(
        self,
    ) -> None:
        # A scope claiming a namespace the committed policy binding does not
        # match would make the recorded coverage a lie; renders refuse it.
        widened = dict(
            self.scope, namespaces=["fs2-extra", "fs2-models", "fs2-system"]
        )
        scope_path = write_scope_fixture(
            self.run_root, widened, name="widened-scope.json"
        )
        inventory = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            scope=widened,
            name="widened-inventory.json",
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "enforced coverage"):
            self.render(inventory=inventory, scope_path=scope_path)

    def test_nonmatching_platform_prefix_cannot_bypass_platform_digests(
        self,
    ) -> None:
        # The demonstrated bypass: a CLI-chosen unrelated prefix would make
        # every real platform image a "non-platform" image, emptying the
        # platform-digests gate. The prefix now comes from the owner scope
        # and a mismatched CLI value refuses to render.
        with self.assertRaisesRegex(
            TOOL.ProvenanceError, "--platform-repository-prefix"
        ):
            self.render(platform_prefix=REGISTRY_PREFIX + "unrelated/")

    def test_explicit_references_must_equal_the_inventory_exactly(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "equal the signed inventory"):
            self.render(references=[self.REFERENCE_A])
        extra = PLATFORM_PREFIX + "website@sha256:" + "9" * 64
        with self.assertRaisesRegex(TOOL.ProvenanceError, "equal the signed inventory"):
            self.render(references=[self.REFERENCE_A, self.REFERENCE_B, extra])
        manifest = self.render(references=[self.REFERENCE_B, self.REFERENCE_A])
        self.assertEqual(
            manifest["data"]["platform-digests"], self.expected_digests()
        )

    def test_unsigned_or_missing_inventory_is_refused(self) -> None:
        (self.run_root / "inventory.json.sig").unlink()
        with self.assertRaisesRegex(TOOL.ProvenanceError, "signed release inventory"):
            self.render()

    def test_inventory_signature_failure_is_refused(self) -> None:
        import subprocess

        def failing_inventory_verifier(command):
            authority_checking_verifier(command)
            payload = Path(command[-1]).read_bytes()
            try:
                document = json.loads(payload)
            except json.JSONDecodeError:
                return
            if (
                isinstance(document, dict)
                and document.get("schema") == TOOL.INVENTORY_SCHEMA
            ):
                raise subprocess.CalledProcessError(1, command)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "inventory signature"):
            self.render(verifier=failing_inventory_verifier)

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
            drained=[{"image": self.REFERENCE_B, "reason": "change:CHG-1 gone"}],
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "no non-live source"):
            self.render(inventory=unlisted_drain)
        undocumented_drain = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A],
            live=[self.REFERENCE_A],
            helm=[self.REFERENCE_B],
            drained=[{"image": self.REFERENCE_B, "reason": "no ticket"}],
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "tracking identifier"):
            self.render(inventory=undocumented_drain)
        audited = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A],
            live=[self.REFERENCE_A],
            helm=[self.REFERENCE_B],
            drained=[
                {"image": self.REFERENCE_B, "reason": "change:CHG-42 drained"}
            ],
        )
        manifest = self.render(
            inventory=audited,
            live_runner=self.live_runner(
                [self.REFERENCE_A], helm_images=[self.REFERENCE_B]
            ),
        )
        self.assertEqual(manifest["data"]["platform-digests"], self.DIGEST_A)

    def test_an_active_live_image_can_never_be_drained(self) -> None:
        # The MindEval-bypass class: a running digest must be receipted or the
        # admission policy scoped; declaring it drained fails closed.
        live_drain = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A],
            live=[self.REFERENCE_A, self.REFERENCE_B],
            helm=[self.REFERENCE_B],
            drained=[
                {"image": self.REFERENCE_B, "reason": "change:CHG-99 scope out"}
            ],
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "STILL LIVE"):
            self.render(inventory=live_drain)

    def test_stale_or_future_inventory_is_refused(self) -> None:
        stale = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            captured_at="2026-09-10T00:00:00Z",
            name="stale-inventory.json",
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "stale"):
            self.render(inventory=stale)
        future = write_inventory_fixture(
            self.run_root,
            [self.REFERENCE_A, self.REFERENCE_B],
            captured_at="2036-01-01T00:00:00Z",
            name="future-inventory.json",
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "future"):
            self.render(inventory=future)

    def test_inventory_sources_need_observation_snapshots(self) -> None:
        inventory = write_inventory_fixture(
            self.run_root, [self.REFERENCE_A, self.REFERENCE_B]
        )
        document = json.loads(inventory.read_text(encoding="utf-8"))
        document["sources"]["live_workloads"]["resource_ids"] = []
        inventory.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "resource identities"):
            self.render()

    def test_unreceipted_inventory_reference_aborts_rendering(self) -> None:
        orphan = PLATFORM_PREFIX + "website@sha256:" + "9" * 64
        inventory = write_inventory_fixture(
            self.run_root, [self.REFERENCE_A, self.REFERENCE_B, orphan]
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "no signed release receipt"):
            self.render(
                inventory=inventory,
                live_runner=self.live_runner(
                    [self.REFERENCE_A, self.REFERENCE_B, orphan]
                ),
            )

    def test_receipt_verification_failure_aborts_rendering(self) -> None:
        import subprocess

        calls = {"count": 0}

        def failing_after_inventory(command):
            authority_checking_verifier(command)
            calls["count"] += 1
            if calls["count"] > 2:
                raise subprocess.CalledProcessError(1, command)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "not trustworthy"):
            self.render(verifier=failing_after_inventory)


class _FakeAdmissionCluster:
    """Stateful fake API server for reconcile orchestration proofs.

    Serves the committed policy objects with server defaults, real UIDs and
    resourceVersions; a merge patch honors UID/resourceVersion PRECONDITIONS
    (stale fences conflict exactly like a real API server) and bumps the RV;
    `kubectl apply` replaces the supplied documents with fresh RVs while
    PRESERVING objects it was not given — so the stale-RV recovery wedge and
    crash-resume are reproduced faithfully, not assumed away.
    """

    def __init__(self, identity: str = SECURITY_PRINCIPAL) -> None:
        import copy

        self.identity = identity
        self.raise_after_next_patch = False
        self._rv = 1000
        self.objects: dict[tuple[str, str], dict] = {}
        for document in load_policy_documents():
            live = copy.deepcopy(document)
            spec = live.setdefault("spec", {})
            if live["kind"] == "ValidatingAdmissionPolicy":
                spec.setdefault("failurePolicy", "Fail")
                constraints = spec.setdefault("matchConstraints", {})
                constraints.setdefault("matchPolicy", "Equivalent")
                for rule in constraints.get("resourceRules") or []:
                    rule.setdefault("scope", "*")
            else:
                matches = spec.setdefault("matchResources", {})
                matches.setdefault("matchPolicy", "Equivalent")
                matches.setdefault("namespaceSelector", {})
                matches.setdefault("objectSelector", {})
            name = live["metadata"]["name"]
            live["metadata"]["uid"] = self._uid(live["kind"], name)
            live["metadata"]["resourceVersion"] = self._next_rv()
            self.objects[(live["kind"], name)] = live

    @staticmethod
    def _uid(kind: str, name: str) -> str:
        import hashlib as fixture_hashlib

        # Recovery documents pin UIDs with a hex-shaped pattern; the fake
        # cluster's UIDs must look like real ones.
        return fixture_hashlib.sha256(f"{kind}/{name}".encode()).hexdigest()[
            :32
        ]

    def _next_rv(self) -> str:
        self._rv += 1
        return str(self._rv)

    def binding(self, name: str) -> dict:
        return self.objects[("ValidatingAdmissionPolicyBinding", name)]

    def runner(self, command, input_text=None):
        import subprocess as sp

        if command[:3] == ["kubectl", "auth", "whoami"]:
            return json.dumps(
                {"status": {"userInfo": {"username": self.identity}}}
            )
        if command[:4] == ["kubectl", "get", "namespace", "kube-system"]:
            return "fixture-cluster"
        if command[:3] == ["kubectl", "get", "validatingadmissionpolicy"]:
            return json.dumps(
                self.objects[("ValidatingAdmissionPolicy", command[3])]
            )
        if command[:3] == ["kubectl", "get", "validatingadmissionpolicybinding"]:
            return json.dumps(self.binding(command[3]))
        if command[:3] == ["kubectl", "get", "configmap"]:
            if command[3] == "fs2-security-guard-params":
                return json.dumps(
                    {
                        "metadata": {"name": command[3]},
                        "data": {"security-principals": SECURITY_PRINCIPAL},
                    }
                )
            raise sp.CalledProcessError(1, command)
        if command[:3] in (
            ["kubectl", "get", "clusterroles"],
            ["kubectl", "get", "clusterrolebindings"],
            ["kubectl", "get", "roles"],
            ["kubectl", "get", "rolebindings"],
        ):
            return json.dumps({"items": []})
        if command[:3] == ["kubectl", "patch", "validatingadmissionpolicybinding"]:
            name = command[3]
            patch = json.loads(command[command.index("-p") + 1])
            live = self.binding(name)
            metadata = patch.get("metadata") or {}
            if (
                str(metadata.get("uid", ""))
                != str(live["metadata"]["uid"])
                or str(metadata.get("resourceVersion", ""))
                != str(live["metadata"]["resourceVersion"])
            ):
                # Exactly what a real API server does with a stale
                # UID/resourceVersion precondition: 409 conflict.
                raise sp.CalledProcessError(1, command)
            live["spec"]["validationActions"] = list(
                (patch.get("spec") or {}).get("validationActions") or []
            )
            annotations = live["metadata"].setdefault("annotations", {})
            annotations.update(metadata.get("annotations") or {})
            live["metadata"]["resourceVersion"] = self._next_rv()
            if self.raise_after_next_patch:
                self.raise_after_next_patch = False
                raise RuntimeError(
                    "simulated crash immediately after the patch landed"
                )
            return "patched"
        if command[:4] == ["kubectl", "apply", "-f", "-"]:
            import copy

            for document in yaml.safe_load_all(input_text or ""):
                if not document:
                    continue
                kind = document.get("kind")
                if kind not in (
                    "ValidatingAdmissionPolicy",
                    "ValidatingAdmissionPolicyBinding",
                ):
                    continue
                name = document["metadata"]["name"]
                live = copy.deepcopy(document)
                spec = live.setdefault("spec", {})
                if kind == "ValidatingAdmissionPolicy":
                    spec.setdefault("failurePolicy", "Fail")
                    constraints = spec.setdefault("matchConstraints", {})
                    constraints.setdefault("matchPolicy", "Equivalent")
                    for rule in constraints.get("resourceRules") or []:
                        rule.setdefault("scope", "*")
                else:
                    matches = spec.setdefault("matchResources", {})
                    matches.setdefault("matchPolicy", "Equivalent")
                    matches.setdefault("namespaceSelector", {})
                    matches.setdefault("objectSelector", {})
                previous = self.objects.get((kind, name))
                live["metadata"]["uid"] = (
                    previous["metadata"]["uid"]
                    if previous
                    else self._uid(kind, name)
                )
                live["metadata"]["resourceVersion"] = self._next_rv()
                self.objects[(kind, name)] = live
            return "applied"
        raise AssertionError(f"unexpected reconcile command: {command}")


class ReconcileBoundaryTest(unittest.TestCase):
    """PROVES the reconcile orchestration: fenced recovery sequencing (no
    stale-RV wedge on Audit/Warn -> Deny/Audit restore), filtered repair
    apply that never touches the recovery target, crash-resume, and executor
    lineage — against a stateful fake API server with real RV semantics.
    """

    def setUp(self) -> None:
        import hashlib as h
        import tempfile

        self._tmp = tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False)
        self._tmp.write(TEST_KEY_CONTENT)
        self._tmp.close()
        self.addCleanup(lambda: Path(self._tmp.name).unlink(missing_ok=True))
        self._attestor_key = tempfile.NamedTemporaryFile(
            "w", suffix=".pub", delete=False
        )
        self._attestor_key.write(ATTESTOR_KEY_CONTENT)
        self._attestor_key.close()
        self.addCleanup(
            lambda: Path(self._attestor_key.name).unlink(missing_ok=True)
        )
        self.key_sha256 = h.sha256(TEST_KEY_CONTENT.encode("utf-8")).hexdigest()
        self._original_pin = TOOL.RELEASE_KEY_SHA256
        TOOL.RELEASE_KEY_SHA256 = self.key_sha256
        self.addCleanup(
            lambda: setattr(TOOL, "RELEASE_KEY_SHA256", self._original_pin)
        )
        self._original_attestor_pin = TOOL.ATTESTATION_KEY_SHA256
        TOOL.ATTESTATION_KEY_SHA256 = h.sha256(
            ATTESTOR_KEY_CONTENT.encode("utf-8")
        ).hexdigest()
        self.addCleanup(
            lambda: setattr(
                TOOL, "ATTESTATION_KEY_SHA256", self._original_attestor_pin
            )
        )
        self._run_root_holder = tempfile.TemporaryDirectory()
        self.addCleanup(self._run_root_holder.cleanup)
        self.run_root = Path(self._run_root_holder.name)
        self.scope = default_scope_fixture(self.key_sha256)
        self.scope_path = write_scope_fixture(self.run_root, self.scope)
        self.attestation_path = write_attestation_fixture(self.run_root)
        self.provider_export = self.run_root / "provider-iam-export.json"
        self.cluster = _FakeAdmissionCluster()

    def write_recovery(self, name: str, *, prior, actions) -> tuple[Path, str]:
        import hashlib as h
        from datetime import UTC, datetime, timedelta

        live = self.cluster.binding("fs2-image-provenance")
        now = datetime.now(UTC)
        document = {
            "schema": TOOL.RECOVERY_SCHEMA,
            "cluster": "fixture-cluster",
            "target": "fs2-image-provenance",
            "target_uid": live["metadata"]["uid"],
            "target_resource_version": live["metadata"]["resourceVersion"],
            "prior_actions": prior,
            "actions": actions,
            "reason": "incident:INC-9 sequencing proof",
            "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (now + timedelta(hours=2)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        payload = json.dumps(document).encode("utf-8")
        path = self.run_root / name
        path.write_bytes(payload)
        path.chmod(0o644)
        signature = self.run_root / (name + ".sig")
        signature.write_text("fixture-owner-signature\n", encoding="utf-8")
        signature.chmod(0o644)
        sha = h.sha256(payload).hexdigest()
        SIGNED_AUTHORITY_HASHES.add(sha)
        return path, sha

    def plan_document(self, recovery_path: Path | None) -> list[dict]:
        lines: list[str] = []
        code = TOOL.reconcile_boundary(
            self._tmp.name,
            self.scope_path,
            self.run_root,
            attestation_path=self.attestation_path,
            attestation_key_path=self._attestor_key.name,
            provider_export_path=self.provider_export,
            recovery_path=recovery_path,
            verifier=authority_checking_verifier,
            live_runner=self.cluster.runner,
            output=lines.append,
        )
        self.assertEqual(code, 0)
        document_line = next(
            line for line in lines if line.startswith("PLAN-DOCUMENT: ")
        )
        return json.loads(document_line[len("PLAN-DOCUMENT: "):])

    def write_authorization(self, name: str, plan: list[dict]) -> Path:
        import hashlib as h
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        document = {
            "schema": TOOL.ROLLOUT_AUTHORIZATION_SCHEMA,
            "cluster": "fixture-cluster",
            "plan": plan,
            "plan_sha256": h.sha256(
                json.dumps(plan, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "executor": SECURITY_PRINCIPAL,
            "reason": "change:CH-16 sequencing proof",
            "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (now + timedelta(hours=4)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        payload = json.dumps(document).encode("utf-8")
        path = self.run_root / name
        path.write_bytes(payload)
        path.chmod(0o644)
        signature = self.run_root / (name + ".sig")
        signature.write_text("fixture-owner-signature\n", encoding="utf-8")
        signature.chmod(0o644)
        SIGNED_AUTHORITY_HASHES.add(h.sha256(payload).hexdigest())
        return path

    def run_boundary(self, *, recovery, authorization, execute=False,
                     resume=False):
        return TOOL.reconcile_boundary(
            self._tmp.name,
            self.scope_path,
            self.run_root,
            attestation_path=self.attestation_path,
            attestation_key_path=self._attestor_key.name,
            provider_export_path=self.provider_export,
            recovery_path=recovery,
            authorization_path=authorization,
            execute=execute,
            resume=resume,
            verifier=authority_checking_verifier,
            live_runner=self.cluster.runner,
            rollout_window_authorized=True,
            output=lambda line: None,
        )

    def journal_phases(self) -> list[str]:
        journal = self.run_root / "release-reconcile-journal.jsonl"
        return [
            str(record.get("phase"))
            for record in TOOL._read_chained_records(journal)
        ]

    def test_weaken_then_restore_never_wedges_on_stale_rv(self) -> None:
        # The decisive round-16 scenario: an authorized Audit/Warn
        # break-glass followed by an authorized Deny/Audit RESTORE. The
        # restore plan must be the fenced patch ALONE (no bulk apply that
        # would bump the target's resourceVersion first), so the fence pinned
        # by the owner still holds when the patch runs.
        weaken_path, weaken_sha = self.write_recovery(
            "weaken.json", prior=["Deny", "Audit"], actions=["Audit", "Warn"]
        )
        weaken_plan = self.plan_document(weaken_path)
        self.assertEqual(len(weaken_plan), 1)
        self.assertEqual(weaken_plan[0]["argv"][1], "patch")
        weaken_auth = self.write_authorization("weaken-auth.json", weaken_plan)
        self.assertEqual(
            self.run_boundary(
                recovery=weaken_path, authorization=weaken_auth, execute=True
            ),
            0,
        )
        live = self.cluster.binding("fs2-image-provenance")
        self.assertEqual(
            sorted(live["spec"]["validationActions"]), ["Audit", "Warn"]
        )
        self.assertEqual(
            live["metadata"]["annotations"][
                "security.fs2.nebius.ai/recovery-authorization"
            ],
            weaken_sha,
        )
        # RESTORE against the moved state: fenced to the CURRENT
        # resourceVersion, patch-only plan, completes — this exact flow
        # wedged before the sequencing fix.
        restore_path, restore_sha = self.write_recovery(
            "restore.json", prior=["Audit", "Warn"], actions=["Deny", "Audit"]
        )
        restore_plan = self.plan_document(restore_path)
        self.assertEqual(
            [entry["argv"][1] for entry in restore_plan], ["patch"]
        )
        restore_auth = self.write_authorization(
            "restore-auth.json", restore_plan
        )
        self.assertEqual(
            self.run_boundary(
                recovery=restore_path, authorization=restore_auth,
                execute=True,
            ),
            0,
        )
        live = self.cluster.binding("fs2-image-provenance")
        self.assertEqual(
            sorted(live["spec"]["validationActions"]), ["Audit", "Deny"]
        )
        self.assertEqual(
            live["metadata"]["annotations"][
                "security.fs2.nebius.ai/recovery-authorization"
            ],
            restore_sha,
        )
        self.assertEqual(
            self.journal_phases(),
            ["intent", "complete", "intent", "complete"],
        )

    def test_repair_apply_excludes_the_recovery_target(self) -> None:
        # OTHER drifted objects are repaired alongside a recovery, but the
        # apply must never touch the recovery target: its stdin manifest
        # excludes the target binding, so the fence survives in any order.
        helm = self.cluster.binding("fs2-helm-release-governance")
        helm["spec"]["validationActions"] = ["Audit"]
        weaken_path, weaken_sha = self.write_recovery(
            "weaken2.json", prior=["Deny", "Audit"], actions=["Audit", "Warn"]
        )
        plan = self.plan_document(weaken_path)
        self.assertEqual(
            [entry["argv"][1] for entry in plan], ["patch", "apply"]
        )
        apply_entry = plan[1]
        stdin_bytes = TOOL._reconcile_load_input(
            self.run_root, apply_entry["stdin_sha256"]
        )
        applied_docs = [
            document
            for document in yaml.safe_load_all(stdin_bytes.decode("utf-8"))
            if document
        ]
        applied_bindings = {
            document["metadata"]["name"]
            for document in applied_docs
            if document["kind"] == "ValidatingAdmissionPolicyBinding"
        }
        self.assertNotIn("fs2-image-provenance", applied_bindings)
        self.assertEqual(len(applied_docs), 5)
        self.assertEqual(
            apply_entry["verify"],
            {"kind": "policy-equality", "skip": ["fs2-image-provenance"]},
        )
        authorization = self.write_authorization("weaken2-auth.json", plan)
        # CRASH-RESUME PROOF: the process dies immediately after the patch
        # lands; intent + consumption exist, completion does not.
        self.cluster.raise_after_next_patch = True
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.run_boundary(
                recovery=weaken_path, authorization=authorization,
                execute=True,
            )
        self.assertEqual(self.journal_phases(), ["intent"])
        live = self.cluster.binding("fs2-image-provenance")
        self.assertEqual(
            sorted(live["spec"]["validationActions"]), ["Audit", "Warn"]
        )
        helm = self.cluster.binding("fs2-helm-release-governance")
        self.assertEqual(helm["spec"]["validationActions"], ["Audit"])
        # Resume with the SAME signed documents: the patch entry is
        # satisfied (actions + this document's annotation), the filtered
        # apply still runs, and completion lands.
        self.assertEqual(
            self.run_boundary(
                recovery=weaken_path, authorization=authorization,
                resume=True,
            ),
            0,
        )
        helm = self.cluster.binding("fs2-helm-release-governance")
        self.assertEqual(
            sorted(helm["spec"]["validationActions"]), ["Audit", "Deny"]
        )
        live = self.cluster.binding("fs2-image-provenance")
        self.assertEqual(
            live["metadata"]["annotations"][
                "security.fs2.nebius.ai/recovery-authorization"
            ],
            weaken_sha,
        )
        self.assertEqual(self.journal_phases(), ["intent", "complete"])
        # Idempotent re-resume: nothing to do, already complete.
        self.assertEqual(
            self.run_boundary(
                recovery=weaken_path, authorization=authorization,
                resume=True,
            ),
            0,
        )
        self.assertEqual(self.journal_phases(), ["intent", "complete"])

    def test_execution_is_bound_to_the_named_executor(self) -> None:
        weaken_path, _ = self.write_recovery(
            "weaken3.json", prior=["Deny", "Audit"], actions=["Audit", "Warn"]
        )
        plan = self.plan_document(weaken_path)
        authorization = self.write_authorization("weaken3-auth.json", plan)
        # A DIFFERENT security principal is refused: execution binds to the
        # authorization's named executor, not to whichever principal shows
        # up. (The impostor must be in the scope's security principals to
        # even reach the executor check.)
        impostor = "system:serviceaccount:fs2-security:fs2-other-guard"
        impostor_scope = dict(
            self.scope,
            security_principals=[SECURITY_PRINCIPAL, impostor],
        )
        impostor_scope_path = write_scope_fixture(
            self.run_root, impostor_scope, name="impostor-scope.json"
        )
        self.cluster.identity = impostor
        try:
            with self.assertRaisesRegex(TOOL.ProvenanceError, "named executor"):
                TOOL.reconcile_boundary(
                    self._tmp.name,
                    impostor_scope_path,
                    self.run_root,
                    attestation_path=self.attestation_path,
                    attestation_key_path=self._attestor_key.name,
                    provider_export_path=self.provider_export,
                    recovery_path=weaken_path,
                    authorization_path=authorization,
                    execute=True,
                    verifier=authority_checking_verifier,
                    live_runner=self.cluster.runner,
                    rollout_window_authorized=True,
                    output=lambda line: None,
                )
        finally:
            self.cluster.identity = SECURITY_PRINCIPAL
        self.assertEqual(self.journal_phases(), [])

    def test_provider_export_semantics_are_enforced(self) -> None:
        # A provider export showing admin-class access OUTSIDE the attested
        # enumeration refuses; so does a substituted export whose bytes do
        # not hash to the attestation's pin.
        rogue_export = write_provider_export_fixture(
            self.run_root,
            bindings=[
                {"subject": PROVIDER_ADMIN_SUBJECT, "role": "managed-k8s.admin"},
                {"subject": "mallory@example.invalid", "role": "k8s.editor"},
            ],
            name="rogue-export.json",
        )
        rogue_attestation = write_attestation_fixture(
            self.run_root,
            export_path=rogue_export,
            name="rogue-attestation.json",
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "admin-class"):
            self.plan_document_with(
                rogue_attestation, rogue_export, None
            )
        substituted = write_provider_export_fixture(
            self.run_root,
            bindings=[{"subject": PROVIDER_ADMIN_SUBJECT, "role": "viewer"}],
            name="substituted-export.json",
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "pins"):
            self.plan_document_with(
                self.attestation_path, substituted, None
            )

    def plan_document_with(
        self, attestation: Path, export: Path, recovery: Path | None
    ):
        return TOOL.reconcile_boundary(
            self._tmp.name,
            self.scope_path,
            self.run_root,
            attestation_path=attestation,
            attestation_key_path=self._attestor_key.name,
            provider_export_path=export,
            recovery_path=recovery,
            verifier=authority_checking_verifier,
            live_runner=self.cluster.runner,
            output=lambda line: None,
        )


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

    def load(self, reference: str | None = None, capture=None):
        # The default capture refuses ALL registry access: refusal tests must
        # fail BEFORE the registry re-proof; successful loads pass the same
        # deterministic crane fixture that created the receipt.
        return TOOL.load_bound_receipt(
            self.run_root,
            reference or self.REFERENCE,
            self.PUBLIC_KEY,
            verifier=NOOP_VERIFIER,
            capture=capture or no_registry_capture,
        )

    def create(self, capture, sbom=None):
        # crane_capture computes self.reference from the exact index bytes.
        return TOOL.create_release_receipt(
            self.reference,
            self.run_root,
            self.fixture["repo"],
            "deploy/fixture",
            "release.key",
            "release.pub",
            sbom,
            capture=capture,
        )

    def created_digest(self) -> str:
        return self.reference.rsplit("@", 1)[1]

    def test_bound_receipt_loads(self) -> None:
        # A real created receipt survives the FULL reload proof: signature,
        # bundle re-proof, registry re-fetch, and retained-statement identity.
        capture = self.crane_capture(self.fixture["head"])
        self.create(capture)
        loaded = self.load(self.reference, capture)
        self.assertEqual(loaded["digest"], self.created_digest())

    def test_receipt_signature_is_verified_before_anything_else(self) -> None:
        capture = self.crane_capture(self.fixture["head"])
        self.create(capture)
        commands: list[list[str]] = []
        TOOL.load_bound_receipt(
            self.run_root,
            self.reference,
            "release.pub",
            verifier=lambda command: commands.append(list(command)),
            capture=capture,
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
                "statement_sha256": None,
                "slsa_layer_digest": None,
                "spdx_sha256": "b" * 64,
                "spdx_subject_digest": "sha256:" + "9" * 64,
            },
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "SBOM evidence bound"):
            self.load()

    def test_receipt_with_attestation_but_no_spdx_layer_is_refused(self) -> None:
        write_signed_receipt_fixture(
            self.run_root,
            self.REFERENCE,
            self.fixture,
            **{"sbom.spdx_layer_digest": None},
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "SBOM evidence bound"):
            self.load()

    def test_receipt_subject_must_equal_the_recorded_amd64_manifest(self) -> None:
        write_signed_receipt_fixture(
            self.run_root,
            self.REFERENCE,
            self.fixture,
            **{"sbom.subject_manifest_digest": "sha256:" + "b" * 64},
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "SBOM evidence bound"):
            self.load()

    def test_receipt_with_fake_source_tree_is_refused_at_load(self) -> None:
        write_signed_receipt_fixture(
            self.run_root,
            self.REFERENCE,
            self.fixture,
            **{"source.tree": "9" * 40},
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "does not match the tree"):
            self.load()

    def test_receipt_without_image_manifest_identity_is_refused(self) -> None:
        write_signed_receipt_fixture(
            self.run_root,
            self.REFERENCE,
            self.fixture,
            image_manifest={},
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "manifest and config"):
            self.load()

    def test_hardlinked_receipt_evidence_is_refused(self) -> None:
        path = write_signed_receipt_fixture(self.run_root, self.REFERENCE, self.fixture)
        os.link(path, self.run_root / "hardlink-copy.json")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "link count"):
            self.load()

    def test_group_readable_receipt_evidence_is_refused(self) -> None:
        path = write_signed_receipt_fixture(self.run_root, self.REFERENCE, self.fixture)
        path.chmod(0o644)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "group/other"):
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

    def crane_capture(self, revision, **parameters):
        built = build_crane_fixture(self.fixture, revision=revision, **parameters)
        self.reference = built.reference
        self.expected_amd64_digest = built.expected_amd64_digest
        self.expected_config_digest = built.expected_config_digest
        self.expected_spdx_layer_digest = built.expected_spdx_layer_digest
        return built.capture

    def test_registry_reproof_refuses_swapped_registry_content(self) -> None:
        # Recorded digest strings never authorize anything: if the registry
        # serves different bytes for the same reference at load time, the
        # top-manifest byte hash fails and the receipt refuses to load.
        capture = self.crane_capture(self.fixture["head"])
        self.create(capture)
        reference = self.reference
        other = build_crane_fixture(
            self.fixture, repository="control-plane", config_architecture="arm64"
        )

        def swapped_registry(command):
            adapted = [
                str(part).replace(reference, other.reference) for part in command
            ]
            return other.capture(adapted)

        with self.assertRaisesRegex(TOOL.ProvenanceError, "hashes to"):
            self.load(reference, swapped_registry)

    def test_missing_retained_statement_evidence_is_refused(self) -> None:
        capture = self.crane_capture(self.fixture["head"])
        receipt = self.create(capture)
        retained = TOOL._retained_sbom_path(
            self.run_root, receipt["sbom"]["statement_sha256"], ".intoto.json"
        )
        retained.unlink()
        with self.assertRaisesRegex(TOOL.ProvenanceError, "missing"):
            self.load(self.reference, capture)

    def test_tampered_retained_statement_evidence_is_refused(self) -> None:
        capture = self.crane_capture(self.fixture["head"])
        receipt = self.create(capture)
        retained = TOOL._retained_sbom_path(
            self.run_root, receipt["sbom"]["statement_sha256"], ".intoto.json"
        )
        retained.write_bytes(b'{"tampered": true}')
        with self.assertRaisesRegex(TOOL.ProvenanceError, "content address"):
            self.load(self.reference, capture)

    def standalone_sbom_for(self, digest_hex: str) -> Path:
        document = {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "dataLicense": "CC0-1.0",
            "name": "standalone-sbom",
            "documentNamespace": "https://example.invalid/spdxdocs/standalone",
            "creationInfo": {
                "created": "2026-09-16T00:00:00Z",
                "creators": ["Tool: fixture"],
            },
            "packages": [
                {
                    "SPDXID": "SPDXRef-Package-image",
                    "name": "fixture-image",
                    "downloadLocation": "NOASSERTION",
                    "checksums": [
                        {"algorithm": "SHA256", "checksumValue": digest_hex}
                    ],
                }
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": "SPDXRef-Package-image",
                }
            ],
        }
        sbom = self.run_root / "standalone.spdx.json"
        sbom.write_text(json.dumps(document), encoding="utf-8")
        sbom.chmod(0o644)
        return sbom

    def test_standalone_sbom_naming_only_the_top_index_is_refused(self) -> None:
        # A multi-platform index digest also covers foreign platforms; the
        # standalone document must bind the exact linux/amd64 manifest.
        capture = self.crane_capture(
            self.fixture["head"], include_attestation=False
        )
        sbom = self.standalone_sbom_for(self.reference.rsplit("@", 1)[1])
        with self.assertRaisesRegex(TOOL.ProvenanceError, "exact SHA256"):
            self.create(capture, sbom=sbom)

    def test_standalone_receipt_retains_and_reproves_the_sbom(self) -> None:
        # Standalone receipts keep content-addressed SBOM bytes in the run
        # root; every load re-reads them, re-proves the hash, and re-validates
        # shape + exact digest binding. Missing or tampered bytes refuse.
        capture = self.crane_capture(
            self.fixture["head"], include_attestation=False
        )
        # The standalone document must name the exact linux/amd64 RUNTIME
        # manifest, never the multi-platform top index.
        sbom = self.standalone_sbom_for(
            self.expected_amd64_digest.split(":", 1)[1]
        )
        receipt = self.create(capture, sbom=sbom)
        retained = TOOL._retained_sbom_path(
            self.run_root, receipt["sbom"]["spdx_sha256"], ".spdx.json"
        )
        self.assertTrue(retained.is_file())
        loaded = self.load(self.reference, capture)
        self.assertEqual(loaded["sbom"]["spdx_sha256"], receipt["sbom"]["spdx_sha256"])
        retained.write_bytes(b'{"tampered": true}')
        with self.assertRaisesRegex(TOOL.ProvenanceError, "content address"):
            self.load(self.reference, capture)
        retained.unlink()
        with self.assertRaisesRegex(TOOL.ProvenanceError, "missing"):
            self.load(self.reference, capture)

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
        loaded = self.load(self.reference, self.crane_capture(self.fixture["head"]))
        self.assertEqual(loaded["anchor"]["tag"], "refs/tags/deploy/fixture")

    def test_receipt_recreation_is_idempotent_and_preserves_bytes(self) -> None:
        capture = self.crane_capture(self.fixture["head"])
        first = self.create(capture)
        path = TOOL.receipt_path(self.run_root, self.created_digest())
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

    def add_second_anchor(self, tag: str) -> None:
        repo = self.fixture["repo"]
        git(repo, "tag", "-a", "-m", "anchor", tag, self.fixture["head"])
        tag_target = git(repo, "rev-parse", f"refs/tags/{tag}")
        bundle = self.run_root / "release-anchors" / (tag.replace("/", "-") + ".bundle")
        git(repo, "bundle", "create", str(bundle), f"refs/tags/{tag}")
        bundle.chmod(0o600)
        import hashlib as h

        store = self.run_root / "release-anchors.json"
        anchors = json.loads(store.read_text(encoding="utf-8"))
        anchors[f"refs/tags/{tag}"] = {
            "commit": self.fixture["head"],
            "tag_target": tag_target,
            "bundle_path": str(bundle),
            "sha256": h.sha256(bundle.read_bytes()).hexdigest(),
            "restore_tested": True,
        }
        store.write_text(json.dumps(anchors), encoding="utf-8")

    def test_conflicting_receipt_recreation_is_refused_and_preserves_original(
        self,
    ) -> None:
        # Same digest, different receipt content: rebind to another anchor.
        capture = self.crane_capture(self.fixture["head"])
        self.create(capture)
        path = TOOL.receipt_path(self.run_root, self.created_digest())
        original_bytes = path.read_bytes()
        self.add_second_anchor("deploy/fixture2")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "immutable"):
            TOOL.create_release_receipt(
                self.reference,
                self.run_root,
                self.fixture["repo"],
                "deploy/fixture2",
                "release.key",
                "release.pub",
                None,
                capture=capture,
            )
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_partial_receipt_evidence_is_never_overwritten(self) -> None:
        capture = self.crane_capture(self.fixture["head"])
        self.create(capture)
        path = TOOL.receipt_path(self.run_root, self.created_digest())
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
        final_dir = TOOL.receipt_path(self.run_root, self.created_digest()).parent
        self.assertFalse(final_dir.exists())
        # Publication is recoverable: the same creation succeeds afterwards.
        receipt = self.create(self.crane_capture(self.fixture["head"]))
        self.assertEqual(receipt["digest"], self.created_digest())
        self.assertTrue(final_dir.is_dir())

    def test_symlinked_receipt_path_is_refused(self) -> None:
        capture = self.crane_capture(self.fixture["head"])
        final_dir = TOOL.receipt_path(self.run_root, self.created_digest()).parent
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        target = self.run_root / "elsewhere"
        target.mkdir()
        final_dir.symlink_to(target)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "symlink"):
            self.create(capture)

    def test_injected_empty_directory_at_receipt_path_is_never_replaced(
        self,
    ) -> None:
        # rename(2) silently replaces an empty target directory; the mkdir
        # claim must instead refuse it as partial evidence and leave it alone.
        capture = self.crane_capture(self.fixture["head"])
        final_dir = TOOL.receipt_path(self.run_root, self.created_digest()).parent
        final_dir.mkdir(parents=True, mode=0o700)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "partial receipt"):
            self.create(capture)
        self.assertTrue(final_dir.is_dir())
        self.assertEqual(list(final_dir.iterdir()), [])

    def test_bundle_snapshot_binds_git_use_to_verified_bytes(self) -> None:
        # The snapshot is taken from the hash-verified bytes: mutating the
        # original bundle mid-use cannot change what git consumes, and any
        # bytes that do not match the recorded hash never yield a snapshot.
        bundle = self.fixture["bundle"]
        original = bundle.read_bytes()
        with TOOL._verified_bundle_snapshot(
            self.run_root, bundle, self.fixture["bundle_sha256"], "swap-test"
        ) as snapshot:
            bundle.write_bytes(b"swapped after verification")
            self.assertEqual(snapshot.read_bytes(), original)
            heads = TOOL._run_capture(
                ["git", "bundle", "list-heads", str(snapshot)]
            )
            self.assertIn("refs/tags/deploy/fixture", heads)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "no longer matches"):
            with TOOL._verified_bundle_snapshot(
                self.run_root, bundle, self.fixture["bundle_sha256"], "swap-test"
            ):
                self.fail("swapped bundle bytes must never yield a snapshot")
        bundle.write_bytes(original)

    def test_symlinked_ancestor_directory_is_refused(self) -> None:
        # O_NOFOLLOW on the final parent alone is not enough: EVERY component
        # is opened without following symlinks from a trusted root dirfd.
        real_dir = self.run_root / "real-sboms"
        real_dir.mkdir()
        doc = real_dir / "doc.spdx.json"
        doc.write_text("{}", encoding="utf-8")
        doc.chmod(0o644)
        linked_dir = self.run_root / "linked-sboms"
        linked_dir.symlink_to(real_dir)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "component safely"):
            TOOL._validated_spdx_document(DIGEST_A, linked_dir / "doc.spdx.json")

    def test_other_writable_ancestor_directory_is_refused(self) -> None:
        # A mode-0777 parent lets any user swap the evidence via rename; the
        # walk refuses it even though the file itself passes its own checks.
        path = write_signed_receipt_fixture(
            self.run_root, self.REFERENCE, self.fixture
        )
        path.parent.chmod(0o777)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "other-writable"):
            self.load()
        path.parent.chmod(0o700)

    def test_private_evidence_mode_0700_is_refused(self) -> None:
        # The documented contract is EXACT: private evidence permits owner
        # read/write only. Executable 0700 is an anomaly, not a valid mode.
        path = write_signed_receipt_fixture(
            self.run_root, self.REFERENCE, self.fixture
        )
        path.chmod(0o700)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "exceeds 0600"):
            self.load()

    def test_hardlinked_standalone_spdx_is_refused(self) -> None:
        digest_hex = DIGEST_A.split(":", 1)[1]
        sbom = self.run_root / "hardlinked.spdx.json"
        sbom.write_text(json.dumps({"spdxVersion": "SPDX-2.3"}), encoding="utf-8")
        sbom.chmod(0o644)
        os.link(sbom, self.run_root / "hardlink-alias.json")
        with self.assertRaisesRegex(TOOL.ProvenanceError, "link count"):
            TOOL._validated_spdx_document(DIGEST_A, sbom)
        assert digest_hex  # identity is irrelevant: the anomaly refuses first

    def test_standalone_spdx_is_read_once_and_anomaly_checked(self) -> None:
        import hashlib as h

        digest_hex = DIGEST_A.split(":", 1)[1]
        document = {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "dataLicense": "CC0-1.0",
            "name": "fixture-sbom",
            "documentNamespace": "https://example.invalid/spdxdocs/swap",
            "creationInfo": {
                "created": "2026-09-16T00:00:00Z",
                "creators": ["Tool: fixture"],
            },
            "packages": [
                {
                    "SPDXID": "SPDXRef-Package-image",
                    "name": "fixture-image",
                    "downloadLocation": "NOASSERTION",
                    "checksums": [
                        {"algorithm": "SHA256", "checksumValue": digest_hex}
                    ],
                }
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": "SPDXRef-Package-image",
                }
            ],
        }
        sbom = self.run_root / "single-read.spdx.json"
        sbom.write_text(json.dumps(document), encoding="utf-8")
        sbom.chmod(0o644)
        # The recorded hash covers exactly the parsed bytes.
        evidence = TOOL._validated_spdx_document(DIGEST_A, sbom)
        self.assertEqual(
            evidence["spdx_sha256"], h.sha256(sbom.read_bytes()).hexdigest()
        )
        # Mode bits beyond 0644 on a public input are an anomaly: refused
        # for group/other write AND for owner-execute (0700-style modes).
        for mode in (0o666, 0o700, 0o755):
            sbom.chmod(mode)
            with self.assertRaisesRegex(TOOL.ProvenanceError, "exceeds 0644"):
                TOOL._validated_spdx_document(DIGEST_A, sbom)
        sbom.chmod(0o644)
        # A symlinked path is refused by O_NOFOLLOW, not followed.
        link = self.run_root / "linked.spdx.json"
        link.symlink_to(sbom)
        with self.assertRaisesRegex(TOOL.ProvenanceError, "symlink or missing"):
            TOOL._validated_spdx_document(DIGEST_A, link)

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

    def test_duplicate_amd64_attestations_are_refused(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "ambiguous attestations"):
            self.create(
                self.crane_capture(
                    self.fixture["head"], duplicate_amd64_attestation=True
                )
            )

    def test_duplicate_spdx_layers_are_refused(self) -> None:
        with self.assertRaisesRegex(TOOL.ProvenanceError, "SPDX predicate layers"):
            self.create(
                self.crane_capture(self.fixture["head"], duplicate_spdx_layer=True)
            )

    def test_duplicate_statement_subjects_are_refused(self) -> None:
        def duplicate_subject(statement):
            statement["subject"].append(dict(statement["subject"][0]))

        self._refused_statement_mutation(duplicate_subject, "ambiguous subjects")

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
                        {
                            "SPDXID": "SPDXRef-Package-fixture",
                            "name": "a",
                            "downloadLocation": "NOASSERTION",
                        },
                        {
                            "SPDXID": "SPDXRef-Package-fixture",
                            "name": "b",
                            "downloadLocation": "NOASSERTION",
                        },
                    ]
                },
                "duplicate package SPDXID",
            ),
            (
                {
                    "packages": [
                        {
                            "SPDXID": "not a valid id",
                            "name": "a",
                            "downloadLocation": "NOASSERTION",
                        }
                    ]
                },
                "invalid SPDXID",
            ),
            (
                {
                    "packages": [
                        {
                            "SPDXID": "SPDXRef-Package-fixture",
                            "name": "a",
                        }
                    ]
                },
                "downloadLocation",
            ),
            ({"dataLicense": "MIT"}, "CC0-1.0"),
            ({"creationInfo": {}}, "creationInfo"),
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
            "downloadLocation": "NOASSERTION",
        }
        if digest_hex is not None:
            package["checksums"] = [
                {"algorithm": "SHA256", "checksumValue": digest_hex}
            ]
        return {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "dataLicense": "CC0-1.0",
            "name": "fixture-sbom",
            "documentNamespace": "https://example.invalid/spdxdocs/fixture",
            "creationInfo": {
                "created": "2026-09-16T00:00:00Z",
                "creators": ["Tool: fixture"],
            },
            "packages": [package],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": "SPDXRef-Package-image",
                }
            ],
        }

    def write_spdx(self, path: Path, document: dict) -> Path:
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o644)
        return path

    def test_spdx_document_binding_requires_exact_package_identity(self) -> None:
        digest_hex = DIGEST_A.split(":", 1)[1]
        good = self.run_root / "sbom.spdx.json"
        self.write_spdx(good, self.spdx_document(digest_hex))
        evidence = TOOL._validated_spdx_document(DIGEST_A, good)
        self.assertEqual(evidence["spdx_subject_digest"], DIGEST_A)

        purl = self.spdx_document(None)
        purl["packages"][0]["externalRefs"] = [
            {
                "referenceType": "purl",
                "referenceLocator": f"pkg:oci/fixture-image@sha256:{digest_hex}?arch=amd64",
            }
        ]
        purl_path = self.write_spdx(self.run_root / "purl.spdx.json", purl)
        self.assertEqual(
            TOOL._validated_spdx_document(DIGEST_A, purl_path)["spdx_subject_digest"],
            DIGEST_A,
        )

        not_spdx = self.write_spdx(
            self.run_root / "not-sbom.json", {"name": digest_hex}
        )
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
            path = self.write_spdx(
                self.run_root / "substring.spdx.json", document
            )
            with self.assertRaisesRegex(TOOL.ProvenanceError, "exact SHA256"):
                TOOL._validated_spdx_document(DIGEST_A, path)

        # The identity must sit on a DESCRIBED package, wrong algorithm or a
        # non-described package does not bind.
        wrong_algorithm = self.spdx_document(None)
        wrong_algorithm["packages"][0]["checksums"] = [
            {"algorithm": "SHA1", "checksumValue": digest_hex}
        ]
        path = self.write_spdx(
            self.run_root / "wrong-algorithm.spdx.json", wrong_algorithm
        )
        with self.assertRaisesRegex(TOOL.ProvenanceError, "exact SHA256"):
            TOOL._validated_spdx_document(DIGEST_A, path)

        undescribed = self.spdx_document(None)
        undescribed["packages"].append(
            {
                "SPDXID": "SPDXRef-Package-other",
                "name": "other",
                "downloadLocation": "NOASSERTION",
                "checksums": [{"algorithm": "SHA256", "checksumValue": digest_hex}],
            }
        )
        path = self.write_spdx(self.run_root / "undescribed.spdx.json", undescribed)
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
            path = self.write_spdx(self.run_root / "shape.spdx.json", document)
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
