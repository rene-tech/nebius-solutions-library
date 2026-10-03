"""Run exact seeded-agent cases only after the existing QA owners have drained.

This is an acceptance supervisor, not a model wrapper or admission-policy owner.
It invokes the existing actual-chat harness unchanged, admits at most two chats
per batch, then waits for native operations to drain before starting another.
It never resubmits, cancels, changes keys/policy or converts a chat into a pass.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import httpx2

sys.path.insert(0, str(Path(__file__).parents[1] / 'gromacs-concurrency-20261003'))
from verify_concurrency import QA_KEY_ID, active_operations, append, load, now, save

IMAGE = 'cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:6d8b2038097b180d5edd997d7346a1b56c879a5f00b4890fcc08b81c2a96b2da'
COMPLETED = {'mpinat-benchsfi', 'mpinat-benchsnc', 'mpinat-benchsni',
             'mpinat-benchstc', 'mpinat-benchsti'}


def gate_state(policy, progress):
    restored = (policy.get('id') == QA_KEY_ID and policy.get('tenant_id') == 'system'
                and policy.get('principal_id') == 'qa' and policy.get('max_concurrency') == 2)
    return {'qa_baseline_restored': restored, 'mpi_terminal_verified': progress.get('all_verified') is True}


def selected_cases(manifest, phase):
    if manifest.get('candidate_image') != IMAGE:
        raise ValueError('Manifest is not the exact r4 client candidate')
    cases = manifest['cases']
    names = [case['case_id'] for case in cases]
    if len(set(names)) != len(names) or set(names) & COMPLETED:
        raise ValueError('Duplicate or already native-complete benchmark selected')
    if phase == 'alanine':
        if names != ['assumed-hosted-gromacs']:
            raise ValueError('Run the prepared hosted alanine example first')
    elif len(cases) != 19 or any(not name.startswith('mpinat-') for name in names):
        raise ValueError('Expected exactly the prepared 19 remaining MPINAT cases')
    return cases


async def wait_empty(http, output, poll):
    previous = None
    while True:
        active = await active_operations(http)
        rows = sorted(({'id': row['id'], 'model_id': row.get('model_id'), 'status': row['status']}
                       for row in active), key=lambda row: row['id'])
        append(output / 'native-admission-observations.jsonl', {'at': now(), 'active': rows})
        if rows != previous:
            print(json.dumps({'phase': 'native_drain', 'active': rows}), flush=True)
            previous = rows
        if not rows:
            return
        await asyncio.sleep(poll)


async def wait_operator_pause(path, output, poll):
    """Only a batch boundary: never interrupt accepted work or modify policy."""
    if path is None or not path.exists():
        return
    print(json.dumps({'phase': 'operator_pause', 'pause_file': str(path)}), flush=True)
    append(output / 'operator-pauses.jsonl', {'at': now(), 'state': 'paused', 'path': str(path)})
    while path.exists():
        await asyncio.sleep(poll)
    append(output / 'operator-pauses.jsonl', {'at': now(), 'state': 'resumed', 'path': str(path)})
    print(json.dumps({'phase': 'operator_resumed'}), flush=True)


def refresh_browser_session(harness, client_root, output, base_url):
    """One normal isolated-account login per pair; inference keys never change."""
    with harness.httpx.Client(base_url=base_url,
                              headers={'Origin': base_url, 'User-Agent': harness.UA}, timeout=45) as client:
        harness.authenticate(client, load(client_root / 'login.json'))
        harness.save(client_root / 'session.json', {'token': client.headers['Authorization'][7:]})
    append(output / 'browser-session-refreshes.jsonl',
           {'at': now(), 'reason': 'batch-boundary QA login', 'inference_key_changed': False})


async def execute(args):
    if args.output.exists():
        raise ValueError('Refusing to overwrite prior agent evidence or replay cases')
    manifest = load(args.manifest)
    cases = selected_cases(manifest, args.phase)
    image = subprocess.check_output(['docker', 'inspect', '--format', '{{.Config.Image}}', args.container], text=True).strip()
    if image != IMAGE:
        raise ValueError('QA container is not the exact accepted r4 image')
    original = load(args.client / 'agent.json')
    if (original.get('model') != 'moonshotai/Kimi-K3'
            or original.get('model_parameters', {}).get('reasoning_effort') != 'high'):
        raise ValueError('Preserve the actual seeded Kimi-K3/high configuration')
    values = dict(line.split('=', 1) for line in (args.client / 'runtime.env').read_text().splitlines() if '=' in line)
    key = values['SCIENTIFIC_MODELS_API_KEY']
    if not key.startswith('fs2_pat_' + QA_KEY_ID.replace('-', '')[:12]):
        raise ValueError('Only the existing system/qa inference identity is permitted')
    args.output.mkdir(parents=True, mode=0o700)
    save(args.output / 'plan.json', {'started_at': now(), 'phase': args.phase, 'client_image': image,
        'source_manifest': str(args.manifest), 'case_ids': [case['case_id'] for case in cases],
        'maximum_chats': args.workers, 'maximum_operations': 2, 'policy_written': False,
        'tenant': 'system', 'principal': 'qa', 'new_gpu_admissions': 'parent-authorized after recorded gates'})
    previous = None
    while True:
        state = gate_state(load(args.parent / 'qa-policy-restored.json', {}),
                           load(args.mpi / 'mpi-lane-progress.json', {}))
        if state != previous:
            print(json.dumps({'phase': 'parent_gate', **state}), flush=True)
            save(args.output / 'parent-gate.json', {'at': now(), **state})
            previous = state
        if all(state.values()):
            break
        await asyncio.sleep(args.poll)
    sys.path.insert(0, str(args.client_source / 'templates/hcls-librechat/scripts/qualification'))
    harness = importlib.import_module('replay_agent_instructions')
    verifier = importlib.import_module('verify_agent_delivery')
    replay = SimpleNamespace(base_url=args.base_url, output=args.output,
        session=args.client / 'session.json', login=args.client / 'login.json',
        reasoning_effort=None, use_seeded_agent=True, deadline=args.deadline,
        instruction_text=(args.client / 'instructions.md').read_text().strip(), cohort_id=uuid.uuid4().hex)
    results = []
    async with httpx2.AsyncClient(base_url=args.origin, headers={'Authorization': 'Bearer ' + key},
                                 timeout=90, trust_env=False) as http:
        for start in range(0, len(cases), args.workers):
            await wait_operator_pause(args.pause_file, args.output, args.poll)
            await wait_empty(http, args.output, args.poll)
            # Recheck after draining in case the operator reserved the lane
            # while a previously accepted native operation was still running.
            await wait_operator_pause(args.pause_file, args.output, args.poll)
            await wait_empty(http, args.output, args.poll)
            refresh_browser_session(harness, args.client, args.output, args.base_url)
            batch = cases[start:start + args.workers]
            # Preserve the real seeded instructions and prompt; no synthetic
            # agent completion, GPU retry or corrective follow-up is injected.
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                token = load(replay.session)['token']
                futures = [pool.submit(harness.run_case, replay, token, original, case, original['model'])
                           for case in batch]
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except Exception as error:
                        outcomes.append({'harness_error_type': type(error).__name__})
            results.extend(outcomes)
            save(args.output / 'summary.json', results)
            # A returned chat can leave a durable GPU operation running. The
            # next pair cannot borrow those slots until the original work ends.
            await wait_empty(http, args.output, args.poll)
            failures = []
            for case, result in zip(batch, outcomes):
                path = args.output / 'moonshotai_Kimi-K3' / case['case_id'] / 'messages.json'
                calls = [part['tool_call'] for msg in load(path, []) for part in msg.get('content', [])
                         if part.get('type') == 'tool_call']
                tool_errors = verifier.tool_failures(calls)
                delivered = any(verifier.read_tool_output(call.get('output')).get('schema')
                                == 'scientific-verified-delivery/v1' for call in calls)
                if (result.get('harness_error_type') or result.get('errors') or result.get('unfinished')
                        or result.get('watchdog_aborted') or result.get('empty_answer')
                        or tool_errors or not delivered):
                    failures.append({'case_id': case['case_id'], 'tool_errors': tool_errors,
                                     'delivered_verified_report': delivered,
                                     'harness_error_type': result.get('harness_error_type')})
            save(args.output / f'batch-{start // args.workers + 1:02}.json',
                 {'at': now(), 'cases': [case['case_id'] for case in batch], 'failures': failures,
                  'native_operations_drained': True, 'semantic_validation': 'required separately'})
            if failures:
                print(json.dumps({'phase': 'paused_for_review', 'failures': failures}), flush=True)
                return 2
    print(json.dumps({'phase': 'agent_cases_terminal', 'cases': len(results),
                      'semantic_validation': 'required separately', 'policy_written': False}), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'client', 'client-source', 'parent', 'mpi', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--phase', choices=['alanine', 'benchmarks'], required=True)
    parser.add_argument('--container', required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:13207')
    parser.add_argument('--origin', default='https://89.169.99.188')
    parser.add_argument('--workers', type=int, choices=[1, 2], default=2)
    parser.add_argument('--deadline', type=int, default=3600)
    parser.add_argument('--poll', type=int, default=45)
    parser.add_argument('--pause-file', type=Path,
                        help='Operator-owned presence pauses only new batches; no cancellation or policy writes')
    args = parser.parse_args()
    if args.phase == 'alanine' and args.workers != 1:
        parser.error('Qualify the hosted starter alone before the benchmark pairs')
    if args.base_url != 'http://127.0.0.1:13207' or args.origin != 'https://89.169.99.188' or args.poll < 45:
        parser.error('Use the fixed isolated candidate, public API and bounded observation interval')
    os.umask(0o077)
    raise SystemExit(asyncio.run(execute(args)))


if __name__ == '__main__':
    main()
