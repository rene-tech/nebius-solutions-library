#!/usr/bin/env python3
"""Prove token-anchor absence/presence with PartialObjectMetadataList only."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

from collect_sai07_secret_metadata import (
    ANCHOR_NAME_PREFIX,
    ANCHOR_NAMESPACE,
    MEDIA_TYPE,
    MetadataError,
    canonical,
    read_ca,
    read_token,
    request_metadata,
    validate_token,
)

SCHEMA = "fs2-serve.nebius.ai/sai07-token-anchor-metadata/v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-server", required=True)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--token-fd", required=True, type=int)
    parser.add_argument("--reader-service-account-uid", required=True)
    parser.add_argument("--anchor-uid", required=True)
    parser.add_argument("--anchor-name", required=True)
    args = parser.parse_args()
    try:
        token = read_token(args.token_fd)
        jti_sha256 = validate_token(
            token,
            service_account_namespace="fs2-system",
            service_account_name="fs2-pod-security-metadata-reader",
            service_account_uid=args.reader_service_account_uid,
            anchor_name=args.anchor_name,
            anchor_uid=args.anchor_uid,
        )
        collection = request_metadata(
            args.api_server,
            read_ca(args.ca_file),
            token,
            ANCHOR_NAMESPACE,
        )
        if set(collection) != {"apiVersion", "kind", "metadata", "items"}:
            raise MetadataError("token-anchor response contains non-metadata fields")
        selected = []
        for item in collection["items"]:
            if not isinstance(item, dict) or set(item) != {"apiVersion", "kind", "metadata"}:
                raise MetadataError("token-anchor response contains Secret payload fields")
            metadata = item.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("name") != args.anchor_name:
                continue
            selected.append(
                {
                    "namespace": metadata.get("namespace"),
                    "name": metadata.get("name"),
                    "uid": metadata.get("uid"),
                    "resource_version": metadata.get("resourceVersion"),
                }
            )
        if len(selected) > 1 or any(not all(isinstance(value, str) and value for value in item.values()) for item in selected):
            raise MetadataError("token-anchor metadata identity is duplicated or incomplete")
        artifact = {
            "schema": SCHEMA,
            "media_type": MEDIA_TYPE,
            "namespace": ANCHOR_NAMESPACE,
            "collection_resource_version": collection["metadata"].get("resourceVersion"),
            "observed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "items": selected,
            "items_sha256": hashlib.sha256(canonical(selected)).hexdigest(),
            "contains_secret_payload": False,
            "reader_service_account_uid": args.reader_service_account_uid,
            "token_jti_sha256": jti_sha256,
            "token_bound_object_ref": {
                "api_version": "v1",
                "kind": "Secret",
                "namespace": ANCHOR_NAMESPACE,
                "name": args.anchor_name,
                "uid": args.anchor_uid,
            },
        }
        if not args.anchor_name.startswith(ANCHOR_NAME_PREFIX):
            raise MetadataError("token-anchor name is outside the generation-addressed prefix")
    except (MetadataError, OSError, UnicodeDecodeError) as error:
        print(f"SAI-07 token-anchor metadata rejected: {error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical(artifact))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
