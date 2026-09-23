"""Read-only verification of this customer's exact GROMACS operation prefix."""
import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

import boto3
from botocore.config import Config
import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--operation-id', type=UUID, required=True)
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
    rows = s3.list_objects_v2(Bucket=bucket, Prefix=f'runs/gromacs/{args.operation_id}/').get('Contents', [])
    for row in rows[:5]:
        head = s3.head_object(Bucket=bucket, Key=row['Key'])
        obj = s3.get_object(Bucket=bucket, Key=row['Key'])
        digest, size = hashlib.sha256(), 0
        with obj['Body'] as body:
            while chunk := body.read(4 * 1024**2):
                digest.update(chunk)
                size += len(chunk)
        print(json.dumps({'key': row['Key'], 'head_size': head['ContentLength'], 'head_size_type': type(head['ContentLength']).__name__,
                          'read_size': size, 'sha256': digest.hexdigest(), 'metadata': head.get('Metadata', {}),
                          'content_encoding': head.get('ContentEncoding')}))
    print(json.dumps({'objects_in_operation_prefix': len(rows)}))


if __name__ == '__main__':
    main()
