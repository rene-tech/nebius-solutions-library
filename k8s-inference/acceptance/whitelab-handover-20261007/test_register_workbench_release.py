import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('register_release', Path(__file__).with_name('register_workbench_release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_extends_only_the_existing_map_and_is_idempotent():
    original = {'metadata': {'labels': {'keep': 'same'}}, 'spec': {'containers': [
        {'name': 'api', 'image': 'keep-image', 'env': [
            {'name': 'FS2_WORKBENCH_RELEASES', 'value': '{"prior":"prior-image"}'},
            {'name': 'UNCHANGED', 'valueFrom': {'secretKeyRef': {'name': 'same', 'key': 'same'}}}]}]}}
    after, values = release.extend(original, 'new-release', 'new-image')
    assert values == {'prior': 'prior-image', 'new-release': 'new-image'}
    assert after['metadata'] == original['metadata']
    assert after['spec']['containers'][0]['image'] == 'keep-image'
    assert after['spec']['containers'][0]['env'][1] == original['spec']['containers'][0]['env'][1]
    assert json.loads(original['spec']['containers'][0]['env'][0]['value']) == {'prior': 'prior-image'}
    assert release.extend(after, 'new-release', 'new-image')[0] == after
    with pytest.raises(ValueError, match='existing release identity'):
        release.extend(after, 'new-release', 'different-image')


def test_absent_map_does_not_invent_configuration():
    with pytest.raises(ValueError, match='one explicit'):
        release.extend({'spec': {'containers': [{'name': 'api'}]}}, 'release', 'image')
