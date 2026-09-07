import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

from prepare_donor import captured_model_revision, restore_compatible_donor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from validate_confidence_archive import validate_confidence_archive


@pytest.mark.parametrize("model", ["esmfold2", "esmfold2-fast"])
def test_capture_preserves_locked_weight_revision_not_source_revision(model):
    solution = Path(__file__).resolve().parents[4]
    lock = json.loads((solution / "models/cancer-immunotherapy/images/structure-secondary/image-lock.json").read_bytes())
    image = next(item for item in lock["images"] if item.get("build_args", {}).get("RUNTIME_ID") == model)
    revision = captured_model_revision(lock, model)
    assert revision == image["build_args"]["MODEL_REVISION"]
    assert revision != image["source"]["revision"]


def test_root_esm_donor_matches_restore_capabilities_without_widening_them():
    restore_capabilities = ["SYS_ADMIN", "SYS_PTRACE", "CHECKPOINT_RESTORE", "NET_ADMIN", "SYS_TIME"]
    pod = {"spec": {"containers": [{"name": "scientific-stage", "securityContext": {
        "runAsUser": 0, "runAsGroup": 0,
        "capabilities": {"add": [*restore_capabilities, "SYS_RESOURCE"]},
    }}]}}
    assert restore_compatible_donor(pod) is pod
    security = pod["spec"]["containers"][0]["securityContext"]
    assert security["capabilities"]["add"] == restore_capabilities
    assert security["runAsUser"] == security["runAsGroup"] == 0
    assert restore_compatible_donor(pod) == pod


def test_isolated_validation_rejects_the_production_revision_mismatch():
    from fs2_serve.scientific_batch.adapters.primitives import ScientificAdapterError

    envelope = {
        "schema": "fs2.nebius.ai/structure-confidence/v1",
        "runtime_id": "esmfold2",
        "model_revision": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
        "input_identity": {"artifact_id": "input", "sha256": "a" * 64},
        "seeds": [101], "samples_per_seed": 1, "results": [{}],
    }
    data = json.dumps(envelope).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo("confidence.json")
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    command = ["fold", "--variant", "esmfold2", "--seed", "101",
               "--expected-raw-input-artifact-id", "input", "--expected-raw-input-sha256", "a" * 64]
    with pytest.raises(ScientificAdapterError, match="identity or cardinality"):
        validate_confidence_archive(buffer.getvalue(), command, "8fc3ff471022fdce52c77030685eb775de0c00a3")
