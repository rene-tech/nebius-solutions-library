import json
from unittest.mock import patch

import pytest

from release_backend import ensure_configmap


@pytest.mark.parametrize('changed', ['data', 'binaryData', None])
def test_existing_maps_are_reused_only_with_identical_bytes(tmp_path, changed):
    desired = {'metadata': {'name': 'content-hash'}, 'data': {'value': 'a'}}
    path = tmp_path / 'map.json'
    path.write_text(json.dumps(desired))
    existing = {**desired, 'immutable': True}
    if changed:
        existing[changed] = {'value': 'different'}
    with patch('release_backend.subprocess.check_output', return_value=json.dumps(existing).encode()), \
         patch('release_backend.subprocess.run') as run:
        if changed:
            with pytest.raises(ValueError, match='different bytes'):
                ensure_configmap(['kubectl'], path, apply=True)
        else:
            assert ensure_configmap(['kubectl'], path, apply=True) == 'reused'
        run.assert_not_called()


@pytest.mark.parametrize('apply,expected', [(False, 1), (True, 2)])
def test_new_map_is_server_validated_before_apply(tmp_path, apply, expected):
    path = tmp_path / 'map.json'
    path.write_text(json.dumps({'metadata': {'name': 'new'}}))
    with patch('release_backend.subprocess.check_output', return_value=b''), \
         patch('release_backend.subprocess.run') as run:
        ensure_configmap(['kubectl'], path, apply=apply)
        assert run.call_count == expected
        assert '--dry-run=server' in run.call_args_list[0].args[0]
