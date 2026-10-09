import copy
import hashlib

import pytest

from stage_agent_inputs import copy_checked, rebase_remaining


def inputs():
    return ({'candidate_image': 'registry.invalid/image@sha256:' + 'a' * 64,
             'cases': [{'case_id': 'mpinat-missing', 'prompt': 'original candidate-aaaaaaaa shape'}]},
            {'coverage': [{'case_id': 'mpinat-missing', 'scientific_case': True, 'native_benchmark_complete': False},
                          {'case_id': 'mpinat-done', 'scientific_case': True, 'native_benchmark_complete': True}]})


def test_rebase_changes_identity_not_scientific_prompt_or_original_manifest():
    remaining, reconciliation = inputs()
    original = copy.deepcopy(remaining)
    result, old, new = rebase_remaining(remaining, reconciliation, 'registry.invalid/image@sha256:' + 'b' * 64, 'c' * 40)
    assert old == 'a' * 8 and new == 'b' * 8
    assert result['cases'][0]['prompt'] == 'original candidate-bbbbbbbb shape'
    assert result['excluded_native_complete'] == ['mpinat-done']
    assert not result['new_gpu_submissions_authorized']
    assert remaining == original


@pytest.mark.parametrize('change', ['complete', 'duplicate', 'missing'])
def test_no_complete_duplicate_or_silently_omitted_case(change):
    remaining, reconciliation = inputs()
    if change == 'complete':
        remaining['cases'].append({'case_id': 'mpinat-done', 'prompt': 'never replay'})
    elif change == 'duplicate':
        remaining['cases'] *= 2
    else:
        remaining['cases'] = []
    with pytest.raises(ValueError, match='Remaining'):
        rebase_remaining(remaining, reconciliation, 'registry.invalid/image@sha256:' + 'b' * 64, 'c' * 40)


def test_public_fixture_copy_preserves_bytes_and_never_overwrites(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'original public fixture')
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / 'workspace/input'
    result = copy_checked(source, target, expected, source.stat().st_size)
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o444
    assert result['sha256'] == expected
    with pytest.raises(FileExistsError):
        copy_checked(source, target, expected, source.stat().st_size)
    with pytest.raises(ValueError, match='identity'):
        copy_checked(source, tmp_path / 'wrong', '0' * 64, source.stat().st_size)
    assert not (tmp_path / 'wrong').exists()
