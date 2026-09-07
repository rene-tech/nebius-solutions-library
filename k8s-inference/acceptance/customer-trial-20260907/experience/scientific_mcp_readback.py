"""Read existing scientific operations through their published MCP tools only."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import time

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from fs2_serve.live_acceptance import MCP_PROTOCOL_VERSION, _mcp_result
from fs2_serve.scientific_run_result import ScientificRunResult


def now():
    return datetime.now(timezone.utc).isoformat()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--operation', action='append', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        raise ValueError('Preserve existing evidence; choose a new output file')
    logging.getLogger('httpx2').setLevel(logging.WARNING)
    token = json.loads(args.credentials.read_text())['credentials']['scientific_access_token']
    evidence = {'scope': 'Academic-token MCP discovery/status/result reads of existing completed HTTP submissions. '
                         'No scientific MCP submission, artifact grant or GPU workload is created.',
                'started_at': now(), 'protocol': MCP_PROTOCOL_VERSION, 'calls': [], 'status': 'running'}
    try:
        async with httpx2.AsyncClient(timeout=30, trust_env=False, follow_redirects=False,
            headers={'authorization': 'Bearer ' + token, 'origin': 'https://89.169.99.188'}) as http:
            async with Client(streamable_http_client('https://89.169.99.188/mcp', http_client=http), mode=MCP_PROTOCOL_VERSION) as client:
                tools = await client.list_tools()
                selected = {tool.name: tool.model_dump(mode='json', by_alias=True) for tool in tools.tools
                            if tool.name in {'list_scientific_models', 'get_scientific_status', 'get_scientific_result'}}
                if set(selected) != {'list_scientific_models', 'get_scientific_status', 'get_scientific_result'}:
                    raise ValueError('required read-only scientific tools missing')
                evidence['published_tool_schemas'] = selected
                for name in ('get_scientific_status', 'get_scientific_result'):
                    schema = selected[name].get('inputSchema', selected[name].get('input_schema', {}))
                    if schema.get('required') != ['operation_id']:
                        raise ValueError('published tool arguments differ from the operation_id contract')
                started = time.monotonic()
                models = _mcp_result(await client.call_tool('list_scientific_models', {}))
                evidence['calls'].append({'tool': 'list_scientific_models', 'completed_at': now(),
                    'client_seconds': time.monotonic() - started,
                    'model_ids': sorted(row['model_id'] for row in models['data']), 'status': 'passed'})
                for operation_id in args.operation:
                    for name in ('get_scientific_status', 'get_scientific_result'):
                        call = {'tool': name, 'operation_id': operation_id, 'started_at': now()}
                        evidence['calls'].append(call)
                        started = time.monotonic()
                        value = _mcp_result(await client.call_tool(name, {'operation_id': operation_id}))
                        call['response_sha256'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                        if name == 'get_scientific_status':
                            operation, batch = value['operation'], value['batch']
                            if operation['id'] != operation_id or operation['status'] != 'succeeded' or batch['status'] != 'succeeded':
                                raise ValueError('scientific operation/batch identity or completion mismatch')
                            call.update(model_id=batch['model_id'], operation_status=operation['status'],
                                        batch_status=batch['status'], result_available=operation['result_available'])
                            if not operation['result_available']:
                                raise ValueError('successful scientific operation did not expose its result')
                        else:
                            result = ScientificRunResult.model_validate(value)
                            if str(result.operation_id) != operation_id or result.terminal_status != 'succeeded' or result.semantic_validation.status != 'passed':
                                raise ValueError('scientific result identity/status/semantic validation mismatch')
                            call.update(terminal_status=result.terminal_status, semantic_validation=result.semantic_validation.status,
                                        result_digest=result.digest,
                                        output_manifest=result.output_manifest.model_dump(mode='json', include={'artifact_id', 'digest', 'size_bytes'}))
                        call.update(status='passed', completed_at=now(), client_seconds=time.monotonic() - started)
                evidence['status'] = 'passed'
    except Exception as error:
        evidence.update(status='failed', failure_type=type(error).__name__)
    evidence['completed_at'] = now()
    encoded = json.dumps(evidence, indent=2) + '\n'
    if token in encoded:
        raise ValueError('credential in receipt')
    args.output.write_text(encoded)
    print(json.dumps({'status': evidence['status'], 'calls': len(evidence['calls']), 'output': str(args.output)}))
    return 0 if evidence['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
