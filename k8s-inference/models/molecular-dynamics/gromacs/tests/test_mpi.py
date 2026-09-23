from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from fs2_gromacs import MPI_PARAMETER_SCHEMA
from fs2_gromacs.contracts import normalize
from fs2_gromacs.mpi import peers, prepare_keys, ssh_options, peer_complete, wait_peer
from fs2_serve.scientific_batch.execution import (
    FileScientificManifestRenderer,
    COLLECTOR_CONTAINER_NAME,
)


def environment(monkeypatch, *, rank=0):
    monkeypatch.setenv("FS2_MPI_RANK", str(rank))
    monkeypatch.setenv("FS2_MPI_HOSTS", "run-gang-0-0.run,run-gang-1-0.run")
    monkeypatch.setenv("FS2_MPI_SSH_SEED", "12" * 32)


def test_mpi_peer_identity_is_attempt_scoped_and_not_a_platform_credential(
    tmp_path, monkeypatch
):
    environment(monkeypatch)
    directory = prepare_keys(
        tmp_path / "rank0", authorized_directory=tmp_path / "home0"
    )
    first = (directory / "client").read_bytes()
    assert (directory / "client").stat().st_mode & 0o777 == 0o600
    assert "StrictHostKeyChecking=yes" in ssh_options(directory)
    environment(monkeypatch, rank=1)
    peer = prepare_keys(tmp_path / "rank1", authorized_directory=tmp_path / "home1")
    assert not (peer / "client").exists()
    assert (tmp_path / "home1/authorized_keys").read_bytes() == (
        tmp_path / "home0/authorized_keys"
    ).read_bytes()
    assert (
        f"AuthorizedKeysFile {tmp_path}/home0/authorized_keys"
        in (directory / "sshd_config").read_text()
    )
    assert (peer / "known_hosts").read_bytes() == (
        directory / "known_hosts"
    ).read_bytes()
    assert (peer / "host").read_bytes() != (directory / "host").read_bytes()
    environment(monkeypatch)
    monkeypatch.setenv("FS2_MPI_SSH_SEED", "34" * 32)
    other = prepare_keys(tmp_path / "attempt2", authorized_directory=tmp_path / "home2")
    assert (other / "client").read_bytes() != first


def test_mpi_discovery_rejects_duplicate_or_non_dns_peers(monkeypatch):
    environment(monkeypatch)
    assert len(peers()) == 2
    for hosts in ["same,same", "a,other;bad", "only-one"]:
        monkeypatch.setenv("FS2_MPI_HOSTS", hosts)
        with pytest.raises(ValueError):
            peers()


@pytest.mark.parametrize(
    ("status", "code"), [("succeeded", 0), ("failed", 1), ("interrupted", 143)]
)
def test_peers_preserve_leader_terminal_state(tmp_path, status, code):
    peer_complete(tmp_path, status)
    assert wait_peer(tmp_path, 1) == code


def test_jobset_config_has_distinct_nodes_and_only_rank_zero_publishes():
    pod = {
        "spec": {
            "containers": [
                {
                    "name": COLLECTOR_CONTAINER_NAME,
                    "command": [
                        "python",
                        "-m",
                        "fs2_serve.scientific_batch.companion",
                        "collect",
                    ],
                    "env": [],
                }
            ]
        }
    }
    resource = SimpleNamespace(
        name="gromacs-abc",
        invocation=SimpleNamespace(working_directory="/mnt/fs2-scientific/probe"),
    )
    FileScientificManifestRenderer._configure_mpi_pod(pod, resource)
    spec = pod["spec"]
    affinity = spec["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ][0]
    assert affinity["topologyKey"] == "kubernetes.io/hostname"
    assert affinity["labelSelector"]["matchLabels"] == {
        "jobset.sigs.k8s.io/jobset-name": "gromacs-abc"
    }
    collector = spec["containers"][0]
    assert "--peer-collector" in collector["command"]
    assert collector["command"][-4:] == [
        "python",
        "-m",
        "fs2_serve.scientific_batch.companion",
        "collect",
    ]
    assert collector["env"][0]["valueFrom"]["fieldRef"]["fieldPath"].endswith(
        "['jobset.sigs.k8s.io/job-index']"
    )


def test_mpi_does_not_advertise_an_uninstalled_plumed_kernel():
    request = {
        "schema": MPI_PARAMETER_SCHEMA,
        "jobs": [
            {
                "id": "gang",
                "steps": [
                    {
                        "id": "md",
                        "command": "mdrun",
                        "args": ["-s", "md.tpr"],
                        "plumed_input": "plumed.dat",
                    }
                ],
            }
        ],
    }
    with pytest.raises(ValidationError):
        normalize(request, mpi=True)
