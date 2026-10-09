"""Check saved agent recovery CSVs against native artifacts; no remote calls.

This deliberately qualifies the five recorded single-segment timing recoveries,
not arbitrary scientific prose, MD convergence or a new runtime deployment.
"""
import argparse
import copy
import csv
import io
import json
import math
from pathlib import Path
import re

from native_timings import verify_native_timings
from reconcile_agent import load, local_path, now, parse_json, save, sha


def field(row, *names):
    present = [row[name] for name in names if name in row]
    if len(present) != 1:
        raise ValueError('Expected one unambiguous report column: ' + '/'.join(names))
    return present[0]


def number(actual, expected, tolerance=0.0005):
    value = float(actual)
    if not math.isfinite(value) or not math.isclose(value, expected, rel_tol=0, abs_tol=tolerance):
        raise ValueError(f'Report number {actual} differs from native value {expected}')


def verify_csv(path, native, materialized, workspace, requested_steps):
    text = path.read_text()
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=',;')
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    commands = {row['step_id']: row for row in native['commands'] if 'mdrun' in row['command']}
    if len(commands) != 3 or len(rows) != 3:
        raise ValueError('Expected exactly three nonempty timing repeats')
    expected_files = {row['path']: row for row in native['files']}
    files = {Path(row['native_path']).name: row for row in materialized['files']}
    verified, seen = [], set()
    for row in rows:
        step = row.get('step_id') or row.get('repeat')
        if step not in commands or step in seen:
            raise ValueError('CSV repeat set differs from native commands')
        seen.add(step)
        command = commands[step]
        if command.get('segment') != 1:
            raise ValueError('This recovery comparison requires one segment per repeat')
        number(field(row, 'ns_per_day', 'performance_ns_per_day'), command['performance_ns_per_day'])
        number(field(row, 'wall_seconds_native_result', 'mdrun_wall_seconds_runner',
                     'wall_time_command_s', 'mdrun_wall_seconds_total_runner', 'mdrun_command_wall_seconds', 'wall_seconds'),
               command['wall_seconds'], tolerance=0.005)
        number(row['requested_steps'], requested_steps, tolerance=0)
        # Executed/committed progress is checked against the actual checkpoint
        # step in the verified successful runtime result, never operation age.
        if row['executed_steps']:
            number(row['executed_steps'], command['checkpoint_step'], tolerance=0)
        durable = field(row, 'durably_completed_steps', 'durably_completed_steps_checkpoint')
        if durable:
            number(durable, command['checkpoint_step'], tolerance=0)
        if row.get('checkpoint_step'):
            number(row['checkpoint_step'], command['checkpoint_step'], tolerance=0)
        filename = Path(field(row, 'log_file', 'mdrun_log', 'gmx_log')).name
        entry = files[filename]
        expected = expected_files[entry['native_path']]
        source = local_path(entry['path'], workspace)
        claimed = field(row, 'log_sha256', 'mdrun_log_sha256', 'gmx_log_sha256')
        if (claimed != expected['sha256'] or sha(source) != claimed
                or source.stat().st_size != expected['size_bytes']):
            raise ValueError('Reported source log does not match the native hash/size')
        arguments = command['command']
        deffnm = arguments[arguments.index('-deffnm') + 1]
        if filename not in {command['log'], deffnm + '.log', deffnm + '.part0001.log'}:
            raise ValueError('Report log belongs to a different repeat')
        log_text = source.read_text()
        logged_rate = re.search(r'Performance:\s+([\d.]+)', log_text)
        if not logged_rate:
            raise ValueError('Referenced log has no actual performance line')
        number(logged_rate.group(1), command['performance_ns_per_day'])
        verified.append({'step_id': step, 'ns_per_day': command['performance_ns_per_day'],
                         'runner_wall_seconds': command['wall_seconds'],
                         'checkpoint_step': command['checkpoint_step'],
                         'log': str(source), 'log_sha256': claimed})
    return verified


def trace_details(messages, operation_id):
    tools, recoveries, violations, delivery_errors = [], [], [], []
    for message in messages:
        for part in message.get('content') or []:
            call = part.get('tool_call') if part.get('type') == 'tool_call' else None
            if not isinstance(call, dict):
                continue
            name = call.get('name', '')
            tools.append(name)
            args = parse_json(call.get('args')) or {}
            if name.startswith(('submit_', 'cancel_', 'run_scientific_workflow')):
                violations.append(name)
            command = args.get('command', '') if isinstance(args, dict) else ''
            if name.startswith('execute_command') and '--recover-operation-id ' in command:
                requested = re.search(r'--recover-operation-id\s+([0-9a-f-]{36})(?:\s|$)', command)
                if requested and requested.group(1) != operation_id:
                    violations.append('Recovery command used another operation')
                if requested:
                    recoveries.append(command)
            if name.startswith('execute_command') and re.search(r'--(idempotency-key|model)\s', command):
                violations.append('Possible submission flags in executed command; inspect retained trace')
            if name.startswith('deliver_scientific_results') and not isinstance(parse_json(call.get('output')), dict):
                delivery_errors.append(call.get('output'))
    return {'tool_names': tools, 'recovery_commands': recoveries, 'submission_flags': violations,
            'delivery_errors': delivery_errors,
            'scope': 'Actual trace retained; lexical flags do not prove arbitrary shell code harmless.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reconciliation', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--traces', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    old, manifest = load(args.reconciliation), load(args.manifest)
    records = []
    for case in manifest['cases']:
        trace = args.traces / 'moonshotai_Kimi-K3' / case['case_id']
        summary = load(trace / 'summary.json')
        folder = local_path(summary['output_directory'], args.workspace)
        receipts = list(folder.rglob('recovery-receipt.json'))
        if len(receipts) != 1:
            raise ValueError('Expected one recovered original operation in this case')
        receipt_folder = receipts[0].parent
        receipt = load(receipts[0])
        if receipt['operation_id'] != case['operation_id'] or receipt['state'] != 'verified':
            raise ValueError('Recovered receipt does not match the requested completed operation')
        request_path = next(row['request_file'] for row in old['receipts']
                            if row['operation_id'] == case['operation_id'] and row.get('request_file'))
        request = load(Path(request_path))
        translated = copy.deepcopy(receipt)
        for ref in translated['verified_artifacts']:
            ref['path'] = str(local_path(ref['path'], args.workspace))
        timings = verify_native_timings(request, translated)
        if not timings['benchmark_complete'] or len(timings['sources']) != 1:
            raise ValueError('Native timing recovery is incomplete or not this single-job suite')
        native = load(Path(timings['sources'][0]['path']))
        steps = request['parameters']['jobs'][0]['steps']
        finite = next(step for step in steps if step['command'] == 'convert-tpr')
        requested = int(finite['args'][finite['args'].index('-nsteps') + 1])
        csv_paths = list(folder.rglob('*.csv'))
        if len(csv_paths) != 1:
            raise ValueError('Expected one saved agent timing CSV')
        mapping = load(receipt_folder / 'native-files.json')['results'][0]
        rows = verify_csv(csv_paths[0], native, mapping, args.workspace, requested)
        record = {'case_id': case['case_id'], 'operation_id': case['operation_id'],
                  'conversation_id': summary['conversation_id'], 'seeded_agent': summary['seeded_agent'],
                  'model': summary['model'], 'reasoning_effort': summary['reasoning_effort'],
                  'native_sources': timings['sources'], 'report_csv': str(csv_paths[0]),
                  'report_csv_sha256': sha(csv_paths[0]), 'verified_rows': rows,
                  'trace': trace_details(load(trace / 'messages.json'), case['operation_id']),
                  'scope': 'Timing CSV and source logs checked; scientific prose/manual review remains separate.'}
        records.append(record)
    save(args.output, {'schema': 'fs2.gromacs-agent-recovery-verification/v1', 'observed_at': now(),
                       'candidate_image': manifest['candidate_image'], 'candidate_source': manifest['candidate_source'],
                       'cases': records, 'new_gpu_submissions_authorized': 0})
    print(json.dumps({'output': str(args.output), 'verified_cases': len(records),
                      'verified_timing_rows': sum(len(row['verified_rows']) for row in records),
                      'delivery_errors': sum(len(row['trace']['delivery_errors']) for row in records),
                      'submission_flags': sum(len(row['trace']['submission_flags']) for row in records)}))


if __name__ == '__main__':
    main()
