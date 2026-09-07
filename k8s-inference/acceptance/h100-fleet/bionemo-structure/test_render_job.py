"""Static and renderer tests for the BioNeMo/structure H100 cohort."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
RENDERER = Path(__file__).with_name("render_job.py")


def _renderer():
    spec = importlib.util.spec_from_file_location("bionemo_structure_renderer", RENDERER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gpu_adapters_discover_capability_instead_of_rejecting_non_sm103() -> None:
    for model in ("boltz2", "genmol", "molmim"):
        source = (ROOT / "models" / "bionemo" / model / "server.py").read_text()
        ast.parse(source)
        assert "get_device_capability(0) != (10, 3)" not in source
        assert "capability != (10, 3)" not in source
        assert 'compute_capability": "10.3"' not in source
        assert 'RUNTIME.compute_capability = f"{capability[0]}.{capability[1]}"' in source


def test_renderer_uses_restartable_runtime_sidecar_and_preserves_resource_caps() -> None:
    renderer = _renderer()
    args = type(
        "Args",
        (),
        {
            "model": "boltz2",
            "repetition": 1,
            "attempt": 1,
            "validator_configmap": "validator",
            "evidence_pvc": "evidence",
            "cache_pvc": "cache",
            "namespace": "fs2-models",
        },
    )()
    job = renderer.render(args)
    pod = job["spec"]["template"]["spec"]
    materializer, runtime = pod["initContainers"]
    assert materializer["name"] == "materialize-validator"
    assert materializer["resources"]["requests"] == {
        "cpu": "10m",
        "memory": "16Mi",
    }
    assert runtime["restartPolicy"] == "Always"
    assert runtime["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert runtime["resources"]["limits"] == {
        "cpu": "32",
        "memory": "192Gi",
        "nvidia.com/gpu": "1",
    }
    assert pod["nodeSelector"] == {
        "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"
    }
    evidence = next(volume for volume in pod["volumes"] if volume["name"] == "evidence")
    assert evidence == {"name": "evidence", "emptyDir": {"sizeLimit": "16Mi"}}
    assert job["spec"]["backoffLimit"] == 0


def test_msa_fallback_is_cpu_and_explicitly_separate_from_exact_pdb70_nim() -> None:
    renderer = _renderer()
    profile = renderer.PROFILES["msa-search-pdb70"]
    assert profile.gpu == 0
    assert "msa-search@sha256:64bd29" in profile.image
    source = (
        ROOT / "models" / "bionemo" / "msa-search-pdb70" / "server.py"
    ).read_text()
    assert "capability-equivalent-non-alias" in source
    assert "actual_databases" in source
    args = type(
        "Args",
        (),
        {
            "model": "msa-search-pdb70",
            "repetition": 1,
            "attempt": 2,
            "validator_configmap": "validator",
            "evidence_pvc": None,
            "cache_pvc": "cache",
            "namespace": "fs2-models",
        },
    )()
    pod = renderer.render(args)["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"capacity.fs2.nebius/pool": "general-cpu"}
    assert pod["tolerations"] == [
        {
            "key": "workload.fs2.nebius/general-cpu",
            "operator": "Equal",
            "value": "true",
            "effect": "NoSchedule",
        }
    ]


def test_local_pdb70_profile_is_cpu_and_uses_pinned_open_runtime() -> None:
    renderer = _renderer()
    profile = renderer.PROFILES["msa-search-pdb70-local"]
    assert profile.gpu == 0
    assert profile.image.endswith(
        "@sha256:f6e514e8773142f381971698d10047d834fbc0d09b6c331cd469685bc2b7ce85"
    )
    args = type(
        "Args",
        (),
        {
            "model": "msa-search-pdb70-local",
            "repetition": 1,
            "attempt": 1,
            "validator_configmap": "validator",
            "evidence_pvc": None,
            "cache_pvc": "cache",
            "namespace": "fs2-models",
        },
    )()
    pod = renderer.render(args)["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"capacity.fs2.nebius/pool": "general-cpu"}
    assert pod["tolerations"] == [
        {
            "key": "workload.fs2.nebius/general-cpu",
            "operator": "Equal",
            "value": "true",
            "effect": "NoSchedule",
        }
    ]


def test_hugging_face_models_use_writable_model_cache_for_xet() -> None:
    renderer = _renderer()
    expected = {
        "HOME": "/models/home",
        "HF_HOME": "/models/huggingface",
        "HF_HUB_CACHE": "/models/huggingface/hub",
        "HF_XET_CACHE": "/models/huggingface/xet",
    }
    for model in ("boltz2", "genmol"):
        assert dict(renderer.PROFILES[model].runtime_env) == expected


def test_open_runtime_successors_are_immutable_health_alias_images() -> None:
    renderer = _renderer()
    assert renderer.PROFILES["proteinmpnn"].image.endswith(
        "@sha256:f27dda10178fb799dc8f75c2b7ced00643a6281fd3e37e95cd353700e6d62e38"
    )
    assert renderer.PROFILES["diffdock"].image.endswith(
        "@sha256:471db264f4c544e1798c090f13b6cf94b3fbd304fb2c91e9f63a3dd33c9d81d7"
    )
