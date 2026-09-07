import hashlib
import json
from pathlib import Path

import pytest
from test_scientific_admin import _readiness

from fs2_serve.model_inventory import build_model_inventory, load_snapshot_capabilities
from fs2_serve.scientific_batch.startup import validate_bundle


@pytest.mark.parametrize("model_id", ["esmfold2", "esmfold2-fast"])
def test_current_esm_options_require_exact_separate_qualified_bundle(model_id, registry):
    directory = Path(__file__).resolve().parents[3] / "acceptance/h100-fleet/snapshots"
    bundle = json.loads((directory / f"{model_id}-bundle.json").read_bytes())
    validate_bundle(bundle, bundle["bundle_id"])
    assert hashlib.sha256((directory / f"{model_id}-h100-20260907.json").read_bytes()).hexdigest() == bundle[
        "qualification_receipt_sha256"
    ]
    capabilities = load_snapshot_capabilities(directory / "capabilities.json")
    profile = _readiness(model_id)
    profile = profile.model_copy(update={
        "readiness": "qualified",
        "backend": profile.backend.model_copy(update={
            "runtime_image_digest": bundle["runtime_image"].split("@")[-1],
            "model_revision": bundle["profile_model_revision"],
        }),
    })

    def projected(bundles):
        inventory = build_model_inventory(registry.list(), [], [profile], scientific_projection_available=True,
            snapshot_capabilities=capabilities, snapshot_bundles=bundles)
        return next(row for row in inventory.items if row.model_id == model_id)

    assert not projected({}).snapshot_selectable
    selected = projected({bundle["bundle_id"]: bundle})
    assert selected.snapshot_selectable
    assert selected.snapshot_bundle_ids == [bundle["bundle_id"]]
    assert selected.snapshot_normal_startup.n == selected.snapshot_restore_startup.n == 3
    other = "esmfold2-fast" if model_id == "esmfold2" else "esmfold2"
    wrong = json.loads((directory / f"{other}-bundle.json").read_bytes())
    assert not projected({wrong["bundle_id"]: wrong}).snapshot_selectable
