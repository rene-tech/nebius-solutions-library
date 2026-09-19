import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import numpy as np


spec = importlib.util.spec_from_file_location("diffdock_seed_patch", Path(__file__).with_name("patch_upstream.py"))
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


CONFORMER = '''def generate_conformer(mol):
    ps = AllChem.ETKDGv2()
    failures, id = 0, -1
    while failures < 3 and id == -1:
        id = AllChem.EmbedMolecule(mol, ps)
        failures += 1
    if id == -1:
        ps.useRandomCoords = True
        AllChem.EmbedMolecule(mol, ps)
        AllChem.MMFFOptimizeMolecule(mol, confId=0)
        return True
    return False
'''


@pytest.mark.parametrize("seed,expected", [(19, [19, 20, 21, 22]), (2147483647, [2147483647, 1, 2, 3]), (None, [-1] * 4)])
def test_deterministic_distinct_retry_seeds_and_unchanged_unseeded_callers(seed, expected):
    calls = []
    api = SimpleNamespace(ETKDGv2=lambda: SimpleNamespace(randomSeed=-1, useRandomCoords=False),
        EmbedMolecule=lambda molecule, params: calls.append(params.randomSeed) or -1,
        MMFFOptimizeMolecule=lambda *args, **kwargs: None)
    namespace = {"AllChem": api}
    exec(patcher.transform("datasets/process_mols.py", CONFORMER), namespace)
    assert namespace["generate_conformer"](object(), seed) is True
    assert calls == expected


def test_changed_pattern_fails_instead_of_silently_losing_seed():
    with pytest.raises(ValueError, match="pattern changed"):
        patcher.transform("datasets/process_mols.py", "def other(): pass")


def test_unknown_source_bytes_fail_before_writing(tmp_path):
    source = tmp_path / "datasets/process_mols.py"
    source.parent.mkdir()
    source.write_text(CONFORMER)
    with pytest.raises(ValueError, match="source digest"):
        patcher.apply(tmp_path)
    assert source.read_text() == CONFORMER


def test_score_and_confidence_dataset_receive_request_seed():
    source = Path(__file__).parents[1] / "adapters/diffdock.py"
    calls = [node for node in ast.walk(ast.parse(source.read_text()))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "InferenceDataset"]
    assert len(calls) == 2
    for call in calls:
        seed = next(keyword.value for keyword in call.keywords if keyword.arg == "random_seed")
        assert isinstance(seed, ast.Name) and seed.id == "seed"


TORUS = '''def sample(sigma):
    out = sigma * np.random.randn(*sigma.shape)
    out = (out + np.pi) % (2 * np.pi) - np.pi
    return out

score_norm_ = score(
    sample(sigma[None].repeat(10000, 0).flatten()),
    sigma[None].repeat(10000, 0).flatten()
).reshape(10000, -1)
score_norm_ = (score_norm_ ** 2).mean(0)
'''


def test_startup_torsion_normalization_is_local_and_repeatable():
    original_state = np.random.get_state()
    tables = []
    try:
        for seed in (7, 23, 1001):
            np.random.seed(seed)
            before = np.random.get_state()
            namespace = {"np": np, "sigma": np.array([0.01, 0.1, 1.0]),
                         "score": lambda values, sigma: values / sigma}
            exec(patcher.transform("utils/torus.py", TORUS), namespace)
            after = np.random.get_state()
            assert before[0] == after[0]
            assert np.array_equal(before[1], after[1])
            assert before[2:] == after[2:]
            tables.append(namespace["score_norm_"])
        assert all(np.array_equal(tables[0], table) for table in tables[1:])
    finally:
        np.random.set_state(original_state)


def test_normal_torsion_samples_still_follow_request_rng():
    namespace = {"np": np, "sigma": np.array([0.01, 0.1]), "score": lambda x, sigma: x}
    exec(patcher.transform("utils/torus.py", TORUS), namespace)
    original_state = np.random.get_state()
    try:
        np.random.seed(19)
        expected = namespace["sample"](np.ones(10))
        np.random.seed(19)
        assert np.array_equal(expected, namespace["sample"](np.ones(10)))
        np.random.seed(23)
        assert not np.array_equal(expected, namespace["sample"](np.ones(10)))
    finally:
        np.random.set_state(original_state)
