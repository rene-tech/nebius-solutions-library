"""Synthetic storage/process doubles; these tests do not qualify native science or GPUs."""

import copy
import hashlib
import json
import re
import struct
from importlib import import_module
from pathlib import Path

import httpx
import pytest
from conftest import MemoryLifecycle, MemoryObjects, bundle
from pydantic import ValidationError

from scientific_ai.contracts import Attempt, FileRecord, Owner, RuntimeProfile, Snapshot, StorageGrant
from scientific_ai.delivery import retained_workspace
from scientific_ai.gromacs import DeliveryPending
from scientific_ai.native_engines import NativeEngineUnavailable, engine_for
from scientific_ai.native_md import NativeMDApplication, NativeMDRun
from scientific_ai.storage import Artifacts


@pytest.fixture(autouse=True)
def native_sources(monkeypatch):
    # Source-only seam until the integrator adds the unchanged packages to the
    # wheel. Importing scientific_ai itself must not require all three runtimes.
    root = Path(__file__).resolve().parents[3] / "models" / "molecular-dynamics"
    for name in ("amber", "namd", "lammps"):
        monkeypatch.syspath_prepend(str(root / name / "runtime"))


@pytest.fixture(params=["amber", "namd", "lammps"])
def engine(request):
    return request.param


def parameters(engine):
    steps = []
    for name in ("prepare", "analyze"):
        step = {"id": name, "expected_outputs": [name + ".dat"]}
        if engine == "namd":
            step.update(config=name + ".in", mode="native")
        else:
            step["input"] = name + ".in"
        if engine == "amber":
            step["kind"] = "tleap"
        steps.append(step)
    return {
        "schema": f"fs2-serve.nebius.ai/{engine}-workflow-request/v1",
        "jobs": [{"id": "replica", "steps": steps}],
    }


@pytest.fixture
def factory(tmp_path):
    def make(engine, *, request=None, files=None):
        owner = Owner(tenant_id="system", principal_id="qa")
        profile = RuntimeProfile(
            revision=engine + "-synthetic-v1",
            checkpoint_compatibility_id=engine + "-synthetic-v1",
            images={
                "h100": "example.invalid/native@sha256:" + "a" * 64,
                "l40s": "example.invalid/native@sha256:" + "b" * 64,
            },
            gpu_preference=("h100", "l40s"),
            allowed_clusters=("test-a", "test-b"),
        )
        attempt = Attempt(
            owner=owner,
            root_operation_id="native-run",
            attempt_id="compute-1",
            epoch=1,
            cluster="test-a",
            gpu_class="h100",
            image=profile.images["h100"],
        )
        payload = bundle(files or {"prepare.in": b"synthetic input", "analyze.in": b"synthetic input"})
        record = FileRecord(
            path="input.tar.gz", sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)
        )
        run = NativeMDRun(
            engine=engine,
            job_id="replica",
            input_bundle=record,
            parameters=request or parameters(engine),
        )
        objects = MemoryObjects()
        objects.data["blobs/" + record.sha256] = payload
        lifecycle = MemoryLifecycle(attempt)
        args = dict(
            run=run,
            attempt=attempt,
            profile=profile,
            grant=StorageGrant(owner=owner, root_operation_id="native-run", grant_ref="synthetic"),
            artifacts=Artifacts(objects),
            lifecycle=lifecycle,
            workspace=tmp_path / "compute-1",
        )
        return args, objects, lifecycle

    return make


def synthetic_process(app, monkeypatch, calls, *, missing_output=False):
    """Replace only process I/O; native stage ordering/validators remain real."""
    worker, engine = app.worker, app.engine.name
    if engine == "amber":
        worker.binaries = {"tleap": "synthetic-tleap"}
    elif engine == "lammps":
        worker.binary = "synthetic-lmp"

        def version(command, **kwargs):
            assert command == ["synthetic-lmp", "-h"]
            return "synthetic native version"

        monkeypatch.setattr(import_module("fs2_lammps.worker").subprocess, "check_output", version)

    def execute(command, *args, **kwargs):
        if worker.stopped:
            raise InterruptedError("synthetic interruption")
        cwd, log = args if args else (kwargs["cwd"], kwargs["log"])
        name = "prepare" if "prepare" in log.name else "analyze"
        calls.append(name)
        log.write_text("Exiting LEaP: Errors = 0\n" if engine == "amber" else "synthetic native log\n")
        if not missing_output:
            (cwd / (name + ".dat")).write_bytes(b"synthetic output")
        return (0, 0.01, {}) if engine == "namd" else (0, 0.01)

    monkeypatch.setattr(worker, "execute", execute)


def initialized(factory, engine, monkeypatch):
    args, objects, lifecycle = factory(engine)
    app = NativeMDApplication(**args)
    calls = []
    synthetic_process(app, monkeypatch, calls)
    app.prepare()
    app.worker.initialize()
    return app, args, objects, lifecycle, calls


def replacement(args, lifecycle, tmp_path, **changes):
    attempt = Attempt.model_validate(
        {**args["attempt"].model_dump(), "attempt_id": "compute-2", "epoch": 2, **changes}
    )
    lifecycle.current = attempt
    return NativeMDApplication(**{**args, "attempt": attempt, "workspace": tmp_path / "compute-2"})


def test_native_schema_and_scientific_parameters_are_preserved(factory, engine):
    source = parameters(engine)
    before = copy.deepcopy(source)
    args, _, _ = factory(engine, request=source)
    app = NativeMDApplication(**args)
    assert source == before == app.run_spec.parameters
    assert app.worker.request == engine_for(engine).normalize(before)
    assert app.worker.checkpoint_mode == "local"
    assert (
        app.worker.recipe
        == import_module(f"fs2_{engine}.worker")
        .Workflow(
            source,
            job_id="replica",
            operation_id="native-run",
            workspace=args["workspace"],
            checkpoint_mode="local",
        )
        .recipe
    )


@pytest.mark.parametrize("bad", ["private", "fanout", "platform", "wrong-job"])
def test_unsupported_private_contracts_fail_without_disclosing_payload(factory, engine, bad):
    args, _, _ = factory(engine)
    value = args["run"].model_dump()
    if bad == "private":
        value["parameters"]["secret"] = "patient-sentinel-never-log"
    elif bad == "fanout":
        value["parameters"]["jobs"].append({**value["parameters"]["jobs"][0], "id": "second"})
    elif bad == "platform":
        value["parameters"]["output_destination"] = "platform-artifacts"
    else:
        value["job_id"] = "unknown"
    with pytest.raises(ValidationError) as error:
        NativeMDRun.model_validate(value)
    assert "patient-sentinel" not in str(error.value)


@pytest.mark.parametrize("change", ["owner", "image", "stale", "unsupported-shape"])
def test_authorization_precedes_any_workspace_or_object_io(factory, engine, change):
    args, objects, lifecycle = factory(engine)
    if change == "owner":
        args["grant"] = args["grant"].model_copy(
            update={"owner": Owner(tenant_id="other", principal_id="qa")}
        )
    elif change == "image":
        args["attempt"] = args["attempt"].model_copy(
            update={"image": "example.invalid/other@sha256:" + "c" * 64}
        )
    elif change == "stale":
        lifecycle.current = None
    else:
        args["profile"] = args["profile"].model_copy(
            update={"images": {"rtx-pro-6000": args["attempt"].image}, "gpu_preference": ("rtx-pro-6000",)}
        )
        args["attempt"] = args["attempt"].model_copy(update={"gpu_class": "rtx-pro-6000"})
        lifecycle.current = args["attempt"]
    with pytest.raises((PermissionError, ValueError)):
        NativeMDApplication(**args)
    assert not objects.puts and not args["workspace"].exists()


def test_native_stages_publish_fenced_manifests_and_success(factory, engine, monkeypatch):
    args, _, lifecycle = factory(engine)
    app, calls = NativeMDApplication(**args), []
    synthetic_process(app, monkeypatch, calls)
    receipt = app.execute()
    assert receipt["export"] == "committed" and calls == ["prepare", "analyze"]
    assert [checkpoint[1] for checkpoint in lifecycle.checkpoints] == [1, 2, 3]
    assert app.worker.committed_generation == 3
    snapshot = app.artifacts.load(lifecycle.done)
    assert snapshot.kind == "result" and snapshot.state["schema"] == app.engine.state_schema
    assert snapshot.result["inventory_complete"] is True
    assert snapshot.result["execution_image"] == args["attempt"].image
    assert snapshot.result["gpu_snapshot_used"] is False
    assert not (app.worker.meta / "gromacs-state.json").exists()


def test_zero_exit_without_required_native_outputs_is_not_success(factory, engine, monkeypatch):
    args, _, lifecycle = factory(engine)
    app, calls = NativeMDApplication(**args), []
    synthetic_process(app, monkeypatch, calls, missing_output=True)
    assert app.execute() == {"compute": "failed", "export": "not-finalized"}
    assert lifecycle.done is None and len(lifecycle.checkpoints) == 1
    assert app.worker.state["completed_steps"] == []


@pytest.mark.parametrize(
    "failure", ["stale", "superseded-during-commit", "denied", "corrupt", "active-child"]
)
def test_checkpoint_failure_does_not_advance_committed_generation(factory, engine, monkeypatch, failure):
    app, _, objects, lifecycle, _ = initialized(factory, engine, monkeypatch)
    previous, puts = lifecycle.head, len(objects.puts)
    (app.worker.data / "new.dat").write_bytes(b"synthetic additional output")
    if failure == "stale":
        lifecycle.current = None
    elif failure == "superseded-during-commit":
        original = objects.put

        def supersede(key, source):
            original(key, source)
            if key.startswith("manifests/"):
                lifecycle.current = None

        monkeypatch.setattr(objects, "put", supersede)
    elif failure == "denied":
        objects.fail_put = True
    elif failure == "corrupt":
        objects.corrupt_get = True
    else:
        app.worker.child = object()
    with pytest.raises((OSError, ValueError, RuntimeError)):
        app.worker.checkpoint()
    assert lifecycle.head == previous and app.worker.committed_generation == 1
    if failure in {"stale", "active-child"}:
        assert len(objects.puts) == puts


def test_completed_stage_restore_uses_fresh_workspace_without_reexecution(
    factory, engine, monkeypatch, tmp_path
):
    args, _, lifecycle = factory(engine)
    app, first_calls = NativeMDApplication(**args), []
    synthetic_process(app, monkeypatch, first_calls)
    native_step = app.worker.run_step

    def stop_after_stage(step):
        native_step(step)
        app.worker.stopped = True
        raise InterruptedError("synthetic lost worker after committed stage")

    monkeypatch.setattr(app.worker, "run_step", stop_after_stage)
    assert app.execute()["compute"] == "interrupted"
    reference = lifecycle.head
    checkpoint = app.artifacts.load(reference)
    assert checkpoint.state["completed_steps"] == ["prepare"]
    app.workspace.rename(tmp_path / "inaccessible-old-workspace")
    recovered = replacement(args, lifecycle, tmp_path, cluster="test-b")
    second_calls = []
    synthetic_process(recovered, monkeypatch, second_calls)
    assert recovered.execute(reference)["export"] == "committed"
    assert first_calls == ["prepare"] and second_calls == ["analyze"]
    assert recovered.worker.state["completed_steps"] == ["prepare", "analyze"]


@pytest.mark.parametrize("change", ["engine", "prefix", "recipe", "owner", "future", "image"])
def test_incompatible_restore_is_rejected_before_files_are_materialized(
    factory, engine, monkeypatch, tmp_path, change
):
    app, args, _, lifecycle, _ = initialized(factory, engine, monkeypatch)
    saved = app.artifacts.load(lifecycle.head).model_dump()
    if change == "engine":
        saved["state"]["schema"] = "different-engine"
    elif change == "prefix":
        saved["state"]["completed_steps"] = ["analyze"]
    elif change == "recipe":
        saved["state"]["recipe_sha256"] = "c" * 64
    elif change == "owner":
        saved["owner"]["principal_id"] = "other"
    elif change == "future":
        saved["epoch"] = 3
    else:
        saved["image"] = args["profile"].images["l40s"]
    reference = app.artifacts.commit(Snapshot.model_validate(saved))
    recovered = replacement(args, lifecycle, tmp_path)
    with pytest.raises(ValueError):
        recovered.prepare(reference)
    assert not recovered.worker.data.exists()


@pytest.mark.parametrize("generation,expected", [(2, "failed"), (3, "succeeded")])
def test_only_transient_transport_after_all_native_validation_can_enter_delivery(
    factory, engine, monkeypatch, generation, expected
):
    args, _, lifecycle = factory(engine)
    app, calls = NativeMDApplication(**args), []
    synthetic_process(app, monkeypatch, calls)
    original = lifecycle.checkpoint

    def outage(attempt, reference, current):
        if current == generation:
            raise httpx.ReadTimeout("synthetic checkpoint transport outage")
        original(attempt, reference, current)

    monkeypatch.setattr(lifecycle, "checkpoint", outage)
    assert app.execute()["compute"] == expected
    assert app.worker.committed_generation == generation - 1
    if expected == "succeeded":
        result = app.artifacts.load(lifecycle.done).result
        assert result["checkpoint_commit_pending"] is True
        assert result["native_committed_generation"] == 2
    else:
        assert lifecycle.done is None and app.final_checkpoint_error is None


def test_revoked_final_checkpoint_is_not_promoted_to_success(factory, engine, monkeypatch):
    args, _, lifecycle = factory(engine)
    app, calls = NativeMDApplication(**args), []
    synthetic_process(app, monkeypatch, calls)
    original = lifecycle.checkpoint

    def deny(attempt, reference, generation):
        if generation == 3:
            raise PermissionError("synthetic authorization withdrawal")
        original(attempt, reference, generation)

    monkeypatch.setattr(lifecycle, "checkpoint", deny)
    assert app.execute()["compute"] == "failed"
    assert lifecycle.done is None and app.worker.committed_generation == 2


@pytest.mark.parametrize("recovery", ["fresh-result", "cpu-delivery"])
def test_lost_finalization_ack_retries_without_native_execution(
    factory, engine, monkeypatch, tmp_path, recovery
):
    args, _, lifecycle = factory(engine)
    app, calls = NativeMDApplication(**args), []
    synthetic_process(app, monkeypatch, calls)
    lifecycle.fail_ack = True
    with pytest.raises(DeliveryPending):
        app.execute()
    reference = json.loads((app.workspace / "delivery.json").read_bytes())["reference"]
    lifecycle.fail_ack = False
    if recovery == "fresh-result":
        resumed = replacement(args, lifecycle, tmp_path)
    else:
        attempt = Attempt.model_validate(
            {
                **args["attempt"].model_dump(),
                "attempt_id": "finalizer",
                "epoch": 2,
                "attempt_role": "finalize",
                "gpus": 0,
                "gpu_class": None,
            }
        )
        lifecycle.current = attempt
        retained = retained_workspace(
            tmp_path,
            attempt=attempt,
            run=args["run"],
            profile=args["profile"],
            compute_pod="compute-1",
            compute_epoch=1,
        )
        resumed = NativeMDApplication(**{**args, "attempt": attempt, "workspace": retained})
        with pytest.raises(PermissionError):
            resumed.execute()
    monkeypatch.setattr(resumed.worker, "run", lambda: pytest.fail("delivery reran native science"))
    receipt = resumed.execute(reference) if recovery == "fresh-result" else resumed.finish_delivery()
    assert receipt["export"] == "committed" and calls == ["prepare", "analyze"]
    assert app.artifacts.load(lifecycle.done).result["status"] == "succeeded"


def test_missing_runtime_dependency_has_an_explicit_error(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError("missing synthetic runtime", name="fs2_amber")

    monkeypatch.setattr("scientific_ai.native_engines.import_module", missing)
    with pytest.raises(NativeEngineUnavailable, match="amber native runtime is not packaged"):
        engine_for("amber").normalize({})


def test_same_image_native_checkpoint_can_move_from_h100_to_l40s(factory, engine, monkeypatch, tmp_path):
    app, args, _, lifecycle, _ = initialized(factory, engine, monkeypatch)
    reference = lifecycle.head
    args["profile"] = args["profile"].model_copy(
        update={"images": {"h100": args["attempt"].image, "l40s": args["attempt"].image}}
    )
    recovered = replacement(args, lifecycle, tmp_path, cluster="test-b", gpu_class="l40s")
    snapshot = recovered.prepare(reference)
    assert snapshot.image == recovered.attempt.image
    assert app.attempt.gpu_class == "h100" and recovered.attempt.gpu_class == "l40s"
    assert recovered.worker.recipe == app.worker.recipe
    assert (recovered.worker.meta / recovered.engine.state_file).is_file()


@pytest.mark.parametrize("change", ["missing", "changed", "omitted", "symlink", "state"])
def test_retained_science_must_still_match_its_result_before_delivery(
    factory, engine, monkeypatch, change, tmp_path
):
    args, _, lifecycle = factory(engine)
    app = NativeMDApplication(**args)
    synthetic_process(app, monkeypatch, [])
    lifecycle.fail_ack = True
    with pytest.raises(DeliveryPending):
        app.execute()
    lifecycle.fail_ack = False
    result = json.loads((app.workspace / "result.json").read_bytes())
    output = app.worker.data / "analyze.dat"
    if change == "missing":
        output.unlink()
    elif change == "changed":
        output.write_bytes(b"changed synthetic output")
    elif change == "omitted":
        result["files"] = [record for record in result["files"] if record["path"] != "analyze.dat"]
    elif change == "symlink":
        target = tmp_path / "outside-private-data"
        target.write_bytes(b"synthetic private data")
        (app.worker.data / "leak").symlink_to(target)
    else:
        state_path = app.worker.meta / app.engine.state_file
        state = json.loads(state_path.read_bytes())
        state["operation_id"] = "another-operation"
        state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError):
        app.finish_delivery(result)
    assert lifecycle.done is None


@pytest.mark.parametrize("engine", ["namd", "lammps"])
def test_native_segment_restart_preserves_engine_checkpoint_semantics(factory, monkeypatch, tmp_path, engine):
    if engine == "namd":
        step = {
            "id": "md",
            "config": "config.namd",
            "mode": "dynamics",
            "steps": 200,
            "segment_steps": 100,
            "output_prefix": "prod",
        }
        files = {"config.namd": b"synthetic configuration"}
    else:
        step = {
            "id": "md",
            "input": "initial.in",
            "expected_outputs": ["final.dat"],
            "continuation": {
                "input": "continue.in",
                "restart_file": "restart.bin",
                "progress_file": "progress.txt",
                "target_step": 200,
            },
        }
        files = {"initial.in": b"synthetic initial script", "continue.in": b"synthetic continuation"}
    request = {
        "schema": f"fs2-serve.nebius.ai/{engine}-workflow-request/v1",
        "jobs": [{"id": "replica", "steps": [step]}],
    }
    args, _, lifecycle = factory(engine, request=request, files=files)
    observed = []

    def processes(app):
        worker = app.worker
        if engine == "lammps":
            worker.binary = "synthetic-lmp"

            def output(command, **kwargs):
                if command[-1] == "-h":
                    return "synthetic engine"
                assert "-restart2info" in command
                return "Current timestep number = " + Path(command[-1]).read_text()

            monkeypatch.setattr(import_module("fs2_lammps.worker").subprocess, "check_output", output)

        def execute(command, *positional, **keyword):
            if worker.stopped:
                raise import_module(f"fs2_{engine}.worker").Interrupted("synthetic worker interruption")
            cwd, log = positional if positional else (keyword["cwd"], keyword["log"])
            if engine == "namd":
                script = (cwd / command[-1]).read_text()
                prefix = re.search(r'^outputName "([^"]+)"', script, re.M)[1]
                start = int(re.search(r"^firsttimestep (\d+)", script, re.M)[1])
                end = start + 100
                observed.append((start, script))
                for suffix in (".coor", ".vel"):
                    (cwd / (prefix + suffix)).write_bytes(struct.pack("<i3d", 1, 0.0, 0.0, 0.0))
                (cwd / (prefix + ".xsc")).write_text(str(end) + " 1 0 0 0 1 0 0 0 1 0 0 0\n")
                log.write_text(
                    f"ETITLE: TS TEMP TOTAL VOLUME\nENERGY: {end} 300 -5 1\nTCL: FS2_SEGMENT_COMPLETE {end}\n"
                )
                return 0, 0.01, {}
            end = 100 if command[-1] == "initial.in" else 200
            observed.append((end - 100, command))
            (cwd / "progress.txt").write_text(str(end))
            (cwd / "restart.bin").write_text(str(end))
            (cwd / "final.dat").write_bytes(b"synthetic final output")
            log.write_text("synthetic native segment log\n")
            return 0, 0.01

        monkeypatch.setattr(worker, "execute", execute)

    first = NativeMDApplication(**args)
    processes(first)
    original = lifecycle.checkpoint

    def interrupt_after_commit(attempt, reference, generation):
        original(attempt, reference, generation)
        if attempt.epoch == 1 and generation == 2:
            first.worker.stopped = True

    monkeypatch.setattr(lifecycle, "checkpoint", interrupt_after_commit)
    assert first.execute()["compute"] == "interrupted"
    snapshot = first.artifacts.load(lifecycle.head)
    assert snapshot.state["completed_steps"] == []
    assert snapshot.state["active_step"]["id"] == "md"
    recovered = replacement(args, lifecycle, tmp_path)
    processes(recovered)
    assert recovered.execute(lifecycle.head)["export"] == "committed"
    assert [start for start, _ in observed] == [0, 100]
    if engine == "namd":
        assert 'binCoordinates "prod.part000001.coor"' in observed[1][1]
        result = recovered.artifacts.load(lifecycle.done).result
        assert result["native_restart_semantics"]["bitwise_continuation_claimed"] is False
    else:
        assert observed[1][1][-1] == "continue.in"
        assert observed[1][1][observed[1][1].index("fs2_restart") + 1] == "1"
