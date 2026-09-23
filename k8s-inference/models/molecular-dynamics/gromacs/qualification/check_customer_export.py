"""Read-only verification of this customer's exact GROMACS operation prefix."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import boto3
from botocore.config import Config
import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--operation-id', type=UUID, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    key = json.loads(args.key_file.read_text())['secret']
    with httpx.Client(base_url='https://89.169.99.188', headers={'Authorization': 'Bearer ' + key}) as http:
        response = http.post('/v1/storage/credentials')
        if response.status_code != 200:
            raise RuntimeError(f'Credential disclosure failed: HTTP {response.status_code}')
        value = response.json()
    s3 = boto3.client('s3', endpoint_url=value['endpoint'], region_name=value['region'],
        aws_access_key_id=value['access_key_id'], aws_secret_access_key=value['secret_access_key'],
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                      request_checksum_calculation='when_required', response_checksum_validation='when_required'))
    bucket = value['bucket_name']
    value.clear()
    prefix = f'runs/gromacs/{args.operation_id}/'
    rows = [row for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix)
            for row in page.get('Contents', [])]
    if not rows:
        raise RuntimeError('The operation has no exported customer objects.')

    def verify(row):
        head = s3.head_object(Bucket=bucket, Key=row['Key'])
        obj = s3.get_object(Bucket=bucket, Key=row['Key'])
        digest, size = hashlib.sha256(), 0
        manifest = bytearray() if '/objects/' not in row['Key'] else None
        with obj['Body'] as body:
            while chunk := body.read(4 * 1024**2):
                digest.update(chunk)
                size += len(chunk)
                if manifest is not None:
                    if size > 16 * 1024**2:
                        raise ValueError('Unexpectedly large customer checkpoint manifest.')
                    manifest.extend(chunk)
        assert size == head['ContentLength'] == row['Size'], row['Key']
        result = {'key': row['Key'], 'size_bytes': size, 'sha256': digest.hexdigest()}
        if manifest is not None:
            result['manifest'] = json.loads(manifest)
        else:
            expected = row['Key'].rsplit('/', 1)[-1]
            metadata = [v for k, v in head.get('Metadata', {}).items() if k.lower() == 'sha256']
            assert expected == result['sha256'] and metadata and all(v == expected for v in metadata), row['Key']
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        verified = list(pool.map(verify, rows))
    indexed = {item['key']: item for item in verified}
    manifests, file_references = 0, 0
    for item in verified:
        manifest = item.get('manifest')
        if manifest is None:
            continue
        assert manifest['schema'] == 'fs2-serve.nebius.ai/gromacs-customer-checkpoint/v1'
        assert manifest['bucket'] == bucket
        names = set()
        for entry in manifest['files']:
            assert entry['key'].startswith(prefix) and entry['path'] not in names
            names.add(entry['path'])
            stored = indexed[entry['key']]
            assert stored['sha256'] == entry['sha256'] and stored['size_bytes'] == entry['size_bytes']
            file_references += 1
        manifests += 1
    assert manifests > 0, 'No complete customer generation was committed.'
    summary = {'operation_id': str(args.operation_id), 'objects_verified': len(rows),
               'bytes_read': sum(item['size_bytes'] for item in verified),
               'manifests_verified': manifests, 'file_references_verified': file_references}
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
        json.dump({'summary': summary, 'objects': verified}, output, indent=2)
        output.write('\n')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
