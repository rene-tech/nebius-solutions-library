"""Fresh real GROMACS CPU execution; transport/lifecycle remain local doubles.

Explicit opt-in: SAI_GMX_DOCKER_IMAGE=<already pulled image@sha256:digest>.
This is not the separate RTX -> L40S / multi-cluster / real-S3 acceptance gate.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest
from conftest import MemoryLifecycle, MemoryObjects, bundle
from fs2_gromacs.files import atomic_json

from scientific_ai.contracts import Attempt, FileRecord, GromacsRun, Owner, RuntimeProfile, StorageGrant
from scientific_ai.gromacs import GromacsApplication
from scientific_ai.storage import Artifacts


def fresh_argon_inputs():
    """216 synthetic argon particles; deterministic velocities; no customer data."""
    atoms = []
    for index in range(216):
        x, y, z = index % 6, (index // 6) % 6, index // 36
        atoms.append(
            f"{index + 1:5d}{'AR':<5}{'AR':>5}{index + 1:5d}"
            f"{0.1 + 0.5 * x:8.3f}{0.1 + 0.5 * y:8.3f}{0.1 + 0.5 * z:8.3f}"
        )
    return {
        "argon.gro": ("Synthetic recovery test\n216\n" + "\n".join(atoms) + "\n3.0 3.0 3.0\n").encode(),
        "topol.top": b"""[ defaults ]
1 2 no 1.0 1.0
[ atomtypes ]
AR 39.948 0.0 A 0.34 0.997
[ moleculetype ]
Argon 1
[ atoms ]
1 AR 1 AR AR 1 0.0 39.948
[ system ]
Synthetic argon
[ molecules ]
Argon 216
""",
        "md.mdp": b"""integrator = md
dt = 0.002
nsteps = 600000
nstlog = 1000
nstenergy = 1000
nstxout-compressed = 1000
cutoff-scheme = Verlet
nstlist = 20
coulombtype = Cut-off
rcoulomb = 1.0
vdwtype = Cut-off
rvdw = 1.0
pbc = xyz
tcoupl = no
pcoupl = no
constraints = none
gen-vel = yes
gen-temp = 120
gen-seed = 1741
""",
    }


@pytest.mark.skipif(not os.environ.get("SAI_GMX_DOCKER_IMAGE"), reason="explicit local native image required")
def test_actual_gromacs_checkpoint_restores_into_fresh_workspace_without_repeating_preparation(
    tmp_path, monkeypatch
):
    image = os.environ["SAI_GMX_DOCKER_IMAGE"]
    monkeypatch.setenv("SAI_GMX_DOCKER_WORKSPACE", str(tmp_path))
    profile = RuntimeProfile(
        revision="synthetic-cpu-v1",
        checkpoint_compatibility_id="gromacs-2026-4-cpu-test-v1",
        images={"cpu-test-only": image},
        allowed_clusters=("local-test",),
        gpu_preference=("cpu-test-only",),
    )
    owner = Owner(tenant_id="system", principal_id="qa")
    first = Attempt(
        owner=owner,
        root_operation_id="synthetic-recovery",
        attempt_id="attempt-1",
        epoch=1,
        cluster="local-test",
        gpu_class="cpu-test-only",
        image=image,
    )
    grant = StorageGrant(owner=owner, root_operation_id=first.root_operation_id, grant_ref="local-test-only")
    input_bytes = bundle(fresh_argon_inputs())
    record = FileRecord(
        path="input.tar.gz", sha256=hashlib.sha256(input_bytes).hexdigest(), size_bytes=len(input_bytes)
    )
    objects = MemoryObjects()
    objects.data["blobs/" + record.sha256] = input_bytes
    parameters = {
        "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
        "threads": 2,
        "segment_minutes": 0.1,
        "checkpoint_minutes": 0.1,
        "max_wall_seconds": 600,
        "jobs": [
            {
                "id": "argon",
                "steps": [
                    {
                        "id": "prepare",
                        "command": "grompp",
                        "args": [
                            "-f",
                            "md.mdp",
                            "-c",
                            "argon.gro",
                            "-p",
                            "topol.top",
                            "-o",
                            "run.tpr",
                            "-maxwarn",
                            "0",
                        ],
                        "expected_outputs": ["run.tpr"],
                    },
                    {
                        "id": "md",
                        "command": "mdrun",
                        "args": [
                            "-s",
                            "run.tpr",
                            "-deffnm",
                            "md",
                            "-nb",
                            "cpu",
                            "-pme",
                            "cpu",
                            "-update",
                            "cpu",
                        ],
                    },
                ],
            }
        ],
    }
    run = GromacsRun(job_id="argon", input_bundle=record, parameters=parameters)
    lifecycle = MemoryLifecycle(first)
    args = dict(
        run=run,
        grant=grant,
        profile=profile,
        lifecycle=lifecycle,
        gmx=str(Path(__file__).with_name("docker-gmx.sh")),
    )
    app = GromacsApplication(
        **args, attempt=first, workspace=tmp_path / "first", artifacts=Artifacts(objects)
    )
    publish = app.publish_checkpoint

    def lose_attempt_after_committed_native_checkpoint(marker):
        publish(marker)
        if any(file["path"].endswith(".cpt") for file in marker["files"]):
            app.worker.stopped = True  # loss of execution after a durable native checkpoint
            raise RuntimeError("synthetic attempt loss")

    app.worker.checkpoint_publisher = lose_attempt_after_committed_native_checkpoint
    first_result = app.execute()
    assert first_result["compute"] == "interrupted", (app.workspace / "result.json").read_text()
    snapshot = app.artifacts.load(lifecycle.head)
    assert "prepare" in snapshot.state["completed_steps"]
    md_commands = [item for item in snapshot.state["commands"] if item["command"][1] == "mdrun"]
    interrupted_step = md_commands[-1]["checkpoint_step"]
    assert 0 < interrupted_step < 600000, "fixture must interrupt mid-simulation, not after completion"
    second = Attempt.model_validate({**first.model_dump(), "attempt_id": "attempt-2", "epoch": 2})
    lifecycle.current = second
    # Hide the original local scratch completely; recovery gets only object bytes.
    (tmp_path / "first").rename(tmp_path / "old-scratch-not-used")
    restored = GromacsApplication(
        **args, attempt=second, workspace=tmp_path / "second", artifacts=Artifacts(objects)
    )
    receipt = restored.execute(lifecycle.head)
    assert receipt["export"] == "committed", (restored.workspace / "result.json").read_text()
    result = json.loads((restored.workspace / "result.json").read_text())
    assert result["completed_steps"] == ["prepare", "md"]
    assert sum(item["command"][1] == "grompp" for item in result["commands"]) == 1
    md_commands = [item for item in result["commands"] if item["command"][1] == "mdrun"]
    assert any("-cpi" in item["command"] for item in md_commands[1:])
    assert md_commands[-1]["checkpoint_step"] == 600000
    assert "GROMACS version:" in result["engine"]
    evidence = {
        "scope": "real GROMACS CPU; local transport/lifecycle doubles; not GPU or fleet qualification",
        "image": image,
        "input_sha256": record.sha256,
        "random_seed": 1741,
        "atoms": 216,
        "interrupted_at_step": interrupted_step,
        "final_step": md_commands[-1]["checkpoint_step"],
        "preparation_executions": 1,
        "attempts": 2,
        "result_reference": receipt["reference"],
    }
    atomic_json(tmp_path / "native-acceptance.json", evidence)
    print(json.dumps(evidence))
