import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


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
