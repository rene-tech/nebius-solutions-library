#!/usr/bin/env python3
"""Collect bounded, read-only provider and backend evidence for SAI-07.

This is the provider-native adapter for the v3 custody contract.  It obtains
identity, IAM, group and bucket data through the Nebius SDK, and obtains S3
control/state data through read-only S3 APIs.  It never calls any credential
enumeration API.  AccessKeyService list responses may themselves carry
``status.secret`` and therefore cannot be made metadata-only by filtering after
receipt; enumerating another credential class would not prove the caller that
signed an S3 request.  Backend authority is instead bound to a unique
service-account custody epoch, an exact group/access-permit closure and reviewed
native/S3 bucket policies.  Prior epoch principals remain present but must have
zero group membership and zero access permits; their credentials are preserved
and cannot inherit the current epoch's authority.  It never requests a
credential secret and never emits Terraform state on stdout.  Each
output is an O_EXCL, mode-0600 generation file; a failed generation is retained
and a retry must use a fresh collection ID and fresh paths.

The checked-in contract is inactive.  This task added source only and did not
execute the collector or contact Nebius/S3.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import boto3
from nebius.api.nebius.iam import v1 as iam
from nebius.api.nebius.storage import v1 as storage
from nebius.sdk import SDK

SCHEMA = "fs2-serve.nebius.ai/sai07-authoritative-provider-evidence/v3"
BACKEND_SCHEMA = "fs2-serve.nebius.ai/sai07-authoritative-backend-evidence/v3"
CONTRACT_SCHEMA = "fs2-serve.nebius.ai/sai07-evidence-collection-contract/v3"
COLLECTION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{15,95}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "stages" / "pod-security-custody" / "custody-trust-lock-v3.json"


class CollectionError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_contract() -> dict[str, Any]:
    descriptor = os.open(CONTRACT, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
            raise CollectionError("v3 custody contract is not a bounded regular file")
        payload = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(payload) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise CollectionError("v3 custody contract changed during its descriptor-fenced read")
    document = payload[:-1] if payload.endswith(b"\n") and not payload.endswith(b"\n\n") else payload
    value = json.loads(document)
    if not isinstance(value, dict) or canonical(value) != document:
        raise CollectionError("v3 custody contract must be canonical JSON")
    if value.get("schema") != CONTRACT_SCHEMA or value.get("activation") != "active":
        raise CollectionError("v3 authoritative evidence collection is not active")
    dependencies = value.get("dependencies")
    if not isinstance(dependencies, dict) or set(dependencies) != {"sai03", "sai04"}:
        raise CollectionError("v3 contract omits exact integration dependencies")
    for dependency, details in dependencies.items():
        if (
            not isinstance(details, dict)
            or set(details) != {"accepted_commit", "status"}
            or details.get("status") != "accepted"
            or not isinstance(details.get("accepted_commit"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", details["accepted_commit"])
        ):
            raise CollectionError(f"{dependency} is not pinned to an accepted exact commit")
    epoch = value.get("custody_epoch")
    if not isinstance(epoch, dict) or set(epoch) != {
        "epoch_id",
        "generation",
        "previous_activation_sha256",
        "principal_id",
        "required_group_ids",
        "required_permits",
        "retired_epochs",
        "retirement_mode",
        "status",
    }:
        raise CollectionError("v3 contract omits the custody epoch boundary")
    generation = epoch.get("generation")
    if (
        epoch.get("status") != "active-reviewed"
        or not COLLECTION_RE.fullmatch(str(epoch.get("epoch_id", "")))
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or not str(epoch.get("principal_id", "")).startswith("serviceaccount-")
        or epoch.get("retirement_mode")
        != "authorization-denied-in-place-credentials-preserved"
    ):
        raise CollectionError("v3 custody epoch is not an active reviewed service-account generation")
    retired = epoch.get("retired_epochs")
    groups = epoch.get("required_group_ids")
    permits = epoch.get("required_permits")
    if (
        not isinstance(retired, list)
        or not isinstance(groups, list)
        or not groups
        or groups != sorted(set(groups))
        or not isinstance(permits, list)
        or not permits
    ):
        raise CollectionError("v3 custody epoch inventory is incomplete or non-canonical")
    if generation == 1:
        if epoch.get("previous_activation_sha256") is not None or retired:
            raise CollectionError("initial custody epoch unexpectedly claims retired predecessors")
    elif (
        not isinstance(epoch.get("previous_activation_sha256"), str)
        or not SHA256_RE.fullmatch(epoch["previous_activation_sha256"])
        or len(retired) != generation - 1
    ):
        raise CollectionError("rotated custody epoch omits its prior activation chain")
    collector = value.get("collector")
    if not isinstance(collector, dict):
        raise CollectionError("v3 custody contract omits collector pinning")
    source = Path(__file__).resolve()
    if source != (ROOT / collector.get("source_path", "")).resolve():
        raise CollectionError("collector source path differs from the repository contract")
    if hashlib.sha256(source.read_bytes()).hexdigest() != collector.get("source_sha256"):
        raise CollectionError("collector source differs from the repository-pinned digest")
    versions = {
        "nebius": importlib.metadata.version("nebius"),
        "boto3": importlib.metadata.version("boto3"),
        "botocore": importlib.metadata.version("botocore"),
    }
    if versions != collector.get("package_versions"):
        raise CollectionError("provider adapter package versions differ from the reviewed contract")
    scope = value.get("scope")
    required_scope = {
        "backend_bucket",
        "backend_bucket_resource_id",
        "backend_endpoint",
        "backend_region",
        "platform_state_key",
        "project_id",
        "tenant_id",
    }
    if (
        not isinstance(scope, dict)
        or set(scope) != required_scope
        or any(not isinstance(scope[field], str) or not scope[field] for field in required_scope)
    ):
        raise CollectionError("active v3 contract has an incomplete provider/backend scope")
    for field in ("max_evidence_bytes", "max_pages_per_collection", "max_state_bytes", "page_size"):
        if (
            not isinstance(collector.get(field), int)
            or isinstance(collector[field], bool)
            or collector[field] <= 0
        ):
            raise CollectionError(f"active v3 contract has invalid collector bound {field}")
    return value


def instant() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def proto(value: Any) -> dict[str, Any]:
    raw = value.to_json(
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise CollectionError("provider SDK returned a non-object protobuf response")
    return decoded


async def call(service: str, method: str, request: Any, operation: Any) -> dict[str, Any]:
    response = await operation
    request_id = await operation.request_id()
    trace_id = await operation.trace_id()
    if not isinstance(request_id, str) or not request_id or not isinstance(trace_id, str) or not trace_id:
        raise CollectionError(f"{service}.{method} omitted provider request provenance")
    return {
        "method": method,
        "provenance": {"request_id": request_id, "trace_id": trace_id},
        "request": proto(request),
        "response": proto(response),
        "service": service,
    }


async def pages(
    service: str,
    method: str,
    make_request: Callable[[str], Any],
    invoke: Callable[[Any], Awaitable[Any]],
    *,
    maximum: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    token = ""
    seen: set[str] = set()
    while True:
        if token in seen:
            raise CollectionError(f"{service}.{method} repeated a pagination token")
        seen.add(token)
        request = make_request(token)
        entry = await call(service, method, request, invoke(request))
        result.append(entry)
        if len(result) > maximum:
            raise CollectionError(f"{service}.{method} exceeded the bounded page count")
        token = entry["response"].get("next_page_token", "")
        if not isinstance(token, str):
            raise CollectionError(f"{service}.{method} returned a non-string page token")
        if not token:
            return result


def metadata_id(value: dict[str, Any], label: str) -> str:
    metadata = value.get("metadata")
    identifier = metadata.get("id") if isinstance(metadata, dict) else None
    if not isinstance(identifier, str) or not identifier:
        raise CollectionError(f"{label} omits metadata.id")
    return identifier


def profile_principal(profile: dict[str, Any], project_id: str) -> str:
    """Derive the unique active service account used for the custody epoch."""

    service_account = profile.get("service_account_profile")
    user = profile.get("user_profile")
    if isinstance(service_account, dict) and user in (None, {}):
        info = service_account.get("info")
        if not isinstance(info, dict):
            raise CollectionError("service-account profile omits account information")
        metadata = info.get("metadata")
        status = info.get("status")
        if (
            not isinstance(metadata, dict)
            or metadata.get("parent_id") != project_id
            or not isinstance(status, dict)
            or status.get("state") != "ACTIVE"
        ):
            raise CollectionError("provider profile is not an active service account in the custody project")
        return metadata_id(info, "provider collector service account")
    raise CollectionError("provider collector profile is not the unique custody service account")


def safe_json(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode()}
    if isinstance(value, dict):
        return {str(key): safe_json(item) for key, item in value.items() if str(key) != "Body"}
    if isinstance(value, (list, tuple)):
        return [safe_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def reject_secret_payload(value: object, path: str = "provider") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key.lower() in {"secret", "secret_access_key", "token"} and item not in (None, "", {}):
                raise CollectionError(f"provider metadata response unexpectedly contains secret material at {child}")
            reject_secret_payload(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_secret_payload(item, f"{path}[{index}]")


def s3_call(method: str, request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    return {"method": method, "request": safe_json(request), "response": safe_json(response), "service": "s3"}


def write_exclusive(path: Path, payload: bytes, maximum: int, label: str) -> None:
    if not path.is_absolute() or ".." in path.parts or len(payload) > maximum:
        raise CollectionError(f"{label} output path or size violates the bounded contract")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise CollectionError(f"{label} output write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def provider_artifact(
    contract: dict[str, Any], credentials: Path, collection_id: str
) -> dict[str, Any]:
    scope = contract["scope"]
    page_size = contract["collector"]["page_size"]
    max_pages = contract["collector"]["max_pages_per_collection"]
    sdk = SDK(credentials_file_name=str(credentials), user_agent_prefix="fs2-sai07-evidence/1")
    try:
        tenants = iam.TenantServiceClient(sdk)
        projects = iam.ProjectServiceClient(sdk)
        service_accounts = iam.ServiceAccountServiceClient(sdk)
        tenant_users = iam.TenantUserAccountServiceClient(sdk)
        groups = iam.GroupServiceClient(sdk)
        memberships = iam.GroupMembershipServiceClient(sdk)
        permits = iam.AccessPermitServiceClient(sdk)
        buckets = storage.BucketServiceClient(sdk)

        started = instant()
        profile_request = iam.GetProfileRequest()
        profile_entry = await call(
            "nebius.iam.v1.ProfileService", "Get", profile_request, sdk.whoami()
        )
        profile = profile_entry["response"]
        whoami = profile_principal(profile, scope["project_id"])
        tenant_request = iam.GetTenantRequest(id=scope["tenant_id"])
        project_request = iam.GetProjectRequest(id=scope["project_id"])
        bucket_request = storage.GetBucketRequest(id=scope["backend_bucket_resource_id"])
        calls: dict[str, Any] = {
            "profile": profile_entry,
            "tenant": await call(
                "nebius.iam.v1.TenantService", "Get", tenant_request, tenants.get(tenant_request)
            ),
            "project": await call(
                "nebius.iam.v1.ProjectService", "Get", project_request, projects.get(project_request)
            ),
            "bucket": await call(
                "nebius.storage.v1.BucketService", "Get", bucket_request, buckets.get(bucket_request)
            ),
        }
        calls["tenants"] = await pages(
            "nebius.iam.v1.TenantService",
            "List",
            lambda token: iam.ListTenantsRequest(page_size=page_size, page_token=token, filter=""),
            tenants.list,
            maximum=max_pages,
        )
        calls["projects"] = await pages(
            "nebius.iam.v1.ProjectService",
            "List",
            lambda token: iam.ListProjectsRequest(
                parent_id=scope["tenant_id"], page_size=page_size, page_token=token, filter=""
            ),
            projects.list,
            maximum=max_pages,
        )
        project_ids = {
            metadata_id(item, "project")
            for page in calls["projects"]
            for item in page["response"].get("items", [])
        }
        calls["service_accounts_by_project"] = {}
        for project_id in sorted(project_ids):
            calls["service_accounts_by_project"][project_id] = await pages(
                "nebius.iam.v1.ServiceAccountService",
                "List",
                lambda token, project_id=project_id: iam.ListServiceAccountRequest(
                    parent_id=project_id, page_size=page_size, page_token=token, filter=""
                ),
                service_accounts.list,
                maximum=max_pages,
            )
        calls["tenant_users"] = await pages(
            "nebius.iam.v1.TenantUserAccountService",
            "List",
            lambda token: iam.ListTenantUserAccountsRequest(
                parent_id=scope["tenant_id"], page_size=page_size, page_token=token, filter=""
            ),
            tenant_users.list,
            maximum=max_pages,
        )

        group_calls: dict[str, Any] = {}
        group_ids: set[str] = set()
        for parent in (scope["tenant_id"], *sorted(project_ids)):
            collected = await pages(
                "nebius.iam.v1.GroupService",
                "List",
                lambda token, parent=parent: iam.ListGroupsRequest(
                    parent_id=parent, page_size=page_size, page_token=token, filter=""
                ),
                groups.list,
                maximum=max_pages,
            )
            group_calls[parent] = collected
            for page in collected:
                for item in page["response"].get("items", []):
                    group_ids.add(metadata_id(item, "group"))
        calls["groups_by_parent"] = group_calls

        principal_ids: set[str] = set()
        principal_collections = [
            calls["tenant_users"],
            *calls["service_accounts_by_project"].values(),
        ]
        for collection in principal_collections:
            for page in collection:
                for item in page["response"].get("items", []):
                    principal_ids.add(metadata_id(item, "principal"))

        calls["group_members"] = {}
        for group_id in sorted(group_ids):
            calls["group_members"][group_id] = await pages(
                "nebius.iam.v1.GroupMembershipService",
                "ListMembers",
                lambda token, group_id=group_id: iam.ListGroupMembershipsRequest(
                    parent_id=group_id, page_size=page_size, page_token=token, filter=""
                ),
                memberships.list_members,
                maximum=max_pages,
            )
        calls["member_of"] = {}
        for subject_id in sorted(principal_ids | group_ids):
            calls["member_of"][subject_id] = await pages(
                "nebius.iam.v1.GroupMembershipService",
                "ListMemberOf",
                lambda token, subject_id=subject_id: iam.ListMemberOfRequest(
                    subject_id=subject_id, page_size=page_size, page_token=token, filter=""
                ),
                memberships.list_member_of,
                maximum=max_pages,
            )
        calls["access_permits"] = {}
        for subject_id in sorted(principal_ids | group_ids):
            calls["access_permits"][subject_id] = await pages(
                "nebius.iam.v1.AccessPermitService",
                "List",
                lambda token, subject_id=subject_id: iam.ListAccessPermitRequest(
                    parent_id=subject_id, page_size=page_size, page_token=token, filter=""
                ),
                permits.list,
                maximum=max_pages,
            )

        return {
            "calls": calls,
            "collection_id": collection_id,
            "collector": {
                "boto3_version": importlib.metadata.version("boto3"),
                "nebius_sdk_version": importlib.metadata.version("nebius"),
                "source_sha256": contract["collector"]["source_sha256"],
            },
            "completed_at": instant(),
            "schema": SCHEMA,
            "scope": {
                "backend_bucket_resource_id": scope["backend_bucket_resource_id"],
                "project_id": scope["project_id"],
                "tenant_id": scope["tenant_id"],
            },
            "started_at": started,
            "whoami_principal_id": whoami,
        }
    finally:
        await sdk.close()


def backend_artifact(
    contract: dict[str, Any], profile: str, collection_id: str
) -> tuple[dict[str, Any], bytes]:
    scope = contract["scope"]
    session = boto3.session.Session(profile_name=profile, region_name=scope["backend_region"])
    if session.get_credentials() is None:
        raise CollectionError("S3 profile did not resolve a credential identity")
    client = session.client("s3", endpoint_url=scope["backend_endpoint"])
    bucket = scope["backend_bucket"]
    key = scope["platform_state_key"]
    calls: dict[str, Any] = {}

    def invoke(name: str, **request: Any) -> dict[str, Any]:
        response = getattr(client, name)(**request)
        if not isinstance(response, dict):
            raise CollectionError(f"S3 {name} returned a non-object response")
        calls[name] = s3_call(name, request, response)
        return response

    started = instant()
    invoke("head_bucket", Bucket=bucket)
    invoke("get_bucket_acl", Bucket=bucket)
    invoke("get_bucket_encryption", Bucket=bucket)
    invoke("get_bucket_versioning", Bucket=bucket)
    invoke("get_object_lock_configuration", Bucket=bucket)
    invoke("get_bucket_policy", Bucket=bucket)
    invoke("get_bucket_policy_status", Bucket=bucket)
    head_before = invoke("head_object", Bucket=bucket, Key=key)
    version = head_before.get("VersionId")
    if not isinstance(version, str) or not version:
        raise CollectionError("platform state object has no immutable version ID")
    invoke("get_object_retention", Bucket=bucket, Key=key, VersionId=version)
    invoke("get_object_legal_hold", Bucket=bucket, Key=key, VersionId=version)
    invoke("get_object_tagging", Bucket=bucket, Key=key, VersionId=version)
    download_request = {"Bucket": bucket, "Key": key, "VersionId": version}
    download = client.get_object(**download_request)
    body = download.pop("Body", None)
    if body is None:
        raise CollectionError("S3 GetObject omitted the platform state body")
    maximum = contract["collector"]["max_state_bytes"]
    state_bytes = body.read(maximum + 1)
    if len(state_bytes) > maximum or body.read(1):
        raise CollectionError("platform state exceeds the bounded evidence contract")
    calls["get_object"] = s3_call("get_object", download_request, download)
    head_after = client.head_object(Bucket=bucket, Key=key, VersionId=version)
    calls["head_object_after"] = s3_call(
        "head_object", {"Bucket": bucket, "Key": key, "VersionId": version}, head_after
    )
    for field in ("VersionId", "ETag", "ContentLength"):
        if head_before.get(field) != head_after.get(field) or download.get(field) != head_before.get(field):
            raise CollectionError(f"platform state {field} drifted during the bounded read")
    return (
        {
            "calls": calls,
            "collection_id": collection_id,
            "collector": {
                "boto3_version": importlib.metadata.version("boto3"),
                "botocore_version": importlib.metadata.version("botocore"),
                "source_sha256": contract["collector"]["source_sha256"],
            },
            "completed_at": instant(),
            "schema": BACKEND_SCHEMA,
            "scope": {
                "bucket": bucket,
                "endpoint": scope["backend_endpoint"],
                "key": key,
                "region": scope["backend_region"],
            },
            "started_at": started,
            "state": {
                "bytes": len(state_bytes),
                "etag": head_before["ETag"],
                "sha256": hashlib.sha256(state_bytes).hexdigest(),
                "version_id": version,
            },
        },
        state_bytes,
    )


async def collect(args: argparse.Namespace) -> None:
    contract = read_contract()
    if not COLLECTION_RE.fullmatch(args.collection_id):
        raise CollectionError("collection ID is not a bounded generation identifier")
    provider = await provider_artifact(contract, args.nebius_credentials, args.collection_id)
    reject_secret_payload(provider)
    backend, state = backend_artifact(contract, args.s3_profile, args.collection_id)
    maximum = contract["collector"]["max_evidence_bytes"]
    write_exclusive(args.provider_output, canonical(provider), maximum, "provider evidence")
    write_exclusive(args.backend_output, canonical(backend), maximum, "backend evidence")
    write_exclusive(args.state_output, state, contract["collector"]["max_state_bytes"], "platform state")
    summary = {
        "backend_evidence_sha256": hashlib.sha256(canonical(backend)).hexdigest(),
        "collection_id": args.collection_id,
        "platform_state_sha256": hashlib.sha256(state).hexdigest(),
        "provider_evidence_sha256": hashlib.sha256(canonical(provider)).hexdigest(),
        "valid": True,
    }
    sys.stdout.buffer.write(canonical(summary))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--collection-id", required=True)
    result.add_argument("--nebius-credentials", required=True, type=Path)
    result.add_argument("--s3-profile", required=True)
    result.add_argument("--provider-output", required=True, type=Path)
    result.add_argument("--backend-output", required=True, type=Path)
    result.add_argument("--state-output", required=True, type=Path)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        for path in (args.nebius_credentials, args.provider_output, args.backend_output, args.state_output):
            if not path.is_absolute() or ".." in path.parts:
                raise CollectionError("all evidence and credential paths must be absolute without traversal")
        asyncio.run(collect(args))
    except (CollectionError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"SAI-07 authoritative evidence collection rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
