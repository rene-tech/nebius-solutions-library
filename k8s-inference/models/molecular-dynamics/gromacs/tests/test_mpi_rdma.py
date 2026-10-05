import pytest
from pathlib import Path

from fs2_gromacs import mpi


def test_rdma_is_fail_closed_and_never_claims_observed_transport(monkeypatch):
    monkeypatch.setenv("FS2_GROMACS_MPI_TRANSPORT", "ucx-rdma")
    monkeypatch.setenv("OMPI_MCA_btl", "self,sm,tcp")
    monkeypatch.setenv("GMX_DISABLE_DIRECT_GPU_COMM", "1")
    monkeypatch.setattr(mpi, "rdma_devices", lambda: [f"mlx5_{i}:1" for i in range(8)])
    result = mpi.configure_transport(2)
    assert "tcp" not in mpi.os.environ["UCX_TLS"]
    assert "OMPI_MCA_btl" not in mpi.os.environ
    assert "GMX_DISABLE_DIRECT_GPU_COMM" not in mpi.os.environ
    assert mpi.os.environ["UCX_IB_GPU_DIRECT_RDMA"] == "yes"
    assert result["rdma_requested"] and result["rdma"] is None
    assert result["transport_observed"] is None
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
        result = mpi.configure_transport(nodes)
        assert result["rdma"] is False
        assert "UCX_NET_DEVICES" not in mpi.os.environ
        assert "UCX_IB_GPU_DIRECT_RDMA" not in mpi.os.environ


def test_rdma_overlay_adds_only_matching_userspace_provider():
    recipe = (Path(__file__).resolve().parents[1] / "runtime/Containerfile.mpi-rdma").read_text()
    assert "RDMA_CORE_VERSION=39.0-1" in recipe
    assert '"ibverbs-providers=${RDMA_CORE_VERSION}"' in recipe
    assert "dpkg-query" in recipe and "/etc/libibverbs.d/mlx5.driver" in recipe
    assert "COPY --from=" not in recipe
    assert recipe.rstrip().endswith("USER 10001:10001")
