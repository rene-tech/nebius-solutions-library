"""Prepare a strictly additive MPI/RDMA candidate from retained native evidence.

This command writes a new output directory only. It neither changes canonical
contracts nor deploys, and native proof never fabricates public-path receipts.
"""

import argparse
import copy
import hashlib
import json
import os
import re
from pathlib import Path

import prepare_rdma
import qualify_rdma
from bind_candidates import SOLUTION, activation, bind, evidence


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def native_result(directory, validation):
    path = directory / "workspace/result.json"
    result = read(path)
    require(
        validation.get("status") == "passed"
        and validation.get("errors") == []
        and validation.get("customer_path_tested") is False
        and validation.get("native_result_sha256") == sha(path)
        and result.get("status") == "succeeded"
        and result.get("inventory_complete") is True
        and result.get("files"),
        "Require a successful, SHA-bound native scientific result, not probe-only evidence",
    )
    data = (directory / "workspace/data").resolve()
    names = set()
    for item in result["files"]:
        file = data / item["path"]
        require(
            file.resolve().is_relative_to(data)
            and item["path"] not in names
            and file.is_file()
            and file.stat().st_size == item["size_bytes"]
            and sha(file) == item["sha256"],
            "Retained native inventory bytes differ from the qualified result",
        )
        names.add(item["path"])
    return result


def observed_image(image, identities):
    require(
        re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image), "Immutable image required"
    )
    digest = image.rsplit("@", 1)[1]
    require(
        identities and all(value.rsplit("@", 1)[-1] == digest for value in identities),
        "Observed image identity does not match the qualified immutable image",
    )


def rank_layout(bindings, hosts):
    # Reuse the actual sixteen-rank/eight-HCA parser, then also bind those
    # hostnames to the observed Pods and check each native local-rank layout.
    qualify_rdma.validate_rank_layout(
        "\n".join("FS2_MPI_RANK_BINDING " + json.dumps(row) for row in bindings)
    )
    require(
        {row["host"] for row in bindings} == hosts,
        "Rank hostnames differ from observed Pods",
    )
    for host in hosts:
        local = [row for row in bindings if row["host"] == host]
        require(
            {row.get("local_rank") for row in local} == set(range(8)),
            "Incomplete local ranks",
        )
        require(
            all(
                row.get("world_size") == 16
                and row.get("local_size") == 8
                and row.get("visible_gpu_count_before_binding") == 8
                and row.get("gromacs_gpu_id") == row["local_rank"]
                for row in local
            ),
            "Native rank/GPU layout is not exactly two by eight",
        )


def rdma_evidence(directory):
    receipt = read(directory / "receipt.json")
    science = receipt.get("native_md", {})
    require(
        receipt.get("status") == "passed"
        and receipt.get("native_only") is True
        and receipt.get("science_executed") is True
        and receipt.get("transport_requested") == "ucx-rdma"
        and receipt.get("cleanup") == "owned JobSet/Pods deleted; absence observed",
        "Require completed native RDMA science and exact-resource cleanup",
    )
    result = native_result(directory, science.get("validation", {}))
    require(
        (result.get("nodes"), result.get("gpus_per_node"), result.get("mpi_ranks"))
        == (2, 8, 16),
        "RDMA scientific result is not exactly two nodes and sixteen GPUs",
    )
    for outcomes, field in (
        (receipt.get("rank_outcomes", []), "exit_code"),
        (science.get("rank_outcomes", []), "worker_exit_code"),
    ):
        require(
            len(outcomes) == 2
            and {row.get("rank") for row in outcomes} == {0, 1}
            and all(row.get(field) == 0 for row in outcomes),
            "Both native node processes must succeed",
        )
    pods = read(directory / "pods.json")
    recorded = receipt.get("pods", [])
    require(
        len(pods) == len(recorded) == 2
        and {p["spec"]["nodeName"] for p in pods} == qualify_rdma.NODES,
        "Missing the exact two observed full H100 nodes",
    )
    for pod in pods:
        row = next((r for r in recorded if r["uid"] == pod["metadata"]["uid"]), None)
        require(
            row
            and row["name"] == pod["metadata"]["name"]
            and row["node"] == pod["spec"]["nodeName"],
            "Observed Pod UID/name/node differs from the receipt",
        )
        container = prepare_rdma.one(pod["spec"]["containers"], "name", "runtime")
        status = prepare_rdma.one(pod["status"]["containerStatuses"], "name", "runtime")
        require(container["image"] == receipt["image"], "Pod requested another image")
        observed_image(receipt["image"], [row["image_id"], status["imageID"]])
        require(
            all(
                container["resources"][kind].get("nvidia.com/gpu") == "8"
                and container["resources"][kind].get(prepare_rdma.RESOURCE) == "1"
                for kind in ("requests", "limits")
            ),
            "Pod lacks exact GPU/RDMA allocation",
        )
    transport = qualify_rdma.validate_transport(
        (directory / "buffer-0.log").read_text()
    )
    host = qualify_rdma.validate_host_collectives(
        (directory / "host-0.log").read_text()
    )
    bindings = qualify_rdma.validate_rank_layout(
        (directory / "buffer-0.log").read_text()
    )
    require(
        transport == receipt.get("transport_proof")
        and host == receipt.get("host_collectives")
        and bindings == receipt.get("rank_bindings"),
        "Recorded transport proof differs from raw logs",
    )
    hosts = {p["spec"]["hostname"] for p in pods}
    rank_layout(bindings, hosts)
    runs = [row for row in result["commands"] if "mdrun" in row["command"]]
    expected = science["validation"].get("repeats", [])
    require(
        len(runs) == len(expected) == 3
        and all(
            row.get("exit_code") == 0
            and row.get("checkpoint_step") == repeat.get("steps")
            and row.get("checkpoint_step", 0) > 0
            and row.get("rank_binding_evidence_complete") is True
            for row, repeat in zip(runs, expected, strict=True)
        ),
        "Require three fully completed native scientific repeats",
    )
    for run in runs:
        rank_layout(run.get("rank_bindings", []), hosts)
        log_name = run.get("log")
        require(
            log_name in {item["path"] for item in result["files"]},
            "Native MPI log is not SHA-bound",
        )
        log = (directory / "workspace/data" / log_name).read_text(errors="replace")
        require(
            "inter-node cfg#" in log and "rc_mlx5/mlx5_" in log and "tcp/" not in log,
            "Native science lacks accelerated inter-node transport or records TCP fallback",
        )
    return {
        "case": directory.name,
        "pool": qualify_rdma.POOL,
        "nodes": 2,
        "gpus_per_node": 8,
        "runtime_image": receipt["image"],
        "receipt_sha256": sha(directory / "receipt.json"),
        "native_result_sha256": sha(directory / "workspace/result.json"),
        "input_sha256": science["input_sha256"],
        "request_sha256": science["request_sha256"],
        "finished_at": receipt["finished_at"],
        "inventory_rehashed_files": len(result["files"]),
        "transport_proof": transport,
        "host_collectives": host,
        "observed_pods": recorded,
        "checks": {"native_science": True, "all_rank_bindings_complete": True},
    }


def prepare(catalog, execution, local_h100, local_l40s, tcp, rdma):
    directories = (local_h100, local_l40s, tcp)
    proof = evidence(directories, mpi=True)
    for directory, pool, nodes in (
        (local_h100, "h100-", 1),
        (local_l40s, "l40s-", 1),
        (tcp, "h100-", 2),
    ):
        receipt = read(directory / "receipt.json")
        result = native_result(directory, receipt["validation"])
        require(
            read(directory / "capacity-before.json")["labels"][
                "accelerator.fs2.nebius/pool-id"
            ].startswith(pool)
            and result.get("nodes") == nodes
            and result.get("gpus_per_node") == 1,
            "Compatibility case has the wrong pool or GPU/node shape",
        )
        identities = [receipt["image_id"]]
        if nodes == 2:
            pods = receipt.get("pods", [])
            require(
                len(pods) == 2
                and len({p["uid"] for p in pods}) == 2
                and len({p["node"] for p in pods}) == 2
                and receipt.get("transport") == "tcp-host-staged"
                and receipt.get("rdma") is False,
                "TCP compatibility must cover two observed, distinct nodes",
            )
            identities.extend(p["image_id"] for p in pods)
        else:
            require(
                receipt.get("pod_uid") and receipt.get("node"),
                "Missing native Pod identity",
            )
        observed_image(receipt["image"], identities)
    rdma_proof = rdma_evidence(rdma)
    require(
        rdma_proof.pop("runtime_image") == proof["runtime_image"],
        "All four native cases must use the same image",
    )
    proof["tests"].append(rdma_proof)
    proof["recorded_at"] = max(test["finished_at"] for test in proof["tests"])
    profiles, desired = copy.deepcopy(catalog), copy.deepcopy(execution)
    fragment = prepare_rdma.shape_fragments(profiles, desired)
    prepare_rdma.one(profiles["profiles"], "model_id", "gromacs-mpi")["workload"][
        "stages"
    ][0]["execution_shapes"].append(fragment["profile_shape"])
    prepare_rdma.one(desired["models"], "model_id", "gromacs-mpi")["stages"][0][
        "execution_shapes"
    ].append(fragment["execution_shape"])
    # Project unchanged Apps against the original map before the generic
    # identity binder sees the additive shape; never discard prior receipts.
    desired["qualification_baselines"] = activation.rebase_qualification_baselines(
        execution, desired, {"gromacs-mpi"}
    )
    profiles["profiles"] = activation.rebase_profile_qualifications(
        profiles["profiles"], execution, desired, {"gromacs-mpi"}
    )
    recipe = activation.source_recipe(SOLUTION, mpi_rdma=True)
    profiles, desired = bind(
        profiles,
        desired,
        {"gromacs-mpi": proof},
        {"gromacs-mpi": activation.digest(recipe)},
    )
    return {
        "scientific-workload-profiles.candidate.json": profiles,
        "scientific-execution-map.candidate.json": desired,
        "runtime-proofs.json": {"gromacs-mpi": proof},
        "source-recipes.json": {"gromacs-mpi": recipe},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("local-h100", "local-l40s", "tcp", "rdma", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    contracts = SOLUTION / "catalog/runtime/contracts"
    parser.add_argument(
        "--profiles", type=Path, default=contracts / "scientific-workload-profiles.json"
    )
    parser.add_argument(
        "--execution", type=Path, default=contracts / "scientific-execution-map.json"
    )
    args = parser.parse_args()
    candidates = prepare(
        read(args.profiles),
        read(args.execution),
        args.local_h100,
        args.local_l40s,
        args.tcp,
        args.rdma,
    )
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in candidates.items():
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(
        json.dumps(
            {
                "candidate_directory": str(args.output),
                "deployed": False,
                "canonical_contracts_changed": False,
                "customer_ready": False,
                "added_shape": prepare_rdma.SHAPE,
                "models_rebound": ["gromacs-mpi"],
            }
        )
    )


if __name__ == "__main__":
    main()
