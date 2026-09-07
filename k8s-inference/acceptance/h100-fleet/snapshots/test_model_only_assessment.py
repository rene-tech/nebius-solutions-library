import hashlib
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
ASSESSMENT = HERE / "proteina-boltzgen-model-only-assessment-20260907.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_unqualified_model_only_assessment_is_bound_to_current_evidence():
    assessment = json.loads(ASSESSMENT.read_bytes())
    capabilities = json.loads((HERE / "capabilities.json").read_bytes())
    entries = {entry["model_id"]: entry for entry in capabilities["models"]}

    assert assessment["decision"] == "keep-normal-load"
    assert assessment["gpu_trials_started"] == 0
    models = {entry["model_id"]: entry for entry in assessment["models"]}
    assert set(models) == {"proteina-complexa", "boltzgen"}

    for model_id, model in models.items():
        assert not model["selectable"]
        assert not model["fresh_pod_restore_passed"]
        assert model["distinct_inputs_passed"] == 0
        capability = entries[model_id]
        assert capability["status"] == "not-yet-qualified"
        assert not capability["selectable"]
        assert capability["bundle"] is None

        evidence = model["evidence"]
        startup_report = (HERE / evidence["startup_report"]).resolve()
        assert startup_report.is_file()
        assert _sha256(startup_report) == evidence["startup_report_sha256"]
        assert (HERE / evidence["public_semantic_acceptance"]).resolve().is_file()


def test_reported_startup_shares_are_calculated_from_observed_medians():
    assessment = json.loads(ASSESSMENT.read_bytes())
    models = {entry["model_id"]: entry for entry in assessment["models"]}

    proteina = models["proteina-complexa"]
    load = proteina["observed_model_load"]["median_seconds"]
    initialized = proteina["normal_startup"]["median_seconds"]
    public = proteina["other_observed_clocks"][
        "public_request_to_validated_completion_median_seconds"
    ]
    assert round(100 * load / initialized, 3) == 28.55
    assert round(100 * load / public, 3) == 3.531

    boltzgen = models["boltzgen"]
    ready = boltzgen["normal_startup"]["median_seconds"]
    design = boltzgen["observed_work"]["isolated_design_valid_output_median_seconds"]
    public = boltzgen["observed_work"]["current_public_multistage_completion_seconds"]
    assert round(100 * ready / design, 3) == 12.504
    assert round(100 * ready / public, 3) == 2.907
