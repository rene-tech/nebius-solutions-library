from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pytest
import torch
from fastapi.testclient import TestClient
from pydantic import ValidationError
from rdkit import Chem


@pytest.fixture
def server(monkeypatch):
    for name in (
        "BIONEMO_SOURCE_REVISION",
        "MOLMIM_NEMO",
        "MOLMIM_NEMO_SHA256",
        "MOLMIM_WEIGHTS_SHA256",
    ):
        monkeypatch.setenv(name, "test")
    spec = importlib.util.spec_from_file_location(
        "molmim_test_runtime", Path(__file__).with_name("server.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_perceiver_outer_residual_is_not_omitted(server, monkeypatch):
    model = server.MolMIMPort.__new__(server.MolMIMPort)
    model.device = torch.device("cpu")
    model.tokenizer = type("Tokenizer", (), {"encode": lambda _, smi: [1]})()
    monkeypatch.setattr(
        model,
        "_p",
        lambda name: torch.ones(1, 512)
        if name.endswith("init_hidden")
        else torch.zeros(640, 512),
    )
    monkeypatch.setattr(model, "_layer", lambda value, *args, **kwargs: value + 1)
    monkeypatch.setattr(model, "_layer_norm", lambda value, *args: value)
    monkeypatch.setattr(model, "_linear", lambda value, *args: value)
    # Six blocks, each cross+1/self+1 plus ORIGINAL block input: h <- 2*h+2.
    assert torch.equal(model.encode("C"), torch.full((1, 1, 512), 190.0))


def test_decoder_does_not_accept_truncated_unfinished_sequences(server, monkeypatch):
    model = server.MolMIMPort.__new__(server.MolMIMPort)
    model.device = torch.device("cpu")
    model.allowed_tokens = torch.tensor([True, True, True])
    model.tokenizer = type(
        "Tokenizer", (), {"bos_id": 0, "eos_id": 2, "decode": lambda _, ids: "C"}
    )()
    lengths = []

    def logits(tokens, latent):
        lengths.append(tokens.shape[1])
        return torch.tensor([[0.0, 2.0, 1.0]])

    monkeypatch.setattr(model, "logits", logits)
    assert model.decode(torch.zeros(1, 1, 512)) == [""]
    assert lengths == list(range(1, 129))


def test_tokenizer_rejects_unknown_tokens_instead_of_silent_substitution(server):
    tokenizer = server.RegexTokenizer(".", "<PAD>\n?\n^\n&\nC")
    with pytest.raises(ValueError, match="outside"):
        tokenizer.encode("N")


def test_tokenizer_preserves_single_backslash_stereobonds(server):
    tokenizer = server.RegexTokenizer(r"C|=|/", "<PAD>\n?\n^\n&\nC\n=\n/\n\\")
    assert tokenizer.decode(tokenizer.encode("C/C=C\\C")) == "C/C=C\\C"


def test_request_rejects_impossible_budget_before_model_work(server):
    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        server.GenerateRequest(smi="CC", particles=1)
    with pytest.raises(ValidationError, match="decode budget"):
        server.GenerateRequest(smi="CC", num_molecules=8, particles=2, iterations=1)


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.populations = []

    def encode(self, _smi):
        return torch.zeros(1, 1, 512)

    def decode(self, latent):
        self.populations.append(latent.clone())
        return next(self.responses)


@pytest.mark.parametrize("minimize", [False, True])
def test_real_cma_updates_and_returns_distinct_feasible_decodes(
    server, minimize, monkeypatch
):
    original = server.cma.CMAEvolutionStrategy
    optimizers = []

    def capture(*args, **kwargs):
        optimizer = original(*args, **kwargs)
        optimizers.append(optimizer)
        return optimizer

    monkeypatch.setattr(server.cma, "CMAEvolutionStrategy", capture)
    model = FakeModel(
        [["CCC", "CCCC", "CCO", "CCN"], ["CCCO", "CCCCO", "CCCN", "CCCCN"]]
    )
    server.RUNTIME.model = model
    result = server._generate(
        server.GenerateRequest(
            smi="CC",
            particles=4,
            iterations=2,
            num_molecules=8,
            min_similarity=0,
            minimize=minimize,
        )
    )
    assert len(result["generated"]) == 8
    assert len({x["sample"] for x in result["generated"]}) == 8
    scores = [x["score"] for x in result["generated"]]
    assert scores == sorted(scores, reverse=not minimize)
    assert all(
        x["model_decoded"] and x["changed_from_input"] for x in result["generated"]
    )
    assert result["metrics"]["optimizer_steps"] == 2
    assert result["metrics"]["algorithm"] == "CMA-ES"
    assert not torch.equal(model.populations[0], model.populations[1])
    assert optimizers[0].countiter == 2
    assert not np.array_equal(optimizers[0].mean, np.zeros(512))
    assert not np.array_equal(optimizers[0].C, np.eye(512))


def test_exhaustion_has_counts_not_input_fallback(server):
    server.RUNTIME.model = FakeModel([["CC", "", "CCC", "CCC"]])
    with pytest.raises(server.GenerationExhausted) as caught:
        server._generate(
            server.GenerateRequest(
                smi="CC", particles=4, num_molecules=2, min_similarity=0
            )
        )
    assert caught.value.counts == {
        "attempted_model_decodes": 4,
        "invalid_decodes": 1,
        "below_similarity": 0,
        "unchanged_decodes": 1,
        "duplicate_decodes": 1,
        "optimizer_steps": 1,
        "distinct_feasible_molecules": 1,
        "requested_molecules": 2,
    }
    assert server.RUNTIME.generated == 0


def test_similarity_is_an_output_constraint(server):
    server.RUNTIME.model = FakeModel([["c1ccccc1", "CCCCCCCC"]])
    with pytest.raises(server.GenerationExhausted) as caught:
        server._generate(
            server.GenerateRequest(smi="CCO", particles=2, min_similarity=1)
        )
    assert caught.value.counts["below_similarity"] == 2


def test_http_exhaustion_and_invalid_molecule_are_actionable_422(server):
    server.RUNTIME.ready = True
    server.RUNTIME.model = FakeModel([["CC", "CC"]])
    client = TestClient(server.app)
    response = client.post("/generate", json={"smi": "CC"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "GENERATION_EXHAUSTED"
    assert "generated" not in response.json()
    response = client.post("/generate", json={"smi": "not a molecule"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_MOLECULE"
    assert server.RUNTIME.failures == 2


def test_cpu_exact_checkpoint_reconstruction(server):
    root = os.getenv("MOLMIM_TEST_EXTRACTED_CHECKPOINT")
    if not root:
        pytest.skip(
            "Set MOLMIM_TEST_EXTRACTED_CHECKPOINT to the hash-verified retained checkpoint"
        )
    root = Path(root)
    assert (
        server._sha256(root / "model_weights.ckpt")
        == "bc246330d019b18a4f0acaa88b65d64eaf6e5e940f1ec82e1ba097aff1fdddee"
    )
    tokenizer = server.RegexTokenizer(
        (root / "048c1f797f464dd5b6a90f60f9405827_molmim.model").read_text(),
        (root / "dd344353154640acbbaea1d4536fa7d0_molmim.vocab").read_text(),
    )
    model = server.MolMIMPort(
        torch.load(root / "model_weights.ckpt", weights_only=True, map_location="cpu"),
        tokenizer,
        device="cpu",
    )
    torch.set_num_threads(2)
    with torch.inference_mode():
        for smi in ("CC(=O)Oc1ccccc1C(=O)O", "Cn1c(=O)c2c(ncn2C)n(C)c1=O"):
            assert model.decode(model.encode(smi)) == [
                Chem.MolToSmiles(Chem.MolFromSmiles(smi))
            ]
        oht = r"CC/C(=C(\c1ccc(O)cc1)c1ccc(OCCN(C)C)cc1)c1ccccc1"
        canonical = Chem.MolToSmiles(Chem.MolFromSmiles(oht))
        assert tokenizer.decode(tokenizer.encode(canonical)) == canonical
        assert Chem.MolFromSmiles(model.decode(model.encode(canonical))[0]) is not None


def test_cma_randomness_is_request_local(server):
    np.random.seed(99)
    before = np.random.get_state()
    server.RUNTIME.model = FakeModel([["CCC", "CCCC"]])
    server._generate(server.GenerateRequest(smi="CC", min_similarity=0))
    after = np.random.get_state()
    assert np.array_equal(before[1], after[1])
    assert before[2:] == after[2:]
