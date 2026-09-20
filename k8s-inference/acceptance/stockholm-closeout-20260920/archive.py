#!/usr/bin/env python3
"""Export Stockholm evidence read-only to a private, compressed operator archive.

Never prints credentials or row contents. Does not revoke, delete or deploy.
The archive can contain encrypted request payloads and must never enter Git.
"""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


POD_SCRIPT = r'''
import asyncio, asyncpg, json, os, re
async def main():
 c = await asyncpg.connect(os.environ['FS2_DATABASE_URL'], command_timeout=120)
 async with c.transaction(isolation='repeatable_read', readonly=True):
  columns = await c.fetch("""SELECT c.table_name,c.column_name FROM information_schema.columns c
    JOIN information_schema.tables t USING(table_schema,table_name)
    WHERE c.table_schema='public' AND t.table_type='BASE TABLE' AND c.table_name LIKE 'fs2_%'
      AND has_column_privilege(current_user, 'public.' || c.table_name, c.column_name, 'SELECT')""")
  tables={}
  for row in columns:
   tables.setdefault(row['table_name'],set()).add(row['column_name'])
  counts={}
  for table,cols in sorted(tables.items()):
   assert re.fullmatch(r'fs2_[a-z_0-9]+',table)
   if 'tenant_id' in cols:
    predicate='tenant_id=$1'
   elif 'operation_id' in cols:
    predicate='operation_id IN (SELECT id FROM fs2_operations WHERE tenant_id=$1)'
   elif 'subject_id' in cols:
    predicate='subject_id IN (SELECT subject_id FROM fs2_telemetry_subjects WHERE tenant_id=$1)'
   else:
    continue
   counts[table]=0
   selected=','.join('"'+col+'"' for col in sorted(cols))
   # The existing role intentionally cannot read every immutable ledger column.
   # Export its readable projection; never broaden production permissions.
   async for row in c.cursor('SELECT to_jsonb(r)::text AS data FROM (SELECT '+selected+' FROM "'+table+'" WHERE '+predicate+') r',
      'stockholm',prefetch=1000):
    print(json.dumps({'table':table,'row':json.loads(row['data'])},separators=(',',':')),flush=True)
    counts[table]+=1
  others={}
  for table in ('fs2_tokens','fs2_inference_users','fs2_model_deployments','fs2_storage_buckets','fs2_user_storage'):
   # Exclude naturally changing usage counters from the retained-key comparison.
   expr="to_jsonb(r) - ARRAY['last_used_at','rate_window_started_at','rate_window_requests','requests_used','gpu_seconds_used','requests_reserved','gpu_seconds_reserved']" if table=='fs2_tokens' else 'to_jsonb(r)'
   rows=await c.fetch('SELECT ('+expr+')::text AS data FROM '+table+" r WHERE tenant_id <> $1 ORDER BY 1",'stockholm')
   import hashlib
   others[table]=hashlib.sha256('\n'.join(r['data'] for r in rows).encode()).hexdigest()
  print(json.dumps({'summary':{'counts':counts,'readable_columns':{t:sorted(v) for t,v in tables.items()},'other_tenant_configuration_sha256':others,
    'snapshot_at':str(await c.fetchval('SELECT transaction_timestamp()'))}}),flush=True)
 await c.close()
asyncio.run(main())
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    destination = args.output / 'stockholm-database.jsonl.gz'
    command = ['kubectl','--kubeconfig',args.kubeconfig,'--context',args.context,
               '-n','fs2-system','exec','-i','deploy/fs2-serve-control-plane','-c','control-plane',
               '--','python','-']
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdin.write(POD_SCRIPT.encode())
    proc.stdin.close()
    summary = None
    with gzip.open(destination, 'xb', compresslevel=3) as stream:
        for line in proc.stdout:
            stream.write(line)
            if line.startswith(b'{"summary":'):
                summary = json.loads(line)['summary']
    code = proc.wait()
    (args.output / 'export-stderr.log').write_bytes(proc.stderr.read())
    if code or summary is None:
        raise RuntimeError('export incomplete; protected partial archive retained, no deletion allowed')
    hashes = {}
    for name, source in {
        'stockholm-demand-followup-20260917': Path('/home/tux/secure-handoff/stockholm-demand-followup-20260917'),
    }.items():
        shutil.copytree(source, args.output / name)
    for path in sorted(args.output.rglob('*')):
        if path.is_file():
            with path.open('rb') as stream:
                hashes[str(path.relative_to(args.output))] = hashlib.file_digest(stream,'sha256').hexdigest()
    manifest = {**summary, 'files_sha256': hashes, 'scope': 'stockholm-only; read-only snapshot; prior acceptance receipts'}
    manifest_path = args.output / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    checksum = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    # Verify decompression and every JSON record independently before authorizing retirement.
    rows = 0
    with gzip.open(destination,'rt') as stream:
        for line in stream:
            json.loads(line)
            rows += 1
    print(json.dumps({'archive':str(args.output),'manifest_sha256':checksum,'json_records_verified':rows,
                      'counts':summary['counts'],'files':len(hashes)}))


if __name__ == '__main__':
    main()
