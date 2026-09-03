"""Focused tests for the final AlphaFold 3 two-stage scientific batch adapter.

Three things are bound here that a hand-written assertion could not bind:

* the argv templates are compared to the runtime image's own machine-readable
  command/IO contract, so a runtime successor that changes the surface fails
  this file instead of the cluster;
* the reference identities are compared to a receipt built by the reference-data
  producer's own ``build_terminal_receipt``, so the adapter cannot drift from
  the document the publisher actually writes;
* the plan is compiled through the real ``ScientificBatchService.submit`` path
  with the real manifest renderer, not a fake plan factory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tarfile
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

import pytest
from conftest import CATALOG_ROOT
from scientific_batch_fakes import FakeScientificBatchCluster, FakeScientificBatchRepository
from test_scientific_batch_execution_handoff import (
    AF3_MANIFEST_SHA256,
    AF3_TREE_SHA256,
    REPO_ROOT,
    af3_qualified_profile,
    af3_renderer,
    af3_scheduling,
)
from test_scientific_batch_production import FakeArtifactAccess, principal

from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.memory_store import MemoryStore
from fs2_serve.scientific_batch.adapters import alphafold3 as af3
from fs2_serve.scientific_batch.adapters import CollectionPendingError
from fs2_serve.scientific_batch.adapters.common import ScientificAdapterError
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.models import ArtifactAccessContext, ScientificInputArtifact
from fs2_serve.scientific_batch.profile_catalog import (
    ScientificProfileCatalog,
    ScientificProfileError,
    ScientificWorkloadProfile,
)
from fs2_serve.scientific_batch.service import ScientificBatchService
from jsonschema import Draft202012Validator

AF3_IMAGE_ROOT = REPO_ROOT / "models/cancer-immunotherapy/images/alphafold3"
COMMAND_IO_CONTRACT = AF3_IMAGE_ROOT / "contracts/af3-command-io-contract.json"
FIXTURES = REPO_ROOT / "models/structure/runtime/alphafold3/fixtures"


def command_io_contract() -> dict[str, Any]:
    return json.loads(COMMAND_IO_CONTRACT.read_text())


def reference_data_producer() -> Any:
    """Import the reference-data plane's own module, not a copy of it."""
    path = REPO_ROOT / "reference-data/reference_data.py"
    spec = importlib.util.spec_from_file_location("fs2_reference_data_producer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def producer_receipt(**overrides: Any) -> dict[str, Any]:
    """A terminal receipt built by the publisher's own constructor."""
    producer = reference_data_producer()
    fields: dict[str, Any] = {
        "bundle_id": af3.REFERENCE_ARTIFACT,
        "revision": af3.REFERENCE_REVISION,
        "tree_sha256": AF3_TREE_SHA256,
        "manifest_sha256": AF3_MANIFEST_SHA256,
        "inventory_sha256": "a" * 64,
        "file_count": 5001,
        "expanded_bytes": 630_000_000_000,
    }
    fields.update(overrides)
    return producer.build_terminal_receipt(**fields)


def request_document(**parameters: Any) -> dict[str, Any]:
    document = json.loads((FIXTURES / "positive-raw.json").read_text())
    document["parameters"].update(parameters)
    return document


def model_input() -> ScientificInputArtifact:
    return ScientificInputArtifact(
        logical_artifact_id="model-input",
        semantic_type="request/v1",
        artifact_id=uuid4(),
        digest="sha256:" + "b" * 64,
        size_bytes=1024,
        media_type="application/json",
    )


def compile_plan(profile_value: dict[str, Any], document: dict[str, Any]) -> Any:
    return af3.compile_run(
        profile_value,
        document,
        operation_id=str(uuid4()),
        variant_id=af3.VARIANT_ID,
        access_context=ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="ordinary-poc"),
        input_artifacts=(model_input(),),
    )


def test_argv_matches_the_runtime_command_io_contract() -> None:
    """The adapter's templates are the runtime's published surface.

    Only one argument may be absent from the contract: the controller's
    localization marker, which StageInvocation requires in argv but the runtime
    does not yet declare a flag for. Recording it as an explicit, single-item
    gap keeps the successor's obligation visible instead of silent.
    """
    contract = command_io_contract()
    assert contract["schema"] == "fs2-serve.nebius.ai/alphafold3-command-io/v1"
    assert tuple(contract["entrypoint"]["command"]) == af3.RUNTIME_COMMAND
    assert tuple(contract["stages"]["data"]["runtime_args"]) == af3.DATA_RUNTIME_ARGS
    assert tuple(contract["stages"]["inference"]["runtime_args"]) == af3.INFERENCE_RUNTIME_ARGS
    assert contract["root_layout"]["reference_root"]["mount_path"] == af3.REFERENCE_MOUNT_PATH
    assert contract["root_layout"]["reference_root"]["readiness_marker"] == af3.REFERENCE_MANIFEST_MARKER
    assert contract["root_layout"]["reference_root"]["single_mount"] is True
    assert contract["root_layout"]["parameters"]["mount_path"] == af3.PARAMETER_MOUNT_PATH
    # The retired wrapper is explicitly unsupported by the runtime itself.
    assert contract["legacy_aliases"]["fs2-run-alphafold3"]["supported"] is False

    profile, profile_value = af3_qualified_profile()
    del profile
    plan = compile_plan(profile_value, request_document())
    declared = set(contract["stages"]["data"]["runtime_args"]) | set(
        contract["stages"]["data"]["optional_runtime_args"]
    )
    declared |= set(contract["stages"]["inference"]["runtime_args"]) | set(
        contract["stages"]["inference"]["optional_runtime_args"]
    )
    emitted_flags = {
        value
        for invocation in plan.invocations
        for value in invocation.argv
        if value.startswith("--")
    }
    assert emitted_flags - declared == af3.PENDING_RUNTIME_ARGS


def test_reference_paths_come_from_a_producer_generated_receipt() -> None:
    """Every reference path equals what the publisher's own code derives."""
    producer = reference_data_producer()
    receipt = producer_receipt()
    binding = af3.ReferenceBinding(
        bundle_id=af3.REFERENCE_ARTIFACT,
        revision=af3.REFERENCE_REVISION,
        tree_sha256=AF3_TREE_SHA256,
        manifest_sha256=AF3_MANIFEST_SHA256,
    )
    assert binding.dataset_sub_path == receipt["storage"]["dataset_sub_path"]
    assert binding.database_root == producer.derive_database_root(receipt)
    assert receipt["storage"]["mount_path"] == af3.REFERENCE_MOUNT_PATH
    assert receipt["storage"]["read_only"] is True
    assert receipt["content"]["inventory_marker"] == af3.REFERENCE_MANIFEST_MARKER
    assert binding.marker_path == f"{binding.database_root}/{af3.REFERENCE_MANIFEST_MARKER}"
    # The sibling manifest document the runtime verifies the tree against is the
    # URI the producer's own consumer transform accepts.
    transformed = producer.derive_preprocess_reference_data(
        receipt, manifest_uri=f"file://{binding.manifest_path}"
    )
    assert transformed["manifest_sha256"] == AF3_MANIFEST_SHA256
    assert transformed["bundle_id"] == af3.REFERENCE_ARTIFACT
    assert transformed["revision"] == af3.REFERENCE_REVISION
    # The receipt, dataset, marker and manifest are siblings under one root, so
    # a subPath mount of the dataset alone could not reach three of the four.
    binding.assert_single_root()
    assert binding.receipt_path.startswith(f"{af3.REFERENCE_MOUNT_PATH}/receipts/")
    assert binding.manifest_path.startswith(f"{af3.REFERENCE_MOUNT_PATH}/manifests/sha256/")


def test_reference_tree_and_manifest_identities_stay_independent() -> None:
    profile, profile_value = af3_qualified_profile()
    del profile
    conflated = deepcopy(profile_value)
    reference = next(
        item
        for item in conflated["artifact_requirements"]
        if item["artifact_id"] == af3.REFERENCE_ARTIFACT
    )
    reference["localization_manifest_sha256"] = reference["content_digest_sha256"]
    with pytest.raises(ScientificAdapterError, match="different objects"):
        compile_plan(conflated, request_document())


def test_cpu_stage_binds_reference_data_and_no_parameters() -> None:
    profile, profile_value = af3_qualified_profile()
    del profile
    plan = compile_plan(profile_value, request_document())
    data = plan.invocation("data-pipeline", "main")

    assert data.runtime_artifacts == (af3.REFERENCE_ARTIFACT,)
    assert af3.PARAMETERS_ARTIFACT not in data.runtime_artifacts
    mount = data.runtime_mounts[0]
    assert mount.mount_path == af3.REFERENCE_MOUNT_PATH
    assert mount.sub_path is None
    assert mount.read_only is True
    assert mount.expected_content_sha256 == AF3_TREE_SHA256
    assert mount.expected_manifest_sha256 == AF3_MANIFEST_SHA256
    assert all(item.mount_path != af3.PARAMETER_MOUNT_PATH for item in data.runtime_mounts)

    # 16 CPU raw-input preprocessing, with both MSA thread flags driven by it.
    assert data.argv[data.argv.index("--threads") + 1] == "16"
    assert data.argv[data.argv.index("--cpu-request") + 1] == "16"
    receipt_path = data.argv[data.argv.index("--reference-receipt") + 1]
    assert receipt_path == (
        "/reference-data/receipts/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28.json"
    )
    assert data.argv[data.argv.index("--reference-receipt") + 1] == producer_receipt_path()


def producer_receipt_path() -> str:
    binding = af3.ReferenceBinding(
        bundle_id=af3.REFERENCE_ARTIFACT,
        revision=af3.REFERENCE_REVISION,
        tree_sha256=AF3_TREE_SHA256,
        manifest_sha256=AF3_MANIFEST_SHA256,
    )
    return binding.receipt_path


def test_gpu_stage_binds_parameters_and_never_reference_data() -> None:
    profile, profile_value = af3_qualified_profile()
    del profile
    plan = compile_plan(profile_value, request_document())
    inference = plan.invocation("inference", "main")

    assert inference.runtime_artifacts == (af3.PARAMETERS_ARTIFACT,)
    assert af3.REFERENCE_ARTIFACT not in inference.runtime_artifacts
    mount = inference.runtime_mounts[0]
    assert mount.mount_path == af3.PARAMETER_MOUNT_PATH
    assert mount.sub_path == "alphafold3/af3.bin.zst"
    assert mount.expected_content_sha256 == af3.PARAMETERS_SHA256
    assert all(af3.REFERENCE_MOUNT_PATH not in value for value in inference.argv)
    assert "--reference-receipt" not in inference.argv
    assert "--threads" not in inference.argv


def test_handoff_travels_as_a_relative_package_not_an_absolute_path() -> None:
    profile, profile_value = af3_qualified_profile()
    del profile
    plan = compile_plan(profile_value, request_document())
    data = plan.invocation("data-pipeline", "main")
    inference = plan.invocation("inference", "main")

    # The CPU stage produces the artifact the GPU stage consumes, and the GPU
    # stage addresses it inside its own workspace. No stage names /output or
    # /handoff, which are image-local paths a relocated package cannot honour.
    assert inference.consumes == (data.produces,)
    assert data.handoff_name == "processed-input"
    handoff_dir = f"{inference.working_directory}/{af3.HANDOFF_MOUNT_DIR}"
    assert inference.argv[inference.argv.index("--handoff-dir") + 1] == handoff_dir
    assert inference.materializations[0].destination == handoff_dir
    assert "--json-path" not in inference.argv
    for invocation in plan.invocations:
        for value in invocation.argv:
            assert not value.startswith("/output")
            assert not value.startswith("/handoff")
            assert value.startswith("/mnt/fs2-scientific") or not value.startswith("/mnt")


def test_one_fold_job_needs_no_selector() -> None:
    """A single fold job runs without --fold-job, exactly as the runtime says."""
    profile, profile_value = af3_qualified_profile()
    del profile
    document = request_document(fold_job_count=1)
    seeds = document["parameters"]["model_seeds"]
    samples = document["parameters"]["num_diffusion_samples"]
    plan = compile_plan(profile_value, document)

    inference = plan.invocation("inference", "main")
    assert "--fold-job" not in inference.argv
    assert inference.max_output_artifacts == len(seeds) * samples + 1
    assert dict(inference.environment)[af3.FOLD_JOBS_ENV] == "1"
    # A single CPU stage packages that one fold job exactly once.
    assert plan.invocation("data-pipeline", "main").max_output_artifacts == 1


@pytest.mark.parametrize("fold_jobs", [2, 7, 64])
def test_multiple_fold_jobs_are_refused_rather_than_left_ambiguous(fold_jobs: int) -> None:
    """Several fold jobs need one GPU run each, which a single sink cannot express.

    The runtime requires --fold-job once a handoff holds more than one job and
    rejects an ambiguous handoff. Selecting per run means one GPU work unit per
    fold job, and an execution plan admits exactly one terminal invocation, so
    the adapter refuses instead of emitting a stage the runtime would reject.
    """
    profile, profile_value = af3_qualified_profile()
    del profile
    with pytest.raises(ScientificAdapterError, match="one fold job"):
        compile_plan(profile_value, request_document(fold_job_count=fold_jobs))


def test_fold_job_count_is_bounded() -> None:
    profile, profile_value = af3_qualified_profile()
    del profile
    with pytest.raises(ScientificAdapterError, match="fold_job_count"):
        compile_plan(profile_value, request_document(fold_job_count=65))
    with pytest.raises(ScientificAdapterError, match="fold_job_count"):
        compile_plan(profile_value, request_document(fold_job_count=0))


def test_retired_command_surface_is_absent_everywhere() -> None:
    profile, profile_value = af3_qualified_profile()
    del profile
    plan = compile_plan(profile_value, request_document())
    for invocation in plan.invocations:
        argv = " ".join(invocation.argv)
        for token in ("fs2-run-alphafold3", "/databases", "--input-json", "--processed-json", "--handoff-tar"):
            assert token not in argv
        assert "--extra-arg" not in invocation.argv
        # Shell-free exec form.
        assert invocation.argv[0] == af3.RUNTIME_COMMAND[0]
        assert invocation.argv[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"}
        assert "-c" not in invocation.argv[:3]


def test_ordinary_request_needs_no_licence_receipt() -> None:
    """An ordinary academic run carries no per-request or per-input receipt."""
    profile, profile_value = af3_qualified_profile()
    del profile
    document = request_document()
    assert "licence_receipt" not in document
    assert "license_receipt" not in document["parameters"]
    assert profile_value["access"]["receipt_digest"] is None

    plan = compile_plan(profile_value, document)
    assert plan.model_id == af3.MODEL_ID
    # A public access context with no receipt digest is accepted; admission came
    # from deployment metadata, not from the caller.
    assert plan.required_model_artifacts == (af3.PARAMETERS_ARTIFACT, af3.REFERENCE_ARTIFACT)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operational_activation", "candidate-not-activated"),
        ("materialization", "quarantine-required"),
        ("license_gate_scope", "materialization-and-execution"),
        ("profile", "standard"),
        ("receipt_digest", "c" * 64),
    ],
)
def test_missing_deployment_authorization_is_refused(field: str, value: object) -> None:
    """The negative deployment-authorization case, one field at a time."""
    profile, profile_value = af3_qualified_profile()
    del profile
    unauthorized = deepcopy(profile_value)
    unauthorized["access"][field] = value
    with pytest.raises(ScientificAdapterError, match=af3.ADMISSION_BLOCKER):
        compile_plan(unauthorized, request_document())


def test_absent_access_block_is_refused() -> None:
    profile, profile_value = af3_qualified_profile()
    del profile
    unauthorized = deepcopy(profile_value)
    unauthorized.pop("access")
    with pytest.raises(ScientificAdapterError, match=af3.ADMISSION_BLOCKER):
        compile_plan(unauthorized, request_document())


def test_enriched_input_is_refused_rather_than_compiled_unrenderable() -> None:
    """A single-stage plan can never satisfy the two-stage canonical profile."""
    profile, profile_value = af3_qualified_profile()
    del profile
    with pytest.raises(ScientificAdapterError, match="enriched input needs its own single-stage profile"):
        compile_plan(
            profile_value,
            request_document(input_mode="enriched", raw_input_sha256="c" * 64),
        )


def af3_profile_catalog(profile: ScientificWorkloadProfile) -> ScientificProfileCatalog:
    def load(name: str) -> Draft202012Validator:
        return Draft202012Validator(json.loads((CATALOG_ROOT / "schema" / name).read_text()))

    parameter_schema = profile.value["interface"]["parameter_schema_definition"]
    return ScientificProfileCatalog(
        profiles={profile.model_id: profile},
        validators={
            "fs2-serve.nebius.ai/scientific-run-request/v1": load("scientific-run-request.schema.json"),
            "fs2-serve.nebius.ai/scientific-run-result/v1": load("scientific-run-result.schema.json"),
            "fs2-serve.nebius.ai/scientific-artifact-manifest/v1": load(
                "scientific-artifact-manifest.schema.json"
            ),
            af3.PARAMETER_SCHEMA: Draft202012Validator(parameter_schema),
        },
    )


@pytest.mark.parametrize("fold_jobs", [1])
@pytest.mark.asyncio
async def test_public_submit_freezes_two_stage_academic_plan(
    tmp_path: Path, fold_jobs: int, cipher: PayloadCipher, hasher: KeyedHasher
) -> None:
    """The real public submit path, with the real renderer as plan factory.

    This is the end the ticket cares about: an authorized academic request with
    no licence receipt goes through ScientificBatchService.submit and comes out
    as a frozen CPU data stage plus a GPU inference stage.
    """
    renderer, profile = af3_renderer(tmp_path)
    catalog = af3_profile_catalog(profile)
    store = MemoryStore(cipher, hasher)
    caller = await principal(store)
    caller = caller.__class__(
        token_id=caller.token_id,
        token_prefix=caller.token_prefix,
        principal_id=caller.principal_id,
        tenant_id=caller.tenant_id,
        scopes=caller.scopes,
        models=frozenset({af3.MODEL_ID}),
        max_concurrency=caller.max_concurrency,
    )
    document = request_document(fold_job_count=fold_jobs)
    # The public submit path resolves the input manifest by artifact UUID, so a
    # submitted request names the stored artifact rather than a logical label.
    document["input_manifest"]["artifact_id"] = str(uuid4())
    pointer = dict(document["input_manifest"])
    artifacts = FakeArtifactAccess(pointer)
    repository = FakeScientificBatchRepository()
    controller = ScientificBatchController(
        repository=repository,
        cluster=FakeScientificBatchCluster(),
        controller_id="af3-adapter-test",
        namespace="fs2-academic-poc",
    )
    service = ScientificBatchService(
        store=store,
        repository=repository,
        controller=controller,
        profiles=catalog,
        scheduling=af3_scheduling(),
        artifacts=artifacts,
        execution_binding=renderer,
        plan_factory=renderer,
    )

    view = await service.submit(
        principal=caller,
        model_id=af3.MODEL_ID,
        request=document,
        idempotency_key=f"af3-{fold_jobs}-fold",
    )

    batch = view["batch"]
    assert batch["model_id"] == af3.MODEL_ID
    assert batch["variant_id"] == af3.VARIANT_ID
    stages = [item["stage_id"] for item in batch["stages"]]
    assert stages == ["data-pipeline", "inference"]

    assert len(repository.records) == 1
    record = next(iter(repository.records.values()))
    frozen = {item.stage_id: item for item in record.scheduling.stages}
    # One namespace for durable state, two queues so an MSA never holds GPU quota.
    assert {item.execution_namespace for item in frozen.values()} == {"fs2-academic-poc"}
    assert frozen["data-pipeline"].resolved_local_queue == "academic-scientific-cpu"
    assert frozen["data-pipeline"].resolved_cluster_queue == "reference-data-cpu"
    assert frozen["data-pipeline"].accelerator_count == 0
    assert frozen["inference"].resolved_local_queue == "academic-scientific"
    assert frozen["inference"].resolved_cluster_queue == "inference-accelerators"
    assert frozen["inference"].accelerator_count == 1

    # Idempotent replay returns the same operation and a byte-identical snapshot.
    replay = await service.submit(
        principal=caller,
        model_id=af3.MODEL_ID,
        request=document,
        idempotency_key=f"af3-{fold_jobs}-fold",
    )
    assert replay["batch"]["batch_id"] == batch["batch_id"]
    assert replay["batch"]["scheduling_snapshot_digest"] == batch["scheduling_snapshot_digest"]
    assert len(repository.records) == 1


@pytest.mark.asyncio
async def test_submit_is_refused_when_deployment_authorization_is_absent(
    tmp_path: Path, cipher: PayloadCipher, hasher: KeyedHasher
) -> None:
    """The negative case at the public boundary, not only inside the adapter."""
    renderer, profile = af3_renderer(tmp_path)
    unauthorized_value = deepcopy(dict(profile.value))
    unauthorized_value["access"] = {
        **unauthorized_value["access"],
        "operational_activation": "candidate-not-activated",
    }
    unauthorized = ScientificWorkloadProfile(MappingProxyType(unauthorized_value))
    catalog = af3_profile_catalog(unauthorized)
    store = MemoryStore(cipher, hasher)
    caller = await principal(store)
    caller = caller.__class__(
        token_id=caller.token_id,
        token_prefix=caller.token_prefix,
        principal_id=caller.principal_id,
        tenant_id=caller.tenant_id,
        scopes=caller.scopes,
        models=frozenset({af3.MODEL_ID}),
        max_concurrency=caller.max_concurrency,
    )
    document = request_document()
    document["input_manifest"]["artifact_id"] = str(uuid4())
    repository = FakeScientificBatchRepository()
    service = ScientificBatchService(
        store=store,
        repository=repository,
        controller=ScientificBatchController(
            repository=repository,
            cluster=FakeScientificBatchCluster(),
            controller_id="af3-adapter-test",
            namespace="fs2-academic-poc",
        ),
        profiles=catalog,
        scheduling=af3_scheduling(),
        artifacts=FakeArtifactAccess(dict(document["input_manifest"])),
        execution_binding=renderer,
        plan_factory=renderer,
    )

    # The public boundary refuses the run: an unauthorized deployment cannot
    # form an execution plan at all, so no Job and no durable state appear.
    with pytest.raises(ScientificProfileError, match="cannot form an execution plan"):
        await service.submit(
            principal=caller,
            model_id=af3.MODEL_ID,
            request=document,
            idempotency_key="af3-unauthorized",
        )
    # Nothing durable was created for a run that was never admissible.
    assert not repository.records


def build_handoff(
    workspace: Path,
    entries: list[tuple[str, bytes]],
    *,
    schema: str = af3.HANDOFF_SCHEMA,
    corrupt_digest: bool = False,
    corrupt_size: bool = False,
) -> Path:
    """Write a handoff directory in the shape the runtime documents."""
    handoff = workspace / af3.DATA_OUTPUT_DIR / af3.HANDOFF_DIR_NAME
    handoff.mkdir(parents=True)
    index_entries = []
    for relative, content in entries:
        payload = handoff / relative
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        index_entries.append(
            {
                "path": relative,
                "bytes": len(content) + (1 if corrupt_size else 0),
                "sha256": ("f" * 64) if corrupt_digest else digest,
            }
        )
    (handoff / af3.HANDOFF_INDEX).write_text(
        json.dumps({"schema": schema, "entries": index_entries}), encoding="utf-8"
    )
    return handoff


def data_invocation() -> Any:
    profile, profile_value = af3_qualified_profile()
    del profile
    plan = compile_plan(profile_value, request_document())
    return plan.invocation("data-pipeline", "main")


def test_collector_packages_the_handoff_with_relative_paths(tmp_path: Path) -> None:
    payload = json.dumps({"name": "job-a", "sequences": []}).encode()
    build_handoff(tmp_path, [("job-a/job-a_data.json", payload)])
    collected = af3.collect_data(data_invocation(), tmp_path)

    assert len(collected.artifacts) == 1
    artifact = collected.artifacts[0]
    assert artifact.name == "processed-input"
    assert artifact.media_type == "application/x-tar"
    assert artifact.compression is None
    assert artifact.path == tmp_path / af3.HANDOFF_PACKAGE
    assert collected.validation["validator_id"] == "alphafold3-data-validator-v1"
    assert collected.validation["fold_jobs"] == 1
    # Entries stay relative to the handoff directory so the GPU pod can
    # reconstruct them under a different mount.
    with tarfile.open(artifact.path) as archive:
        names = sorted(item.name for item in archive.getmembers() if item.isfile())
    assert names == ["./index.json", "./job-a/job-a_data.json"]
    assert all(not name.startswith("/") for name in names)
    # No partially written package survives.
    assert not (tmp_path / f".{af3.HANDOFF_PACKAGE}.partial").exists()


def test_collector_waits_for_an_atomically_complete_handoff(tmp_path: Path) -> None:
    with pytest.raises(CollectionPendingError, match=af3.HANDOFF_DIR_NAME):
        af3.collect_data(data_invocation(), tmp_path)

    (tmp_path / af3.DATA_OUTPUT_DIR / af3.HANDOFF_DIR_NAME).mkdir(parents=True)
    with pytest.raises(CollectionPendingError, match=af3.HANDOFF_INDEX):
        af3.collect_data(data_invocation(), tmp_path)


@pytest.mark.parametrize(
    ("kwargs", "entries", "match"),
    [
        ({"corrupt_digest": True}, [("job-a/job-a_data.json", b"{}")], "recorded digest"),
        ({"corrupt_size": True}, [("job-a/job-a_data.json", b"{}")], "recorded size"),
        ({"schema": "wrong/v1"}, [("job-a/job-a_data.json", b"{}")], "document"),
        ({}, [], "records no fold job"),
        (
            {},
            [("job-a/job-a_data.json", b"{}"), ("job-b/job-b_data.json", b"{}")],
            "more than one fold job",
        ),
    ],
)
def test_collector_refuses_an_untrustworthy_handoff(
    tmp_path: Path, kwargs: dict[str, Any], entries: list[tuple[str, bytes]], match: str
) -> None:
    build_handoff(tmp_path, entries, **kwargs)
    with pytest.raises(ScientificAdapterError, match=match):
        af3.collect_data(data_invocation(), tmp_path)


def test_collector_refuses_an_escaping_entry_path(tmp_path: Path) -> None:
    handoff = build_handoff(tmp_path, [("job-a/job-a_data.json", b"{}")])
    index = json.loads((handoff / af3.HANDOFF_INDEX).read_text())
    index["entries"][0]["path"] = "../../escaped.json"
    (handoff / af3.HANDOFF_INDEX).write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="contained and relative"):
        af3.collect_data(data_invocation(), tmp_path)


def test_the_two_runtime_artifacts_sit_on_two_different_storage_planes() -> None:
    """AlphaFold 3 is a mixed-plane consumer, and the licence decides which plane.

    The public reference bundle comes from the shared read-only reference plane;
    the licensed parameters come from the tenant-private academic claim. A single
    shared PVC holding both would put licensed bytes on the public plane, so the
    two planes are asserted to be distinct and each artifact is asserted to be on
    the correct side.
    """
    contract = json.loads(
        (REPO_ROOT / "catalog/runtime/contracts/scientific-execution-targets.json").read_text()
    )
    bindings = {
        (item["model_id"], item["stage_id"]): item
        for item in contract["bindings"]
        if item["model_id"] == af3.MODEL_ID
    }
    data_mount = next(
        item
        for item in bindings[(af3.MODEL_ID, "data-pipeline")]["mounts"]
        if item["artifact_id"] == af3.REFERENCE_ARTIFACT
    )
    parameter_mount = next(
        item
        for item in bindings[(af3.MODEL_ID, "inference")]["mounts"]
        if item["artifact_id"] == af3.PARAMETERS_ARTIFACT
    )

    # Public plane: an operator-owned host root, no tenant claim at all.
    assert data_mount["kind"] == "operator-host-path"
    assert data_mount["host_path"] == "/mnt/fs2-reference-data/data"
    assert data_mount["claim_name"] is None
    assert data_mount["claim_namespace"] is None
    assert data_mount["read_only"] is True

    # Tenant-private plane: the academic claim, in the academic namespace.
    assert parameter_mount["kind"] == "private"
    assert parameter_mount["claim_name"] == "academic-assets-runtime-rwx"
    assert parameter_mount["claim_namespace"] == "fs2-academic-poc"
    assert parameter_mount["host_path"] is None
    assert parameter_mount["read_only"] is True

    # The planes are genuinely different, and the licensed artifact never rides
    # the public one.
    assert data_mount["kind"] != parameter_mount["kind"]
    assert data_mount["claim_name"] != parameter_mount["claim_name"]
    assert parameter_mount["claim_name"] != "scientific-model-artifacts"

    # The adapter's own stage closure keeps one artifact per stage, so no stage
    # can ever hold both planes.
    closure = af3.STAGE_EXECUTION_CONTRACTS
    assert closure["data-pipeline"].runtime_artifacts == (af3.REFERENCE_ARTIFACT,)
    assert closure["inference"].runtime_artifacts == (af3.PARAMETERS_ARTIFACT,)
    assert set(closure["data-pipeline"].runtime_artifacts).isdisjoint(
        closure["inference"].runtime_artifacts
    )
