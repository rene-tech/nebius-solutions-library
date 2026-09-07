"""Low-rate public Qwen HTTP/MCP traffic during a separate customer trial.

One request at a time; no inference retries. Status polling is recorded and is
not counted as new inference. Latencies include gateway/network/operation time,
not isolated GPU decode or TTFT. Stop only through the explicit stop file.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from fs2_serve.live_acceptance import MCP_PROTOCOL_VERSION, _mcp_result

TERMINAL = {'succeeded', 'failed', 'cancelled', 'expired'}
CASES = [
    ('arithmetic', 'What is 17 plus 25? Reply with only the integer.', '42'),
    ('translation', 'Translate the English noun cat into German. Reply with only Katze.', 'Katze'),
    ('sorting', 'Sort the letters T G A C alphabetically. Reply with only ACGT, no spaces.', 'ACGT'),
]


def utc():
    return datetime.now(timezone.utc).isoformat()


def hash_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def phase(path):
    return json.loads(path.read_text()).get('phase', 'baseline') if path.exists() else 'baseline'


async def sample(args, credentials, ordinal):
    surface = 'http' if ordinal % 2 else 'mcp'
    name, prompt, expected = CASES[(ordinal - 1) % len(CASES)]
    payload = {'model': 'qwen3-8b', 'messages': [{'role': 'user', 'content': prompt}],
               'max_tokens': 32, 'temperature': 0, 'stream': False,
               'chat_template_kwargs': {'enable_thinking': False}}
    record = {'ordinal': ordinal, 'surface': surface, 'case': name, 'request': payload,
              'request_sha256': hash_json(payload), 'expected_output': expected,
              'phase': phase(args.phase_file), 'started_at': utc(),
              'model_id': 'qwen3-8b', 'submission_attempts': 1, 'status_polls': 0}
    started = time.monotonic()
    key = 'trial-experience-' + uuid4().hex
    try:
        if surface == 'http':
            async with httpx.AsyncClient(base_url=args.origin, timeout=30, trust_env=False,
                                         headers={'authorization': 'Bearer ' + credentials['inference_access_token']}) as client:
                response = await client.post('/v1/chat/completions', json=payload,
                                            headers={'Idempotency-Key': key, 'x-fs2-wait-seconds': '0'})
                record['submission_http_status'] = response.status_code
                response.raise_for_status()
                current = response.json()
                operation_id = response.headers.get('x-fs2-operation-id') or current['id']
                record['operation_id'] = operation_id
                while current.get('status') not in TERMINAL:
                    if time.monotonic() - started > 120:
                        raise TimeoutError('operation deadline')
                    await asyncio.sleep(0.25)
                    response = await client.get('/v1/operations/' + operation_id)
                    record['status_polls'] += 1
                    response.raise_for_status()
                    current = response.json()
                record['operation'] = current
                if current['status'] != 'succeeded':
                    raise ValueError('operation did not succeed')
                response = await client.get('/v1/operations/' + operation_id + '/result')
                response.raise_for_status()
                result = response.json()
        else:
            async with httpx2.AsyncClient(timeout=30, trust_env=False, follow_redirects=False,
                headers={'authorization': 'Bearer ' + credentials['mcp_inference_token'], 'origin': args.origin}) as http:
                async with Client(streamable_http_client(args.origin + '/mcp', http_client=http), mode=MCP_PROTOCOL_VERSION) as client:
                    current = _mcp_result(await client.call_tool('invoke_model', {
                        'model_id': 'qwen3-8b', 'protocol': 'openai-chat', 'payload': payload,
                        'idempotency_key': key, 'wait_seconds': 0}))
                    operation_id = current['id']
                    record.update(operation_id=operation_id, mcp_protocol=client.protocol_version)
                    while current.get('status') not in TERMINAL:
                        if time.monotonic() - started > 120:
                            raise TimeoutError('operation deadline')
                        await asyncio.sleep(0.25)
                        record['status_polls'] += 1
                        current = _mcp_result(await client.call_tool('get_operation', {'operation_id': operation_id}))
                    record['operation'] = current
                    if current['status'] != 'succeeded':
                        raise ValueError('operation did not succeed')
                    result = _mcp_result(await client.call_tool('get_operation_result', {'operation_id': operation_id}))['result']
        content = result['choices'][0]['message']['content'].strip()
        record.update(actual_output=content, response_sha256=hash_json(result), usage=result.get('usage'),
                      correctness_passed=content == expected, status='passed' if content == expected else 'failed',
                      failure=None if content == expected else 'semantic_output_mismatch')
    except Exception as error:
        record.update(status='failed', correctness_passed=False, failure=type(error).__name__)
        if isinstance(error, httpx.HTTPStatusError):
            record['failure_http_status'] = error.response.status_code
        # Deliberately omit exception strings, request headers and transport dumps.
    record.update(completed_at=utc(), client_seconds=round(time.monotonic() - started, 6))
    return record


async def discovery(args, credentials):
    row = {'started_at': utc(), 'phase': phase(args.phase_file), 'checks': []}
    async with httpx.AsyncClient(base_url=args.origin, timeout=30, trust_env=False) as client:
        for path, credential in (('/readyz', 'inference_access_token'), ('/v1/models', 'inference_access_token'),
                                 ('/v1/scientific-models', 'scientific_access_token')):
            start = time.monotonic()
            check = {'path': path, 'credential_role': credential}
            try:
                response = await client.get(path, headers={'authorization': 'Bearer ' + credentials[credential]})
                check['http_status'] = response.status_code
                check['passed'] = response.status_code == 200
                if path != '/readyz' and response.status_code == 200:
                    check['model_ids'] = sorted(item.get('model_id', item.get('id')) for item in response.json()['data'])
            except Exception as error:
                check.update(passed=False, failure=type(error).__name__)
            check['client_seconds'] = round(time.monotonic() - start, 6)
            row['checks'].append(check)
    row['completed_at'] = utc()
    return row


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--origin', default='https://89.169.99.188')
    parser.add_argument('--interval', type=float, default=25)
    parser.add_argument('--phase-file', type=Path)
    parser.add_argument('--resume-after', type=Path,
                        help='Retain old records, continue the next ordinal and original cadence; never replay a request.')
    args = parser.parse_args()
    if not 20 <= args.interval <= 30:
        parser.error('bounded trial requires interval 20–30 seconds')
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    args.phase_file = args.phase_file or args.output / 'phase.json'
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpx2').setLevel(logging.WARNING)
    credentials = json.loads(args.credentials.read_text())['credentials']
    ordinal = 0
    if args.resume_after:
        prior = json.loads(args.resume_after.read_text().splitlines()[-1])
        ordinal = prior['ordinal']
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(prior['started_at'])).total_seconds()
        await asyncio.sleep(max(0, args.interval - elapsed))
    (args.output / 'harness.json').write_text(json.dumps({
        'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'started_at': utc(), 'interval_seconds': args.interval, 'resumes_after_ordinal': ordinal,
        'resume_receipt_sha256': hashlib.sha256(args.resume_after.read_bytes()).hexdigest() if args.resume_after else None,
        'public_health_path': '/readyz', 'tls_verified': True,
        'clock': 'Non-streaming public request through complete validated response, including transport/session setup.',
        'client_placement': 'Existing orchestration host outside the cluster; not an in-cluster GPU-only benchmark.',
    }, indent=2) + '\n')
    first = True
    with (args.output / 'interactive.jsonl').open('x') as results, (args.output / 'discovery.jsonl').open('x') as checks:
        while not (args.output / 'stop.json').exists():
            ordinal += 1
            start = time.monotonic()
            record = await sample(args, credentials, ordinal)
            encoded = json.dumps(record)
            if any(value in encoded for value in credentials.values() if isinstance(value, str)):
                raise ValueError('credential in receipt')
            results.write(encoded + '\n')
            results.flush()
            print(json.dumps({key: record[key] for key in ('ordinal', 'surface', 'phase', 'status', 'client_seconds')}), flush=True)
            if first or ordinal % 4 == 0:
                checks.write(json.dumps(await discovery(args, credentials)) + '\n')
                checks.flush()
                first = False
            while time.monotonic() - start < args.interval and not (args.output / 'stop.json').exists():
                await asyncio.sleep(0.5)
    print(json.dumps({'status': 'stopped', 'samples': ordinal, 'stopped_at': utc()}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
