import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("preview2_server", ROOT / "models/structure/openfold3-preview2/server.py")
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)
FIXTURE = ROOT / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/openfold3-native/fixtures/request-20aa.json"


def test_original_scientific_input_is_preserved(tmp_path):
    request = json.loads(FIXTURE.read_text())
    request_id, input_id, query = SERVER.prepare_request(request, tmp_path)
    assert request_id == input_id == request["request_id"]
    chain = query["queries"][input_id]["chains"][0]
    original = request["inputs"][0]["molecules"][0]
    assert chain["sequence"] == original["sequence"]
    assert Path(chain["main_msa_file_paths"][0]).read_text().rstrip("\n") == original["msa"]["main"]["a3m"]["alignment"]
    assert Path(chain["main_msa_file_paths"][0]).name == "inline_main.a3m"
    assert query["queries"][input_id]["use_main_msas"] is True
    assert query["seeds"] == [42]


def test_scores_are_actual_upstream_outputs(tmp_path):
    cif = tmp_path / "model.cif"
    cif.write_text("data_real")
    confidence = {source: index + 0.25 for index, source in enumerate(SERVER.SCORES.values())}
    result = SERVER.scored_structure("request-a", cif, confidence)
    for target, source in SERVER.SCORES.items():
        assert result[target] == confidence[source]
    assert "not NVIDIA NIM" in result["source"]
    confidence["gpde"] = float("nan")
    with pytest.raises(ValueError, match="nonfinite"):
        SERVER.scored_structure("request-a", cif, confidence)


def test_no_silent_semantic_parameter_dropping(tmp_path):
    request = json.loads(FIXTURE.read_text())
    request["inputs"][0]["molecules"][0]["diffusion_samples"] = 5
    with pytest.raises(ValueError, match="one diffusion sample"):
        SERVER.prepare_request(request, tmp_path)


def test_distinct_chains_have_distinct_upstream_msa_representatives(tmp_path):
    request = json.loads(FIXTURE.read_text())
    import copy
    chain_b = copy.deepcopy(request["inputs"][0]["molecules"][0])
    chain_b["id"] = "B"
    request["inputs"][0]["molecules"].append(chain_b)
    _, input_id, query = SERVER.prepare_request(request, tmp_path)
    chains = query["queries"][input_id]["chains"]
    assert Path(chains[0]["main_msa_file_paths"][0]).parent != Path(chains[1]["main_msa_file_paths"][0]).parent
