import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, ValidationError

from fs2_gromacs import MPI_PARAMETER_SCHEMA
from fs2_gromacs.contracts import canonical, normalize, request_schema
from fs2_gromacs.mpi import (
    configure_launcher,
    configure_transport,
    launch_command,
    validate_shape,
)
from fs2_gromacs.mpi_files import InputStager
from fs2_gromacs.mpi_rank import RECEIPT_PREFIX, bind_rank, read_rank_bindings
from fs2_gromacs.worker import Workflow


def request(nodes=2, gpus=None):
    value = {
        "schema": MPI_PARAMETER_SCHEMA,
        "nodes": nodes,
        "jobs": [
            {
                "id": "gang",
                "steps": [{"id": "md", "command": "mdrun", "args": ["-s", "run.tpr"]}],
            }
        ],
    }
    if gpus is not None:
        value["gpus_per_node"] = gpus
    return value


@pytest.mark.parametrize(
    "nodes,gpus", [(1, 1), (1, 2), (1, 4), (1, 8), (2, 8), (4, 4), (8, 2), (8, 1)]
)
def test_schema_and_normalizer_accept_bounded_shapes_without_changing_physics(
    nodes, gpus
):
    body = request(nodes, gpus)
    Draft202012Validator(request_schema(mpi=True)).validate(body)
    normalized = normalize(body, mpi=True)
    assert normalized["nodes"] * normalized.get("gpus_per_node", 1) <= 16
    assert (
        normalized["jobs"][0]["steps"][0]["args"] == body["jobs"][0]["steps"][0]["args"]
    )


@pytest.mark.parametrize(
    "nodes,gpus", [(0, 1), (9, 1), (3, 8), (5, 4), (2, 3), (2, True), (2, 16)]
)
def test_both_public_schema_and_runtime_reject_unreserved_shapes(nodes, gpus):
    body = request(nodes, gpus)
    with pytest.raises(ValidationError):
        Draft202012Validator(request_schema(mpi=True)).validate(body)
    with pytest.raises(ValidationError):
        normalize(body, mpi=True)


def test_defaults_preserve_legacy_checkpoint_recipe_and_explicit_default_equivalence(
    tmp_path,
):
    legacy = normalize(request(), mpi=True)
    explicit = normalize(request(gpus=1), mpi=True)
    assert "gpus_per_node" not in legacy
    assert explicit == legacy
    worker = Workflow(
        request(), job_id="gang", operation_id="op", workspace=tmp_path, mpi=True
    )
    legacy_hash = hashlib.sha256(
        canonical({"request": legacy, "job": "gang", "image": worker.engine_id})
    ).hexdigest()
    assert worker.recipe == legacy_hash
    changed = Workflow(
        request(gpus=8), job_id="gang", operation_id="op", workspace=tmp_path, mpi=True
    )
    assert changed.recipe != legacy_hash


def test_changed_gpu_shape_cannot_silently_resume_previous_native_checkpoint(tmp_path):
    first = Workflow(
        request(2, 8), job_id="gang", operation_id="op", workspace=tmp_path, mpi=True
    )
    first.data.mkdir()
    (first.data / "state.cpt").write_bytes(b"checkpoint for the frozen 2x8 recipe")
    first.checkpoint()
    changed = Workflow(
        request(2, 4), job_id="gang", operation_id="op", workspace=tmp_path, mpi=True
    )
    with pytest.raises(ValueError, match="another workflow"):
        changed.initialize()
    assert (
        first.data / "state.cpt"
    ).read_bytes() == b"checkpoint for the frozen 2x8 recipe"


@pytest.mark.parametrize("flag", ["-gpu_id", "-gputasks"])
def test_customer_device_mapping_cannot_override_local_rank_binding(flag):
    body = request(2, 8)
    body["jobs"][0]["steps"][0]["args"] += [flag, "0"]
    with pytest.raises(ValueError, match="platform-managed"):
        normalize(body, mpi=True)


def test_frozen_controller_shape_must_match_request(monkeypatch):
    monkeypatch.setenv("FS2_MPI_HOSTS", "first,second")
    monkeypatch.setenv("FS2_MPI_RANK", "0")
    monkeypatch.setenv("FS2_GROMACS_MPI_NODES", "2")
    monkeypatch.setenv("FS2_GROMACS_MPI_GPUS_PER_NODE", "8")
    monkeypatch.setenv("FS2_GROMACS_MPI_RANKS_PER_NODE", "8")
    monkeypatch.setenv("FS2_GROMACS_MPI_TOTAL_RANKS", "16")
    validate_shape(normalize(request(2, 8), mpi=True))
    monkeypatch.setenv("FS2_GROMACS_MPI_TOTAL_RANKS", "2")
    with pytest.raises(ValueError, match="TOTAL_RANKS"):
        validate_shape(normalize(request(2, 8), mpi=True))


@pytest.mark.parametrize("nodes,gpus", [(1, 1), (1, 8), (2, 8), (8, 1)])
def test_launcher_records_real_shape_and_keeps_device_visibility_local(
    tmp_path, monkeypatch, nodes, gpus
):
    hosts = ["localhost"] if nodes == 1 else [f"node-{i}" for i in range(nodes)]
    monkeypatch.setenv("FS2_MPI_HOSTS", ",".join(hosts))
    monkeypatch.setenv("FS2_MPI_RANK", "0")
    monkeypatch.delenv("FS2_GROMACS_MPI_TRANSPORT", raising=False)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "coordinator-only-UUID")
    calls = []
    monkeypatch.setattr(
        "fs2_gromacs.mpi.subprocess.run",
        lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0),
    )
    directory = tmp_path / ".fs2/mpi"
    configure_launcher(tmp_path, directory, 4, gpus)
    assert len(calls) == (nodes if nodes > 1 else 0)
    assert (directory / "hosts").read_text().splitlines() == [
        f"{host} slots={gpus}" for host in hosts
    ]
    receipt = json.loads((tmp_path / ".fs2/mpi-topology.json").read_text())
    assert receipt["ranks"] == nodes * gpus
    assert receipt["rank_per_node"] == gpus
    assert receipt["threads_per_rank"] == 4
    assert receipt["transport"] == ("ucx-local" if nodes == 1 else "tcp-host-staged")
    assert receipt["rdma"] is False and receipt["transport_observed"] is None
    command = launch_command(
        normalize(request(nodes, gpus), mpi=True), ["gmx_mpi", "mdrun", "-s", "run.tpr"]
    )
    assert command[command.index("-np") + 1] == str(nodes * gpus)
    assert command[command.index("--map-by") + 1] == f"ppr:{gpus}:node"
    assert "CUDA_VISIBLE_DEVICES" not in command
    assert "fs2_gromacs.mpi_rank" in command
    assert command[-4:] == ["gmx_mpi", "mdrun", "-s", "run.tpr"]


def test_transport_does_not_silently_fallback_or_claim_rdma(monkeypatch):
    monkeypatch.setenv("FS2_GROMACS_MPI_TRANSPORT", "ucx-local")
    with pytest.raises(ValueError, match="only supported within one"):
        configure_transport(2)
    monkeypatch.setenv("FS2_GROMACS_MPI_TRANSPORT", "rdma")
    with pytest.raises(ValueError, match="Unsupported"):
        configure_transport(2)
    monkeypatch.setenv("FS2_GROMACS_MPI_TRANSPORT", "tcp-host-staged")
    receipt = configure_transport(1)
    assert receipt["direct_gpu_communication"] == "disabled"
    assert receipt["rdma"] is False
    monkeypatch.setenv("FS2_GROMACS_MPI_TRANSPORT", "ucx-local")
    receipt = configure_transport(1)
    assert receipt["direct_gpu_communication"] == "autodetect"
    import os

    assert "GMX_DISABLE_DIRECT_GPU_COMM" not in os.environ
    assert os.environ["OMPI_MCA_pml"] == "ucx"
    assert os.environ["UCX_TLS"] == "self,sm,cuda_copy,cuda_ipc"


@pytest.mark.parametrize("local_rank", range(8))
def test_every_local_rank_uses_its_own_visible_gpu_uuid(monkeypatch, local_rank):
    identifiers = [f"GPU-00000000-0000-0000-0000-{i:012d}" for i in range(8)]
    monkeypatch.setattr("fs2_gromacs.mpi_rank.visible_gpu_uuids", lambda: identifiers)
    monkeypatch.setenv("OMPI_COMM_WORLD_LOCAL_RANK", str(local_rank))
    monkeypatch.setenv("OMPI_COMM_WORLD_LOCAL_SIZE", "8")
    monkeypatch.setenv("OMPI_COMM_WORLD_RANK", str(8 + local_rank))
    monkeypatch.setenv("OMPI_COMM_WORLD_SIZE", "16")
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "all-admitted-devices")
    receipt = bind_rank(nodes=2, gpus_per_node=8)
    import os

    assert os.environ["CUDA_VISIBLE_DEVICES"] == "all-admitted-devices"
    assert os.environ["GMX_GPU_ID"] == str(local_rank)
    assert receipt["world_rank"] == 8 + local_rank
    assert receipt["gpu_uuid"] == identifiers[local_rank]
    assert receipt["gromacs_gpu_id"] == local_rank
    assert receipt["gpu_binding"] == "GMX_GPU_ID"


def test_device_allocation_mismatch_fails_instead_of_oversubscribing(monkeypatch):
    monkeypatch.setenv("OMPI_COMM_WORLD_LOCAL_RANK", "0")
    monkeypatch.setenv("OMPI_COMM_WORLD_LOCAL_SIZE", "8")
    monkeypatch.setenv("OMPI_COMM_WORLD_RANK", "0")
    monkeypatch.setenv("OMPI_COMM_WORLD_SIZE", "16")
    monkeypatch.setattr("fs2_gromacs.mpi_rank.visible_gpu_uuids", lambda: ["GPU-one"])
    with pytest.raises(ValueError, match="Visible CUDA device count"):
        bind_rank(nodes=2, gpus_per_node=8)
    monkeypatch.setenv("OMPI_COMM_WORLD_SIZE", "2")
    with pytest.raises(ValueError, match="process topology"):
        bind_rank(nodes=2, gpus_per_node=8)


def test_rank_receipts_distinguish_missing_or_duplicate_device_evidence(tmp_path):
    log = tmp_path / "native.log"

    def line(rank, identifier):
        return (
            RECEIPT_PREFIX
            + json.dumps(
                {
                    "schema": "fs2-serve.nebius.ai/gromacs-mpi-rank-binding/v1",
                    "world_rank": rank,
                    "world_size": 2,
                    "gpu_uuid": identifier,
                    "host": "pod",
                }
            )
            + "\n"
        )

    log.write_text(
        line(0, "GPU-first") + line(1, "GPU-second") + "Performance: 123.4\n"
    )
    assert read_rank_bindings(log, expected_ranks=2)["rank_binding_evidence_complete"]
    log.write_text(line(0, "GPU-first") + line(1, "GPU-first"))
    assert not read_rank_bindings(log, expected_ranks=2)[
        "rank_binding_evidence_complete"
    ]
    log.write_text(line(0, "GPU-first"))
    assert not read_rank_bindings(log, expected_ranks=2)[
        "rank_binding_evidence_complete"
    ]


def test_one_node_input_staging_never_starts_ssh_or_zero_worker_executor(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FS2_MPI_HOSTS", "localhost")
    monkeypatch.setenv("FS2_MPI_RANK", "0")
    data = tmp_path / "data"
    data.mkdir()
    (data / "md.tpr").write_bytes(b"unchanged TPR")
    monkeypatch.setattr(
        "fs2_gromacs.mpi_files.subprocess.run",
        lambda *a, **kw: pytest.fail("no remote staging for one node"),
    )
    assert (
        InputStager(tmp_path).stage(data, ["-s", "md.tpr"], timeout=10)["peers"] == []
    )


def test_generated_public_mpi_contracts_match_canonical_contract():
    root = Path(__file__).resolve().parents[4]
    canonical_schema = request_schema(mpi=True)
    for relative in (
        "catalog/runtime/schema/gromacs-mpi-workflow-request.schema.json",
        "components/control-plane/src/fs2_serve/model_input_schemas/gromacs-mpi-workflow.json",
    ):
        assert json.loads((root / relative).read_text()) == canonical_schema


def test_one_node_main_needs_no_ssh_seed_or_server(tmp_path, monkeypatch):
    body = request(1, 8)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(body))
    monkeypatch.setattr(
        "sys.argv",
        [
            "mpi",
            "--workspace",
            str(tmp_path),
            "--request",
            str(path),
            "--operation-id",
            "op",
        ],
    )
    monkeypatch.setenv("FS2_MPI_HOSTS", "localhost")
    monkeypatch.setenv("FS2_MPI_RANK", "0")
    monkeypatch.delenv("FS2_MPI_SSH_SEED", raising=False)
    monkeypatch.setattr(
        "fs2_gromacs.mpi.prepare_keys",
        lambda *a, **kw: pytest.fail("must not create SSH keys"),
    )
    monkeypatch.setattr(
        "fs2_gromacs.mpi.subprocess.Popen",
        lambda *a, **kw: pytest.fail("must not start SSH"),
    )
    monkeypatch.setattr(
        "fs2_gromacs.mpi.run_coordinator",
        lambda *a, **kw: {
            "operation_id": "op",
            "job_id": "gang",
            "status": "succeeded",
            "error": None,
        },
    )
    from fs2_gromacs.mpi import main

    with pytest.raises(SystemExit) as outcome:
        main()
    assert outcome.value.code == 0
