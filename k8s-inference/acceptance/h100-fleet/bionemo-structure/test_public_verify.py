import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fs2_bio_public_verify_test", HERE / "public_verify.py")
verifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)


def specimen():
    expected = verifier.expected_runtime("genmol")
    digest = expected["image"].split("@", 1)[1]
    spec_digest = "sha256:" + "a" * 64
    pod = {
        "metadata": {
            "uid": "pod-uid",
            "name": "genmol-pod",
            "namespace": "fs2-models",
            "annotations": {
                "fs2-serve.nebius.ai/spec-digest": spec_digest,
                "fs2.nebius/runtime-image-digest": digest,
                "fs2.nebius/model-content-digest": expected["model_content_digest"],
            },
        },
        "status": {"containerStatuses": [{"imageID": "registry/image@" + digest}]},
    }
    operation = {
        "model_revision": "dynamic:" + spec_digest,
        "runtime": {"pod_uid": "pod-uid"},
    }
    return expected, pod, operation


def test_dynamic_runtime_binds_exact_selected_image_content_and_source_revision():
    expected, pod, operation = specimen()
    result = verifier.runtime_binding([pod], operation, expected)
    assert result["model_revision"] == expected["model_revision"]
    assert result["model_content_digest"] == expected["model_content_digest"]


@pytest.mark.parametrize("field", ["image", "content", "route"])
def test_dynamic_runtime_rejects_any_identity_mismatch(field):
    expected, pod, operation = specimen()
    pod = copy.deepcopy(pod)
    operation = copy.deepcopy(operation)
    if field == "image":
        pod["status"]["containerStatuses"][0]["imageID"] = "registry/image@sha256:" + "b" * 64
    elif field == "content":
        pod["metadata"]["annotations"]["fs2.nebius/model-content-digest"] = "sha256:" + "b" * 64
    else:
        operation["model_revision"] = "dynamic:sha256:" + "b" * 64
    with pytest.raises(verifier.public.AcceptanceError):
        verifier.runtime_binding([pod], operation, expected)


def test_public_native_output_is_validated_with_frozen_runtime_semantics(tmp_path):
    paths = []
    native = verifier.native_validator("proteinmpnn")
    input_sequence = native.EXPECTED_INPUT_SEQUENCE
    for index, (seed, sequence) in enumerate(
        ((2370, "A" * len(input_sequence)), (2371, "C" * len(input_sequence))), 1
    ):
        path = tmp_path / f"response-{index}.json"
        path.write_text(
            json.dumps(
                {
                    "mfasta": (
                        f">input seed={seed} designed_chains=['A']\n{input_sequence}\n"
                        f">sample=1 score=0.5 global_score=0.5 seq_recovery=0.0\n"
                        f"{sequence}\n"
                    ),
                    "scores": [0.5],
                    "probs": [[[1.0 / 21.0] * 21 for _ in input_sequence]],
                }
            )
        )
        paths.append(path)
    result = verifier.validate_pair("proteinmpnn", {}, paths, tmp_path)
    assert result["status"] == "PASS"
    assert [item["designed_sequence_length"] for item in result["results"]] == [76, 76]


@pytest.mark.parametrize("model", ["proteinmpnn", "diffdock"])
def test_native_public_cases_bind_pinned_validator_and_two_requests(model):
    contract, cases = verifier.cases_for(model)
    assert contract == verifier.native_validator_contract(model)
    assert len(cases) == 2
    assert cases[0].payload_sha256 != cases[1].payload_sha256


def diffdock_response(*, ligand=None, degenerate=False):
    native = verifier.native_validator("diffdock")
    fixture_path = (
        verifier.ROOT
        / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/"
        "diffdock-native/fixtures/1ubq-aspirin-request.json"
    )
    fixture = native._read_fixture(fixture_path)
    lines = ["", "     FS2          3D", ""]
    lines.append(f"{13:3d}{13:3d}  0  0  0  0  0  0  0  0999 V2000")
    for index in range(13):
        coordinate = 0.0 if degenerate else float(index)
        lines.append(
            f"{coordinate:10.4f}{coordinate + 0.1:10.4f}{coordinate + 0.2:10.4f} "
            "C   0  0  0  0  0  0  0  0  0  0  0  0"
        )
    for index in range(1, 13):
        lines.append(f"{index:3d}{index + 1:3d}  1  0")
    lines.extend([f"{13:3d}{1:3d}  1  0", "M  END", ""])
    return native, fixture, {
        "details": "success: generated 1 pose(s)",
        "ligand": ligand or fixture["ligand"],
        "ligand_positions": ["\n".join(lines)],
        "position_confidence": [-1.25],
        "protein": fixture["protein"],
        "status": "success",
        "trajectory": ["trajectory"],
    }


@pytest.mark.parametrize("mutation", ["wrong-ligand", "degenerate-coordinates"])
def test_diffdock_native_validator_rejects_semantic_mutation(mutation):
    native, fixture, response = diffdock_response(
        ligand="CCO" if mutation == "wrong-ligand" else None,
        degenerate=mutation == "degenerate-coordinates",
    )
    with pytest.raises(native.ValidationError):
        native._validate_response(response, fixture)
