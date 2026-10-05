import json
from types import SimpleNamespace

import pytest
import qualify_rdma as probe


def test_probe_uses_real_sixteen_rank_request_and_only_owned_full_nodes():
    assert probe.native.normalize(probe.REQUEST, mpi=True)["gpus_per_node"] == 8
    value = probe.manifest(
        "fs2-lynx-rdma-test",
        "registry/test@sha256:" + "a" * 64,
        sorted(probe.NODES),
        "0" * 64,
    )
    pod = value["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["topology.nebius.com/gpu-cluster-id"] == probe.CLUSTER
    assert pod["containers"][0]["resources"]["requests"]["rdma.fs2.nebius/hca"] == "1"
    assert pod["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] == "8"
    assert not pod["containers"][0]["securityContext"]["allowPrivilegeEscalation"]
    assert not any("hostPath" in v for v in pod["volumes"])
    assert "configure_launcher(w,d,request['threads'],8)" in probe.PROBE_CODE
    with pytest.raises(ValueError):
        probe.manifest("customer", "registry:test", sorted(probe.NODES), "0" * 64)
    locked = probe.manifest(
        "fs2-lynx-rdma-test",
        "registry/test@sha256:" + "a" * 64,
        sorted(probe.NODES),
        "0" * 64,
        ipc_lock=True,
    )
    context = locked["spec"]["replicatedJobs"][0]["template"]["spec"]["template"][
        "spec"
    ]["containers"][0]["securityContext"]
    assert context == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"], "add": ["IPC_LOCK"]},
    }
    file_locked = probe.manifest(
        "fs2-lynx-rdma-test",
        "registry/test@sha256:" + "a" * 64,
        sorted(probe.NODES),
        "0" * 64,
        file_ipc_lock=True,
    )
    pod = file_locked["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
    context = pod["containers"][0]["securityContext"]
    assert context == {
        "allowPrivilegeEscalation": True,
        "capabilities": {"drop": ["ALL"], "add": ["IPC_LOCK"]},
    }
    assert pod["securityContext"]["runAsUser"] == 10001
    assert not pod.get("hostIPC", False)
    assert not context.get("privileged", False)


def test_configuration_or_gpu_query_alone_is_not_transport_evidence():
    with pytest.raises(ValueError, match="did not pass"):
        probe.validate_transport("UCX_TLS=rc_x MPIX_Query_cuda_support=1")
    summary = json.dumps(
        {"kind": "summary", "status": "passed", "ranks": 16}, separators=(",", ":")
    )
    with pytest.raises(ValueError, match="endpoint"):
        probe.validate_transport(summary)
    log = summary + "\nep_cfg[2]: tag(rc_mlx5/mlx5_0:1)\n"
    proof = probe.validate_transport(log)
    assert proof["transport_observed"] == "rc_mlx5"
    assert not proof["gpudirect_zero_copy_observed"]
    with pytest.raises(ValueError, match="TCP"):
        probe.validate_transport(log + "ep_cfg[3]: tag(tcp/eth0)\n")


def test_ucx_119_first_use_cuda_protocol_tables_are_process_scoped():
    summary = json.dumps(
        {"kind": "summary", "status": "passed", "ranks": 16}, separators=(",", ":")
    )
    header = (
        "[1.0] [pod-a:1 :0] | ucp_context_0 inter-node cfg#10 | send from cuda/GPU0 |\n"
    )
    unrelated = "[1.0] [pod-b:2 :0] | 1..4M | zero-copy | rc_mlx5/mlx5_4:1 |\n"
    with pytest.raises(ValueError, match="endpoint"):
        probe.validate_transport(summary + "\n" + header + unrelated)
    actual = "[1.0] [pod-a:1 :0] | 1..4M | zero-copy | rc_mlx5/mlx5_4:1 |\n"
    proof = probe.validate_transport(summary + "\n" + header + unrelated + actual)
    assert proof["gpudirect_zero_copy_observed"]
    assert proof["cuda_inter_node_protocol_tables"] == 1


def test_rank_layout_requires_all_eight_distinct_rails_per_host():
    bindings = [
        {
            "world_rank": rank,
            "host": f"host{rank // 8}",
            "gpu_uuid": f"GPU-{rank}",
            "ucx_net_device": f"mlx5_{(rank + 4) % 8}:1",
            "rdma_layout": "one-topology-local-hca-per-rank",
        }
        for rank in range(16)
    ]

    def log(values):
        return "\n".join(
            "FS2_MPI_RANK_BINDING " + json.dumps(value) for value in values
        )

    assert len(probe.validate_rank_layout(log(bindings))) == 16
    bindings[1]["ucx_net_device"] = bindings[0]["ucx_net_device"]
    with pytest.raises(ValueError, match="eight HCAs"):
        probe.validate_rank_layout(log(bindings))


def test_host_collective_proof_requires_all_sizes_and_operations():
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

    def log(values):
        return "\n".join(json.dumps(row, separators=(",", ":")) for row in values)

    assert len(probe.validate_host_collectives(log(rows))["completed"]) == 9
    with pytest.raises(ValueError, match="incomplete"):
        probe.validate_host_collectives(log(rows[1:]))


def test_tcp_control_is_explicit_and_cannot_fall_back_for_science():
    for host, source in ((False, None), (True, "/fixture")):
        with pytest.raises(ValueError, match="never RDMA fallback"):
            probe.run(
                SimpleNamespace(
                    transport="tcp-host-staged", host_collectives=host, input=source
                )
            )
    value = probe.manifest(
        "fs2-lynx-rdma-control",
        "registry/test@sha256:" + "a" * 64,
        sorted(probe.NODES),
        "0" * 64,
        transport="tcp-host-staged",
    )
    env = value["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"][
        "containers"
    ][0]["env"]
    assert {"name": "FS2_GROMACS_MPI_TRANSPORT", "value": "tcp-host-staged"} in env
