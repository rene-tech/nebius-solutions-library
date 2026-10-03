import copy
import csv
import hashlib
import json

import pytest

import verify_agent_recovery as module


def fixture(tmp_path):
    native = {'commands': [], 'files': []}
    mapping = {'files': []}
    rows = []
    for index in range(1, 4):
        step = f'repeat-{index}'
        path = tmp_path / f'repeat{index}.part0001.log'
        path.write_text(f'Performance: {index}.5 1\nWriting checkpoint, step 10000\n')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        native['files'].append({'path': path.name, 'sha256': digest, 'size_bytes': path.stat().st_size})
        mapping['files'].append({'path': str(path), 'native_path': path.name})
        native['commands'].append({'step_id': step, 'segment': 1, 'checkpoint_step': 10000,
            'command': ['gmx', 'mdrun', '-deffnm', f'repeat{index}'], 'log': f'fs2-{step}.log',
            'performance_ns_per_day': index + 0.5, 'wall_seconds': 8.125})
        rows.append({'repeat': step, 'requested_steps': 10000, 'executed_steps': 10000,
                     'durably_completed_steps': 10000, 'ns_per_day': index + 0.5,
                     'wall_seconds_native_result': 8.125, 'log_file': path.name, 'log_sha256': digest})
    path = tmp_path / 'timings.csv'
    with path.open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path, native, mapping


def test_all_three_actual_native_rows_and_logs_required(tmp_path):
    path, native, mapping = fixture(tmp_path)
    rows = module.verify_csv(path, native, mapping, tmp_path, 10000)
    assert len(rows) == 3
    assert rows[0]['runner_wall_seconds'] == 8.125
    path.write_text(path.read_text().splitlines()[0] + '\n')
    with pytest.raises(ValueError, match='three nonempty'):
        module.verify_csv(path, native, mapping, tmp_path, 10000)


def test_report_cannot_change_rate_committed_steps_or_log_bytes(tmp_path):
    path, native, mapping = fixture(tmp_path)
    bad = copy.deepcopy(native)
    bad['commands'][0]['checkpoint_step'] = 9999
    with pytest.raises(ValueError, match='differs from native'):
        module.verify_csv(path, bad, mapping, tmp_path, 10000)
    original = path.read_text()
    path.write_text(original.replace(',1.5,', ',1000.5,'))
    with pytest.raises(ValueError, match='differs from native'):
        module.verify_csv(path, native, mapping, tmp_path, 10000)
    path.write_text(original)
    (tmp_path / 'repeat1.part0001.log').write_text('changed')
    with pytest.raises(ValueError, match='native hash/size'):
        module.verify_csv(path, native, mapping, tmp_path, 10000)


def test_explicit_unknown_work_counts_are_not_treated_as_zero(tmp_path):
    path, native, mapping = fixture(tmp_path)
    rows = list(csv.DictReader(path.open()))
    for row in rows:
        row['executed_steps'] = ''
        row['durably_completed_steps'] = ''
        row['checkpoint_step'] = '10000'
    with path.open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert len(module.verify_csv(path, native, mapping, tmp_path, 10000)) == 3


def test_recovery_help_is_not_another_operation_or_submission():
    operation = '925bed89-2914-4977-9535-a1295f4ea4cc'
    def call(command):
        return {'type': 'tool_call', 'tool_call': {'name': 'execute_command_mcp_environment-execution',
                'args': json.dumps({'command': command})}}
    messages = [{'content': [call('python helper.py --recover-operation-id --help'),
                              call('python helper.py --recover-operation-id ' + operation)]}]
    result = module.trace_details(messages, operation)
    assert not result['submission_flags']
    assert len(result['recovery_commands']) == 1
    result = module.trace_details(messages, 'f' * 36)
    assert result['submission_flags'] == ['Recovery command used another operation']
