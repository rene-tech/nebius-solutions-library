"""Read-only binding of each actual agent result to its selected MPINAT inputs.

This supplements report/download verification. It never admits, cancels or
replays work, and a completed report about another case cannot pass.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import httpx

from run_agent_cases import R5_IMAGE, selected_cases
from verify_agent_study import sha, verify_study, verify_timing_report, workspace_path

sys.path.insert(0, str(Path(__file__).parents[2] / 'models/molecular-dynamics/gromacs/runtime'))
from fs2_gromacs.contracts import normalize, canonical as runtime_canonical


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode()


def load(path):
    return json.loads(path.read_text())


def is_terminal(state):
    return state in {'completed', 'failed', 'cancelled', 'needs_attention'}


def save_new(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    path.chmod(0o600)


def frozen_study(container, identifier):
    identifier = str(uuid.UUID(identifier))
    code = """import json,pathlib,sys
paths=list(pathlib.Path('/workspace/.scientific-studies').glob('*/'+sys.argv[1]+'/receipt.json'))
assert len(paths)==1
p=paths[0]; print(json.dumps({'record':json.loads(p.read_text()),'plan':json.loads(p.with_name('plan.json').read_text())}))
"""
    return json.loads(subprocess.check_output(['docker', 'exec', container, 'python3', '-c', code, identifier]))


def expected_case(case_id, staging, fetch):
    name = case_id.removeprefix('mpinat-')
    parameter = next(row for row in staging['parameters'] if row['case_id'] == case_id)
    prefix = '/workspace/inputs/mpinat/' + name + '/'
    rows = {}
    for filename in ('input.tar.gz', 'provenance.json'):
        row = next(row for row in staging['files'] if row['target'].endswith('/inputs/mpinat/' + name + '/' + filename))
        rows[filename] = {'path': prefix + filename, 'sha256': row['sha256'], 'size_bytes': row['size_bytes']}
    parameters_bytes = fetch(prefix + 'parameters-candidate.json')
    if sha(parameters_bytes) != parameter['sha256']:
        raise ValueError('Staged parameter file differs from the pre-admission hash')
    parameters = json.loads(parameters_bytes)
    provenance = json.loads(fetch(rows['provenance.json']['path'], rows['provenance.json']))
    if provenance['id'] != name or provenance['bundle_sha256'] != rows['input.tar.gz']['sha256']:
        raise ValueError('Original TPR provenance differs from the selected bundle')
    return {'case_id': case_id, 'source': rows['input.tar.gz'], 'provenance': provenance,
            'parameters': parameters, 'parameter_path': prefix + 'parameters-candidate.json',
            'parameter_file_sha256': parameter['sha256'], 'parameter_size_bytes': len(parameters_bytes),
            'parameters_sha256': sha(canonical(parameters)),
            'idempotency_key': 'fs2-mpinat-candidate-b948ecca-' + name + '-20261003'}


def validate_plan(expected, frozen, study):
    plan, record = frozen['plan'], frozen['record']
    identity = sha(canonical({'plan': plan, 'output': record['output_directory'], 'owner': record['owner']}))
    if (record['id'] != study['id'] or record['identity'] != identity
            or record['state'] != 'completed' or record['output_directory'] != study['output_directory']):
        raise ValueError('Immutable admitted study identity does not match its published result')
    model_steps = [step for step in plan['steps'] if step['kind'] in {'batch', 'native'}]
    if len(model_steps) != 1:
        raise ValueError('One selected benchmark must map to one native model step')
    step = model_steps[0]
    required = {'kind': 'batch', 'model': 'gromacs', 'tool': 'submit_gromacs_workflow',
                'operation': 'run-workflow', 'source': expected['source']['path'],
                'parameters': expected['parameter_path'],
                'idempotency_key': expected['idempotency_key']}
    # The installed public schema permits omission: its helper binds encoding
    # from the contract plus actual source magic. validate_native still requires
    # the resulting immutable request descriptor to be gzip for these fixtures.
    if (any(step.get(key) != value for key, value in required.items())
            or ('compression' in step and step['compression'] != 'gzip')):
        raise ValueError('Study changed the requested source, model, parameters or native request identity')
    for path, digest, size in ((expected['source']['path'], expected['source']['sha256'], expected['source']['size_bytes']),
                              (expected['parameter_path'], expected['parameter_file_sha256'], expected['parameter_size_bytes'])):
        value = record['inputs'].get(path, {})
        if value.get('sha256') != digest or value.get('size_bytes') != size:
            raise ValueError('Frozen study inputs do not match the original staged bytes')
    operation = record['steps'][step['id']].get('operation_id')
    if not operation or study['steps'][step['id']].get('operation_id') != operation:
        raise ValueError('Native operation is not bound to the admitted model step')
    return {'model_step': step['id'], 'operation_id': operation, 'plan_identity': identity}


def validate_native(expected, binding, report, receipt, mapping, fetch, request_receipt=None):
    request_receipt = request_receipt or receipt
    identity = request_receipt['identity']
    if report['operation_id'] != binding['operation_id'] or receipt.get('operation_id') != binding['operation_id']:
        raise ValueError('Timing report belongs to another operation')
    if request_receipt.get('operation_id') != binding['operation_id']:
        raise ValueError('Original submission receipt belongs to another operation')
    for key, value in {'model_id': 'gromacs', 'source_sha256': expected['source']['sha256'],
                       'parameters_sha256': expected['parameters_sha256'],
                       'idempotency_key': expected['idempotency_key']}.items():
        if identity.get(key) != value:
            raise ValueError('Native receipt identity differs from selected case: ' + key)
    if request_receipt.get('request_descriptor', {}).get('compression') != 'gzip':
        raise ValueError('Native upload did not use the requested gzip encoding')
    expected_repeats = {(job['id'], step['id']) for job in expected['parameters']['jobs']
                        for step in job['steps'] if step['command'] == 'mdrun'}
    rows = report['timing_rows']
    observed_repeats = {(row.get('job_id'), row.get('step_id')) for row in rows}
    segments = {(row.get('job_id'), row.get('step_id'), row.get('segment')) for row in rows}
    # One original repeat may contain several native checkpoint segments. The
    # report verifier checks each segment's successful command, log and hashes;
    # selected-case identity must match logical repeats, not a fixed row count.
    if (len(expected_repeats) != 3 or observed_repeats != expected_repeats
            or len(segments) != len(rows)
            or any(row.get('requested_steps') != 10000 for row in rows)):
        raise ValueError('Expected exactly three original 10000-step timing repeats')
    native_results = []
    for source in report['sources']:
        reference = next(item for item in receipt['verified_artifacts'] if item['path'] == source['path'])
        native = json.loads(fetch(source['path'], reference))
        recipe = sha(runtime_canonical({'request': normalize(expected['parameters']),
                                        'job': native['job_id'], 'image': native['engine_id']}))
        if native.get('recipe_sha256') != recipe:
            raise ValueError('Native runtime recipe differs from the exact requested parameters')
        original = next((f for f in native['files'] if f['path'] == 'original.tpr'), None)
        if (original is None or original['sha256'] != expected['provenance']['tpr_sha256']
                or original['size_bytes'] != expected['provenance']['tpr_bytes']):
            raise ValueError('Native original TPR differs from upstream selected-case bytes')
        files = next(row['files'] for row in mapping['results'] if row['source_result_sha256'] == source['sha256'])
        bound = next(row for row in files if row['native_path'] == 'original.tpr')
        fetch(bound['path'], original)
        native_results.append({'source_sha256': source['sha256'], 'recipe_sha256': recipe,
                               'original_tpr_sha256': original['sha256']})
    return {**binding, 'case_id': expected['case_id'], 'input_sha256': expected['source']['sha256'],
            'parameter_file_sha256': expected['parameter_file_sha256'],
            'parameters_sha256': expected['parameters_sha256'], 'native_results': native_results,
            'repeat_count': 3, 'requested_steps_per_repeat': 10000, 'selected_case_verified': True}


def validate_recovery_identity(submission, recovery):
    """Join two real receipts; never copy request identity into a recovery file."""
    operation = submission.get('operation_id')
    if (not operation or recovery.get('operation_id') != operation
            or recovery.get('identity', {}).get('operation_id') != operation
            or recovery.get('state') != 'verified'):
        raise ValueError('Recovery is not verified for the original submitted operation')
    for key in ('endpoint', 'caller_fingerprint'):
        expected = submission.get('identity', {}).get(key)
        if not expected or recovery.get('identity', {}).get(key) != expected:
            raise ValueError('Recovery changed the original caller or endpoint')


def verify_case(client, container, case_id, staging, study):
    def fetch(path, expected=None):
        response = client.get('/api/scientific-demos/workspace/file', params={'path': workspace_path(path)})
        response.raise_for_status()
        value = response.content
        if expected and (len(value) != expected['size_bytes'] or sha(value) != expected['sha256']):
            raise ValueError('Case-binding download differs from its recorded bytes')
        return value
    expected = expected_case(case_id, staging, fetch)
    frozen = frozen_study(container, study['id'])
    binding = validate_plan(expected, frozen, study)
    verification = verify_study(client, study)
    report_ref = next(row for row in study['artifacts'] if Path(row['path']).name == 'native-timing-report.json')
    report = json.loads(fetch(report_ref['path'], report_ref))
    receipt = json.loads(fetch(report['receipt_file']))
    mapping = json.loads(fetch(str(Path(report['receipt_file']).parent / 'native-files.json')))
    result = validate_native(expected, binding, report, receipt, mapping, fetch)
    return {**result, 'study_id': study['id'], 'report_verification': verification,
            'client_image': R5_IMAGE, 'native_replayed': False, 'agent_path': 'saved-study'}


def direct_delivery(calls, verifier):
    """Require an actual terminal native-MD delivery, not an admission or text claim."""
    deliveries = [call for call in calls if verifier.read_tool_output(call.get('output')).get('schema')
                  == 'scientific-verified-delivery/v1']
    if len(deliveries) != 1:
        raise ValueError('Expected one actual direct native-MD/report delivery')
    call = deliveries[0]
    output = verifier.read_tool_output(call.get('output'))
    results = verifier.read_tool_output(call.get('args'))['results']
    native = [item for item in results if item.get('kind') == 'native-md']
    files = {Path(item['path']).name: item['path'] for item in results if item.get('kind') == 'file'}
    required = {'native-timing-report.json', 'native-timings.csv', 'native-timing-report.md'}
    if output.get('status') != 'completed' or len(native) != 1 or not required <= files.keys():
        raise ValueError('Direct path did not deliver the original native result and all requested timing reports')
    return {'receipt_directory': native[0]['path'], 'files': files,
            'report_markdown': output['report_markdown']}


def verify_direct_case(client, case_id, staging, messages, verifier, harness):
    calls = [p['tool_call'] for m in messages for p in m.get('content', []) if p.get('type') == 'tool_call']
    summary = harness.summarize(messages)
    tool_errors = verifier.tool_failures(calls)
    if summary['errors'] or summary['empty_answer'] or summary['unfinished']:
        raise ValueError('Direct scientific path contains an unresolved tool/chat failure')
    delivered = direct_delivery(calls, verifier)
    if delivered['report_markdown'] not in summary['visible_text']:
        raise ValueError('Verified scientific delivery was altered or omitted from the final reply')
    downloads = []

    def fetch(path, expected=None):
        response = client.get('/api/scientific-demos/workspace/file', params={'path': workspace_path(path)})
        response.raise_for_status()
        value = response.content
        if expected and (len(value) != expected['size_bytes'] or sha(value) != expected['sha256']):
            raise ValueError('Direct-case download differs from its recorded bytes')
        downloads.append({'path': path, 'size_bytes': len(value), 'sha256': sha(value)})
        return value

    expected = expected_case(case_id, staging, fetch)
    report = json.loads(fetch(delivered['files']['native-timing-report.json']))
    receipt_path = report['receipt_file']
    receipt_file = Path(receipt_path)
    if (receipt_file.parent != Path(delivered['receipt_directory'])
            or receipt_file.name not in {'receipt.json', 'recovery-receipt.json'}):
        raise ValueError('Delivered report is not bound to the delivered original native receipt')
    receipt = json.loads(fetch(receipt_path))
    request_receipt = receipt
    recovery_binding = None
    if receipt_file.name == 'recovery-receipt.json':
        # The direct benchmark fixture preserves its original submission in
        # <case>/receipt; the separate recovery is never allowed to replace it.
        original_path = str(receipt_file.parent.parent / 'receipt' / 'receipt.json')
        original_bytes = fetch(original_path)
        request_receipt = json.loads(original_bytes)
        validate_recovery_identity(request_receipt, receipt)
        recovery_binding = {'submission_receipt_file': original_path,
                            'submission_receipt_sha256': sha(original_bytes),
                            'recovery_receipt_file': receipt_path,
                            'operation_id': receipt['operation_id'], 'same_operation_verified': True}
    mapping = json.loads(fetch(str(Path(delivered['receipt_directory']) / 'native-files.json')))
    rows = fetch(delivered['files']['native-timings.csv']).decode()
    markdown = fetch(delivered['files']['native-timing-report.md']).decode()
    verification = verify_timing_report(report, rows, markdown, fetch)
    binding = {'model_step': None, 'operation_id': receipt['operation_id'], 'plan_identity': None}
    result = validate_native(expected, binding, report, receipt, mapping, fetch, request_receipt)
    links = []
    for url in sorted(set(re.findall(r'\]\((/demos\?[^)]+)\)', summary['visible_text']))):
        directory, file = verifier.workspace_selection(url)
        route = '/api/scientific-demos/workspace' + ('/file' if file else '')
        response = client.get(route, params={'path': file or directory})
        response.raise_for_status()
        links.append({'path': file or directory, 'http_status': response.status_code})
    if not links:
        raise ValueError('Direct completed result omitted usable customer links')
    return {**result, 'study_id': None, 'report_verification': {**verification,
            'authenticated_downloads': downloads, 'delivered_links': links}, 'client_image': R5_IMAGE,
            'native_replayed': False, 'agent_path': 'direct-batch-mcp', 'direct_delivery_verified': True,
            'recovery_binding': recovery_binding, 'observed_tool_errors': tool_errors,
            'customer_path_clean': not tool_errors,
            'delivery_outcome': 'verified_after_tool_errors' if tool_errors else 'verified'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'staging', 'replay', 'client', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--container', required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:13208')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--resume', action='store_true', help='Continue read-only verification without overwriting prior proofs')
    args = parser.parse_args()
    if args.base_url != 'http://127.0.0.1:13208':
        raise ValueError('Use only the isolated exact r5 candidate')
    image = subprocess.check_output(['docker', 'inspect', '--format', '{{.Config.Image}}', args.container], text=True).strip()
    if image != R5_IMAGE:
        raise ValueError('Candidate image differs')
    cases = selected_cases(load(args.manifest), 'benchmarks')
    staging = load(args.staging)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=args.resume)
    sys.path.insert(0, '/home/tux/worktrees/scientific-ai-workbench-lifecycle-20261002/templates/hcls-librechat/scripts/qualification')
    import replay_agent_instructions as harness
    import verify_agent_delivery as verifier
    from replay_agent_instructions import UA, authenticate
    from verify_agent_delivery import read_tool_output
    from run_agent_cases import admitted_studies
    from types import SimpleNamespace
    verified = set()
    if args.resume:
        for case in cases:
            proof = args.output / (case['case_id'] + '.json')
            if proof.exists():
                record = load(proof)
                if (record.get('case_id') != case['case_id'] or record.get('client_image') != R5_IMAGE
                        or record.get('selected_case_verified') is not True):
                    raise ValueError('Existing case proof differs from the exact cohort')
                verified.add(case['case_id'])
    with httpx.Client(base_url=args.base_url, headers={'Origin': args.base_url, 'User-Agent': UA}, timeout=90) as client:
        client.headers['Authorization'] = 'Bearer ' + load(args.client / 'session.json')['token']
        while True:
            for case in cases:
                case_id = case['case_id']
                messages = args.replay / 'moonshotai_Kimi-K3' / case_id / 'messages.json'
                if case_id in verified or not messages.exists():
                    continue
                calls = [p['tool_call'] for m in load(messages) for p in m.get('content', []) if p.get('type') == 'tool_call']
                ids = admitted_studies(calls, SimpleNamespace(read_tool_output=read_tool_output))
                if not ids:
                    result = verify_direct_case(client, case_id, staging, load(messages), verifier, harness)
                    save_new(args.output / (case_id + '.json'), result)
                    verified.add(case_id)
                    print(json.dumps({'case_id': case_id, 'operation_id': result['operation_id'],
                        'selected_case_verified': True, 'agent_path': 'direct-batch-mcp',
                        'total_verified': len(verified)}), flush=True)
                    continue
                if len(ids) != 1:
                    raise ValueError('Actual chat did not bind exactly one durable study')
                response = client.get('/api/scientific-demos/studies/' + ids[0])
                if response.status_code == 401:
                    authenticate(client, load(args.client / 'login.json'))
                    response = client.get('/api/scientific-demos/studies/' + ids[0])
                response.raise_for_status()
                study = response.json()
                if not is_terminal(study['state']):
                    continue
                try:
                    result = verify_case(client, args.container, case_id, staging, study)
                except Exception as error:
                    save_new(args.output / (case_id + '-failure.json'),
                             {'case_id': case_id, 'study_id': ids[0], 'error_type': type(error).__name__,
                              'error': str(error)[:800], 'selected_case_verified': False})
                    raise
                save_new(args.output / (case_id + '.json'), result)
                verified.add(case_id)
                print(json.dumps({'case_id': case_id, 'operation_id': result['operation_id'],
                                  'selected_case_verified': True, 'total_verified': len(verified)}), flush=True)
            if len(verified) == len(cases) or not args.watch:
                break
            time.sleep(45)


if __name__ == '__main__':
    main()
