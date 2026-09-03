from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_yaml(relative: str) -> dict:
    value = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{relative} must contain one YAML object")
    return value


class SchedulingObservabilityContractTests(unittest.TestCase):
    def test_kueue_controller_enables_both_fair_sharing_layers(self) -> None:
        values = load_yaml("stages/foundation/values/kueue.yaml")
        manager = yaml.safe_load(
            values["managerConfig"]["controllerManagerConfigYaml"]
        )

        self.assertEqual(
            manager["fairSharing"]["preemptionStrategies"],
            ["LessThanOrEqualToFinalShare", "LessThanInitialShare"],
        )
        self.assertEqual(
            manager["admissionFairSharing"],
            {"usageHalfLifeTime": "168h", "usageSamplingInterval": "5m"},
        )
        self.assertEqual(
            values["controllerManager"]["featureGates"],
            [{"name": "PartialAdmission", "enabled": True}],
        )
        self.assertEqual(
            values["controllerManager"]["nodeSelector"],
            {"workload.fs2.nebius/system": "true"},
        )
        self.assertEqual(
            values["controllerManager"]["manager"]["image"],
            {
                "repository": "registry.k8s.io/kueue/kueue",
                "tag": "v0.17.8@sha256:cecba825d0b0feab9bed2835efe2eb8d825512f1616c8762ab80c53f2ea6afe6",
                "pullPolicy": "IfNotPresent",
            },
        )
        self.assertEqual(
            manager["waitForPodsReady"]["requeuingStrategy"],
            {
                "timestamp": "Creation",
                "backoffLimitCount": 5,
                "backoffBaseSeconds": 15,
                "backoffMaxSeconds": 300,
            },
        )
        self.assertEqual(manager["waitForPodsReady"]["timeout"], "2h")
        self.assertEqual(manager["waitForPodsReady"]["recoveryTimeout"], "15m")

    def test_queue_renderer_is_pool_driven_and_retains_stable_addresses(self) -> None:
        module_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "modules/kueue-scheduling").glob("*.tf"))
        )
        queue_source = (ROOT / "stages/workloads/queue.tf").read_text(
            encoding="utf-8"
        )

        self.assertNotRegex(module_source.lower(), r"\b(?:b300|h100|nvidia)\b")
        self.assertIn('resource "kubernetes_manifest" "async_cluster_queue"', queue_source)
        self.assertIn('resource "kubernetes_manifest" "model_local_queue"', queue_source)
        self.assertIn('module "kueue_scheduling"', queue_source)
        self.assertIn("pool.node.gpus_per_node * pool.capacity.max_nodes", queue_source)
        self.assertIn(
            'resource "kubernetes_config_map_v1" "scientific_scheduling_contract"',
            queue_source,
        )
        self.assertIn("create_before_destroy = true", queue_source)
        self.assertIn("module.kueue_scheduling.contract_sha256", queue_source)

    def test_raw_data_stage_capacity_gates_are_present_and_documented(self) -> None:
        """A 16 CPU / 64 GiB stage cannot be admitted by a smaller pool or quota."""

        queue_source = (ROOT / "stages/workloads/queue.tf").read_text(encoding="utf-8")
        root_locals = (ROOT / "locals.tf").read_text(encoding="utf-8")
        root_main = (ROOT / "main.tf").read_text(encoding="utf-8")
        module_source = (ROOT / "modules/kueue-scheduling/main.tf").read_text(encoding="utf-8")

        # The canonical request is a floor in both projections.
        for source in (queue_source, root_locals):
            self.assertIn("cpu_millicores = 16000", source)
            self.assertIn("memory_mib     = 65536", source) if "memory_mib     = 65536" in source else self.assertIn("memory_mib = 65536", source)
            self.assertIn("max(request.cpu_millicores", source)
            self.assertIn("max(request.memory_mib", source)

        # It must fit one node and the quota of the queue that admits it.
        self.assertIn("schedulable_capacity.cpu_millicores", module_source)
        self.assertIn("core_quota.cpu_millicores >= request.cpu_millicores", module_source)
        # The facade checks the class's own facts, whichever owner supplied
        # them, rather than a hard-coded class name.
        self.assertIn(
            "request.cpu_millicores <= local.root_cpu_stage_class_facts[class_name]"
            ".schedulable_capacity.cpu_millicores",
            root_main,
        )
        self.assertIn(
            "local.root_cpu_stage_class_facts[class_name].queue.nominal_cpu_millicores"
            " >= request.cpu_millicores",
            root_main,
        )
        root_locals = (ROOT / "locals.tf").read_text(encoding="utf-8")
        self.assertIn("root_reference_cpu_capacity.cpu_millicores", root_locals)

        # Raw mode is explicit and requires the data plane and core admission.
        self.assertIn("academic_raw_data_stages", queue_source)
        self.assertIn("academic_raw_data_stages", root_main)

        # The documented acceptance configuration states the required sizes.
        readme = (ROOT / "acceptance/scientific-scheduling/README.md").read_text(encoding="utf-8")
        for required in ("16000", "65536", "64Gi", "32 vCPU / 128 GB", "node-group replacement"):
            with self.subTest(required=required):
                self.assertIn(required, readme)

    def test_accelerator_resource_name_grammar_follows_its_mode(self) -> None:
        """A DRA DeviceClass has no slash; an extended resource must have one."""

        import json

        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "catalog/profiles/accelerator-pools.schema.json").read_text(encoding="utf-8")
        )
        catalog = json.loads(
            (ROOT / "catalog/profiles/accelerator-pools.json").read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(catalog)), [])

        # The GB300 class is DRA and its DeviceClass name has no slash.
        gb300 = catalog["accelerator_classes"]["nvidia-gb300-288gb"]["resource_api"]
        self.assertEqual(gb300, {"mode": "dra", "resource_name": "gpu.nvidia.com"})

        resource_api = schema["$defs"]["acceleratorClass"]["properties"]["resource_api"] if (
            "acceleratorClass" in schema.get("$defs", {})
        ) else None
        for mode, name, valid in (
            ("dra", "gpu.nvidia.com", True),
            ("dra", "nvidia.com/gpu", False),
            ("extended-resource", "nvidia.com/gpu", True),
            ("extended-resource", "gpu.nvidia.com", False),
        ):
            with self.subTest(mode=mode, name=name):
                probe = json.loads(json.dumps(catalog))
                probe["accelerator_classes"]["nvidia-gb300-288gb"]["resource_api"] = {
                    "mode": mode,
                    "resource_name": name,
                }
                errors = list(validator.iter_errors(probe))
                self.assertEqual(not errors, valid, [error.message for error in errors[:2]])

    def test_scientific_service_policy_is_restartable_only(self) -> None:
        module_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "modules/kueue-scheduling").glob("*.tf"))
        )
        root_source = (ROOT / "variables.tf").read_text(encoding="utf-8")
        workload_source = (ROOT / "stages/workloads/variables.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn('policy.preemption_mode == "restartable"', module_source)
        self.assertIn('class.preemption_mode == "restartable"', root_source)
        self.assertIn('preemption_mode         = optional(string, "restartable")', workload_source)

    def test_staged_accelerator_handoff_rechecks_label_and_resource_names(self) -> None:
        for relative in (
            "stages/foundation/accelerator_pool_contract.tf",
            "stages/workloads/accelerator_pool_contract.tf",
        ):
            with self.subTest(relative=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("length(pool.accelerator_class) <= 63", source)
                # A qualified resource name is a prefix of at most 253 plus a
                # name of at most 63, so 317 in total, and each half is bounded
                # separately. A node label key follows its own rule.
                self.assertIn("length(pool.resource_api.resource_name) <= 317", source)
                self.assertIn('length(split("/", pool.resource_api.resource_name)) == 2', source)
                self.assertIn('length(split("/", pool.resource_api.resource_name)[0]) <= 253', source)
                self.assertIn('length(split("/", pool.resource_api.resource_name)[1]) <= 63', source)
                self.assertIn("[-a-z0-9]{0,61}", source)

    def test_dynamic_model_controller_accepts_every_rendered_queue_and_priority(
        self,
    ) -> None:
        source = (ROOT / "stages/workloads/model_controller.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "sort(keys(module.kueue_scheduling.contract.local_queues))", source
        )
        self.assertIn(
            "sort(keys(module.kueue_scheduling.contract.workload_priority_classes))",
            source,
        )
        self.assertNotIn(
            "localQueues                   = "
            "[local.selected_accelerator_pool_profile.queue.local_queue_name]",
            source,
        )

    def test_dcgm_keeps_exact_gpu_and_pod_identity_at_five_seconds(self) -> None:
        base = load_yaml("stages/workloads/values/dcgm-exporter.yaml")
        cadence = load_yaml(
            "stages/workloads/values/dcgm-cadence-profiles.yaml"
        )
        standard = cadence["profiles"]["standard"]
        cold = cadence["profiles"]["coldStartCampaign"]

        self.assertIs(base["serviceMonitor"]["honorLabels"], True)
        regex = base["serviceMonitor"]["metricRelabelings"][0]["regex"].lower()
        self.assertNotIn("uuid", regex)
        self.assertNotIn("pod_uid", regex)
        self.assertEqual(standard["attributionMetricCollectionInterval"], "5s")
        self.assertEqual(
            standard["helmValues"]["arguments"], ["--collect-interval=5000"]
        )
        self.assertEqual(standard["helmValues"]["serviceMonitor"]["interval"], "5s")
        self.assertEqual(standard["minimumNominalWindowSeconds"], 10)
        self.assertEqual(cold["helmValues"]["serviceMonitor"]["interval"], "1s")

        for profile in (standard, cold):
            profile_text = str(
                profile["helmValues"]["serviceMonitor"]["metricRelabelings"]
            ).lower()
            self.assertNotIn("uuid", profile_text)
            self.assertNotIn("pod_uid", profile_text)

    def test_raw_traces_and_kubernetes_events_have_one_collector_path(self) -> None:
        gateway = load_yaml("stages/foundation/values/otel-gateway.yaml")
        cluster = load_yaml("stages/foundation/values/otel-cluster.yaml")
        node = load_yaml("stages/foundation/values/otel-node.yaml")

        self.assertEqual(
            gateway["config"]["exporters"]["otlp/tempo"]["endpoint"],
            "fs2-tempo.fs2-observability.svc.cluster.local:4317",
        )
        self.assertEqual(
            gateway["config"]["service"]["pipelines"]["traces"]["exporters"],
            ["spanmetrics", "otlp/tempo"],
        )
        deleted = {
            item["key"]
            for item in gateway["config"]["processors"]["resource/sanitize"][
                "attributes"
            ]
        }
        self.assertIn("http.request.header.authorization", deleted)
        self.assertNotIn("tenant.id", deleted)
        self.assertNotIn("api.key.id", deleted)

        self.assertEqual(cluster["mode"], "deployment")
        self.assertEqual(cluster["replicaCount"], 1)
        self.assertIs(cluster["presets"]["kubernetesEvents"]["enabled"], True)
        self.assertEqual(
            cluster["config"]["exporters"]["otlp"]["endpoint"],
            "fs2-otel-gateway.fs2-observability.svc.cluster.local:4317",
        )
        extracted = {
            item["key"]: item["tag_name"]
            for item in node["config"]["processors"]["k8sattributes"]["extract"][
                "labels"
            ]
        }
        self.assertEqual(extracted["fs2.nebius.ai/model-id"], "fs2.model.id")
        self.assertEqual(
            extracted["fs2.nebius.ai/workload-id"], "fs2.workload.id"
        )
        self.assertEqual(extracted["fs2.nebius.ai/attempt-id"], "fs2.attempt.id")

    def test_tempo_is_persistent_pinned_and_grafana_discoverable(self) -> None:
        tempo = load_yaml("stages/foundation/values/tempo.yaml")
        foundation = (
            ROOT / "stages/foundation/observability_backends.tf"
        ).read_text(encoding="utf-8")
        workload_network = (
            ROOT / "stages/workloads/observability.tf"
        ).read_text(encoding="utf-8")
        control_plane = (ROOT / "stages/workloads/control_plane.tf").read_text(
            encoding="utf-8"
        )

        self.assertEqual(tempo["tempo"]["tag"], "2.9.0")
        self.assertEqual(tempo["tempo"]["retention"], "168h")
        self.assertIs(tempo["persistence"]["enabled"], True)
        self.assertEqual(tempo["persistence"]["size"], "50Gi")
        self.assertEqual(
            tempo["persistence"]["storageClassName"], "compute-csi-default-sc"
        )
        self.assertRegex(foundation, re.compile(r'version\s+=\s+local\.chart_versions\.tempo'))
        self.assertIn('grafana_datasource = "1"', foundation)
        self.assertIn('port     = "3200"', workload_network)
        self.assertRegex(control_plane, re.compile(r"tempo\s+=\s+true"))


if __name__ == "__main__":
    unittest.main()


class CpuStageClassContractTests(unittest.TestCase):
    """The one CPU stage class schema, its assembler, and its refusals."""

    SCHEMA = "catalog/runtime/schema/cpu-stage-classes.schema.json"
    HANDOFF = "handoff/scientific-scheduling/README.md"
    SCHEMA_ID = "fs2-serve.nebius.ai/cpu-stage-classes/v1"

    def schema(self) -> dict:
        return json.loads((ROOT / self.SCHEMA).read_text(encoding="utf-8"))

    def reference_data_class(self, **overrides: object) -> dict:
        cpu_class = {
            "local_queue": "academic-scientific-cpu",
            "cluster_queue": "reference-data-cpu",
            "namespace": "fs2-academic-poc",
            "resource_flavor": "reference-data-cpu",
            "eligible_pool_ids": ["reference-cpu"],
            "pool_resolution": {"mode": "per-pool-flavor", "pool_id": "reference-cpu"},
            "node_selector": {"workload.fs2.nebius/reference-data": "true"},
            "tolerations": [
                {
                    "key": "workload.fs2.nebius/reference-data",
                    "operator": "Equal",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ],
            "schedulable_capacity": {
                "cpu": "30000m",
                "memory": "122880Mi",
                "ephemeral_storage": "114688Mi",
                "cpu_millicores": 30000,
                "memory_mib": 122880,
                "ephemeral_storage_mib": 114688,
            },
        }
        cpu_class.update(overrides)
        return cpu_class

    def general_cpu_class(self, **overrides: object) -> dict:
        """A multi-pool class: one flavor, so the pool is a Node observation."""

        cpu_class = {
            "local_queue": "general-cpu",
            "cluster_queue": "general-cpu",
            "namespace": "fs2-models",
            "resource_flavor": "general-cpu",
            "eligible_pool_ids": ["general-cpu-small", "general-cpu-large"],
            "pool_resolution": {
                "mode": "node-label-observation",
                "node_label_key": "accelerator.fs2.nebius/pool-id",
            },
            "node_selector": {"workload.fs2.nebius/general-cpu": "true"},
            "tolerations": [],
            "schedulable_capacity": {
                "cpu": "15500m",
                "memory": "61440Mi",
                "ephemeral_storage": "0Mi",
                "cpu_millicores": 15500,
                "memory_mib": 61440,
                "ephemeral_storage_mib": 0,
            },
        }
        cpu_class.update(overrides)
        return cpu_class

    def document(self, **classes: dict) -> dict:
        return {"schema": self.SCHEMA_ID, "cpu_classes": classes}

    def test_the_schema_is_the_agreed_identifier_and_shape(self) -> None:
        schema = self.schema()
        self.assertEqual(
            schema["$id"], "https://fs2-serve.nebius.ai/schema/cpu-stage-classes/v1"
        )
        self.assertEqual(schema["properties"]["schema"]["const"], self.SCHEMA_ID)
        self.assertIn("cpu_classes", schema["properties"])
        self.assertNotIn("classes", schema["properties"])
        # Executable placement plus the two facts a consumer cannot infer:
        # which pools are eligible, and how the actual one is determined.
        self.assertEqual(
            sorted(schema["$defs"]["cpu_stage_class"]["required"]),
            sorted(
                [
                    "local_queue",
                    "cluster_queue",
                    "namespace",
                    "resource_flavor",
                    "node_selector",
                    "tolerations",
                    "schedulable_capacity",
                    "eligible_pool_ids",
                    "pool_resolution",
                ]
            ),
        )

    def test_an_assembled_document_with_both_classes_validates(self) -> None:
        jsonschema.validate(
            self.document(
                **{
                    "reference-data": self.reference_data_class(),
                    "general-cpu": self.general_cpu_class(),
                }
            ),
            self.schema(),
        )

    def test_expected_pools_are_never_presented_as_the_actual_pool(self) -> None:
        """One flavor over N pools cannot name the pool that ran the stage."""

        schema = self.schema()
        rejected = {
            "a multi-pool class claiming one pool": self.general_cpu_class(
                pool_resolution={
                    "mode": "node-label-observation",
                    "node_label_key": "accelerator.fs2.nebius/pool-id",
                    "pool_id": "general-cpu-small",
                }
            ),
            "per-pool-flavor with no pool": self.reference_data_class(
                pool_resolution={"mode": "per-pool-flavor"}
            ),
            "per-pool-flavor naming a Node label": self.reference_data_class(
                pool_resolution={
                    "mode": "per-pool-flavor",
                    "pool_id": "reference-cpu",
                    "node_label_key": "accelerator.fs2.nebius/pool-id",
                }
            ),
            "no eligible pools at all": self.reference_data_class(eligible_pool_ids=[]),
        }
        for label, cpu_class in rejected.items():
            with self.subTest(rejected=label):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(self.document(x=cpu_class), schema)

    def test_toleration_and_capacity_semantics_are_exact(self) -> None:
        schema = self.schema()
        equal_without_value = {"key": "d", "operator": "Equal", "effect": "NoSchedule"}
        exists_with_value = {
            "key": "d",
            "operator": "Exists",
            "value": "true",
            "effect": "NoSchedule",
        }
        exists_without_value = {"key": "d", "operator": "Exists", "effect": "NoSchedule"}
        accepted = {
            # An untainted class states an empty list rather than omitting it.
            "no tolerations": self.reference_data_class(tolerations=[]),
            "exists without a value": self.reference_data_class(
                tolerations=[exists_without_value]
            ),
            # Zero ephemeral storage means none is advertised, which is legal.
            "no ephemeral budget": self.general_cpu_class(),
        }
        for label, cpu_class in accepted.items():
            with self.subTest(accepted=label):
                jsonschema.validate(self.document(x=cpu_class), schema)

        rejected = {
            "equal without a value": self.reference_data_class(
                tolerations=[equal_without_value]
            ),
            "exists with a value": self.reference_data_class(
                tolerations=[exists_with_value]
            ),
            "no node routing": self.reference_data_class(node_selector={}),
            "zero cpu": self.reference_data_class(
                schedulable_capacity={
                    "cpu": "0m",
                    "memory": "1Mi",
                    "ephemeral_storage": "0Mi",
                    "cpu_millicores": 0,
                    "memory_mib": 1,
                    "ephemeral_storage_mib": 0,
                }
            ),
            "capacity with no raw quantity": self.reference_data_class(
                schedulable_capacity={
                    "cpu_millicores": 1,
                    "memory_mib": 1,
                    "ephemeral_storage_mib": 0,
                }
            ),
            "capacity quantity that is not a quantity": self.reference_data_class(
                schedulable_capacity={
                    "cpu": "30 cores",
                    "memory": "122880Mi",
                    "ephemeral_storage": "0Mi",
                    "cpu_millicores": 30000,
                    "memory_mib": 122880,
                    "ephemeral_storage_mib": 0,
                }
            ),
        }
        for label, cpu_class in rejected.items():
            with self.subTest(rejected=label):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(self.document(x=cpu_class), schema)

    def test_capacity_quantities_cannot_contradict_their_integers(self) -> None:
        """One number, two spellings, so a consumer may read either."""

        schema = self.schema()
        # The schema pins the canonical unit, so a Gi memory or a bare cpu
        # count is refused even before the numbers are compared.
        for label, capacity in (
            (
                "memory in Gi",
                {
                    "cpu": "15500m",
                    "memory": "60Gi",
                    "ephemeral_storage": "0Mi",
                    "cpu_millicores": 15500,
                    "memory_mib": 61440,
                    "ephemeral_storage_mib": 0,
                },
            ),
            (
                "cpu without the millicore suffix",
                {
                    "cpu": "16",
                    "memory": "61440Mi",
                    "ephemeral_storage": "0Mi",
                    "cpu_millicores": 16000,
                    "memory_mib": 61440,
                    "ephemeral_storage_mib": 0,
                },
            ),
        ):
            with self.subTest(rejected=label):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(
                        self.document(
                            x=self.reference_data_class(schedulable_capacity=capacity)
                        ),
                        schema,
                    )

        # The numeric equality itself is enforced where the values are
        # assembled, because JSON Schema cannot compare two fields.
        variables = (ROOT / "modules/kueue-scheduling/variables.tf").read_text(
            encoding="utf-8"
        )
        for rule in (
            'class.schedulable_capacity.cpu == "${class.schedulable_capacity.cpu_millicores}m"',
            'class.schedulable_capacity.memory == "${class.schedulable_capacity.memory_mib}Mi"',
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, variables)

    def test_a_qualified_key_at_the_boundary_is_accepted_by_every_layer(self) -> None:
        """317 characters is the largest a Kubernetes qualified name can be."""

        key = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 61]) + "/" + "b" * 63
        self.assertEqual(len(key), 317)
        schema = self.schema()
        jsonschema.validate(
            self.document(
                x=self.reference_data_class(
                    node_selector={key: "true"},
                    tolerations=[
                        {
                            "key": key,
                            "operator": "Equal",
                            "value": "true",
                            "effect": "NoSchedule",
                        }
                    ],
                )
            ),
            schema,
        )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                self.document(x=self.reference_data_class(node_selector={key + "c": "true"})),
                schema,
            )
        # The module's variable validation and its contract precondition use
        # the same bound, so no layer silently rejects what another allows.
        for relative in (
            "modules/kueue-scheduling/variables.tf",
            "modules/kueue-scheduling/main.tf",
        ):
            with self.subTest(layer=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("length(toleration.key) <= 317", source)
                self.assertNotIn("length(toleration.key) <= 253", source)

    def test_the_module_enforces_the_same_rules_as_the_schema(self) -> None:
        """A schema nothing is checked against is a proposal, not a contract."""

        variables = (ROOT / "modules/kueue-scheduling/variables.tf").read_text(
            encoding="utf-8"
        )
        cpu_classes = variables.split('variable "cpu_classes"', 1)[1].split(
            '\nvariable "', 1
        )[0]
        for required in (
            "node_selector = map(string)",
            "tolerations = list(object({",
            "schedulable_capacity = object({",
            "eligible_pool_ids = list(string)",
            "pool_resolution = object({",
        ):
            with self.subTest(required=required):
                self.assertIn(required, cpu_classes)
        self.assertIn('toleration.operator == "Equal"', cpu_classes)
        self.assertIn('class.pool_resolution.mode == "per-pool-flavor"', cpu_classes)
        self.assertIn("class.schedulable_capacity.ephemeral_storage_mib >= 0", cpu_classes)
        self.assertIn("class.schedulable_capacity.cpu_millicores >= 1", cpu_classes)

    def test_the_contract_bytes_name_the_version_and_hash_each_entry(self) -> None:
        """A schema file alone is not a handoff; the emitted document carries it."""

        main = (ROOT / "modules/kueue-scheduling/main.tf").read_text(encoding="utf-8")
        self.assertIn(f'cpu_classes_schema = "{self.SCHEMA_ID}"', main)
        # Per-entry digest, so a contributor can confirm its own class was
        # published unaltered and a consumer can tell which class changed.
        self.assertIn("cpu_class_digests = {", main)
        self.assertIn("class_name => sha256(jsonencode(class))", main)

    def test_the_workloads_stage_is_the_single_assembler(self) -> None:
        queue = (ROOT / "stages/workloads/queue.tf").read_text(encoding="utf-8")
        self.assertIn("local.contributed_cpu_classes", queue)
        self.assertIn("scientific_cpu_classes = merge(", queue)

    def test_no_class_fact_can_be_authored_twice(self) -> None:
        """Capacity and queue facts come from the plane that creates them."""

        # No tfvars surface anywhere repeats a CPU class's capacity, queue, or
        # node routing: a second operator-authored copy can drift from the
        # pool that is actually created, and the drift shows up only as a
        # stage that never schedules.
        for relative in (
            "variables.tf",
            "stages/workloads/variables.tf",
            "stages/foundation/variables.tf",
        ):
            with self.subTest(surface=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("cpu_stage_class_facts", source)
                self.assertNotIn("general_cpu_classes", source)

        # The one class this repository produces is derived field by field
        # from the reference plane's own storage contract.
        queue = (ROOT / "stages/workloads/queue.tf").read_text(encoding="utf-8")
        reference_class = queue.split("reference-data = {", 1)[1].split("\n    } : {}", 1)[0]
        for derived in (
            "var.reference_data.storage_contract.cpu_pool.id",
            "var.reference_data.storage_contract.cpu_pool.node_labels",
            "var.reference_data.storage_contract.cpu_pool.taint",
            "var.reference_data.storage_contract.cpu_pool.schedulable_capacity",
        ):
            with self.subTest(derived=derived):
                self.assertIn(derived, reference_class)
        # The raw quantities are formatted from the same numbers, so they
        # cannot disagree with the integers beside them.
        self.assertIn(
            'cpu               = "${var.reference_data.storage_contract.cpu_pool'
            '.schedulable_capacity.cpu_millicores}m"',
            reference_class,
        )

    def test_the_root_preflight_is_not_hard_coded_to_one_class(self) -> None:
        main = (ROOT / "main.tf").read_text(encoding="utf-8")
        self.assertIn(
            "contains(keys(local.root_cpu_stage_class_facts), class_name)", main
        )
        self.assertNotIn('contains(["reference-data"], class_name)', main)
        # The facts are derived from the plane that creates the capacity, and
        # there is no tfvars surface that could disagree with it.
        root_locals = (ROOT / "locals.tf").read_text(encoding="utf-8")
        self.assertIn("root_cpu_stage_class_facts = merge(", root_locals)
        self.assertNotIn("cpu_stage_class_facts,", root_locals.split("merge(", 1)[0])
        self.assertIn("general-cpu = {", root_locals)
        self.assertIn("local.general_cpu_largest_node.cpu_millicores", root_locals)
        self.assertIn("local.general_cpu_lane_capacity.cpu_millicores", root_locals)

    def test_the_integrated_general_cpu_producer_is_recorded_truthfully(self) -> None:
        handoff = re.sub(
            r"\s+", " ", (ROOT / self.HANDOFF).read_text(encoding="utf-8")
        )
        self.assertIn("The general-CPU producer is integrated", handoff)
        self.assertIn("general-cpu", handoff)
        # One document, not two.
        self.assertIn("There is no second document and no second key", handoff)
        self.assertNotIn("Two ConfigMaps and two keys", handoff)
        self.assertNotIn("Integrated BLOCK", handoff)
        self.assertIn("refusal, not substitution", handoff)


class LocalQueueRebindOwnershipTests(unittest.TestCase):
    """Every owner of a LocalQueue must plan a replacement, not an update.

    Kueue 0.17.8 makes spec.clusterQueue immutable, so an owner without a
    replacement identity in state produces a plan the API server rejects.
    """

    OWNERS = {
        "the additional model lanes": (
            "stages/workloads/queue.tf",
            "terraform_data.additional_local_queue_binding",
        ),
        "the stable model queue": (
            "stages/workloads/queue.tf",
            "terraform_data.model_local_queue_binding",
        ),
        "the academic licensed lane": (
            "modules/academic-assets/main.tf",
            "terraform_data.academic_local_queue_binding",
        ),
        "the reference-data plane": (
            "reference-data/terraform/main.tf",
            "terraform_data.local_queue_binding",
        ),
        "the general CPU lane": (
            "stages/workloads/general_cpu.tf",
            "terraform_data.general_cpu_local_queue_binding",
        ),
    }

    def test_every_local_queue_owner_plans_a_replacement(self) -> None:
        for owner, (relative, binding) in self.OWNERS.items():
            with self.subTest(owner=owner):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(f'resource "terraform_data" "{binding.split(".")[1]}"', source)
                self.assertIn(f"replace_triggered_by = [{binding}", source)

    def test_no_local_queue_is_rendered_without_one(self) -> None:
        """A LocalQueue with no binding identity would attempt an update."""

        for relative in sorted({relative for relative, _ in self.OWNERS.values()}):
            source = (ROOT / relative).read_text(encoding="utf-8")
            rendered = source.count("kind        = \"LocalQueue\"") + source.count(
                "kind = \"LocalQueue\""
            )
            with self.subTest(source=relative):
                # Every literal LocalQueue in this file is covered by a
                # binding; queues rendered from the module contract are
                # covered by the two bindings above.
                self.assertLessEqual(rendered, source.count("replace_triggered_by"))


class SchedulingContractHandoffTests(unittest.TestCase):
    """The ConfigMap digest handoff, and the boundary this branch does not cross."""

    def test_the_stage_publishes_an_immutable_content_addressed_configmap(self) -> None:
        queue = (ROOT / "stages/workloads/queue.tf").read_text(encoding="utf-8")
        self.assertIn('scheduling_contract_key    = "kueue-scheduling.json"', queue)
        self.assertIn("immutable = true", queue)
        # The name carries the digest, so a policy change makes a new object
        # instead of mutating one a controller is already reading.
        self.assertIn(
            'substr(local.scheduling_contract_sha256, 0, 12)',
            queue,
        )
        outputs = (ROOT / "stages/workloads/outputs.tf").read_text(encoding="utf-8")
        reference = outputs.split('output "scheduling_contract_ref"', 1)[1].split(
            "\noutput ", 1
        )[0]
        for field in ("schema", "config_map_name", "namespace", "key", "sha256"):
            with self.subTest(field=field):
                self.assertIn(field, reference)

    def test_the_handoff_requires_verifying_raw_bytes_before_any_write(self) -> None:
        handoff = re.sub(
            r"\s+",
            " ",
            (ROOT / "handoff/scientific-scheduling/README.md").read_text(
                encoding="utf-8"
            ),
        )
        for requirement in (
            "exact applied bytes",
            "refuse to create the Job or JobSet",
            "Hash the raw string exactly as read from the API",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, handoff)

    def test_this_branch_edits_no_controller_or_chart_source(self) -> None:
        """The wiring is a documented dependency, not an edit made here."""

        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/main"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if merge_base.returncode != 0:
            self.skipTest("origin/main is not available")
        changed = subprocess.run(
            ["git", "diff", "--name-only", merge_base.stdout.strip()],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.split()
        owned_elsewhere = [
            path
            for path in changed
            if path.startswith(("components/control-plane/", "charts/"))
            or path == "inference-stack"
        ]
        self.assertEqual(owned_elsewhere, [], owned_elsewhere)


class CpuStageClassCrossOwnerTests(unittest.TestCase):
    """The exact field names and shape every contributor must match.

    Two owners producing CPU stage classes drifted on four points: a
    class-level `pool_id`, a `cpu_class_schema` key, a `cpu_class_entry_sha256`
    output, and a second `reference-data` producer. These pin the canonical
    side so a mismatch fails here rather than after a merge.
    """

    SCHEMA = "catalog/runtime/schema/cpu-stage-classes.schema.json"

    def schema(self) -> dict:
        return json.loads((ROOT / self.SCHEMA).read_text(encoding="utf-8"))

    def canonical_class(self) -> dict:
        return {
            "local_queue": "general-cpu",
            "cluster_queue": "general-cpu",
            "namespace": "fs2-models",
            "resource_flavor": "general-cpu",
            "eligible_pool_ids": ["general-cpu-small", "general-cpu-large"],
            "pool_resolution": {
                "mode": "node-label-observation",
                "node_label_key": "accelerator.fs2.nebius/pool-id",
            },
            "node_selector": {"workload.fs2.nebius/general-cpu": "true"},
            "tolerations": [],
            "schedulable_capacity": {
                "cpu": "15500m",
                "memory": "61440Mi",
                "ephemeral_storage": "0Mi",
                "cpu_millicores": 15500,
                "memory_mib": 61440,
                "ephemeral_storage_mib": 0,
            },
        }

    def test_pool_identity_lives_only_in_pool_resolution(self) -> None:
        schema = self.schema()
        properties = schema["$defs"]["cpu_stage_class"]["properties"]
        self.assertNotIn("pool_id", properties)
        self.assertIn("pool_id", properties["pool_resolution"]["properties"])
        # A class-level pool_id is rejected, not ignored: beside a flavor that
        # spans several pools it reads as an assignment nobody made.
        with_class_pool_id = {**self.canonical_class(), "pool_id": "general-cpu-small"}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {
                    "schema": "fs2-serve.nebius.ai/cpu-stage-classes/v1",
                    "cpu_classes": {"general-cpu": with_class_pool_id},
                },
                schema,
            )
        # The canonical shape validates.
        jsonschema.validate(
            {
                "schema": "fs2-serve.nebius.ai/cpu-stage-classes/v1",
                "cpu_classes": {"general-cpu": self.canonical_class()},
            },
            schema,
        )

    def test_the_contract_field_names_are_the_canonical_ones(self) -> None:
        main = (ROOT / "modules/kueue-scheduling/main.tf").read_text(encoding="utf-8")
        contract = main.split("  contract = {", 1)[1]
        self.assertIn("cpu_classes_schema", contract)
        self.assertIn("cpu_class_digests", contract)
        # The competing names, so a rename shows up here instead of in a merge.
        self.assertNotIn("cpu_class_schema ", contract)
        self.assertNotIn("cpu_class_entry_sha256", contract)

    def test_one_producer_owns_the_reference_data_class(self) -> None:
        queue = (ROOT / "stages/workloads/queue.tf").read_text(encoding="utf-8")
        self.assertIn(
            'condition     = !contains(keys(local.contributed_cpu_classes), "reference-data")',
            queue,
        )
