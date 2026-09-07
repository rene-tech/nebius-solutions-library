import hashlib
import json

from conftest import CATALOG_ROOT, SOLUTION_ROOT
from fs2_serve_catalog.artifacts import load_artifact_manifest


def test_evo2_record_binds_actual_merged_checkpoint_and_qualified_receipt():
    entry = json.loads((CATALOG_ROOT / "deployment-runtimes/evo2-40b-portable-h100.json").read_text())
    artifact = entry["record"]["cache"]["artifact"]
    manifest = load_artifact_manifest(
        CATALOG_ROOT / "deployment-runtimes/artifacts" / f"evo2-40b-{artifact['manifest_digest']}.json"
    )
    assert manifest.digest == artifact["manifest_digest"]
    assert manifest.expanded_bytes == artifact["expanded_bytes"] == 82253491694
    assert len(manifest.files) == 1
    assert manifest.files[0].path == "evo2_40b.pt"
    assert manifest.files[0].sha256 == "dd299612b1c1cdded0dfdcaf4d16f98fc97458261d80f4d662429f0ccb316bc3"
    raw = (SOLUTION_ROOT / "acceptance/h100-fleet/medical-media/evo2-qualification.json").read_bytes()
    receipt = json.loads(raw)
    assert entry["qualification"]["evidence"]["cold_start_acceptance_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["artifact_manifest_sha256"] == manifest.digest
    assert receipt["runtime_image"] == entry["record"]["runtime"]["image"]["reference"]
    assert receipt["hardware"]["gpu_count"] == entry["record"]["resources"]["gpu"]["count"] == 2
    assert not entry["qualification"]["states"]["http_mcp_qualified"]
