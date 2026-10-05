import copy
import json

import bind_rdma as binder
import pytest
from test_activate_mpi import contracts

IMAGE = "registry.test/runtime@sha256:" + "a" * 64


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def inventory(directory, nodes=1, gpus=1, commands=None):
    data = directory / "workspace/data/value.log"
    data.parent.mkdir(parents=True)
    data.write_text("synthetic test-only inter-node cfg#2 tag(rc_mlx5/mlx5_0:1)\n")
    result = {
        "status": "succeeded",
        "inventory_complete": True,
        "nodes": nodes,
        "gpus_per_node": gpus,
        "mpi_ranks": nodes * gpus,
        "files": [
            {
                "path": data.name,
                "size_bytes": data.stat().st_size,
                "sha256": binder.sha(data),
            }
        ],
        "commands": commands or [],
    }
    save(directory / "workspace/result.json", result)
    return {
        "status": "passed",
        "errors": [],
        "customer_path_tested": False,
        "native_result_sha256": binder.sha(directory / "workspace/result.json"),
    }


def generic(directory, pool, nodes=1):
    validation = inventory(directory, nodes)
    validation.update(
        expected_failure_case=False, checks={"all_rank_bindings_complete": True}
    )
    receipt = {
        "image": IMAGE,
        "image_id": IMAGE,
        "allowed_image_digests": [IMAGE.split("@")[1]],
        "validation": validation,
        "worker_exit_code": 0,
        "customer_path_tested": False,
        "gpus": 1,
        "input_sha256": "b" * 64,
        "request_sha256": "c" * 64,
        "finished_at": "2026-10-05T18:57:00Z",
        "pod_uid": "local",
        "node": "local-node",
    }
    if nodes == 2:
        receipt.update(
            transport="tcp-host-staged",
            rdma=False,
            pods=[{"uid": str(i), "node": str(i), "image_id": IMAGE} for i in range(2)],
        )
    save(directory / "receipt.json", receipt)
    save(
        directory / "capacity-before.json",
        {"labels": {"accelerator.fs2.nebius/pool-id": pool}},
    )


def rdma(directory):
    bindings = [
        {
            "world_rank": i,
            "world_size": 16,
            "local_rank": i % 8,
            "local_size": 8,
            "host": f"host-{i // 8}",
            "gpu_uuid": f"GPU-{i}",
            "ucx_net_device": f"mlx5_{i % 8}:1",
            "rdma_layout": "one-topology-local-hca-per-rank",
            "visible_gpu_count_before_binding": 8,
            "gromacs_gpu_id": i % 8,
        }
        for i in range(16)
    ]
    commands = [
        {
            "command": ["mdrun"],
            "log": "value.log",
            "exit_code": 0,
            "checkpoint_step": 50000,
            "rank_binding_evidence_complete": True,
            "rank_bindings": bindings,
        }
        for _ in range(3)
    ]
    validation = inventory(directory, 2, 8, commands)
    validation["repeats"] = [{"steps": 50000} for _ in range(3)]
    buffer = json.dumps(
        {"kind": "summary", "status": "passed", "ranks": 16}, separators=(",", ":")
    )
    buffer += "\nep_cfg[2]: tag(rc_mlx5/mlx5_0:1)\n"
    buffer += "\n".join("FS2_MPI_RANK_BINDING " + json.dumps(row) for row in bindings)
    (directory / "buffer-0.log").write_text(buffer)
    rows = [
        {
            "kind": "host-collective",
            "collective": name,
            "message_bytes": size,
            "status": "passed",
        }
        for name in ("Bcast", "Scatterv", "Alltoall")
        for size in (9000000, 16777216, 33554432)
    ]
    rows.append({"kind": "host-collectives-summary", "status": "passed", "ranks": 16})
    host = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows)
    (directory / "host-0.log").write_text(host)
    pods, records = [], []
    for i, node in enumerate(sorted(binder.qualify_rdma.NODES)):
        name, uid = f"pod-{i}", f"uid-{i}"
        resources = {
            kind: {"nvidia.com/gpu": "8", binder.prepare_rdma.RESOURCE: "1"}
            for kind in ("requests", "limits")
        }
        pods.append(
            {
                "metadata": {"name": name, "uid": uid},
                "spec": {
                    "nodeName": node,
                    "hostname": f"host-{i}",
                    "containers": [
                        {"name": "runtime", "image": IMAGE, "resources": resources}
                    ],
                },
                "status": {
                    "containerStatuses": [{"name": "runtime", "imageID": IMAGE}]
                },
            }
        )
        records.append({"name": name, "uid": uid, "node": node, "image_id": IMAGE})
    save(directory / "pods.json", pods)
    save(
        directory / "receipt.json",
        {
            "status": "passed",
            "native_only": True,
            "science_executed": True,
            "transport_requested": "ucx-rdma",
            "cleanup": "owned JobSet/Pods deleted; absence observed",
            "image": IMAGE,
            "pods": records,
            "rank_bindings": bindings,
            "rank_outcomes": [{"rank": i, "exit_code": 0} for i in range(2)],
            "transport_proof": binder.qualify_rdma.validate_transport(buffer),
            "host_collectives": binder.qualify_rdma.validate_host_collectives(host),
            "finished_at": "2026-10-05T18:58:00Z",
            "native_md": {
                "validation": validation,
                "input_sha256": "b" * 64,
                "request_sha256": "c" * 64,
                "rank_outcomes": [{"rank": i, "worker_exit_code": 0} for i in range(2)],
            },
        },
    )


@pytest.fixture
def cases(tmp_path):
    directories = [
        tmp_path / name for name in ("local-h100", "local-l40s", "tcp", "rdma")
    ]
    generic(directories[0], "h100-1x")
    generic(directories[1], "l40s-4x")
    generic(directories[2], "h100-1x", 2)
    rdma(directories[3])
    return directories


def catalogs():
    execution, catalog, _, _ = contracts()
    shape = {
        "id": "multi-node-8gpu",
        "min_parallelism": 2,
        "max_parallelism": 2,
        "placement": {
            "accelerator": {
                "count": 8,
                "pool_ids": [binder.qualify_rdma.POOL],
                "resource_name": "nvidia.com/gpu",
            }
        },
    }
    catalog["profiles"][-1]["workload"]["stages"][0]["execution_shapes"].append(shape)
    execution["models"][-1]["stages"][0]["execution_shapes"] = [copy.deepcopy(shape)]
    reference = binder.activation.digest(
        {"schema": execution["schema"], "models": execution["models"]}
    )
    execution["qualification_baselines"] = {
        reference: [row["model_id"] for row in execution["models"]]
    }
    for profile in catalog["profiles"]:
        profile["qualification"]["execution_map_sha256"] = reference
    return catalog, execution


def test_additive_candidate_preserves_old_shapes_apps_and_never_fabricates_public_proof(
    cases,
):
    source = catalogs()
    before = copy.deepcopy(source)
    candidates = binder.prepare(*source, *cases)
    assert source == before
    profiles = candidates["scientific-workload-profiles.candidate.json"]
    execution = candidates["scientific-execution-map.candidate.json"]
    assert execution["models"][:-1] == source[1]["models"][:-1]
    assert execution["snapshot_bundles"] == source[1]["snapshot_bundles"]
    assert (
        execution["models"][-1]["stages"][0]["execution_shapes"][:-1]
        == source[1]["models"][-1]["stages"][0]["execution_shapes"]
    )
    for old, new in zip(
        source[0]["profiles"][:-1], profiles["profiles"][:-1], strict=True
    ):
        observed = copy.deepcopy(new)
        observed["qualification"]["execution_map_sha256"] = old["qualification"][
            "execution_map_sha256"
        ]
        assert observed == old
    qualified = profiles["profiles"][-1]["qualification"]
    assert qualified["public_completion_receipt_sha256"] is None
    assert qualified["scheduler_eligibility_receipt_sha256"] is None
    assert (
        profiles["profiles"][-1]["workload"]["stages"][0]["execution_shapes"][-1]["id"]
        == "multi-node-8gpu-rdma"
    )
    proof = candidates["runtime-proofs.json"]["gromacs-mpi"]
    assert len(proof["tests"]) == 4 and proof["customer_ready"] is False
    assert proof["customer_paths_tested"] is False
    binder.activation.validate_profile_qualifications(profiles["profiles"], execution)


@pytest.mark.parametrize(
    "change",
    [
        "probe-only",
        "image",
        "bytes",
        "result-sha",
        "host",
        "hca",
        "tcp",
        "missing-host-check",
        "node-exit",
        "incomplete-native",
    ],
)
def test_rejects_incomplete_or_mismatched_rdma_evidence(cases, change):
    directory = cases[3]
    receipt = binder.read(directory / "receipt.json")
    if change == "probe-only":
        receipt["science_executed"] = False
    elif change == "image":
        receipt["image"] = "registry.test/runtime@sha256:" + "f" * 64
    elif change == "bytes":
        (directory / "workspace/data/value.log").write_text("mutated")
    elif change == "result-sha":
        receipt["native_md"]["validation"]["native_result_sha256"] = "0" * 64
    elif change in {"host", "hca"}:
        log = directory / "buffer-0.log"
        log.write_text(
            log.read_text().replace('"host-0"', '"foreign"')
            if change == "host"
            else log.read_text().replace("mlx5_1:1", "mlx5_0:1")
        )
    elif change == "tcp":
        log = directory / "buffer-0.log"
        log.write_text(log.read_text() + "\nep_cfg[3]: tag(tcp/eth0)\n")
    elif change == "missing-host-check":
        (directory / "host-0.log").write_text("")
    elif change == "node-exit":
        receipt["native_md"]["rank_outcomes"][1]["worker_exit_code"] = 1
    else:
        receipt["native_md"]["validation"]["repeats"] = []
    save(directory / "receipt.json", receipt)
    with pytest.raises(ValueError):
        binder.prepare(*catalogs(), *cases)


def test_different_compatibility_image_and_foreign_paths_reject(cases):
    path = cases[1] / "receipt.json"
    value = binder.read(path)
    value["image"] = "registry.test/runtime@sha256:" + "d" * 64
    save(path, value)
    with pytest.raises(ValueError, match="one exact image"):
        binder.prepare(*catalogs(), *cases)
    value["image"] = IMAGE
    save(path, value)
    result_path = cases[1] / "workspace/result.json"
    result = binder.read(result_path)
    result["files"][0]["path"] = "../../outside"
    save(result_path, result)
    value["validation"]["native_result_sha256"] = binder.sha(result_path)
    save(path, value)
    with pytest.raises(ValueError, match="inventory bytes"):
        binder.prepare(*catalogs(), *cases)


def test_native_tcp_fallback_rejects_even_if_device_probe_and_file_hashes_pass(cases):
    directory = cases[3]
    log = directory / "workspace/data/value.log"
    log.write_text(log.read_text() + "inter-node cfg#3 tag(tcp/eth0)\n")
    result_path = directory / "workspace/result.json"
    result = binder.read(result_path)
    result["files"][0].update(sha256=binder.sha(log), size_bytes=log.stat().st_size)
    save(result_path, result)
    receipt = binder.read(directory / "receipt.json")
    receipt["native_md"]["validation"]["native_result_sha256"] = binder.sha(result_path)
    save(directory / "receipt.json", receipt)
    with pytest.raises(ValueError, match="TCP fallback"):
        binder.prepare(*catalogs(), *cases)


def test_cli_writes_only_private_candidates_and_refuses_existing_output(
    cases, tmp_path, monkeypatch
):
    catalog, execution = catalogs()
    profile_path, execution_path = (
        tmp_path / "profiles.json",
        tmp_path / "execution.json",
    )
    save(profile_path, catalog)
    save(execution_path, execution)
    before = (profile_path.read_bytes(), execution_path.read_bytes())
    output = tmp_path / "candidate"
    argv = [
        "bind_rdma",
        "--profiles",
        str(profile_path),
        "--execution",
        str(execution_path),
        "--output",
        str(output),
    ]
    for option, path in zip(
        ("local-h100", "local-l40s", "tcp", "rdma"), cases, strict=True
    ):
        argv += ["--" + option, str(path)]
    monkeypatch.setattr("sys.argv", argv)
    binder.main()
    assert (profile_path.read_bytes(), execution_path.read_bytes()) == before
    assert len(list(output.iterdir())) == 4
    with pytest.raises(FileExistsError):
        binder.main()
