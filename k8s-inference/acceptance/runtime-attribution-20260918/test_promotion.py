import copy
import json

import prepare_promotion as promotion
import pytest
from prepare_candidate import MODEL, candidate_template
from test_candidate import baseline


def fixture(monkeypatch):
    bundle = baseline()
    digest = candidate_template(bundle)["templateDigest"]
    monkeypatch.setattr(promotion, "QUALIFIED_TEMPLATE", digest)
    names = (
        {MODEL, "genmol", "proteinmpnn", "cosmos3-nano"}
        | promotion.existing.VOICES
        | {f"sibling-{n}" for n in range(11)}
    )
    envelope = {
        "revision": "before",
        "qualifications": {
            name: {
                "templateDigests": ["old"],
                "templateRefs": {"legacy": "old"},
                "templateCacheTiers": {"old": "SharedFilesystem"},
                "gpuSnapshotBundles": {"original": "retained"},
            }
            for name in names
        },
        "pools": {"existing": "untouched"},
    }
    rows = [
        {
            "model_id": name,
            "states": {
                "registered": True,
                "runtime_ready": True,
                "route_active": True,
                "semantic_qualified": True,
                "http_mcp_qualified": True,
                "cold_start_qualified": True,
                "elasticity_qualified": True,
            },
            "evidence": {
                "audited_catalog_sha256": "catalog",
                "retained_deployments_sha256": "prior",
                "http_mcp_acceptance_sha256": "old-http",
                "cold_start_acceptance_sha256": "old-cold",
            },
        }
        for name in sorted(names)
    ]
    routes = {
        "qualification-projection.json": json.dumps({"rows": rows}),
        "deployment-runtimes.json": '{"models":{"existing":"unchanged"}}',
        "lean-routes.json": "unchanged bytes",
    }
    configuration = {"operator_settings": "unchanged", "image_identity": "unchanged"}
    deployments = [
        {
            "metadata": {"name": MODEL, "namespace": "fs2-models"},
            "spec": {
                "modelRef": MODEL,
                "runtime": {
                    "image": "unchanged",
                    "templateRef": {"name": "legacy", "digest": "old"},
                },
                "fastStart": {"level": "Off"},
                "cache": {
                    "tier": "SharedFilesystem",
                    "snapshotPreference": "Prefer",
                    "snapshotRef": {"name": "old"},
                },
                "availability": {"minReplicas": 1, "maxReplicas": 2},
                "resources": {"unchanged": True},
            },
        }
    ]
    return envelope, [bundle], routes, configuration, deployments


def test_preserves_all_siblings_old_snapshots_admin_limits_and_only_changes_cxr_contract(
    monkeypatch,
):
    values = fixture(monkeypatch)
    before = copy.deepcopy(values)
    envelope, bundles, routes, admin, proposals = promotion.extend(*values)
    assert values == before
    assert bundles[:-1] == before[1]
    assert admin == before[3]
    assert routes["deployment-runtimes.json"] == before[2]["deployment-runtimes.json"]
    assert routes["lean-routes.json"] == before[2]["lean-routes.json"]
    for model in envelope["qualifications"]:
        assert (
            envelope["qualifications"][model]["gpuSnapshotBundles"]
            == before[0]["qualifications"][model]["gpuSnapshotBundles"]
        )
        if model != MODEL:
            assert (
                envelope["qualifications"][model] == before[0]["qualifications"][model]
            )
    rows = json.loads(routes["qualification-projection.json"])["rows"]
    for row, previous in zip(
        rows,
        json.loads(before[2]["qualification-projection.json"])["rows"],
        strict=True,
    ):
        if row["model_id"] != MODEL:
            assert row == previous
        else:
            assert {key for key, value in row["states"].items() if value} == {
                "registered",
                "runtime_ready",
            }
            assert row["evidence"]["retained_deployments_sha256"] == promotion.PROOF
            assert row["evidence"]["cold_start_acceptance_sha256"] is None
    changed = proposals[0]["spec"]
    assert changed["cache"] == {
        "tier": "SharedFilesystem",
        "snapshotPreference": "Never",
    }
    assert changed["runtime"]["image"] == before[4][0]["spec"]["runtime"]["image"]
    changed["cache"] = before[4][0]["spec"]["cache"]
    changed["runtime"] = before[4][0]["spec"]["runtime"]
    assert changed == before[4][0]["spec"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_voice",
        "unqualified_digest",
        "duplicate_model",
        "missing_row",
        "already_registered",
    ],
)
def test_rejects_stale_or_unqualified_capture(monkeypatch, mutation):
    values = fixture(monkeypatch)
    if mutation == "missing_voice":
        values[0]["qualifications"].pop(next(iter(promotion.existing.VOICES)))
    elif mutation == "unqualified_digest":
        monkeypatch.setattr(promotion, "QUALIFIED_TEMPLATE", "sha256:" + "0" * 64)
    elif mutation == "duplicate_model":
        values[4].append(copy.deepcopy(values[4][0]))
    elif mutation == "missing_row":
        values[2]["qualification-projection.json"] = '{"rows":[]}'
    else:
        values[0]["qualifications"][MODEL]["templateDigests"].append(
            promotion.QUALIFIED_TEMPLATE
        )
    with pytest.raises(ValueError):
        promotion.extend(*values)
