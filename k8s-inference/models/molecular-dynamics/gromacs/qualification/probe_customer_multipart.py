"""Diagnose S3Transfer with one public fixture in a task-only customer prefix."""
import argparse
import hashlib
import json
import os
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--required-checksums-only', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        raise ValueError('Keep the original probe receipt')
    digest = hashlib.file_digest(args.source.open('rb'), 'sha256').hexdigest()
    with httpx.Client(base_url='https://89.169.99.188', timeout=90, trust_env=False,
                      headers={'Authorization': 'Bearer ' + json.loads(args.key_file.read_text())['secret']}) as http:
        response = http.post('/v1/storage/credentials')
        response.raise_for_status()
        credentials = response.json()
    optional = {'request_checksum_calculation': 'when_required', 'response_checksum_validation': 'when_required'} if args.required_checksums_only else {}
    s3 = boto3.client('s3', endpoint_url=credentials['endpoint'], region_name=credentials['region'],
                     aws_access_key_id=credentials['access_key_id'], aws_secret_access_key=credentials['secret_access_key'],
                     config=Config(signature_version='s3v4', retries={'mode': 'standard', 'max_attempts': 5},
                                   s3={'addressing_style': 'path'}, **optional))
    bucket = credentials['bucket_name']
    credentials.clear()
    key = 'qualification/gromacs-20260923-multipart/' + ('required/' if optional else 'default/') + digest
    result = {'key': key, 'bytes': args.source.stat().st_size, 'sha256': digest, 'uploaded': False,
              'required_checksums_only': args.required_checksums_only}
    try:
        s3.upload_file(str(args.source), bucket, key,
            Config=TransferConfig(multipart_threshold=64 * 1024**2, multipart_chunksize=64 * 1024**2,
                                  max_concurrency=2, max_io_queue=4, io_chunksize=1024**2),
            ExtraArgs={'Metadata': {'sha256': digest}, 'ContentType': 'application/octet-stream'})
        obj = s3.get_object(Bucket=bucket, Key=key)
        downloaded = hashlib.sha256()
        with obj['Body'] as body:
            while chunk := body.read(4 * 1024**2):
                downloaded.update(chunk)
        assert downloaded.hexdigest() == digest
        result.update(uploaded=True, downloaded_verified=True)
    except Exception as error:
        result['error_class'] = type(error).__name__
        seen = set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            if isinstance(error, ClientError):
                result.update(provider_code=error.response.get('Error', {}).get('Code'),
                              http_status=error.response.get('ResponseMetadata', {}).get('HTTPStatusCode'),
                              request_id=error.response.get('ResponseMetadata', {}).get('RequestId'))
            error = error.__cause__ or error.__context__
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
