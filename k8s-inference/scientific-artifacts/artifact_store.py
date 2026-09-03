#!/usr/bin/env python3
"""Canonical object layout for the scientific result artifact store.

The Terraform in `stages/infrastructure` and `stages/workloads` provisions the
bucket, the writer identity and the credential Secret. This module owns the one
thing both the store and its consumers must agree on exactly: how a committed
artifact is addressed.

    scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>
        /shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>

Every component is a single path segment, so one tenant's prefix can never be a
prefix of another's, and the content digest is the last segment, so a rerun
that produces identical bytes writes the identical key instead of a new one.

`smoke` exercises the provisioned store for real: it signs an upload handle,
uploads through it, finalizes by streaming the stored object back and verifying
its digest, signs a download handle, reads through it, and proves that the
bucket-scoped writer cannot address anything outside `scientific/v1/`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = "scientific/v1"
TENANT_ROOT = f"{ROOT}/tenants"
DIRECTIONS = ("input", "output")

# Bounds from the scientific batch contract: 64 stages per result, 1,024
# independently admitted work units per stage, 10 attempts per work unit.
MAX_SHARD = 1023
MAX_ATTEMPT = 10

TENANT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
OPERATION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
STAGE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$")


class ArtifactLayoutError(ValueError):
    """A component cannot be expressed in the canonical layout."""


@dataclass(frozen=True)
class ArtifactAddress:
    """One committed artifact, fully identified."""

    tenant: str
    operation: str
    stage: str
    shard: int
    attempt: int
    direction: str
    digest: str

    @property
    def key(self) -> str:
        return object_key(
            tenant=self.tenant,
            operation=self.operation,
            stage=self.stage,
            shard=self.shard,
            attempt=self.attempt,
            direction=self.direction,
            digest=self.digest,
        )


def _segment(name: str, value: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.match(value) is None:
        raise ArtifactLayoutError(f"{name} is not a valid artifact path segment")
    # Belt and braces: the patterns already exclude these, but an address that
    # can escape its prefix is the one failure that must never be possible.
    if "/" in value or value in {".", ".."}:
        raise ArtifactLayoutError(f"{name} must be a single path segment")
    return value


def _ordinal(name: str, value: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactLayoutError(f"{name} must be a whole number")
    if value < minimum or value > maximum:
        raise ArtifactLayoutError(f"{name} must be between {minimum} and {maximum}")
    return value


def tenant_prefix(tenant: str) -> str:
    """Return the exclusive key prefix owned by one tenant.

    The trailing slash matters: without it `acme` would also match `acme-labs`.
    """

    return f"{TENANT_ROOT}/{_segment('tenant', tenant, TENANT_PATTERN)}/"


def operation_prefix(tenant: str, operation: str) -> str:
    return (
        f"{tenant_prefix(tenant)}operations/"
        f"{_segment('operation', operation, OPERATION_PATTERN)}/"
    )


def object_key(
    *,
    tenant: str,
    operation: str,
    stage: str,
    shard: int,
    attempt: int,
    direction: str,
    digest: str,
) -> str:
    """Return the canonical object key for one committed artifact."""

    if direction not in DIRECTIONS:
        raise ArtifactLayoutError("direction must be 'input' or 'output'")
    return "".join(
        (
            operation_prefix(tenant, operation),
            f"stages/{_segment('stage', stage, STAGE_PATTERN)}/",
            f"shards/{_ordinal('shard', shard, minimum=0, maximum=MAX_SHARD)}/",
            f"attempts/{_ordinal('attempt', attempt, minimum=1, maximum=MAX_ATTEMPT)}/",
            f"{direction}/sha256/{_segment('digest', digest, DIGEST_PATTERN)}",
        )
    )


def parse_object_key(key: str) -> ArtifactAddress:
    """Recover the address from a canonical key, rejecting anything else."""

    parts = key.split("/")
    shape = (
        len(parts) == 15
        and parts[0:2] == ["scientific", "v1"]
        and parts[2] == "tenants"
        and parts[4] == "operations"
        and parts[6] == "stages"
        and parts[8] == "shards"
        and parts[10] == "attempts"
        and parts[12] in DIRECTIONS
        and parts[13] == "sha256"
    )
    if not shape:
        raise ArtifactLayoutError("key is not in the canonical scientific/v1 layout")
    try:
        shard = int(parts[9])
        attempt = int(parts[11])
    except ValueError as error:
        raise ArtifactLayoutError("shard and attempt must be whole numbers") from error
    if parts[9] != str(shard) or parts[11] != str(attempt):
        raise ArtifactLayoutError("shard and attempt must be canonical decimals")
    address = ArtifactAddress(
        tenant=_segment("tenant", parts[3], TENANT_PATTERN),
        operation=_segment("operation", parts[5], OPERATION_PATTERN),
        stage=_segment("stage", parts[7], STAGE_PATTERN),
        shard=_ordinal("shard", shard, minimum=0, maximum=MAX_SHARD),
        attempt=_ordinal("attempt", attempt, minimum=1, maximum=MAX_ATTEMPT),
        direction=parts[12],
        digest=_segment("digest", parts[14], DIGEST_PATTERN),
    )
    if address.key != key:
        raise ArtifactLayoutError("key is not in the canonical scientific/v1 layout")
    return address


def belongs_to_tenant(key: str, tenant: str) -> bool:
    """Report whether a key lies inside exactly this tenant's prefix."""

    return key.startswith(tenant_prefix(tenant))


def normalize_media_types(media_types: Iterable[str]) -> tuple[str, ...]:
    """Return the sorted, lowercased allowlist, rejecting malformed entries."""

    normalized = sorted({str(item).strip().lower() for item in media_types if str(item).strip()})
    if not normalized:
        raise ArtifactLayoutError("the media-type allowlist must not be empty")
    for media_type in normalized:
        if len(media_type) > 128 or MEDIA_TYPE_PATTERN.match(media_type) is None:
            raise ArtifactLayoutError(f"{media_type!r} is not an exact media type")
    return tuple(normalized)


def media_type_allowed(media_type: str, allowlist: Iterable[str]) -> bool:
    return str(media_type).strip().lower() in set(normalize_media_types(allowlist))


# Only an exact 404 proves an object is gone. A 403 means the caller is not
# allowed to look, which is precisely the answer a bucket-scoped writer gets for
# a key it may not read, so treating it as deletion would let a surviving object
# pass as cleaned up.
DELETED_STATUS = 404


def absence_confirmed(status: object) -> bool:
    """Report whether a probe status proves the object no longer exists."""

    return status == DELETED_STATUS


def error_status(error: BaseException) -> Any:
    """Extract the HTTP status from a botocore error, or name the failure."""

    response = getattr(error, "response", None)
    if isinstance(response, dict):
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status is not None:
            return status
    return type(error).__name__


# Deletion is only proven when all three of these answer 404: the current key,
# the exact version that was written, and the handle that was already signed.
REQUIRED_ABSENCE_PROBES = ("current", "previously_signed_handle", "written_version")


def cleanup_confirmed(cleanup: Mapping[str, Any]) -> bool:
    """Report whether the store proved every written object is gone."""

    if cleanup.get("kept") or cleanup.get("residual"):
        return False
    probes = cleanup.get("verified_absent") or {}
    # `all` over an empty or partial mapping is true, so the probe set has to be
    # checked before its contents.
    if set(probes) != set(REQUIRED_ABSENCE_PROBES):
        return False
    return all(probe.get("absent") is True for probe in probes.values())


def probe_absent(head_object: Any, **kwargs: Any) -> dict[str, Any]:
    """HEAD one object and report whether the store proves it is gone."""

    try:
        head_object(**kwargs)
    except Exception as error:  # noqa: BLE001 - the status is the result
        status = error_status(error)
        return {"absent": absence_confirmed(status), "status": status}
    return {"absent": False, "status": 200}


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_credentials(path: Path) -> tuple[str, str]:
    """Read the same JSON document the Kubernetes Secret carries.

    The credential is never accepted from argv or the environment, so it cannot
    end up in a process listing, a shell history or a CI log.
    """

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("artifact store credentials must be a JSON object")
    access_key = document.get("access_key_id")
    secret_key = document.get("secret_access_key")
    if not isinstance(access_key, str) or not isinstance(secret_key, str):
        raise ValueError(
            "artifact store credentials must name access_key_id and secret_access_key"
        )
    if not access_key or not secret_key:
        raise ValueError("artifact store credentials must be non-empty")
    return access_key, secret_key


def _client(arguments: argparse.Namespace) -> Any:
    import boto3  # imported lazily so the layout is usable without boto3
    from botocore.config import Config

    access_key, secret_key = read_credentials(Path(arguments.credentials_file))
    return boto3.client(
        "s3",
        endpoint_url=arguments.endpoint,
        region_name=arguments.region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": arguments.addressing_style},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _http(method: str, url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, data=body, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def smoke(arguments: argparse.Namespace) -> dict[str, Any]:
    """Run the real signed PUT, finalize, signed GET and isolation checks."""

    client = _client(arguments)
    payload = arguments.payload.encode("utf-8")
    digest = sha256_hex(payload)
    address = ArtifactAddress(
        tenant=arguments.tenant,
        operation=arguments.operation,
        stage=arguments.stage,
        shard=arguments.shard,
        attempt=arguments.attempt,
        direction="output",
        digest=digest,
    )
    key = address.key
    if not media_type_allowed(arguments.media_type, arguments.media_types):
        raise ValueError(f"{arguments.media_type} is not in the approved media types")

    steps: dict[str, Any] = {}

    put_url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": arguments.bucket,
            "Key": key,
            "ContentType": arguments.media_type,
        },
        ExpiresIn=arguments.handle_ttl_seconds,
        HttpMethod="PUT",
    )
    put_status, _ = _http("PUT", put_url, body=payload, headers={"Content-Type": arguments.media_type})
    steps["signed_put"] = {"key": key, "status": put_status, "bytes": len(payload)}
    if put_status not in (200, 201):
        raise RuntimeError(f"signed PUT failed with HTTP {put_status}")

    # Finalize: the store, not the client, decides what was actually committed.
    head = client.head_object(Bucket=arguments.bucket, Key=key)
    streamed = hashlib.sha256()
    size = 0
    body = client.get_object(Bucket=arguments.bucket, Key=key)["Body"]
    for chunk in iter(lambda: body.read(1024 * 1024), b""):
        streamed.update(chunk)
        size += len(chunk)
    steps["finalize"] = {
        "declared_sha256": digest,
        "stored_sha256": streamed.hexdigest(),
        "declared_bytes": len(payload),
        "stored_bytes": size,
        "content_type": head.get("ContentType"),
        "version_id": head.get("VersionId"),
        "digest_matches": streamed.hexdigest() == digest,
        "size_matches": size == len(payload),
    }
    if streamed.hexdigest() != digest or size != len(payload):
        raise RuntimeError("finalize digest or size mismatch; the object was not committed")

    get_url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": arguments.bucket, "Key": key},
        ExpiresIn=arguments.handle_ttl_seconds,
        HttpMethod="GET",
    )
    get_status, fetched = _http("GET", get_url)
    steps["signed_get"] = {
        "status": get_status,
        "bytes": len(fetched),
        "sha256": sha256_hex(fetched),
        "digest_matches": sha256_hex(fetched) == digest,
    }
    if get_status != 200 or sha256_hex(fetched) != digest:
        raise RuntimeError("signed GET did not return the committed bytes")

    # The writer key is scoped to scientific/v1/*; anything else must be denied
    # even though the same key signs the request.
    outside_key = f"reference-data/blobs/sha256/{digest}"
    outside_url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": arguments.bucket, "Key": outside_key},
        ExpiresIn=arguments.handle_ttl_seconds,
        HttpMethod="PUT",
    )
    outside_status, _ = _http("PUT", outside_url, body=payload)
    steps["outside_scope_put"] = {"key": outside_key, "status": outside_status, "denied": outside_status in (401, 403)}

    other_tenant = "isolation-probe"
    other_key = ArtifactAddress(
        tenant=other_tenant,
        operation=arguments.operation,
        stage=arguments.stage,
        shard=arguments.shard,
        attempt=arguments.attempt,
        direction="output",
        digest=digest,
    ).key
    steps["tenant_isolation"] = {
        "tenant": arguments.tenant,
        "tenant_prefix": tenant_prefix(arguments.tenant),
        "other_tenant_key": other_key,
        "disjoint": not belongs_to_tenant(other_key, arguments.tenant)
        and not belongs_to_tenant(key, other_tenant),
    }

    # The writer holds storage.object-editor, which is object scoped: it cannot
    # list the bucket's versions. Delete exactly the versions this run created,
    # using the IDs the store already returned, and record what the role would
    # not allow rather than pretending the bucket is empty.
    cleaned: list[str] = []
    residual: list[str] = []
    if not arguments.keep:
        stored_version = head.get("VersionId")
        if stored_version:
            try:
                client.delete_object(Bucket=arguments.bucket, Key=key, VersionId=stored_version)
                cleaned.append(f"{key}@{stored_version}")
            except Exception as error:  # noqa: BLE001 - reported, never swallowed
                residual.append(f"{key}@{stored_version}: {type(error).__name__}")
        try:
            marker = client.delete_object(Bucket=arguments.bucket, Key=key)
            marker_version = marker.get("VersionId")
            if marker_version:
                try:
                    client.delete_object(
                        Bucket=arguments.bucket, Key=key, VersionId=marker_version
                    )
                    cleaned.append(f"{key}@{marker_version}")
                except Exception as error:  # noqa: BLE001
                    residual.append(f"{key}@{marker_version}: {type(error).__name__}")
        except Exception as error:  # noqa: BLE001
            residual.append(f"{key}: {type(error).__name__}")
    # A delete call returning 204 is not proof the object is gone. Ask the store
    # again, for the current key, for the exact version that was written and
    # through the handle that was already signed, and require an exact 404.
    def absent(**extra: str) -> dict[str, Any]:
        return probe_absent(client.head_object, Bucket=arguments.bucket, Key=key, **extra)

    verified: dict[str, Any] = {}
    if not arguments.keep:
        verified["current"] = absent()
        verified["written_version"] = (
            absent(VersionId=head["VersionId"])
            if head.get("VersionId")
            # Without a version ID there is nothing to prove, and an unproven
            # probe must not read as proof.
            else {"absent": False, "status": "no-version-id-returned"}
        )
        signed_status, _ = _http("GET", get_url)
        verified["previously_signed_handle"] = {
            "status": signed_status,
            "absent": absence_confirmed(signed_status),
        }
    steps["cleanup"] = {
        "deleted_versions": cleaned,
        "residual": residual,
        "kept": bool(arguments.keep),
        "required_probes": list(REQUIRED_ABSENCE_PROBES),
        "verified_absent": verified,
        # Anything the object-scoped role could not remove is reclaimed by the
        # bucket's one-day noncurrent-version and delete-marker rules.
        "residual_reclaimed_by": "expire-noncurrent-versions,remove-expired-delete-markers",
    }

    return {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-store-smoke/v1",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bucket": arguments.bucket,
        "endpoint": arguments.endpoint,
        "region": arguments.region,
        "object_key": key,
        "handle_ttl_seconds": arguments.handle_ttl_seconds,
        "steps": steps,
        "passed": bool(
            steps["finalize"]["digest_matches"]
            and steps["finalize"]["size_matches"]
            and steps["signed_get"]["digest_matches"]
            and steps["outside_scope_put"]["denied"]
            and steps["tenant_isolation"]["disjoint"]
            and (arguments.keep or cleanup_confirmed(steps["cleanup"]))
        ),
        "deletion_proven": cleanup_confirmed(steps["cleanup"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    keys = subparsers.add_parser("key", help="print one canonical object key")
    keys.add_argument("--tenant", required=True)
    keys.add_argument("--operation", required=True)
    keys.add_argument("--stage", required=True)
    keys.add_argument("--shard", type=int, default=0)
    keys.add_argument("--attempt", type=int, default=1)
    keys.add_argument("--direction", choices=DIRECTIONS, default="output")
    keys.add_argument("--digest", required=True)

    live = subparsers.add_parser("smoke", help="run the live signed-handle smoke test")
    live.add_argument("--endpoint", required=True)
    live.add_argument("--bucket", required=True)
    live.add_argument("--region", required=True)
    live.add_argument("--addressing-style", default="path", choices=("path", "virtual"))
    live.add_argument(
        "--credentials-file",
        required=True,
        help="JSON with access_key_id and secret_access_key; never passed on argv",
    )
    live.add_argument("--tenant", default="fs2-acceptance")
    live.add_argument("--operation", required=True)
    live.add_argument("--stage", default="semantic-validation")
    live.add_argument("--shard", type=int, default=0)
    live.add_argument("--attempt", type=int, default=1)
    live.add_argument("--media-type", default="application/json")
    live.add_argument("--media-types", nargs="+", default=["application/json"])
    live.add_argument("--handle-ttl-seconds", type=int, default=600)
    live.add_argument("--payload", default='{"probe":"scientific-artifact-store"}')
    live.add_argument("--keep", action="store_true", help="leave the object in place")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "key":
        print(
            object_key(
                tenant=arguments.tenant,
                operation=arguments.operation,
                stage=arguments.stage,
                shard=arguments.shard,
                attempt=arguments.attempt,
                direction=arguments.direction,
                digest=arguments.digest,
            )
        )
        return 0
    result = smoke(arguments)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
