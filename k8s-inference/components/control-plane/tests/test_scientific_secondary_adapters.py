"""Executable contracts for the four non-AF3 secondary structure adapters."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

from fs2_serve.scientific_batch import ResourceClass, ScientificAdapterError, compile_adapter_run
from fs2_serve.scientific_batch.adapters import esmfold2, esmfold2_fast, openfold3, protenix_v2
from fs2_serve.scientific_batch.adapters.common import load_output_manifest

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
IMAGE_HANDOFF = ADAPTER_ROOT / "secondary-r4-image-handoff.json"
WRAPPER_ROOT_ENV = "FS2_SECONDARY_WRAPPER_ROOT"

MODULES = {
    "esmfold2": esmfold2,
    "esmfold2-fast": esmfold2_fast,
    "protenix-v2": protenix_v2,
    "openfold3": openfold3,
}
POSITIVE_FIXTURES = {
    "esmfold2": ("positive-sequence", "positive-msa"),
    "esmfold2-fast": ("positive-short", "positive-recycles"),
    "protenix-v2": ("positive-complex", "positive-monomer"),
    "openfold3": ("positive-complex", "positive-monomer"),
}
NEGATIVE_FIXTURES = {
    "esmfold2": "negative-invalid-sequence",
    "esmfold2-fast": "negative-msa",
    "protenix-v2": "negative-duplicate-seeds",
    "openfold3": "negative-alphafold-alias",
}
STAGE_SHAPES = {
    "esmfold2": (
        ("prepare-input", "cpu", 64, "restart", "restartable"),
        ("fold", "gpu", 64, "restart", "restartable"),
    ),
    "esmfold2-fast": (
        ("prepare-input", "cpu", 64, "restart", "restartable"),
        ("fold", "gpu", 128, "restart", "restartable"),
    ),
    "protenix-v2": (
        ("prepare-data", "cpu", 64, "restart", "restartable"),
        ("sample-structure", "gpu", 32, "restart", "restartable"),
    ),
    "openfold3": (
        ("data-pipeline", "cpu", 32, "none", "non_preemptible"),
        ("inference", "gpu", 32, "restart", "restartable"),
    ),
}
OPERATIONS = {
    "esmfold2": "predict-structure",
    "esmfold2-fast": "predict-protein-structure",
    "protenix-v2": "predict-complex-structure",
    "openfold3": "predict-complex-structure",
}
SERVICE_CLASSES = {
    "esmfold2": ["interactive", "customer-batch"],
    "esmfold2-fast": ["presentation", "interactive", "customer-batch"],
    "protenix-v2": ["customer-batch"],
    "openfold3": ["customer-batch"],
}


def fixture(model_id: str, name: str) -> dict[str, object]:
    value = json.loads((ADAPTER_ROOT / model_id / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def contract(model_id: str) -> dict[str, object]:
    value = json.loads((ADAPTER_ROOT / model_id / "contract.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def profile(model_id: str) -> dict[str, object]:
    module = MODULES[model_id]
    stages = [
        {
            "id": stage_id,
            "needs": [] if index == 0 else [STAGE_SHAPES[model_id][index - 1][0]],
            "resource_class": resource_class,
            "admission_mode": "independent-jobs",
            "min_parallelism": 1,
            "max_parallelism": maximum,
            "checkpoint_mode": checkpoint,
            "preemption_mode": preemption,
        }
        for index, (stage_id, resource_class, maximum, checkpoint, preemption) in enumerate(STAGE_SHAPES[model_id])
    ]
    return {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile/v1",
        "model_id": model_id,
        "display_name": model_id,
        "execution_mode": "scientific-batch",
        "state": "candidate-unqualified",
        "route_exposed": False,
        "source": {
            "kind": "git",
            "repository": module.SOURCE_REPOSITORY,
            "revision": module.SOURCE_REVISION,
            "review_url": f"https://github.com/{module.SOURCE_REPOSITORY}/tree/{module.SOURCE_REVISION}",
            "classification": "candidate-input",
        },
        "execution_identity": {
            "model_revision": module.SOURCE_REVISION,
            "runtime_image_digest": None,
            "runtime_recipe_sha256": None,
            "workload_recipe_sha256": None,
            "artifact_manifest_digest": None,
            "execution_identity_sha256": None,
        },
        "interface": {
            "protocol": "scientific-batch-v1",
            "submit_endpoint": f"/v1/models/{model_id}:submit",
            "request_schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "result_schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
            "parameter_schema": module.PARAMETER_SCHEMA,
            "operations": [OPERATIONS[model_id]],
            "service_classes": SERVICE_CLASSES[model_id],
            "mcp": {
                "discoverable": True,
                "invocable": False,
                "tool_name": f"submit_{model_id.replace('-', '_')}",
                "description": "Candidate-only adapter contract.",
            },
        },
        "access": {
            "profile": "standard",
            "state": "unverified",
            "receipt_digest": None,
            "credentials_embedded": False,
        },
        "resources": {
            "gpu_count": 1,
            "gpu_topology": "single-gpu",
            "host_architectures": ["amd64"],
            "compatible_pool_ids": ["h100-1x", "h100-reserved-8x"],
            "required_node_labels": {"accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"},
        },
        "workload": {
            "stages": stages,
            "retry": {"max_attempts": 2, "retryable_exit_codes": [137, 143]},
            "cancellation": {"mode": "terminate-attempt", "grace_seconds": 60},
        },
        "semantic_validation": {
            "validator_id": module.VALIDATOR_ID,
            "state": "candidate-unqualified",
        },
        "policy": {
            "commercial_use": "allowed",
            "non_clinical": True,
            "limitations": ["No semantic H100 qualification or route activation."],
        },
    }


def compile_fixture(model_id: str, name: str):
    return MODULES[model_id].compile_run(
        profile(model_id),
        fixture(model_id, name),
        operation_id=f"op-{model_id}-contract",
    )


def _argument(argv: tuple[str, ...], name: str) -> str:
    assert argv.count(name) == 1, argv
    return argv[argv.index(name) + 1]


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_two_positive_fixtures_compile_to_cpu_then_gpu(model_id: str) -> None:
    for name in POSITIVE_FIXTURES[model_id]:
        plan = compile_fixture(model_id, name)
        assert plan.model_id == model_id
        assert plan.variant_id == MODULES[model_id].VARIANT_ID
        assert [stage.resource_class for stage in plan.controller_plan.stages] == [
            ResourceClass.CPU,
            ResourceClass.GPU,
        ]
        assert plan.controller_plan.stages[1].depends_on == (plan.controller_plan.stages[0].stage_id,)
        assert plan.invocations[0].runtime_artifacts == (
            (protenix_v2.MODEL_ARTIFACT,) if model_id == "protenix-v2" else ()
        )
        assert plan.invocations[1].runtime_artifacts == tuple(
            MODULES[model_id].STAGE_EXECUTION_CONTRACTS[plan.invocations[1].stage_id]["runtime_artifacts"]
        )


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_negative_fixture_fails_before_any_workload(model_id: str) -> None:
    with pytest.raises(ScientificAdapterError):
        compile_fixture(model_id, NEGATIVE_FIXTURES[model_id])


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_public_dispatch_is_explicit_and_variant_fenced(model_id: str) -> None:
    plan = compile_adapter_run(
        model_id,
        profile(model_id),
        fixture(model_id, POSITIVE_FIXTURES[model_id][0]),
        operation_id=f"op-{model_id}-dispatch",
        variant_id=MODULES[model_id].VARIANT_ID,
    )
    assert plan.model_id == model_id
    with pytest.raises(ScientificAdapterError, match="variant_id"):
        compile_adapter_run(
            model_id,
            profile(model_id),
            fixture(model_id, POSITIVE_FIXTURES[model_id][0]),
            operation_id=f"op-{model_id}-dispatch",
            variant_id="wrong-backend",
        )


def test_esmf2_variants_use_distinct_artifacts_and_fast_rejects_msa() -> None:
    full = compile_fixture("esmfold2", "positive-sequence").invocation("fold", "main")
    fast = compile_fixture("esmfold2-fast", "positive-short").invocation("fold", "main")
    assert _argument(full.argv, "--variant") == "esmfold2"
    assert _argument(fast.argv, "--variant") == "esmfold2-fast"
    assert _argument(full.argv, "--model-dir") == "/models/esmfold2"
    assert _argument(fast.argv, "--model-dir") == "/models/esmfold2-fast"
    assert set(full.runtime_artifacts) == {"esmfold2-trunk", "esmc-6b", "esmfold2-ccd"}
    assert set(fast.runtime_artifacts) == {"esmfold2-fast-trunk", "esmc-6b", "esmfold2-ccd"}
    with pytest.raises(ScientificAdapterError, match="rejects MSA"):
        compile_fixture("esmfold2-fast", "negative-msa")


def test_exact_r4_argv_and_cache_contracts() -> None:
    esm = compile_fixture("esmfold2", "positive-sequence")
    prepare, fold = esm.invocations
    assert prepare.argv[:2] == ("/usr/local/bin/fs2-run-esmfold2", "prepare-input")
    assert fold.argv[:2] == ("/usr/local/bin/fs2-run-esmfold2", "fold")
    assert _argument(fold.argv, "--hardware-mode") == "h100"
    assert _argument(fold.argv, "--esmc-precision") == "bf16"
    assert _argument(fold.argv, "--num-loops") == "20"
    assert _argument(fold.argv, "--num-sampling-steps") == "200"
    assert _argument(fold.argv, "--ccd-path") == "/databases/esmfold2/ccd.pkl"

    protenix = compile_fixture("protenix-v2", "positive-monomer")
    prep, pred = protenix.invocations
    assert prep.argv[:2] == ("/usr/local/bin/fs2-run-protenix", "prep")
    assert pred.argv[:2] == ("/usr/local/bin/fs2-run-protenix", "pred")
    assert _argument(pred.argv, "--seeds") == "7,19"
    assert _argument(pred.argv, "--sample-count") == "2"
    assert _argument(pred.argv, "--checkpoint") == "/models/protenix-v2/checkpoint/protenix-v2.pt"
    assert _argument(pred.argv, "--common-dir") == "/models/protenix-v2/common"
    assert {"--disable-templates", "--disable-rna-msa"} <= set(pred.argv)
    protenix_cache = {
        "TRITON_CACHE_DIR": "/cache/protenix/triton",
        "CUEQ_TRITON_CACHE_DIR": "/cache/protenix/cueq-triton",
        "TORCH_EXTENSIONS_DIR": "/cache/protenix/torch-extensions",
        "XDG_CACHE_HOME": "/cache/protenix/xdg",
    }
    assert protenix_cache.items() <= dict(pred.environment).items()

    openfold = compile_fixture("openfold3", "positive-complex")
    data, inference = openfold.invocations
    assert data.argv[:2] == ("/usr/local/bin/fs2-run-openfold3", "prepare")
    assert inference.argv[:2] == ("/usr/local/bin/fs2-run-openfold3", "predict")
    assert not data.runtime_artifacts
    assert _argument(inference.argv, "--checkpoint") == "/models/openfold3/of3-ob-2025-06-30-174k.pt"
    assert _argument(inference.argv, "--ccd-path") == "/databases/openfold3/components.bcif"
    assert _argument(inference.argv, "--num-model-seeds") == "2"
    assert _argument(inference.argv, "--model-seeds") == "13,31"
    assert _argument(inference.argv, "--use-templates") == "false"
    openfold_cache = {
        "TRITON_CACHE_DIR": "/cache/openfold3/triton",
        "TORCH_EXTENSIONS_DIR": "/cache/openfold3/torch-extensions",
        "XDG_CACHE_HOME": "/cache/openfold3/xdg",
    }
    assert openfold_cache.items() <= dict(inference.environment).items()

    for plan in (esm, protenix, openfold):
        for invocation in plan.invocations:
            assert invocation.argv[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"}
            assert not any(token in {"-c", ";", "&&", "|"} or "$(" in token for token in invocation.argv)
            stage_contract = MODULES[plan.model_id].STAGE_EXECUTION_CONTRACTS[invocation.stage_id]
            environment = dict(invocation.environment)
            assert environment["FS2_SCIENTIFIC_COLLECTOR_ID"] == stage_contract["collector_id"]
            assert environment["FS2_SCIENTIFIC_VALIDATOR_ID"] == stage_contract["validator_id"]


def _load_wrapper(root: Path, filename: str) -> ModuleType:
    sys.path.insert(0, str(root))
    try:
        spec = importlib.util.spec_from_file_location(f"fs2_secondary_contract_{filename}", root / f"{filename}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(root))


def _parse_wrapper_argv(wrapper: ModuleType, argv: tuple[str, ...]):
    handlers = {
        "prepare-input": "_prepare",
        "fold": "_fold",
        "prep": "_prep",
        "pred": "_pred",
        "prepare": "_prepare",
        "predict": "_predict",
    }
    handler = handlers[argv[1]]
    with mock.patch.object(wrapper, handler) as parsed_handler:
        if len(inspect.signature(wrapper.main).parameters) == 0:
            with mock.patch.object(sys, "argv", [argv[0], *argv[1:]]):
                wrapper.main()
        else:
            wrapper.main(list(argv[1:]))
        assert parsed_handler.call_count == 1
        return parsed_handler.call_args.args[0]


def _marker_artifacts(model_id: str, invocation) -> list[dict[str, object]]:
    by_id = {
        item["artifact_id"]: item
        for item in contract(model_id)["runtime_artifacts"]
        if isinstance(item, dict)
    }
    receipt = "d" * 64
    return [
        {
            "artifact_id": artifact_id,
            "mount_path": by_id[artifact_id]["mount_path"],
            "content_digest": f"sha256:{by_id[artifact_id]['content_sha256']}",
            "localization_receipt_digest": f"sha256:{receipt}",
            "sub_path": None,
            "expected_manifest_sha256": by_id[artifact_id].get("localization_manifest_sha256"),
            "readiness_receipt_sha256": receipt,
            "authorization_receipt_sha256": None,
        }
        for artifact_id in invocation.runtime_artifacts
    ]


@pytest.mark.skipif(
    not os.environ.get(WRAPPER_ROOT_ENV),
    reason=f"set {WRAPPER_ROOT_ENV} to the exact r4 wrapper source",
)
def test_every_generated_argv_cross_runs_through_exact_r4_parsers_and_markers(tmp_path: Path) -> None:
    root = Path(os.environ[WRAPPER_ROOT_ENV]).resolve()
    source_commit = json.loads(IMAGE_HANDOFF.read_text(encoding="utf-8"))["image_source_commit"]
    git = shutil.which("git")
    assert git is not None
    repository = Path(
        subprocess.run(  # noqa: S603 - fixed git executable and an operator-supplied local path
            [git, "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    relative = root.relative_to(repository)
    parser_sources = (
        "run_esmfold2.py",
        "run_protenix.py",
        "run_openfold3.py",
        "handoff_contract.py",
        "result_contract.py",
        "runtime_localization.py",
    )
    subprocess.run(  # noqa: S603 - fixed git executable and validated immutable revision
        [
            git,
            "-C",
            str(repository),
            "diff",
            "--quiet",
            source_commit,
            "--",
            *(str(relative / filename) for filename in parser_sources),
        ],
        check=True,
    )
    wrappers = {
        "esmfold2": _load_wrapper(root, "run_esmfold2"),
        "esmfold2-fast": _load_wrapper(root, "run_esmfold2"),
        "protenix-v2": _load_wrapper(root, "run_protenix"),
        "openfold3": _load_wrapper(root, "run_openfold3"),
    }
    for model_id, wrapper in wrappers.items():
        plan = compile_fixture(model_id, POSITIVE_FIXTURES[model_id][0])
        for invocation in plan.invocations:
            parsed = _parse_wrapper_argv(wrapper, invocation.argv)
            if not invocation.runtime_artifacts:
                continue
            marker_path = tmp_path / f"{model_id}-{invocation.stage_id}.json"
            marker = {
                "schema": "fs2-serve.nebius.ai/runtime-localization-marker/v1",
                "operation_id": "00000000-0000-4000-8000-000000000010",
                "attempt_id": "00000000-0000-4000-8000-000000000011",
                "tenant_id": "adapter-contract-tenant",
                "model_id": model_id,
                "variant_id": plan.variant_id,
                "stage_id": invocation.stage_id,
                "artifacts": _marker_artifacts(model_id, invocation),
            }
            marker_path.write_text(json.dumps(marker, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            parsed.runtime_localization_marker = str(marker_path)
            environment = {
                "FS2_OPERATION_ID": marker["operation_id"],
                "FS2_ATTEMPT_ID": marker["attempt_id"],
                "FS2_TENANT_ID": marker["tenant_id"],
                "FS2_VARIANT_ID": plan.variant_id,
                "FS2_STAGE_ID": invocation.stage_id,
                "FS2_RUNTIME_LOCALIZATION_MARKER": str(marker_path),
                "FS2_ARTIFACT_ACCESS_RECEIPT_DIGEST": "",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                validated = wrapper._validate_runtime_localization_args(invocation.argv[1], parsed)
            assert validated["model_id"] == model_id


def _mmcif(seed: int, sample: int) -> bytes:
    rows = [
        f"ATOM {index} C CA A {index + seed / 1000:.3f} {index + sample / 1000:.3f} {index + 1:.3f}"
        for index in range(1, 11)
    ]
    return (
        "data_prediction\n#\nloop_\n_atom_site.group_PDB\n_atom_site.id\n"
        "_atom_site.type_symbol\n_atom_site.label_atom_id\n_atom_site.label_asym_id\n"
        "_atom_site.Cartn_x\n_atom_site.Cartn_y\n_atom_site.Cartn_z\n"
        + "\n".join(rows)
        + "\n#\n"
    ).encode("ascii")


def _write_confidence_workspace(
    tmp_path: Path,
    *,
    runtime_id: str,
    model_revision: str,
    seeds: tuple[int, ...],
    samples: int,
) -> Path:
    workspace = tmp_path / runtime_id
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    results = []
    for seed in seeds:
        for sample in range(samples):
            structure = outputs / f"prediction-{seed}-{sample}.cif"
            structure.write_bytes(_mmcif(seed, sample))
            summary = outputs / f"summary-{seed}-{sample}.json"
            summary.write_text(json.dumps({"seed": seed, "sample": sample}) + "\n", encoding="utf-8")
            results.append(
                {
                    "seed": seed,
                    "sample_index": sample,
                    "upstream_summary": summary.name,
                    "structure": {
                        "filename": structure.name,
                        "sha256": hashlib.sha256(structure.read_bytes()).hexdigest(),
                        "bytes": structure.stat().st_size,
                    },
                    "metrics": {"plddt": 87.5, "ptm": 0.71},
                }
            )
    envelope = {
        "schema": "fs2.nebius.ai/structure-confidence/v1",
        "runtime_id": runtime_id,
        "model_revision": model_revision,
        "seeds": list(seeds),
        "samples_per_seed": samples,
        "results": results,
    }
    (outputs / "confidence.json").write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
    return workspace


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_result_collectors_validate_and_publish_the_exact_closure(model_id: str, tmp_path: Path) -> None:
    request = fixture(model_id, POSITIVE_FIXTURES[model_id][0])
    module = MODULES[model_id]
    if model_id in {"esmfold2", "esmfold2-fast"}:
        parameters = esmfold2.Parameters.parse(request["parameters"], fast=model_id.endswith("-fast"))
        seeds, samples = (parameters.seed,), 1
        revision = module.MODEL_REVISION
    elif model_id == "protenix-v2":
        parameters = protenix_v2.Parameters.parse(request["parameters"])
        seeds, samples = parameters.seeds, parameters.sample_count
        revision = protenix_v2.OUTPUT_MODEL_REVISION
    else:
        parameters = openfold3.Parameters.parse(request["parameters"])
        seeds, samples = parameters.seeds, 1
        revision = openfold3.SOURCE_REVISION
    workspace = _write_confidence_workspace(
        tmp_path,
        runtime_id=model_id,
        model_revision=revision,
        seeds=seeds,
        samples=samples,
    )
    first = module.collect_stage_output(module.RESULT_COLLECTOR_ID, request, workspace)
    second = module.collect_stage_output(module.RESULT_COLLECTOR_ID, request, workspace)
    assert first == second
    entries = first.manifest["entries"]
    assert isinstance(entries, list)
    assert len(entries) == len(seeds) * samples * 2 + 1
    assert {entry["name"] for entry in entries} >= {"confidence", f"prediction.{seeds[0]}.0"}
    assert len(first.blobs) == len(entries)
    reopened = load_output_manifest(
        first.manifest,
        artifact_loader=lambda artifact_id: first.blobs[artifact_id],
        maximum_entries=64,
        maximum_total_bytes=8 * 1024 * 1024 * 1024,
    )
    assert len(reopened) == len(entries)

    envelope_path = workspace / "outputs/confidence.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["results"][0]["metrics"]["plddt"] = 101
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ScientificAdapterError, match="metric"):
        module.collect_stage_output(module.RESULT_COLLECTOR_ID, request, workspace)


@pytest.mark.parametrize("model_id", tuple(MODULES))
def test_prepare_collectors_are_deterministic_and_unknown_ids_fail(model_id: str, tmp_path: Path) -> None:
    module = MODULES[model_id]
    request = fixture(model_id, POSITIVE_FIXTURES[model_id][0])
    workspace = tmp_path / model_id
    workspace.mkdir()
    if model_id in {"esmfold2", "esmfold2-fast"}:
        filename, content = "prepared-input.json", b'{"sequences":[]}\n'
    else:
        filename, content = "handoff.tar.zst", b"deterministic-stage-handoff"
    (workspace / filename).write_bytes(content)
    first = module.collect_stage_output(module.PREPARE_COLLECTOR_ID, request, workspace)
    second = module.collect_stage_output(module.PREPARE_COLLECTOR_ID, request, workspace)
    assert first == second
    with pytest.raises(ScientificAdapterError, match="unsupported"):
        module.collect_stage_output("unknown-collector-v1", request, workspace)


def test_contract_documents_match_code_and_the_exact_r4_handoff() -> None:
    handoff = json.loads(IMAGE_HANDOFF.read_text(encoding="utf-8"))
    assert handoff["state"] == "build-only-not-activated"
    assert handoff["semantic_h100_qualification"] is False
    assert handoff["route_activation_allowed"] is False
    assert "alphafold3" not in {item["model_id"] for item in handoff["images"]}
    images = {item["model_id"]: item for item in handoff["images"]}
    assert set(images) == set(MODULES)
    for model_id, module in MODULES.items():
        value = contract(model_id)
        assert value["model_id"] == module.MODEL_ID
        assert value["variant_id"] == module.VARIANT_ID
        assert value["source"]["repository"] == module.SOURCE_REPOSITORY
        assert value["source"]["revision"] == module.SOURCE_REVISION
        assert value["activation"] == {
            "profile_state": "candidate-unqualified",
            "route_exposed": False,
            "semantic_h100_qualified": False,
        }
        assert value["runtime_image"]["repository"] == images[model_id]["repository"]
        assert value["runtime_image"]["tag"] == images[model_id]["tag"]
        assert value["runtime_image"]["digest"] == images[model_id]["digest"]
        assert value["runtime_image"]["state"] == "build-only-not-semantic-qualified"
        assert {
            stage["stage_id"]: {
                "collector_id": stage["collector_id"],
                "validator_id": stage["validator_id"],
                "runtime_artifacts": tuple(stage["runtime_artifacts"]),
            }
            for stage in value["stages"]
        } == dict(module.STAGE_EXECUTION_CONTRACTS)


def test_exact_published_receipts_match_the_committed_image_handoff_when_available() -> None:
    handoff = json.loads(IMAGE_HANDOFF.read_text(encoding="utf-8"))
    root = Path(handoff["evidence"]["source"])
    if not root.is_dir():
        pytest.skip("operator-local r4 publication receipts are not present")
    for image in handoff["images"]:
        receipt = json.loads((root / image["model_id"] / "build-receipt.json").read_text(encoding="utf-8"))
        entries = receipt["images"]
        assert len(entries) == 1
        assert entries[0]["target"] == f"{image['repository']}:{image['tag']}"
        assert entries[0]["published_digest"] == image["digest"]
        assert receipt["image_source_revision"] == handoff["image_source_commit"]
