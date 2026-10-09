import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fs2_gromacs import mpi
from fs2_gromacs.mpi_rank import match_rdma_devices
from fs2_gromacs.worker import Workflow


def test_rdma_is_fail_closed_and_never_claims_observed_transport(monkeypatch):
    monkeypatch.setenv("FS2_GROMACS_MPI_TRANSPORT", "ucx-rdma")
    monkeypatch.setenv("OMPI_MCA_btl", "self,sm,tcp")
    monkeypatch.setenv("GMX_DISABLE_DIRECT_GPU_COMM", "1")
    monkeypatch.setattr(mpi, "rdma_devices", lambda: [f"mlx5_{i}:1" for i in range(8)])
    monkeypatch.setattr(mpi, "check_rdma_memlock", lambda: {"status": "passed"})
    result = mpi.configure_transport(2)
    assert "tcp" not in mpi.os.environ["UCX_TLS"]
    assert "OMPI_MCA_btl" not in mpi.os.environ
    assert "GMX_DISABLE_DIRECT_GPU_COMM" not in mpi.os.environ
    assert mpi.os.environ["UCX_IB_GPU_DIRECT_RDMA"] == "yes"
    assert result["rdma_requested"] and result["rdma"] is None
    assert result["transport_observed"] is None
    assert result["ucx_queue_settings"] == mpi.RDMA_UCX_SETTINGS
    assert all(mpi.os.environ[name] == value for name, value in mpi.RDMA_UCX_SETTINGS.items())
    assert result["ucx_net_devices"].split(",") == [f"mlx5_{i}:1" for i in range(8)]
    with pytest.raises(ValueError, match="two-node"):
        mpi.configure_transport(1)


def test_host_sysfs_without_allocated_verbs_devices_is_not_rdma(tmp_path):
    sysfs = tmp_path / "sys"
    sysfs.mkdir()
    for i in range(8):
        (sysfs / f"mlx5_{i}").mkdir()
    with pytest.raises(ValueError, match="accessible HCA"):
        mpi.rdma_devices(sysfs, tmp_path / "dev")


def test_exact_eight_active_allocated_hcas(tmp_path):
    sysfs, devfs = tmp_path / "sys", tmp_path / "dev"
    devfs.mkdir()
    for i in range(8):
        port = sysfs / f"mlx5_{i}" / "ports" / "1"
        port.mkdir(parents=True)
        (port / "link_layer").write_text("InfiniBand\n")
        (port / "state").write_text("4: ACTIVE\n")
        (devfs / f"uverbs{i}").touch()
    assert mpi.rdma_devices(sysfs, devfs) == [f"mlx5_{i}:1" for i in range(8)]
    (sysfs / "mlx5_0" / "ports" / "1" / "state").write_text("1: DOWN\n")
    with pytest.raises(ValueError, match="active InfiniBand"):
        mpi.rdma_devices(sysfs, devfs)


def test_tcp_and_local_clear_rdma_resource_restrictions(monkeypatch):
    for nodes, mode in [(2, "tcp-host-staged"), (1, "ucx-local")]:
        monkeypatch.setenv("FS2_GROMACS_MPI_TRANSPORT", mode)
        monkeypatch.setenv("UCX_NET_DEVICES", "mlx5_0:1")
        monkeypatch.setenv("UCX_IB_GPU_DIRECT_RDMA", "yes")
        for name in mpi.RDMA_UCX_SETTINGS:
            monkeypatch.setenv(name, "9999")
        result = mpi.configure_transport(nodes)
        assert result["rdma"] is False
        assert "UCX_NET_DEVICES" not in mpi.os.environ
        assert "UCX_IB_GPU_DIRECT_RDMA" not in mpi.os.environ
        assert not set(mpi.RDMA_UCX_SETTINGS).intersection(mpi.os.environ)


def test_rdma_launcher_forwards_exact_bounded_queue_settings(monkeypatch):
    monkeypatch.setenv("FS2_GROMACS_MPI_HOSTFILE", "/tmp/admitted-hosts")
    for name, value in mpi.RDMA_UCX_SETTINGS.items():
        monkeypatch.setenv(name, value)
    command = mpi.launch_command({"nodes": 2, "gpus_per_node": 8}, ["gmx_mpi", "--version"])
    for name in mpi.RDMA_UCX_SETTINGS:
        assert command[command.index(name) - 1] == "-x"
    assert not any("MAX_BUFS" in name for name in mpi.RDMA_UCX_SETTINGS)


def test_singleton_tools_never_inherit_distributed_rdma_network(monkeypatch):
    workflow = Workflow.__new__(Workflow)
    workflow.mpi = True
    workflow.request = {"threads": 8}
    monkeypatch.setenv("FS2_GROMACS_MPI_TRANSPORT", "ucx-rdma")
    monkeypatch.setenv("OMPI_MCA_pml", "ucx")
    monkeypatch.setenv("OMPI_MCA_pml_ucx_tls", "rc_mlx5")
    monkeypatch.setenv("UCX_TLS", "rc_x,self,sm,cuda_copy,cuda_ipc")
    monkeypatch.setenv("UCX_NET_DEVICES", "mlx5_0:1,mlx5_1:1")
    singleton = workflow.command_environment()
    assert singleton["OMPI_MCA_pml"] == "ob1"
    assert singleton["OMPI_MCA_btl"] == "self"
    assert not any(key.startswith(("UCX_", "OMPI_MCA_pml_ucx_")) for key in singleton)
    distributed = workflow.command_environment(distributed=True)
    assert distributed["OMPI_MCA_pml"] == "ucx"
    assert distributed["UCX_TLS"] == "rc_x,self,sm,cuda_copy,cuda_ipc"
    assert mpi.os.environ["OMPI_MCA_pml"] == "ucx"
    workflow.mpi = False
    assert workflow.command_environment()["UCX_TLS"] == distributed["UCX_TLS"]


def test_rdma_overlay_adds_only_matching_userspace_provider():
    recipe = (Path(__file__).resolve().parents[1] / "runtime/Containerfile.mpi-rdma").read_text()
    assert "RDMA_CORE_VERSION=39.0-1" in recipe
    assert '"ibverbs-providers=${RDMA_CORE_VERSION}"' in recipe
    assert "dpkg-query" in recipe and "/etc/libibverbs.d/mlx5.driver" in recipe
    assert "COPY --from=runtime_source /opt/ompi /opt/ompi" in recipe
    assert 'Path("/opt/gromacs-mpi/bin/gmx_mpi").read_bytes() == (root/"gmx_mpi").read_bytes()' in recipe
    assert 'os.listxattr("/opt/gromacs-mpi/bin/gmx_mpi")' in recipe
    assert '0x02000001,1<<14,0,0,0' in recipe
    assert recipe.rstrip().endswith("USER 10001:10001")


def test_only_rdma_uses_fixed_capability_binary(monkeypatch):
    for transport in ("ucx-local", "tcp-host-staged"):
        monkeypatch.setenv("FS2_GROMACS_MPI_TRANSPORT", transport)
        assert mpi.workflow_binary() is None
    monkeypatch.setenv("FS2_GROMACS_MPI_TRANSPORT", "ucx-rdma")
    assert mpi.workflow_binary() == "/opt/fs2-rdma/bin/gmx_mpi"


def test_rdma_requires_effective_nonroot_capability_not_bounding_bit_only(monkeypatch):
    value = {"status": "passed", "uid": 10001, "euid": 10001,
             "cap_effective": "4000", "cap_permitted": "4000", "cap_bounding": "4000",
             "no_new_privs": 0, "locked_bytes": 64 * 1024 * 1024}
    monkeypatch.setattr(mpi.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(value)))
    assert mpi.check_rdma_memlock() == value
    value["cap_effective"] = "0"
    with pytest.raises(ValueError, match="non-root IPC_LOCK"):
        mpi.check_rdma_memlock()


def test_rdma_mapping_uses_pci_locality_not_mismatched_gpu_nic_index():
    gpu = {f"GPU-{i}": Path(f"/sys/devices/pci0000:00/bridge{i}/gpu") for i in range(8)}
    hca = {f"mlx5_{i}:1": Path(f"/sys/devices/pci0000:00/bridge{(i + 4) % 8}/nic") for i in range(8)}
    result = match_rdma_devices(gpu, hca)
    assert result == {f"GPU-{i}": f"mlx5_{(i + 4) % 8}:1" for i in range(8)}
    hca["mlx5_1:1"] = hca["mlx5_0:1"]
    with pytest.raises(ValueError, match="unique topology-local"):
        match_rdma_devices(gpu, hca)
