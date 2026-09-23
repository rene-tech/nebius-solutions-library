"""Archive exact terminal failed/cancelled task exports; optionally free their space.

No bucket-wide cleanup and no successful/customer operation prefix is allowed.
Every byte is retained locally and hash-verified before any deletion. Manifests
and metadata are retained so an operator can restore the original object keys.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

import boto3
from botocore.config import Config
import httpx

OWNED = {
    'd4b5dfc4-2bc2-45e2-84a8-14c500763371',
    '6ac88364-d1c3-4052-ba13-101fbc519af6',
    '98d3798b-c7e1-458c-b75e-e3fef01b3562',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--operation-id', choices=sorted(OWNED), required=True)
    parser.add_argument('--status', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--delete-archived', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    status = json.loads(args.status.read_text())
    status = status.get('final', status)
    assert status['operation']['id'] == args.operation_id
    assert status['operation']['tenant_id'] == status['operation']['principal_id'] == 'rene'
    assert status['operation']['model_id'] == 'gromacs-mpi'
    assert status['operation']['status'] in {'failed', 'cancelled'}
    assert all(attempt['resource_released'] for stage in status['batch']['stages'] for attempt in stage['attempts'])
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with httpx.Client(base_url='https://89.169.99.188', timeout=90, trust_env=False,
                      headers={'Authorization': 'Bearer ' + json.loads(args.key_file.read_text())['secret']}) as http:
        response = http.post('/v1/storage/credentials')
        response.raise_for_status()
        credentials = response.json()
    s3 = boto3.client('s3', endpoint_url=credentials['endpoint'], region_name=credentials['region'],
                     aws_access_key_id=credentials['access_key_id'], aws_secret_access_key=credentials['secret_access_key'],
                     config=Config(signature_version='s3v4', request_checksum_calculation='when_required',
                                   response_checksum_validation='when_required', s3={'addressing_style': 'path'}))
    bucket = credentials['bucket_name']
    credentials.clear()
    prefix = f'runs/gromacs-mpi/{args.operation_id}/'
    listed = [item for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix)
              for item in page.get('Contents', [])]
    archive = {'bucket': bucket, 'prefix': prefix, 'operation_id': args.operation_id, 'objects': [], 'deleted': []}
    receipt = args.output / 'archive-private.json'
    for index, item in enumerate(listed):
        obj = s3.get_object(Bucket=bucket, Key=item['Key'])
        path = args.output / f'object-{index:05d}.bin'
        digest = hashlib.sha256()
        with path.open('xb') as output, obj['Body'] as body:
            while chunk := body.read(4 * 1024**2):
                output.write(chunk)
                digest.update(chunk)
        assert path.stat().st_size == item['Size'] == obj['ContentLength']
        if '/objects/' in item['Key']:
            assert digest.hexdigest() == item['Key'].rsplit('/', 1)[1]
        archive['objects'].append({'key': item['Key'], 'local': path.name, 'bytes': item['Size'],
                                   'sha256': digest.hexdigest(), 'etag': obj['ETag'],
                                   'metadata': obj.get('Metadata', {}), 'content_type': obj.get('ContentType')})
        receipt.write_text(json.dumps(archive, indent=2))
    if args.delete_archived:
        for item in archive['objects']:
            head = s3.head_object(Bucket=bucket, Key=item['key'])
            assert head['ContentLength'] == item['bytes'] and head['ETag'] == item['etag']
            assert hashlib.file_digest((args.output / item['local']).open('rb'), 'sha256').hexdigest() == item['sha256']
            s3.delete_object(Bucket=bucket, Key=item['key'])
            archive['deleted'].append(item['key'])
            receipt.write_text(json.dumps(archive, indent=2))
    print(json.dumps({'operation_id': args.operation_id, 'archived_objects': len(archive['objects']),
                      'archived_bytes': sum(item['bytes'] for item in archive['objects']),
                      'deleted_objects': len(archive['deleted']), 'recoverable_archive': str(args.output)}))


if __name__ == '__main__':
    main()
