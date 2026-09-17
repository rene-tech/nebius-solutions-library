"""The Terraform-to-chart wiring for the dedicated scientific artifact store.

Three separate files have to agree for the store to work at all: the
infrastructure stage that creates the bucket and tenant principals, the workloads
stage that projects broker identity and chart values, and the control-plane chart
that declares those values. A name that one side emits and another does not
declare fails silently, so the seam is asserted here rather than trusted.

`scientific-artifacts/artifact-store-contract.json` is that seam written down.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

DEPLOY_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = DEPLOY_ROOT / "scientific-artifacts/artifact-store-contract.json"
INFRASTRUCTURE = DEPLOY_ROOT / "stages/infrastructure/scientific_artifacts.tf"
INFRASTRUCTURE_OUTPUTS = DEPLOY_ROOT / "stages/infrastructure/outputs.tf"
INFRASTRUCTURE_VARIABLES = DEPLOY_ROOT / "stages/infrastructure/variables.tf"
WORKLOADS = DEPLOY_ROOT / "stages/workloads/scientific_artifacts.tf"
WORKLOADS_VARIABLES = DEPLOY_ROOT / "stages/workloads/variables.tf"
CONTROL_PLANE = DEPLOY_ROOT / "stages/workloads/control_plane.tf"
ROOT_VARIABLES = DEPLOY_ROOT / "variables.tf"
ROOT_LOCALS = DEPLOY_ROOT / "locals.tf"
ROOT_MAIN = DEPLOY_ROOT / "main.tf"
STACK = DEPLOY_ROOT / "inference-stack"
CHART = DEPLOY_ROOT / "charts/control-plane/fs2-serve-control-plane"

TERRAFORM_SOURCES = (
    INFRASTRUCTURE,
    INFRASTRUCTURE_OUTPUTS,
    WORKLOADS,
    WORKLOADS_VARIABLES,
    CONTROL_PLANE,
    ROOT_VARIABLES,
    ROOT_LOCALS,
)


def block(text: str, opening: str) -> str:
    """Return the balanced `{...}` body that follows `opening`."""

    start = text.index(opening) + len(opening)
    depth = 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
    raise AssertionError(f"unbalanced block for {opening!r}")


def assigned_names(body: str) -> set[str]:
    """Names assigned at the top level of one HCL block body."""

    names: set[str] = set()
    depth = 0
    for line in body.splitlines():
        stripped = line.strip()
        if depth == 0:
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
            if match:
                names.add(match.group(1))
        depth += line.count("{") + line.count("[") - line.count("}") - line.count("]")
    return names


class ArtifactStoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        cls.infrastructure = INFRASTRUCTURE.read_text(encoding="utf-8")
        cls.infrastructure_outputs = INFRASTRUCTURE_OUTPUTS.read_text(encoding="utf-8")
        cls.infrastructure_variables = INFRASTRUCTURE_VARIABLES.read_text(encoding="utf-8")
        cls.workloads = WORKLOADS.read_text(encoding="utf-8")
        cls.workloads_variables = WORKLOADS_VARIABLES.read_text(encoding="utf-8")
        cls.control_plane = CONTROL_PLANE.read_text(encoding="utf-8")
        cls.root_variables = ROOT_VARIABLES.read_text(encoding="utf-8")
        cls.root_locals = ROOT_LOCALS.read_text(encoding="utf-8")
        cls.root_main = ROOT_MAIN.read_text(encoding="utf-8")
        cls.stack = STACK.read_text(encoding="utf-8")


class ChartValueWiringTests(ArtifactStoreContractTests):
    def emitted(self, opening: str) -> set[str]:
        return assigned_names(block(self.workloads, opening))

    def test_terraform_emits_exactly_the_canonical_artifact_values(self) -> None:
        self.assertEqual(
            self.emitted("scientificArtifacts = {"),
            set(self.contract["chart"]["scientificArtifacts"]),
        )

    def test_terraform_emits_exactly_the_canonical_batch_gates(self) -> None:
        self.assertEqual(
            self.emitted("scientificBatch = {"),
            set(self.contract["chart"]["scientificBatch"]),
        )

    def test_terraform_emits_the_canonical_broker_reference(self) -> None:
        broker = self.contract["credential_broker"]
        self.assertIn(
            f'"https://${{local.scientific_artifact_broker_name}}-{{tenant_hash}}.fs2-system.svc:${{local.scientific_artifact_broker_port}}/v1"',
            self.workloads,
        )
        self.assertEqual(broker["service_template"], "fs2-artifact-{tenant_hash}")
        self.assertIn("scientific_artifact_provider_bindings", self.workloads)
        self.assertIn("service_account_token", self.workloads)
        legacy = block(
            self.workloads,
            'resource "kubernetes_secret_v1" "scientific_artifact_store" {',
        )
        self.assertIn('name      = "fs2-serve-artifact-store"', legacy)
        self.assertIn('"fs2.nebius.ai/authorization" = "none"', legacy)
        broker = block(
            self.workloads,
            'resource "kubernetes_deployment_v1" "scientific_artifact_broker" {',
        )
        self.assertNotIn("scientific_artifact_store", broker)

    def test_the_egress_allowlist_and_rollout_annotation_reach_the_chart(self) -> None:
        self.assertIn("artifactStoreCidrs", self.workloads)
        self.assertIn(self.contract["credential_broker"]["provider_bindings_annotation"], self.workloads)
        # podAnnotations is an existing declared chart value rendered into the
        # control-plane pod template, so a rotation restarts the deployment.
        self.assertIn("podAnnotations = {", self.workloads)

    def test_the_overrides_are_appended_to_the_control_plane_release(self) -> None:
        self.assertIn("yamlencode(local.scientific_chart_overrides)", self.control_plane)
        self.assertIn("kubernetes_deployment_v1.scientific_artifact_broker", self.control_plane)

    def test_the_obsolete_artifact_service_wiring_is_not_revived(self) -> None:
        for forbidden in self.contract["chart"]["forbidden_values"]:
            for source in TERRAFORM_SOURCES:
                with self.subTest(value=forbidden, source=source.name):
                    self.assertNotIn(forbidden, source.read_text(encoding="utf-8"))

    def test_the_chart_declares_every_emitted_value_once_it_ships_them(self) -> None:
        # The control-plane chart is owned by the batch-controller workstream.
        # Assert agreement as soon as it declares these values, so the seam
        # cannot drift after that work merges.
        schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
        declared = schema.get("properties", {})
        if "scientificArtifacts" not in declared:
            self.assertNotIn(
                "scientificBatch",
                declared,
                "the chart declares one half of the seam but not the other",
            )
            self.skipTest(
                "control-plane chart does not declare scientificArtifacts yet; "
                "Helm ignores the emitted values until the controller work merges"
            )
        self.assertEqual(
            set(self.contract["chart"]["scientificArtifacts"])
            - set(declared["scientificArtifacts"].get("properties", {})),
            set(),
        )
        self.assertEqual(
            set(self.contract["chart"]["scientificBatch"])
            - set(declared.get("scientificBatch", {}).get("properties", {})),
            set(),
        )


class SecretSafetyTests(ArtifactStoreContractTests):
    def test_the_artifact_plane_isolates_one_provider_key_per_tenant_broker(self) -> None:
        self.assertIn('nebius_iam_v2_access_key" "scientific_artifact_tenant', self.infrastructure)
        self.assertIn("scientific_artifacts_tenant_broker_access", self.infrastructure_outputs)
        self.assertIn(
            '{"active_generation", "authorized_generations", "generations"}',
            (DEPLOY_ROOT / "inference-stack").read_text(encoding="utf-8"),
        )
        self.assertNotIn(
            "fs2-system/fs2-artifact-<sha256(tenant)[0:12]>",
            (DEPLOY_ROOT / "inference-stack").read_text(encoding="utf-8"),
        )
        self.assertIn('credential_mode = "TENANT_ISOLATED_BROKER_KEYS"', self.infrastructure)
        self.assertIn("broker_object_roles = []", self.infrastructure_outputs)
        self.assertIn("quarantined_legacy_identity", self.infrastructure_outputs)
        self.assertIn(
            "bucket_roles = local.scientific_artifacts_legacy_authorized ? local.scientific_artifacts_object_roles : []",
            self.infrastructure_outputs,
        )
        self.assertTrue(self.contract["storage"]["legacy_shared_provider_authorized"])
        self.assertFalse(self.contract["storage"]["ordinary_apply_irreversible_cutover"])

    def test_no_stage_variable_or_output_can_carry_the_object_store_secret(self) -> None:
        for forbidden in self.contract["credential_broker"]["forbidden_handoff_fields"]:
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(forbidden)}\s*=")
            for name, source in (
                ("infrastructure", self.infrastructure),
                ("infrastructure outputs", self.infrastructure_outputs),
                ("workloads variables", self.workloads_variables),
                ("root variables", self.root_variables),
                ("root locals", self.root_locals),
            ):
                with self.subTest(field=forbidden, source=name):
                    self.assertIsNone(pattern.search(source))

    def test_each_broker_mounts_only_its_one_tenant_provider_identity(self) -> None:
        deployment = block(
            self.workloads,
            'resource "kubernetes_deployment_v1" "scientific_artifact_broker" {',
        )
        self.assertIn('name = "provider-identity"', deployment)
        self.assertIn("service_account_token", deployment)
        self.assertIn("scientific_artifact_tenant_broker[each.key]", deployment)
        self.assertIn("FS2_ARTIFACT_BROKER_PROVIDER_ACCESS_KEY_FILE", deployment)
        self.assertIn("FS2_ARTIFACT_BROKER_PROVIDER_SECRET_KEY_FILE", deployment)
        self.assertNotIn("authority_signing_secret_name", deployment)

    def test_retained_broker_generations_roll_out_before_stable_service_switch(self) -> None:
        deployment = block(
            self.workloads,
            'resource "kubernetes_deployment_v1" "scientific_artifact_broker" {',
        )
        service = block(
            self.workloads,
            'resource "kubernetes_service_v1" "scientific_artifact_broker" {',
        )
        generation_service = block(
            self.workloads,
            'resource "kubernetes_service_v1" "scientific_artifact_broker_generation" {',
        )

        self.assertIn("for_each = local.scientific_artifact_all_generation_access", deployment)
        self.assertIn('"fs2.nebius.ai/credential-generation"', deployment)
        self.assertIn('-g${each.value.generation}', deployment)
        self.assertIn('"fs2.nebius.ai/credential-generation" = tostring(each.value.generation)', service)
        self.assertIn(
            "depends_on = [kubernetes_deployment_v1.scientific_artifact_broker]",
            service,
        )
        self.assertIn("for_each = local.scientific_artifact_all_generation_access", generation_service)
        self.assertIn('-g${each.value.generation}', generation_service)
        self.assertIn(
            '"fs2.nebius.ai/credential-generation" = tostring(each.value.generation)',
            generation_service,
        )

    def test_each_tenant_has_an_exact_provider_principal_and_prefix(self) -> None:
        self.assertIn('resource "nebius_iam_v1_service_account" "scientific_artifact_tenant"', self.infrastructure)
        self.assertIn('resource "nebius_iam_v1_group" "scientific_artifact_tenant"', self.infrastructure)
        self.assertIn('resource "nebius_iam_v2_access_key" "scientific_artifact_tenant"', self.infrastructure)
        self.assertIn('path  = "scientific/v1/tenants/${tenant_id}/*"', self.infrastructure)
        self.assertIn("prevent_destroy = true", self.infrastructure)

    def test_the_generated_workloads_handoff_is_shape_checked(self) -> None:
        self.assertIn(
            "scientific artifact provider handoff",
            self.stack,
        )
        for field in self.contract["credential_broker"]["provider_binding_fields"]:
            self.assertIn(f'"{field}"', self.stack)
        self.assertIn("artifact-tenant-broker-access/v2", self.stack)
        self.assertIn("per-tenant credential generations", self.stack)
        self.assertIn('"legacy_quarantine"', self.stack)

    def test_ed25519_private_key_is_issuer_only_and_brokers_receive_public_key(self) -> None:
        issuer_start = self.workloads.index(
            'resource "kubernetes_deployment_v1" "scientific_artifact_authority" {'
        )
        broker_start = self.workloads.index(
            'resource "kubernetes_deployment_v1" "scientific_artifact_broker" {'
        )
        issuer = self.workloads[issuer_start:broker_start]
        broker = self.workloads[broker_start:]
        self.assertIn("FS2_ARTIFACT_AUTHORITY_SIGNING_KEY_FILE", issuer)
        self.assertIn("FS2_ARTIFACT_BROKER_AUTHORITY_VERIFICATION_KEY_FILE", broker)
        self.assertNotIn("FS2_ARTIFACT_AUTHORITY_SIGNING_KEY_FILE", broker)


class BucketProvisioningTests(ArtifactStoreContractTests):
    def test_the_bucket_is_versioned_regional_and_dedicated(self) -> None:
        for resource in ("scientific_artifacts", "scientific_artifacts_disposable"):
            body = block(self.infrastructure, f'resource "nebius_storage_v1_bucket" "{resource}" {{')
            with self.subTest(resource=resource):
                self.assertIn(
                    f'versioning_policy     = "{self.contract["storage"]["versioning_policy"]}"',
                    body,
                )
                self.assertIn(
                    f'default_storage_class = "{self.contract["storage"]["storage_class"]}"',
                    body,
                )
                self.assertIn("var.scientific_artifacts.object_storage.bucket_name", body)
                self.assertIn("lifecycle_configuration = {", body)

    def test_the_broker_permit_is_delete_free_and_scoped_to_the_canonical_prefix(self) -> None:
        storage = self.contract["storage"]
        for role in storage["object_roles"]:
            self.assertIn(f'"{role}"', self.infrastructure)
        self.assertIn('path  = "scientific/v1/tenants/${tenant_id}/*"', self.infrastructure)
        for resource in ("scientific_artifacts", "scientific_artifacts_disposable"):
            body = block(self.infrastructure, f'resource "nebius_storage_v1_bucket" "{resource}" {{')
            self.assertIn("paths    = [binding.path]", body)
            self.assertIn("roles    = local.scientific_artifacts_object_roles", body)
        self.assertNotIn('"storage.object-editor"', self.infrastructure)
        # Project-wide roles would let the key read the model cache and registry.
        self.assertNotIn('role        = "editor"', self.infrastructure)
        self.assertNotIn('role        = "viewer"', self.infrastructure)

    def test_the_lifecycle_never_expires_current_or_noncurrent_object_versions(self) -> None:
        storage = self.contract["storage"]
        rules = block(self.infrastructure, "scientific_artifacts_lifecycle_rules = [")
        for rule_id in storage["lifecycle_rule_ids"]:
            self.assertIn(f'id                                = "{rule_id}"', rules)
        self.assertIn(
            f"days_after_initiation = {storage['abort_incomplete_multipart_upload_days']}",
            rules,
        )
        self.assertIsNone(storage["noncurrent_version_expiration_days"])
        self.assertNotIn("noncurrent_days", rules)
        self.assertNotIn("expire-noncurrent-versions", rules)
        self.assertIn("expired_object_delete_marker = true", rules)
        # An expiration in days would delete live results behind the application.
        self.assertIn("days = null", rules)
        self.assertIn(
            f'current_object_expiration              = "{storage["current_object_expiration"]}"',
            self.infrastructure_outputs,
        )

    def test_retained_and_disposable_storage_are_distinct_resources(self) -> None:
        retained = block(self.infrastructure, 'resource "nebius_storage_v1_bucket" "scientific_artifacts" {')
        disposable = block(
            self.infrastructure, 'resource "nebius_storage_v1_bucket" "scientific_artifacts_disposable" {'
        )
        self.assertIn("prevent_destroy = true", retained)
        self.assertNotIn("prevent_destroy", disposable)
        self.assertIn("count = local.scientific_artifacts_retain ? 1 : 0", retained)
        self.assertIn("count = local.scientific_artifacts_dispose ? 1 : 0", disposable)

    def test_the_reference_data_bucket_is_never_reused_or_broadened(self) -> None:
        self.assertIn(
            "var.scientific_artifacts.object_storage.bucket_name != var.reference_data.object_storage.bucket_name",
            self.infrastructure,
        )
        self.assertIn(
            "local.scientific_artifacts_bucket_name != local.reference_data_bucket_name",
            self.root_main,
        )
        # The reference-data policy keeps its own paths and its own role.
        reference = (DEPLOY_ROOT / "stages/infrastructure/storage.tf").read_text(encoding="utf-8")
        # The result store owns its own file; nothing here creates or widens it.
        self.assertNotIn("scientific_artifacts", reference)
        self.assertNotIn("storage.object-editor", reference)
        self.assertIn('paths    = ["reference-data/*", "inputs/*", "preprocessing/*"]', reference)


class TfvarsSurfaceTests(ArtifactStoreContractTests):
    def test_the_root_surface_exposes_every_required_knob(self) -> None:
        body = block(self.root_variables, "scientific_artifacts = optional(object({")
        for field in (
            "enabled",
            "lifecycle",
            "object_storage",
            "tenant_ids",
            "broker",
            "retention_days",
            "handle_ttl_seconds",
            "max_artifact_bytes",
            "egress_cidrs",
            "media_types",
        ):
            with self.subTest(field=field):
                self.assertIn(field, assigned_names(body) | {"lifecycle", "object_storage"})
        self.assertIn("bucket_name  = optional(string)", body)
        self.assertIn("max_size_gib = optional(number, 4096)", body)
        self.assertIn('retention_mode = optional(string, "disposable")', body)

    def test_the_bucket_name_is_derived_but_overridable(self) -> None:
        self.assertIn(
            'scientific_artifacts_bucket_name = coalesce(\n'
            "    var.deployment.storage.scientific_artifacts.object_storage.bucket_name,\n"
            '    "${var.deployment.name}-${local.run_id}-scientific-artifacts",\n'
            "  )",
            self.root_locals,
        )

    def test_the_default_media_types_are_exact_and_sorted(self) -> None:
        body = block(self.root_variables, "media_types = optional(set(string), [")
        declared = re.findall(r'"([^"]+)"', body)
        self.assertEqual(declared, sorted(declared))
        self.assertIn("application/json", declared)
        self.assertIn("application/x-tar", declared)
        self.assertIn("chemical/x-mmcif", declared)
        self.assertIn("chemical/x-pdb", declared)
        self.assertIn("text/csv", declared)
        self.assertNotIn("*/*", declared)

    def test_an_enabled_store_cannot_have_an_empty_egress_allowlist(self) -> None:
        # alltrue over an empty collection is true, so the allowlist needs its
        # own length check at the root as well as in the stage.
        self.assertIn(
            "length(var.deployment.storage.scientific_artifacts.egress_cidrs) > 0",
            self.root_variables,
        )
        self.assertIn("length(var.scientific_artifacts.egress_cidrs) > 0", self.workloads_variables)

    def test_only_exact_host_addresses_may_be_allow_listed(self) -> None:
        for source in (self.root_variables, self.workloads_variables):
            self.assertIn('endswith(cidr, "/32") || endswith(cidr, "/128")', source)

    def test_write_only_secret_data_requires_terraform_1_11(self) -> None:
        # An older binary treats data_wo as an unknown attribute, which would
        # put credential material back into state, so every root and the
        # wrapper preflight have to agree on the floor.
        for versions in sorted(DEPLOY_ROOT.glob("**/versions.tf")):
            if ".terraform" in versions.parts:
                continue
            with self.subTest(root=versions.relative_to(DEPLOY_ROOT).as_posix()):
                self.assertIn('required_version = ">= 1.11.0, < 2.0.0"', versions.read_text(encoding="utf-8"))
        self.assertIn("MINIMUM_TERRAFORM_VERSION = (1, 11, 0)", self.stack)
        self.assertIn("require_terraform_version(args.terraform)", self.stack)
        self.assertIn(
            "Terraform 1.11 or newer",
            (DEPLOY_ROOT / "README.md").read_text(encoding="utf-8"),
        )

    def test_storage_stays_deployable_while_batch_and_academic_stay_false(self) -> None:
        batch = block(self.root_variables, "scientific_batch = optional(object({")
        self.assertIn("enabled        = optional(bool, false)", batch)
        self.assertIn("writes_enabled = optional(bool, false)", batch)
        # Nothing about the store depends on the batch gates.
        store = block(self.root_variables, "scientific_artifacts = optional(object({")
        self.assertNotIn("scientific_batch", store)
        self.assertNotIn("academic", store)
        self.assertTrue(self.contract["gates"]["storage_independently_deployable"])


class FeatureGateTests(ArtifactStoreContractTests):
    def test_rotation_switch_cannot_revoke_a_retained_generation(self) -> None:
        self.assertIn(
            "setequals(generation.authorized_generations, generation.retained_generations)",
            self.infrastructure_variables,
        )
        self.assertIn(
            "set(generations) != {str(generation) for generation in authorized}",
            self.stack,
        )
        self.assertIn(
            "sum(generation <= active for generation in authorized) != active",
            self.stack,
        )
        self.assertIn(
            "every retained generation still authorized during reversible legacy-overlap",
            self.root_variables,
        )
        self.assertIn("generation.active_generation == 1", self.infrastructure_variables)
        self.assertIn("or active != 1", self.stack)

    def test_ordinary_apply_cannot_create_an_irreversible_authority_cutover(self) -> None:
        self.assertIn(
            "scientific_artifact_irreversible_cutover_authorized = false",
            self.workloads,
        )
        for resource in (
            'resource "kubernetes_service_account_v1" "scientific_artifact_authority_cutover" {',
            'resource "kubernetes_manifest" "scientific_artifact_authority_cutover_network_policy" {',
            'resource "kubernetes_job_v1" "scientific_artifact_authority_cutover" {',
            'resource "kubernetes_deployment_v1" "scientific_artifact_authority_cutover" {',
        ):
            body = block(self.workloads, resource)
            self.assertIn(
                "count = local.scientific_artifact_irreversible_cutover_authorized ? 1 : 0",
                body,
            )
        self.assertIn('migration.phase == "legacy-overlap"', self.root_variables)
        self.assertNotIn(
            'contains(["legacy-overlap", "tenant-broker-active"]',
            self.root_variables,
        )

    def test_batch_requires_the_store_and_writes_require_batch(self) -> None:
        self.assertIn(
            "!var.deployment.scientific_batch.enabled ||\n        var.deployment.storage.scientific_artifacts.enabled",
            self.root_variables,
        )
        self.assertIn(
            "!var.deployment.scientific_batch.writes_enabled ||\n        var.deployment.scientific_batch.enabled",
            self.root_variables,
        )
        self.assertIn(
            "!var.scientific_batch.enabled || var.scientific_artifacts.enabled",
            self.workloads,
        )
        self.assertIn(
            "!var.scientific_batch.writes_enabled || var.scientific_batch.enabled",
            self.workloads,
        )

    def test_the_orchestrator_refuses_an_ungated_batch_before_any_apply(self) -> None:
        self.assertIn(
            "staged scientific batch execution requires the dedicated artifact store",
            self.stack,
        )
        self.assertIn(
            "scientific batch Kubernetes writes require the batch controller gate",
            self.stack,
        )

    def test_the_workloads_stage_pins_the_exact_infrastructure_contract(self) -> None:
        body = block(self.workloads_variables, 'variable "scientific_artifacts" {')
        self.assertIn('"fs2-serve.nebius.ai/scientific-artifact-storage/v1"', body)
        self.assertIn("storage_contract.writer.roles", body)
        self.assertIn('"storage.uploader", "storage.object-viewer", "storage.object-lister"', body)
        self.assertIn('!contains(var.scientific_artifacts.storage_contract.writer.roles, "storage.object-editor")', body)
        self.assertIn('writer.credential_mode == "TENANT_ISOLATED_BROKER_KEYS"', body)
        self.assertIn('join(",", principal.paths) == "scientific/v1/tenants/${tenant_id}/*"', body)
        self.assertIn("length(var.scientific_artifacts.storage_contract.writer.broker_object_roles) == 0", body)
        self.assertIn('layout.root == "scientific/v1"', body)


class LayoutAgreementTests(ArtifactStoreContractTests):
    def test_terraform_publishes_the_same_object_key_template(self) -> None:
        template = self.contract["object_layout"]["object_key"]
        root = self.contract["object_layout"]["root"]
        # The infrastructure output builds the same template from the root local.
        self.assertIn(
            "${local.scientific_artifacts_root}" + template[len(root):],
            self.infrastructure_outputs,
        )
        self.assertIn(template, (DEPLOY_ROOT / "outputs.tf").read_text(encoding="utf-8"))

    def test_the_writer_scope_covers_the_whole_layout_and_nothing_else(self) -> None:
        root = self.contract["object_layout"]["root"]
        self.assertEqual(self.contract["storage"]["writer_path_template"], f"{root}/tenants/<tenant>/*")
        self.assertTrue(self.contract["object_layout"]["object_key"].startswith(f"{root}/"))


if __name__ == "__main__":
    unittest.main()
