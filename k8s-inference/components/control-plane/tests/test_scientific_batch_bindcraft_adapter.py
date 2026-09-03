"""Adversarial tests for the native BindCraft/PyRosetta scientific adapter."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from fs2_serve.scientific_batch import ResourceClass, ScientificAdapterError, compile_adapter_run
from fs2_serve.scientific_batch.adapters import bindcraft

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters/bindcraft"
LOCALIZATION_CONTRACT = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-artifact-localization.json"
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"


def fixture(name: str) -> dict[str, Any]:
    value = json.loads((ADAPTER_ROOT / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


GRANTED_AUTHORIZATION = {
    "authorization_id": "fs2-cancer-immunotherapy-academic-poc-2026-09-02",
    "tenant_id": "tenant-academic",
    "use_authorization_status": "Granted",
    "execution_authorization_status": "Authorized",
}


def profile(*, authorization: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the catalog profile the adapter validates against.

    The BindCraft profile is not in the published set yet; that belongs to the
    runtime-onboarding task. `test_the_profile_matches_the_catalog_when_published`
    binds this shape to the real one as soon as it lands.
    """

    access: dict[str, Any] = {
        "profile": "academic",
        "state": "verified",
        "receipt_digest": None,
        "credentials_embedded": False,
        "request_time_license_receipt_required": False,
    }
    if authorization is not None:
        access["authorization"] = authorization
    return {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile/v1",
        "model_id": "bindcraft",
        "display_name": "BindCraft",
        "execution_mode": "scientific-batch",
        "state": "candidate-unqualified",
        "route_exposed": False,
        "source": {
            "kind": "git",
            "repository": bindcraft.SOURCE_REPOSITORY,
            "revision": bindcraft.SOURCE_REVISION,
            "review_url": f"https://github.com/{bindcraft.SOURCE_REPOSITORY}/tree/{bindcraft.SOURCE_REVISION}",
            "classification": "candidate-input",
        },
        "execution_identity": {
            "model_revision": bindcraft.SOURCE_REVISION,
            "runtime_image_digest": None,
            "runtime_recipe_sha256": "5" * 64,
            "workload_recipe_sha256": "6" * 64,
            "execution_map_revision": "7" * 64,
            "artifact_manifest_digest": None,
            "execution_identity_sha256": None,
        },
        "interface": {
            "protocol": "scientific-batch-v1",
            "submit_endpoint": "/v1/models/bindcraft:submit",
            "request_schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "result_schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
            "parameter_schema": bindcraft.PARAMETER_SCHEMA,
            "operations": ["design-binder"],
            "service_classes": ["customer-batch", "interactive"],
            "mcp": {"discoverable": True, "invocable": False, "tool_name": "submit_bindcraft", "description": "x"},
        },
        "access": access,
        "resources": {
            "gpu_count": 1,
            "gpu_topology": "single-gpu",
            "host_architectures": ["amd64"],
            "compatible_pool_ids": ["h100"],
            "required_node_labels": {},
        },
        "workload": {
            "stages": [
                {
                    "id": "design",
                    "needs": [],
                    "resource_class": "gpu",
                    "admission_mode": "independent-jobs",
                    "min_parallelism": 1,
                    "max_parallelism": bindcraft.MAX_DESIGN_SHARDS,
                    "checkpoint_mode": "restart",
                    "preemption_mode": "restartable",
                },
                {
                    "id": "aggregate",
                    "needs": ["design"],
                    "resource_class": "cpu",
                    "admission_mode": "independent-jobs",
                    "min_parallelism": 1,
                    "max_parallelism": 1,
                    "checkpoint_mode": "none",
                    "preemption_mode": "non_preemptible",
                },
            ],
            "retry": {"max_attempts": 2, "retryable_exit_codes": [137, 143]},
            "cancellation": {"mode": "terminate-attempt", "grace_seconds": 60},
        },
        "semantic_validation": {"validator_id": "bindcraft", "state": "candidate-unqualified"},
        "policy": {"commercial_use": "prohibited", "non_clinical": True, "limitations": ["academic only"]},
    }


def granted() -> dict[str, Any]:
    return profile(authorization=GRANTED_AUTHORIZATION)


def compile_fixture(name: str, *, operation_id: str = "op-bindcraft-01") -> Any:
    return bindcraft.compile_run(granted(), fixture(name), operation_id=operation_id)


def contract_artifacts() -> dict[str, dict[str, Any]]:
    document = json.loads(LOCALIZATION_CONTRACT.read_text(encoding="utf-8"))
    return {item["artifact_id"]: item for item in document["artifacts"]}


def test_every_tree_binding_matches_the_merged_localization_contract() -> None:
    """The adapter names no tree identity the accepted contract does not.

    Archive provenance and extracted-tree inventory are separate digests, and
    the contract is the authority for both. This runs against the real merged
    contract, not a copy.
    """

    artifacts = contract_artifacts()
    for binding in bindcraft.tree_bindings():
        entry = artifacts[binding.artifact_id]
        assert binding.archive_sha256 == entry["archive"]["sha256"]
        assert binding.tree_inventory_sha256 == entry["tree"]["inventory_sha256"]
        assert binding.entry_count == entry["tree"]["entry_count"]
        assert binding.mount_path in entry["tree"]["mount_paths"]
        # The two identities must never collapse into one.
        assert binding.archive_sha256 != binding.tree_inventory_sha256


def test_the_four_trees_span_two_planes_not_one_claim() -> None:
    """Only the licensed tree is tenant-private; the other three are public."""

    artifacts = contract_artifacts()
    visibility = {
        binding.artifact_id: artifacts[binding.artifact_id].get("visibility", "public")
        for binding in bindcraft.tree_bindings()
    }
    assert visibility[bindcraft.PYROSETTA_ARTIFACT] == bindcraft.PYROSETTA_VISIBILITY == "tenant-private"
    assert visibility[bindcraft.AF2_ARTIFACT] == "public"
    assert visibility[bindcraft.MPNN_VANILLA_ARTIFACT] == "public"
    assert visibility[bindcraft.MPNN_SOLUBLE_ARTIFACT] == "public"
    # The two ColabDesign trees come from one archive and differ only as trees.
    vanilla, soluble = (
        next(item for item in bindcraft.tree_bindings() if item.artifact_id == artifact_id)
        for artifact_id in (bindcraft.MPNN_VANILLA_ARTIFACT, bindcraft.MPNN_SOLUBLE_ARTIFACT)
    )
    assert vanilla.archive_sha256 == soluble.archive_sha256
    assert vanilla.tree_inventory_sha256 != soluble.tree_inventory_sha256


def test_each_stage_binds_the_trees_it_actually_needs() -> None:
    """Design reads all four; aggregate carries what the outer gate demands.

    The wrapper admits trees only in run-trajectory, but the shared outer
    entrypoint verifies AlphaFold2 and binds PyRosetta on every non-smoke
    command before exec'ing it, so two of four is the floor for aggregate.
    """

    plan = compile_fixture("positive-default-lane")
    assert set(plan.required_model_artifacts) == set(bindcraft.DESIGN_RUNTIME_ARTIFACTS)
    aggregate = plan.invocation("aggregate", "main")
    assert set(aggregate.runtime_artifacts) == set(bindcraft.AGGREGATE_RUNTIME_ARTIFACTS)
    aggregate_paths = {item.mount_path for item in aggregate.runtime_trees}
    assert aggregate_paths == {bindcraft.AF2_PATH, bindcraft.PYROSETTA_PATH}
    assert bindcraft.MPNN_VANILLA_PATH not in aggregate_paths
    for shard in plan.controller_plan.stages[0].shards:
        design = plan.invocation("design", shard)
        assert set(design.runtime_artifacts) == set(bindcraft.DESIGN_RUNTIME_ARTIFACTS)
        assert len(design.runtime_trees) == 4


def test_every_bound_tree_is_reachable_through_the_plan() -> None:
    """A localized tree nobody names is a mount the model cannot find."""

    plan = compile_fixture("positive-default-lane")
    for binding in bindcraft.tree_bindings():
        assert any(item.names_tree_path(binding) for item in plan.invocations), binding.artifact_id
    environment = dict(plan.invocation("design", "design-000").environment)
    # PYTHONPATH is a concatenation, so each tree also gets an exact-value name.
    assert environment["FS2_ARTIFACT_ROOT"] == bindcraft.AF2_PATH
    assert environment["FS2_BINDCRAFT_PYROSETTA_TREE"] == bindcraft.PYROSETTA_PATH
    assert environment["PYTHONPATH"].split(":", 1)[0] == bindcraft.PYROSETTA_PATH


def test_stage_argv_is_shell_free_and_uses_the_reviewed_outer_entrypoint() -> None:
    plan = compile_fixture("positive-default-lane")
    for invocation in plan.invocations:
        argv = invocation.argv
        assert argv[:2] == ("python", "/opt/fs2/runtime_entrypoint.py")
        assert argv[2] == "/opt/fs2/bin/bindcraft-batch"
        assert argv[3] in {"run-trajectory", "aggregate"}
        assert argv[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"}
        assert not any(token in {"-c", ";", "&&", "|"} or "$(" in token for token in argv)
        assert "--runtime-localization-marker" in argv
    design = plan.invocation("design", "design-000")
    assert design.argv[design.argv.index("--settings-sha256") + 1] == bindcraft.SETTINGS_SHA256
    assert design.argv[design.argv.index("--filters-sha256") + 1] == bindcraft.FILTERS_SHA256
    assert "--pyrosetta-required" in design.argv
    aggregate = plan.invocation("aggregate", "main")
    assert aggregate.argv[aggregate.argv.index("--expected-shards") + 1] == "4"
    assert "--atomic-rename" in aggregate.argv


def test_the_default_mpnn_lane_is_vanilla_and_soluble_is_never_implicit() -> None:
    assert bindcraft.DEFAULT_MPNN_LANE == "vanilla"
    assert "mpnn_lane" not in fixture("positive-default-lane")["parameters"]
    plan = compile_fixture("positive-default-lane")
    for invocation in plan.invocations:
        assert dict(invocation.environment)["FS2_BINDCRAFT_MPNN_WEIGHTS"] == "original"
    soluble = compile_fixture("positive-soluble-lane")
    for invocation in soluble.invocations:
        assert dict(invocation.environment)["FS2_BINDCRAFT_MPNN_WEIGHTS"] == "soluble"
    # Both trees stay mounted whichever lane is chosen.
    for plan_under_test in (plan, soluble):
        design = plan_under_test.invocation("design", "design-000")
        paths = {item.mount_path for item in design.runtime_trees}
        assert {bindcraft.MPNN_VANILLA_PATH, bindcraft.MPNN_SOLUBLE_PATH} <= paths

    request = fixture("positive-soluble-lane")
    request["parameters"]["mpnn_lane"] = "SOLUBLE"
    with pytest.raises(ScientificAdapterError, match="mpnn_lane"):
        bindcraft.compile_run(granted(), request, operation_id="op-bindcraft-01")


def test_the_adapter_refuses_what_the_native_schema_cannot_express() -> None:
    cases = (
        ({"target": {"chain": "AB", "hotspot_residues": [56]}}, "one alphanumeric character"),
        ({"target": {"chain": "A", "hotspot_residues": [10000]}}, "hotspot residue"),
        ({"binder_length": {"minimum": 39, "maximum": 90}}, "binder_length.minimum"),
        ({"binder_length": {"minimum": 55, "maximum": 201}}, "binder_length.maximum"),
        ({"seed": 2**31}, "seed"),
    )
    for override, expected in cases:
        request = fixture("positive-default-lane")
        request["parameters"].update(override)
        with pytest.raises(ScientificAdapterError, match=expected):
            bindcraft.compile_run(granted(), request, operation_id="op-bindcraft-01")


def test_every_requested_design_is_assigned_to_exactly_one_shard() -> None:
    for designs in (1, 2, 31, 32, 33, 100, 319, 320):
        request = fixture("positive-default-lane")
        request["parameters"]["designs"] = designs
        plan = bindcraft.compile_run(granted(), request, operation_id="op-bindcraft-01")
        shards = plan.controller_plan.stages[0].shards
        assert len(shards) == min(designs, bindcraft.MAX_DESIGN_SHARDS)
        quotas = [
            int(dict(plan.invocation("design", shard).environment)["FS2_BINDCRAFT_ACCEPTED_DESIGNS"])
            for shard in shards
        ]
        assert sum(quotas) == designs, f"{designs} designs lost {designs - sum(quotas)}"
        assert min(quotas) >= 1
        assert max(quotas) <= bindcraft.MAX_DESIGNS_PER_SHARD


def test_a_request_above_the_executable_design_ceiling_fails_closed() -> None:
    assert bindcraft.EXECUTABLE_DESIGN_CEILING == 320
    for designs in (321, 641, 1024):
        request = fixture("positive-default-lane")
        request["parameters"]["designs"] = designs
        with pytest.raises(ScientificAdapterError, match="at most 320 designs"):
            bindcraft.compile_run(granted(), request, operation_id="op-bindcraft-01")


def test_admission_is_deployment_bound_and_takes_no_request_receipt() -> None:
    payload = json.dumps(fixture("positive-default-lane"))
    assert "receipt" not in payload and "licen" not in payload
    assert compile_fixture("positive-default-lane").model_id == "bindcraft"

    for authorization in (
        None,
        {**GRANTED_AUTHORIZATION, "use_authorization_status": "Missing"},
        {**GRANTED_AUTHORIZATION, "execution_authorization_status": "NotAuthorized"},
    ):
        with pytest.raises(ScientificAdapterError, match="authoriz"):
            bindcraft.compile_run(
                profile(authorization=authorization), fixture("positive-default-lane"), operation_id="op-1"
            )
    receipt_required = granted()
    receipt_required["access"]["request_time_license_receipt_required"] = True
    with pytest.raises(ScientificAdapterError, match="per-request licence receipt"):
        bindcraft.compile_run(receipt_required, fixture("positive-default-lane"), operation_id="op-1")

    with pytest.raises(ScientificAdapterError, match="unknown \\['license_receipt'\\]"):
        bindcraft.compile_run(granted(), fixture("negative-caller-execution-fields"), operation_id="op-1")


def test_the_stage_topology_is_a_gpu_fanout_and_one_cpu_aggregate() -> None:
    plan = compile_fixture("positive-default-lane")
    stages = plan.controller_plan.stages
    assert tuple(stage.stage_id for stage in stages) == ("design", "aggregate")
    assert stages[0].resource_class is ResourceClass.GPU
    assert stages[1].resource_class is ResourceClass.CPU
    assert stages[1].depends_on == ("design",)
    assert stages[1].shards == ("main",)


def test_dispatch_reaches_the_adapter_through_the_public_allow_list() -> None:
    plan = compile_adapter_run(
        "bindcraft",
        granted(),
        fixture("positive-default-lane"),
        operation_id="op-bindcraft-01",
        variant_id=bindcraft.VARIANT_ID,
    )
    assert plan.model_id == "bindcraft"
    assert plan.variant_id == "v1-5-3-pyrosetta-academic"
    with pytest.raises(ScientificAdapterError, match="variant_id"):
        compile_adapter_run(
            "bindcraft",
            granted(),
            fixture("positive-default-lane"),
            operation_id="op-bindcraft-01",
            variant_id="upstream-pyrosetta",
        )


def publish_shard(workspace: Path, *, index: int, designs: int = 1, sequence: str | None = None) -> None:
    artifacts = workspace / "output" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "shard.json").write_text(
        json.dumps(
            {
                "backend_id": bindcraft.BACKEND_ID,
                "source_revision": bindcraft.SOURCE_REVISION,
                "index": index,
                "seed": 4096 + index,
                "status": "succeeded",
            }
        ),
        encoding="utf-8",
    )
    body = sequence or "AAAGGGCCCWWWYYYFFFLLLVVVIIIMMMTTTSSSNNNQQQHHHKKKRRRDDDEEEPPPAAAGG"
    for ordinal in range(designs):
        (artifacts / f"candidate-{ordinal:03d}-metrics.json").write_text(
            json.dumps(
                {
                    "candidate_id": f"native-s{index:03d}-c{ordinal:03d}",
                    "shard_index": index,
                    "sequence": body,
                    "scoring_engine": "pyrosetta",
                    "iptm": 0.82,
                }
            ),
            encoding="utf-8",
        )
        (artifacts / f"candidate-{ordinal:03d}.pdb").write_text("ATOM\nTER\nEND\n", encoding="utf-8")
        (artifacts / f"candidate-{ordinal:03d}-relaxed-complex.pdb").write_text("ATOM\nTER\nEND\n", encoding="utf-8")


def test_the_collector_returns_every_result_file_and_gates_the_science(tmp_path: Path) -> None:
    """Referenced-but-uncollected files die with the Pod, taking the result."""

    request = fixture("positive-default-lane")
    workspace = tmp_path / "design-000"
    (workspace / "output").mkdir(parents=True)
    with pytest.raises(ScientificAdapterError, match="has not been published"):
        bindcraft.collect_output(request, workspace)

    publish_shard(workspace, index=0, designs=2)
    collected = bindcraft.collect_output(request, workspace)
    names = [entry["name"] for entry in collected.manifest["entries"]]  # type: ignore[index]
    assert names.count("shard") == 1
    # Every candidate contributes metrics, structure and relaxed complex.
    assert len(names) == 1 + 2 * 3
    assert len(set(names)) == len(names)
    # Blobs are keyed by the helper's content-addressed artifact identity.
    assert len(collected.blobs) == len(names)
    pointers = {entry["artifact"]["artifact_id"] for entry in collected.manifest["entries"]}  # type: ignore[index]
    assert pointers == set(collected.blobs)

    metrics = workspace / "output" / "artifacts" / "candidate-000-metrics.json"
    tampered = json.loads(metrics.read_text(encoding="utf-8"))
    tampered["scoring_engine"] = "rosettapy"
    metrics.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="PyRosetta"):
        bindcraft.collect_output(request, workspace)


def test_the_collector_holds_candidates_to_the_requested_binder_length(tmp_path: Path) -> None:
    request = fixture("positive-default-lane")
    window = request["parameters"]["binder_length"]
    workspace = tmp_path / "design-000"
    (workspace / "output").mkdir(parents=True)
    for length in (window["minimum"] - 1, window["maximum"] + 1):
        publish_shard(workspace, index=0, designs=1, sequence="A" * length)
        with pytest.raises(ScientificAdapterError, match="binder length"):
            bindcraft.collect_output(request, workspace)
    publish_shard(workspace, index=0, designs=1, sequence="A" * window["maximum"])
    assert bindcraft.collect_output(request, workspace).manifest["manifest_id"] == "bindcraft.shard.000.results"


def test_a_shard_from_another_backend_or_revision_is_refused(tmp_path: Path) -> None:
    request = fixture("positive-default-lane")
    workspace = tmp_path / "design-000"
    (workspace / "output").mkdir(parents=True)
    publish_shard(workspace, index=0, designs=1)
    record = workspace / "output" / "artifacts" / "shard.json"
    original = json.loads(record.read_text(encoding="utf-8"))
    for field, value, expected in (
        ("status", "failed", "succeeded run"),
        ("backend_id", "open-binder", "succeeded run"),
        ("source_revision", "0" * 40, "another source revision"),
    ):
        tampered = copy.deepcopy(original)
        tampered[field] = value
        record.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(ScientificAdapterError, match=expected):
            bindcraft.collect_output(request, workspace)


def test_the_profile_matches_the_catalog_when_published() -> None:
    """Bind the in-test profile to the real one once onboarding publishes it."""

    published = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    entry = next((item for item in published["profiles"] if item["model_id"] == "bindcraft"), None)
    if entry is None:
        pytest.skip("the BindCraft workload profile is not in the published catalog yet")
    assert entry["source"]["revision"] == bindcraft.SOURCE_REVISION
    assert entry["interface"]["parameter_schema"] == bindcraft.PARAMETER_SCHEMA
    assert [stage["id"] for stage in entry["workload"]["stages"]] == ["design", "aggregate"]
    assert entry["workload"]["stages"][0]["max_parallelism"] == bindcraft.MAX_DESIGN_SHARDS


def test_collected_results_carry_the_right_media_type_and_immutable_metadata(tmp_path: Path) -> None:
    """JSON is JSON and PDB is PDB, each with a content-addressed pointer.

    ``collect_output_files`` used to derive media type with branches only for
    CSV and mmCIF, so everything else fell through to ``chemical/x-pdb``.
    BindCraft is the first adapter to emit JSON results, which made a shard
    record and its candidate metrics arrive labelled as structures. The shared
    helper now has a JSON branch; these assertions hold both sides of it.
    """

    workspace = tmp_path / "design-000"
    (workspace / "output").mkdir(parents=True)
    publish_shard(workspace, index=0, designs=2)
    collected = bindcraft.collect_output(fixture("positive-default-lane"), workspace)
    entries = collected.manifest["entries"]
    by_name = {entry["name"]: entry for entry in entries}  # type: ignore[union-attr]

    for name in ("shard", "candidate-000-metrics", "candidate-001-metrics"):
        assert by_name[name]["semantic_type"].endswith("-json/v1")
        assert by_name[name]["artifact"]["media_type"] == "application/json"
    for name in (
        "candidate-000-structure",
        "candidate-000-relaxed-complex",
        "candidate-001-structure",
        "candidate-001-relaxed-complex",
    ):
        assert by_name[name]["artifact"]["media_type"] == "chemical/x-pdb"

    # Immutable metadata: every pointer is content-addressed and matches bytes.
    import hashlib

    for entry in entries:  # type: ignore[union-attr]
        pointer = entry["artifact"]
        content = collected.blobs[pointer["artifact_id"]]
        assert pointer["sha256"] == hashlib.sha256(content).hexdigest()
        assert pointer["size_bytes"] == len(content)
        assert pointer["sha256"][:32] in pointer["artifact_id"]


def test_the_shared_collector_types_json_csv_and_structures_apart(tmp_path: Path) -> None:
    """Regression for the shared helper itself, independent of BindCraft."""

    from fs2_serve.scientific_batch.adapters.common import collect_output_files

    workspace = tmp_path / "shared"
    workspace.mkdir()
    (workspace / "metrics.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    (workspace / "binder.pdb").write_text("ATOM\nTER\nEND\n", encoding="utf-8")
    (workspace / "complex.cif").write_text("data_x\n", encoding="utf-8")
    (workspace / "ranking.csv").write_text("id,score\n1,0.5\n", encoding="utf-8")
    collected = collect_output_files(
        workspace,
        (
            ("metrics", "some-metrics-json/v1", workspace / "metrics.json", False),
            ("structure", "protein-structure-pdb/v1", workspace / "binder.pdb", False),
            ("complex", "protein-complex-structure/v1", workspace / "complex.cif", False),
            ("ranking", "some-ranking-csv/v1", workspace / "ranking.csv", True),
        ),
        manifest_id="shared.media.types",
        maximum_total_bytes=1 << 20,
    )
    observed = {
        entry["name"]: entry["artifact"]["media_type"]  # type: ignore[index]
        for entry in collected.manifest["entries"]  # type: ignore[union-attr]
    }
    assert observed == {
        "metrics": "application/json",
        "structure": "chemical/x-pdb",
        "complex": "chemical/x-mmcif",
        "ranking": "text/csv",
    }
