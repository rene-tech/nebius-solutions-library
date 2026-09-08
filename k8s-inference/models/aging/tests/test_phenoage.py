import math

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aging.contracts import ClinicalRequest, ClinicalSample
from aging.phenoage.runtime import COEFFICIENTS, ClinicalPhenoAgeRuntime, clinical_age
from aging.server import create_app


def synthetic_sample(**overrides):
    return {
        "sample_id": "synthetic-50",
        "age_years": 50,
        "albumin_g_l": 45,
        "creatinine_umol_l": 80,
        "glucose_mmol_l": 5,
        "c_reactive_protein_mg_dl": 0.1,
        "lymphocyte_percent": 30,
        "mean_cell_volume_fl": 90,
        "red_cell_distribution_width_percent": 13,
        "alkaline_phosphatase_u_l": 70,
        "white_blood_cell_count_10e3_per_ul": 6,
        **overrides,
    }


def test_primary_supplement_reference_and_distinct_second_request():
    sample = ClinicalSample(**synthetic_sample())
    assert clinical_age(sample) == pytest.approx(41.90792243377999, abs=1e-10)
    second = ClinicalSample(**synthetic_sample(sample_id="synthetic-60", age_years=60))
    assert clinical_age(second) - clinical_age(sample) == pytest.approx(
        10 * 0.0804 / 0.090165
    )


def test_stable_algebra_matches_literal_primary_equations():
    for age in (20, 50, 90):
        sample = ClinicalSample(**synthetic_sample(age_years=age))
        fields = sample.model_dump(exclude={"sample_id"})
        fields["c_reactive_protein_mg_dl"] = math.log(
            fields["c_reactive_protein_mg_dl"]
        )
        xb = -19.9067 + sum(
            fields[key] * weight for key, weight in COEFFICIENTS.items()
        )
        mortality = 1 - math.exp(
            -math.exp(xb) * (math.exp(120 * 0.0076927) - 1) / 0.0076927
        )
        literal = 141.50225 + math.log(-0.00553 * math.log(1 - mortality)) / 0.090165
        assert clinical_age(sample) == pytest.approx(literal, abs=1e-10)


def test_does_not_reproduce_pyaging_gamma_mixup_or_silently_floor_crp():
    assert clinical_age(ClinicalSample(**synthetic_sample())) != pytest.approx(
        51.52728733548396
    )
    small = clinical_age(
        ClinicalSample(**synthetic_sample(c_reactive_protein_mg_dl=0.001))
    )
    floor = clinical_age(
        ClinicalSample(**synthetic_sample(c_reactive_protein_mg_dl=0.01))
    )
    assert floor - small == pytest.approx(0.0954 * math.log(10) / 0.090165)


@pytest.mark.parametrize(
    "field,value",
    [
        ("c_reactive_protein_mg_dl", 0),
        ("c_reactive_protein_mg_dl", -1),
        ("age_years", float("nan")),
        ("glucose_mmol_l", float("inf")),
        ("lymphocyte_percent", 101),
        ("age_years", -1),
    ],
)
def test_invalid_units_domain_and_nonfinite_inputs(field, value):
    with pytest.raises(ValidationError):
        ClinicalSample(**synthetic_sample(**{field: value}))


def test_missing_or_duplicate_sample_inputs_rejected():
    incomplete = synthetic_sample()
    del incomplete["albumin_g_l"]
    with pytest.raises(ValidationError):
        ClinicalSample(**incomplete)
    with pytest.raises(ValidationError):
        ClinicalRequest(samples=[synthetic_sample(), synthetic_sample()])


def test_finite_high_linear_predictor_does_not_saturate_intermediate_cdf():
    sample = ClinicalSample(**synthetic_sample(creatinine_umol_l=10000))
    assert math.isfinite(clinical_age(sample))


def test_real_cpu_http_batch_schema_metrics_and_two_distinct_predictions(monkeypatch):
    monkeypatch.delenv("AGING_DEVICE", raising=False)
    with TestClient(create_app("phenoage")) as client:
        assert client.get("/v1/health/ready").json()["device"] == "cpu"
        payload = {
            "samples": [
                synthetic_sample(),
                synthetic_sample(sample_id="second", age_years=60),
            ]
        }
        response = client.post("/v1/predict", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["sample_count"] == 2
        assert body["predictions"][0]["phenotypic_age_years"] == pytest.approx(
            41.90792243377999
        )
        assert (
            body["predictions"][1]["phenotypic_age_years"]
            > body["predictions"][0]["phenotypic_age_years"]
        )
        assert body["gpu_snapshot"] == "not-applicable-cpu"
        assert (
            'fs2_model_requests_total{model="phenoage"} 1'
            in client.get("/metrics").text
        )
        assert (
            client.post(
                "/v1/predict", json={"samples": [synthetic_sample(log_crp=0.1)]}
            ).status_code
            == 422
        )


def test_cpu_formula_refuses_misleading_gpu_configuration(monkeypatch):
    monkeypatch.setenv("AGING_DEVICE", "cuda")
    with pytest.raises(ValueError, match="CPU formula"):
        with TestClient(create_app("phenoage")):
            pass


def test_metadata_keeps_formula_target_explicit():
    assert (
        ClinicalPhenoAgeRuntime().metadata()["input_modality"]
        == "age-and-nine-blood-biomarkers"
    )
