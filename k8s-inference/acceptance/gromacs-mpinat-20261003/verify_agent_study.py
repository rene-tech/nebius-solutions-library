"""Read actual Runs deliverables and verify native timing provenance; no writes to serving.

Study completion is not benchmark completion. This acceptance helper downloads
the declared report plus exact native source/log files using the QA browser
session, independently checks their hashes/rows, and never submits model work.
"""
import csv
import hashlib
import io
import json
import math
from pathlib import PurePosixPath
import shlex


def sha(data):
    return hashlib.sha256(data).hexdigest()


def workspace_path(value):
    path = PurePosixPath(value)
    if not path.is_relative_to('/workspace') or '..' in path.parts:
        raise ValueError('Expected an exact mounted workspace file')
    return str(path.relative_to('/workspace'))


def verify_timing_report(report, rows, markdown, fetch, expected_repeats=3):
    if (report.get('schema') != 'scientific-ai/native-md-timing-report/v1'
            or report.get('complete') is not True or report.get('gaps')
            or report.get('reported_repeats') != expected_repeats
            or not report.get('timing_rows')):
        raise ValueError('Missing or incomplete native timing report, not benchmark success')
    receipt = json.loads(fetch(report['receipt_file']))
    if receipt.get('state') != 'verified' or receipt.get('operation_id') != report['operation_id']:
        raise ValueError('Report does not bind a verified original operation')
    mapping = json.loads(fetch(str(PurePosixPath(report['receipt_file']).parent / 'native-files.json')))
    native = {}
    for source in report['sources']:
        reference = next((a for a in receipt['verified_artifacts']
                          if a['path'] == source['path'] and a['sha256'] == source['sha256']), None)
        if reference is None:
            raise ValueError('Native source is not in the original verified receipt')
        value = json.loads(fetch(source['path'], reference))
        if (value.get('schema') != 'fs2-serve.nebius.ai/gromacs-workflow-result/v1'
                or value.get('operation_id') != report['operation_id'] or value.get('status') != 'succeeded'
                or value.get('inventory_complete', True) is not True):
            raise ValueError('Native result does not prove completed GROMACS work')
        native[source['sha256']] = value
    csv_rows = list(csv.DictReader(io.StringIO(rows)))
    if len(csv_rows) != len(report['timing_rows']):
        raise ValueError('CSV and native JSON row counts differ')
    required_columns = {'job_id', 'step_id', 'segment', 'requested_steps', 'performance_ns_per_day',
                        'wall_seconds', 'command', 'log_file', 'log_sha256', 'source_result_sha256'}
    if any(not required_columns <= set(row) for row in csv_rows):
        raise ValueError('CSV omits required native timing/provenance columns')
    seen, repeats = set(), set()
    for row, table in zip(report['timing_rows'], csv_rows):
        source = native[row['source_result_sha256']]
        key = (row['job_id'], row['step_id'], row['segment'])
        if key in seen or row['job_id'] != source['job_id'] or row['step_id'] not in source['completed_steps']:
            raise ValueError('Duplicate or uncompleted native timing row')
        seen.add(key)
        repeats.add(key[:2])
        matches = [c for c in source['commands'] if c.get('step_id') == row['step_id']
                   and c.get('segment') == row['segment'] and 'mdrun' in c.get('command', [])]
        if len(matches) != 1:
            raise ValueError('Native simulation command identity is ambiguous')
        command = matches[0]
        if row['command'] != command['command'] or command.get('exit_code') != 0:
            raise ValueError('Report command differs from successful native execution')
        for field in ('performance_ns_per_day', 'wall_seconds'):
            value = row.get(field)
            if (type(value) not in (float, int) or not math.isfinite(value) or value <= 0
                    or value != command.get(field)):
                raise ValueError('Report timing differs from native measurement')
        for field in ('executed_steps', 'durably_completed_steps', 'checkpoint_step'):
            if row.get(field) != command.get(field):
                raise ValueError('Report invents unrecorded progress counts')
        def option(arguments, key):
            return arguments[arguments.index(key) + 1] if key in arguments else None
        requested = option(command['command'], '-nsteps')
        if requested is None:
            target = option(command['command'], '-s')
            prior = source['commands'][:source['commands'].index(command)]
            conversions = [c for c in prior if 'convert-tpr' in c.get('command', [])
                           and c.get('directory', '.') == command.get('directory', '.')
                           and target is not None and option(c['command'], '-o') == target]
            requested = option(conversions[-1]['command'], '-nsteps') if conversions else None
        requested = int(requested) if requested is not None and int(requested) >= 0 else None
        if row.get('requested_steps') != requested:
            raise ValueError('Requested work count differs from the exact native TPR/command')
        files = next(item['files'] for item in mapping['results']
                     if item['source_result_sha256'] == row['source_result_sha256'])
        mapped = next(item for item in files if item['native_path'] == command['log'])
        expected = next(item for item in source['files'] if item['path'] == command['log'])
        if (mapped['path'] != row['log_file'] or mapped['sha256'] != expected['sha256']
                or row['log_sha256'] != expected['sha256']):
            raise ValueError('Timing source log is not the original native file')
        fetch(row['log_file'], expected)
        for field, actual in table.items():
            expected_value = shlex.join(row['command']) if field == 'command' else row.get(field)
            if actual != ('' if expected_value is None else str(expected_value)):
                raise ValueError('CSV measurement differs from verified timing JSON: ' + field)
        if row['step_id'] not in markdown or str(row['performance_ns_per_day']) not in markdown:
            raise ValueError('Markdown omitted a native timing measurement')
    if len(repeats) != expected_repeats:
        raise ValueError('Resumed segments are not the requested distinct repeat set')
    return {'operation_id': report['operation_id'], 'verified_native_segments': len(seen),
            'verified_repeats': len(repeats), 'source_result_hashes': sorted(native),
            'native_report_verified': True, 'scientific_convergence_claimed': False}


def verify_study(client, study, expected_repeats=3):
    if study.get('state') != 'completed' or not study.get('artifacts'):
        raise ValueError('Study has not published terminal deliverables')
    downloads, data = [], {}

    def fetch(path, expected=None):
        response = client.get('/api/scientific-demos/workspace/file', params={'path': workspace_path(path)})
        response.raise_for_status()
        value = response.content
        if expected and (len(value) != expected['size_bytes'] or sha(value) != expected['sha256']):
            raise ValueError('Authenticated download differs from the recorded artifact')
        downloads.append({'path': path, 'size_bytes': len(value), 'sha256': sha(value)})
        return value

    for item in study['artifacts']:
        value = fetch(item['path'], item)
        name = PurePosixPath(item['path']).name
        if name in data:
            raise ValueError('Ambiguous duplicate report artifact name')
        data[name] = value
    names = ('native-timing-report.json', 'native-timings.csv', 'native-timing-report.md')
    if not set(names) <= set(data):
        raise ValueError('Study completed without requested native timing deliverables')
    result = verify_timing_report(json.loads(data[names[0]]), data[names[1]].decode(),
                                 data[names[2]].decode(), fetch, expected_repeats)
    return {**result, 'study_id': study['id'], 'authenticated_downloads': downloads,
            'durable_study_delivery_verified': True}
