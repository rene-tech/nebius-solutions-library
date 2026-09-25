"""A pass for another input, model or recipe must not qualify this release."""

import asyncio

import pytest
import qualify_pack
import run_example as runner


def fixture(tmp_path):
    objects, cases, proofs = [], [], []

    def add(path, value):
        body = runner.encoded(value)
        (tmp_path / path).write_bytes(body)
        objects.append(
            {
                "path": path,
                "sha256": runner.sha(body),
                "size_bytes": len(body),
                "provenance": {
                    "source": "test",
                    "license": "CC0-1.0",
                    "attribution": "test",
                    "transformation": "none",
                },
            }
        )

    for index in range(10):
        recipe = {"model_id": "test-model", "arguments": {"seed": index}}
        path, case_id = f"recipe-{index}.json", f"test/case-{index}"
        add(path, {"recipes": [recipe]})
        cases.append(
            {
                "id": case_id,
                "category": "test",
                "recipes": path,
                "compatible_model_ids": ["test-model"],
            }
        )
        proofs.append(
            {
                "case_id": case_id,
                "model_id": "test-model",
                "recipe_sha256": runner.sha(runner.encoded(recipe)),
                "input_sha256": runner.sha(runner.encoded(recipe["arguments"])),
                "state": "passed",
            }
        )
    manifest = {
        "categories": [{"id": "test"}],
        "cases": cases,
        "objects": objects,
        "live_model_ids": ["test-model"],
    }
    (tmp_path / "manifest.json").write_bytes(runner.encoded(manifest))
    report = tmp_path / "proof.json"
    report.write_bytes(runner.encoded({"results": proofs}))
    return manifest, proofs, report


def test_complete_exact_coverage(tmp_path):
    _, _, report = fixture(tmp_path)
    _, selected, missing = asyncio.run(qualify_pack.coverage(tmp_path, [report]))
    assert len(selected) == 10 and missing == []


@pytest.mark.parametrize(
    "field", ["state", "recipe_sha256", "input_sha256", "model_id"]
)
def test_unrelated_or_failed_receipt_does_not_pass(tmp_path, field):
    _, proofs, report = fixture(tmp_path)
    proofs[0][field] = "different"
    report.write_bytes(runner.encoded({"results": proofs}))
    _, selected, missing = asyncio.run(qualify_pack.coverage(tmp_path, [report]))
    assert len(selected) == 9 and missing == [
        {"case_id": "test/case-0", "model_id": "test-model"}
    ]


def test_missing_provenance_blocks_publication(tmp_path):
    manifest, _, report = fixture(tmp_path)
    manifest["objects"][0]["provenance"]["license"] = ""
    (tmp_path / "manifest.json").write_bytes(runner.encoded(manifest))
    with pytest.raises(ValueError, match="provenance_missing"):
        asyncio.run(qualify_pack.coverage(tmp_path, [report]))


def test_uncovered_live_model_blocks_publication(tmp_path):
    manifest, _, report = fixture(tmp_path)
    manifest["live_model_ids"].append("uncovered-model")
    (tmp_path / "manifest.json").write_bytes(runner.encoded(manifest))
    with pytest.raises(ValueError, match="live_model_coverage_incomplete"):
        asyncio.run(qualify_pack.coverage(tmp_path, [report]))


def test_explicit_five_workflow_category_still_requires_every_recipe(tmp_path):
    manifest, proofs, report = fixture(tmp_path)
    manifest["cases"] = manifest["cases"][:5]
    manifest["categories"][0]["minimum_cases"] = 5
    (tmp_path / "manifest.json").write_bytes(runner.encoded(manifest))
    _, selected, missing = asyncio.run(qualify_pack.coverage(tmp_path, [report]))
    assert len(selected) == 5 and not missing
    report.write_bytes(runner.encoded({"results": proofs[1:]}))
    _, selected, missing = asyncio.run(qualify_pack.coverage(tmp_path, [report]))
    assert len(selected) == 4 and missing == [
        {"case_id": "test/case-0", "model_id": "test-model"}
    ]
