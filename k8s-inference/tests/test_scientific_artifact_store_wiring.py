"""The Terraform-to-chart wiring for the dedicated scientific artifact store.

Three separate files have to agree for the store to work at all: the
infrastructure stage that creates the bucket and the key, the workloads stage
that projects the credential and the chart values, and the control-plane chart
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

    def test_terraform_emits_the_canonical_secret_reference(self) -> None:
        self.assertEqual(
            self.emitted("artifactStore = {"),
            set(self.contract["chart"]["secrets.artifactStore"]),
        )
        credential = self.contract["credential"]
        self.assertIn(f'scientific_artifacts_secret_name = "{credential["secret_name"]}"', self.workloads)
        self.assertIn(f'scientific_artifacts_secret_key  = "{credential["secret_key"]}"', self.workloads)
        self.assertIn(f'namespace = "{credential["namespace"]}"', self.workloads)

    def test_the_egress_allowlist_and_rollout_annotation_reach_the_chart(self) -> None:
        self.assertIn("artifactStoreCidrs", self.workloads)
        for annotation in self.contract["credential"]["rotation_annotations"]:
            self.assertIn(annotation, self.workloads)
        # podAnnotations is an existing declared chart value rendered into the
        # control-plane pod template, so a rotation restarts the deployment.
        self.assertIn("podAnnotations = {", self.workloads)

    def test_the_overrides_are_appended_to_the_control_plane_release(self) -> None:
        self.assertIn("yamlencode(local.scientific_chart_overrides)", self.control_plane)
        self.assertIn("kubernetes_secret_v1.scientific_artifact_store", self.control_plane)

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
    def test_the_access_key_is_delivered_only_through_mysterybox(self) -> None:
        self.assertIn('secret_delivery_mode = "MYSTERY_BOX"', self.infrastructure)
        # The mode is a constant, not a knob: an INLINE key would land in state.
        self.assertNotIn('secret_delivery_mode = "INLINE"', self.infrastructure)
        self.assertNotIn("var.scientific_artifacts.secret_delivery_mode", self.infrastructure)
        self.assertEqual(self.infrastructure.count("secret_delivery_mode"), 1)

    def test_only_identity_reference_and_revision_leave_the_infrastructure_stage(self) -> None:
        body = block(
            self.infrastructure_outputs,
            'output "scientific_artifacts_object_storage_access" {',
        )
        self.assertIn("sensitive   = true", body)
        emitted = assigned_names(block(body, "value = var.scientific_artifacts.enabled ? {"))
        self.assertEqual(set(emitted), set(self.contract["credential"]["propagated_fields"]))

    def test_no_stage_variable_or_output_can_carry_the_object_store_secret(self) -> None:
        for forbidden in self.contract["credential"]["forbidden_fields"]:
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

    def test_the_credential_is_written_write_only_and_never_persisted(self) -> None:
        secret = block(self.workloads, 'resource "kubernetes_secret_v1" "scientific_artifact_store" {')
        self.assertIn("data_wo = {", secret)
        self.assertIn("data_wo_revision = local.scientific_artifacts_revision", secret)
        # A plain `data` map would write the secret straight into workloads state.
        self.assertNotIn("\n  data = {", secret)
        self.assertIn(
            'ephemeral "nebius_mysterybox_v1_secret_payload_entry" "scientific_artifacts"',
            self.workloads,
        )
        self.assertIn(
            "ephemeral.nebius_mysterybox_v1_secret_payload_entry.scientific_artifacts[0].data.string_value",
            self.workloads,
        )

    def test_the_secret_document_matches_what_the_control_plane_reads(self) -> None:
        for field in self.contract["credential"]["document_fields"]:
            self.assertIn(field, self.workloads)
        self.assertIn("jsonencode({", self.workloads)

    def test_the_non_secret_receipt_carries_only_identity(self) -> None:
        receipt = block(self.workloads, 'resource "terraform_data" "scientific_artifacts_contract" {')
        self.assertIn("credential_revision", receipt)
        self.assertIn("credential_generation", receipt)
        self.assertIn("credential_identity_sha256", receipt)
        self.assertNotIn("string_value", receipt)

    def test_a_replaced_key_cannot_repeat_the_previous_rollout_identity(self) -> None:
        # The cloud resource_version restarts at zero on replacement, so a
        # revision derived from it alone would silently repeat after a rotation.
        self.assertNotIn(
            "var.scientific_artifacts.object_storage_access.resource_version + 1",
            self.workloads,
        )
        identity = block(self.workloads, "scientific_artifacts_credential_identity = local.scientific_artifacts_enabled ? join(\"|\", [")
        for field in ("key_id", "access_key_id", "secret_reference_id", "resource_version"):
            self.assertIn(field, identity)
        self.assertIn("var.scientific_artifacts.credential_generation * 16777216", self.workloads)

    def test_the_generated_workloads_handoff_is_shape_checked(self) -> None:
        self.assertIn(
            "the scientific artifact access handoff must contain exactly the key's",
            self.stack,
        )
        for field in self.contract["credential"]["propagated_fields"]:
            self.assertIn(f'"{field}",', self.stack)
        self.assertIn(
            "the scientific artifact bundle must never carry object-storage secret material",
            self.stack,
        )


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

    def test_the_writer_permit_is_scoped_to_the_canonical_prefix(self) -> None:
        storage = self.contract["storage"]
        self.assertIn(
            f'scientific_artifacts_writer_role = "{storage["writer_role"]}"',
            self.infrastructure,
        )
        self.assertIn(
            f'scientific_artifacts_path_scope  = "{storage["writer_paths"][0]}"',
            self.infrastructure,
        )
        for resource in ("scientific_artifacts", "scientific_artifacts_disposable"):
            body = block(self.infrastructure, f'resource "nebius_storage_v1_bucket" "{resource}" {{')
            self.assertIn("paths    = [local.scientific_artifacts_path_scope]", body)
            self.assertIn("roles    = [local.scientific_artifacts_writer_role]", body)
        # Project-wide roles would let the key read the model cache and registry.
        self.assertNotIn('role        = "editor"', self.infrastructure)
        self.assertNotIn('role        = "viewer"', self.infrastructure)

    def test_the_lifecycle_reclaims_waste_but_never_a_current_object(self) -> None:
        storage = self.contract["storage"]
        rules = block(self.infrastructure, "scientific_artifacts_lifecycle_rules = [")
        for rule_id in storage["lifecycle_rule_ids"]:
            self.assertIn(f'id                                = "{rule_id}"', rules)
        self.assertIn(
            f"days_after_initiation = {storage['abort_incomplete_multipart_upload_days']}",
            rules,
        )
        self.assertIn(
            f"noncurrent_days = {storage['noncurrent_version_expiration_days']}",
            rules,
        )
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
        self.assertIn("chemical/x-pdb", declared)
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
        self.assertIn('writer.role == "storage.object-editor"', body)
        self.assertIn('join(",", var.scientific_artifacts.storage_contract.writer.paths) == "scientific/v1/*"', body)
        self.assertIn('writer.secret_delivery == "MYSTERY_BOX"', body)
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
        self.assertEqual(self.contract["storage"]["writer_paths"], [f"{root}/*"])
        self.assertTrue(self.contract["object_layout"]["object_key"].startswith(f"{root}/"))


if __name__ == "__main__":
    unittest.main()
