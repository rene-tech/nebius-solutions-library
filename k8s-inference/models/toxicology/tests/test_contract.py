import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from rdkit import Chem

from toxicology.contracts import ADMETRequest, CToxRequest
from toxicology.inputs import parse_rows, prepare_rows
from toxicology.server import create_app


class FakeRuntime:
    endpoints = {"test": {"units": "unitless"}}

    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.calls = []
        self.entered = threading.Event()

    def metadata(self):
        return {"model_version": "test", "limitations": ["synthetic test double"]}

    def predict(self, smiles, **kwargs):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.entered.set()
        self.calls.append(smiles)
        time.sleep(0.04)
        self.active -= 1
        return [{"predictions": {"test": float(len(s))}} for s in smiles]


@pytest.mark.parametrize("payload", [{}, {"smiles": "CCO", "csv": "smiles\nCCO"}, {"molecules": []}, {"smiles": "CCO", "unknown": True}, {"smiles": "CCO", "endpoints": ["x", "x"]}])
def test_invalid_contract(payload):
    with pytest.raises(ValidationError):
        ADMETRequest.model_validate(payload)


def test_ctox_method_and_seed():
    assert CToxRequest(smiles="CCO").method == "rf-ssl"
    with pytest.raises(ValidationError):
        CToxRequest(smiles="CCO", method="unpublished")
    with pytest.raises(ValidationError):
        CToxRequest(smiles="CCO", seed=-1)


def test_csv_errors_do_not_drop_or_reorder_records():
    rows = parse_rows(ADMETRequest(csv="id,smiles\nstereo,N[C@@H](C)C(=O)O\nbad,invalid\nsalt,CC(=O)[O-].[Na+]\nempty,\ncopy,N[C@@H](C)C(=O)O", id_column="id"))
    good, results = prepare_rows(rows)
    assert [r.id for r in good] == ["stereo", "salt", "copy"]
    assert results[1]["error"]["code"] == "invalid_smiles"
    assert results[3]["error"]["code"] == "missing_smiles"
    assert results[2]["warnings"] == ["disconnected_fragments_present"]
    assert "@" in results[0]["canonical_smiles"]
    assert len(results) == 5


def test_sdf_input():
    mol = Chem.MolFromSmiles("CCO")
    mol.SetProp("_Name", "ethanol")
    text = Chem.MolToMolBlock(mol) + "\n$$$$\n"
    good, _ = prepare_rows(parse_rows(ADMETRequest(sdf=text)))
    assert [r.id for r in good] == ["ethanol"]
    assert Chem.MolToSmiles(Chem.MolFromSmiles(good[0].smiles)) == "CCO"


@pytest.mark.parametrize("payload", [
    {"csv": "wrong\nCCO"},
    {"csv": "smiles\nCCO", "id_column": "missing"},
    {"csv": "id,smiles\nsame,CCO\nsame,CCN", "id_column": "id"},
    {"csv": "smiles\n" + "CCO\n" * 1001},
])
def test_invalid_file_contract(payload):
    with pytest.raises(ValueError):
        parse_rows(ADMETRequest(**payload))


def test_http_partial_unknown_and_no_prediction_for_invalid():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime_factory=lambda: runtime)) as client:
        response = client.post("/v1/predict", json={"molecules": [{"id": "a", "smiles": "CCO"}, {"id": "b", "smiles": "invalid"}]})
        assert response.status_code == 200
        result = response.json()
        assert (result["status"], result["succeeded"], result["failed"]) == ("partial", 1, 1)
        assert runtime.calls == [["CCO"]]
        assert [row["id"] for row in result["results"]] == ["a", "b"]
        assert client.post("/v1/predict", json={"smiles": "CCO", "endpoints": ["unknown"]}).status_code == 422
        assert client.post("/v1/predict", json={"smiles": "invalid"}).json()["status"] == "failed"
        assert client.get("/v1/health/ready").status_code == 200
        assert 'fs2_toxicology_molecules_total{model="admet-ai"} 1' in client.get("/metrics").text


def test_bounded_admission_responsive_probes_and_chunk_fairness(monkeypatch):
    monkeypatch.setenv("TOX_MAX_INFLIGHT", "2")
    monkeypatch.setenv("TOX_CHUNK_SIZE", "1")
    runtime = FakeRuntime()
    app = create_app(runtime_factory=lambda: runtime)

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                large = asyncio.create_task(client.post("/v1/predict", json={"molecules": [{"id": str(i), "smiles": "CCO"} for i in range(8)]}))
                await asyncio.to_thread(runtime.entered.wait, 2)
                small = asyncio.create_task(client.post("/v1/predict", json={"smiles": "CCN"}))
                await asyncio.sleep(0.01)
                started = time.perf_counter()
                assert (await client.get("/readyz")).status_code == 200
                assert time.perf_counter() - started < 0.5
                denied = await client.post("/v1/predict", json={"smiles": "CC"})
                assert denied.status_code == 429 and denied.headers["retry-after"] == "2"
                assert (await small).status_code == 200
                assert not large.done()
                assert (await large).status_code == 200
        assert runtime.maximum == 1
        assert runtime.calls.index(["CCN"]) < 7

    asyncio.run(exercise())
