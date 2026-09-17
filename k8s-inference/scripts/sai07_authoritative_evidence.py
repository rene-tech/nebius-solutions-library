"""Pure reconstruction of SAI-07 provider/backend evidence.

The collector records raw provider responses.  This module independently
checks pagination, scope and cross-collection completeness, derives the IAM
graph and Terraform-state inventory, and evaluates the fail-closed boundary.
It performs no network or subprocess operation and never returns state values.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any

import verify_sai07_custody_manifest_bundle_v2 as manifest_v2

PROVIDER_SCHEMA = "fs2-serve.nebius.ai/sai07-authoritative-provider-evidence/v1"
BACKEND_SCHEMA = "fs2-serve.nebius.ai/sai07-authoritative-backend-evidence/v1"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
MAX_COLLECTION_AGE = dt.timedelta(minutes=10)
SKEW = dt.timedelta(seconds=30)


class EvidenceError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise EvidenceError(f"{label} fields differ from the authoritative evidence contract")
    return value


def nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{label} must be a non-empty string")
    return value


def sha256(value: object, label: str) -> str:
    value = nonempty(value, label)
    if not SHA256_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a lowercase SHA-256")
    return value


def timestamp(value: object, label: str) -> dt.datetime:
    value = nonempty(value, label)
    if not value.endswith("Z"):
        raise EvidenceError(f"{label} must be a UTC RFC3339 instant")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise EvidenceError(f"{label} is malformed") from error
    if parsed.tzinfo != dt.UTC:
        raise EvidenceError(f"{label} must be UTC")
    return parsed


def collection_window(value: dict[str, Any], label: str) -> None:
    started = timestamp(value["started_at"], f"{label}.started_at")
    completed = timestamp(value["completed_at"], f"{label}.completed_at")
    now = dt.datetime.now(dt.UTC)
    if completed < started or completed - started > MAX_COLLECTION_AGE:
        raise EvidenceError(f"{label} collection duration is invalid")
    if started > now + SKEW or now - completed > MAX_COLLECTION_AGE + SKEW:
        raise EvidenceError(f"{label} collection is stale or from the future")


def metadata(value: object, label: str) -> tuple[str, str, str]:
    value = exact(value, set(value) if isinstance(value, dict) else set(), label)
    item = value.get("metadata")
    if not isinstance(item, dict):
        raise EvidenceError(f"{label} omits metadata")
    identifier = nonempty(item.get("id"), f"{label}.metadata.id")
    parent = nonempty(item.get("parent_id"), f"{label}.metadata.parent_id")
    version = item.get("resource_version")
    if isinstance(version, int) and not isinstance(version, bool):
        version = str(version)
    version = nonempty(version, f"{label}.metadata.resource_version")
    return identifier, parent, version


def unary(
    value: object,
    *,
    service: str,
    method: str,
    request: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    item = exact(value, {"method", "provenance", "request", "response", "service"}, label)
    if item["service"] != service or item["method"] != method or item["request"] != request:
        raise EvidenceError(f"{label} is not the exact source-pinned provider operation")
    provenance = exact(item["provenance"], {"request_id", "trace_id"}, f"{label} provenance")
    nonempty(provenance["request_id"], f"{label} provider request ID")
    nonempty(provenance["trace_id"], f"{label} provider trace ID")
    if not isinstance(item["response"], dict):
        raise EvidenceError(f"{label} response is not an object")
    return item["response"]


def paged(
    value: object,
    *,
    service: str,
    method: str,
    fixed_request: dict[str, Any],
    page_size: int,
    max_pages: int,
    item_field: str,
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > max_pages:
        raise EvidenceError(f"{label} page sequence is empty or out of bounds")
    token = ""
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        entry = exact(
            raw,
            {"method", "provenance", "request", "response", "service"},
            f"{label}[{index}]",
        )
        if entry["service"] != service or entry["method"] != method:
            raise EvidenceError(f"{label}[{index}] uses another provider operation")
        provenance = exact(
            entry["provenance"], {"request_id", "trace_id"}, f"{label}[{index}] provenance"
        )
        nonempty(provenance["request_id"], f"{label}[{index}] provider request ID")
        nonempty(provenance["trace_id"], f"{label}[{index}] provider trace ID")
        expected = {**fixed_request, "filter": "", "page_size": page_size, "page_token": token}
        if entry["request"] != expected:
            raise EvidenceError(f"{label}[{index}] request breaks the exhaustive pagination chain")
        response = entry["response"]
        if not isinstance(response, dict):
            raise EvidenceError(f"{label}[{index}] response is not an object")
        page_items = response.get(item_field)
        next_token = response.get("next_page_token", "")
        if not isinstance(page_items, list) or not isinstance(next_token, str):
            raise EvidenceError(f"{label}[{index}] response pagination fields are malformed")
        if token in seen or (next_token and next_token in seen):
            raise EvidenceError(f"{label} repeats a page token")
        seen.add(token)
        if len(page_items) > page_size:
            raise EvidenceError(f"{label}[{index}] exceeds the requested page size")
        if index < len(value) - 1 and not next_token:
            raise EvidenceError(f"{label} contains pages after a terminal response")
        if index == len(value) - 1 and next_token:
            raise EvidenceError(f"{label} omits a nonterminal provider page")
        for item in page_items:
            if not isinstance(item, dict):
                raise EvidenceError(f"{label}[{index}] contains a non-object item")
            items.append(item)
        token = next_token
    return items


def unique_resources(items: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        identifier, _, _ = metadata(item, f"{label}[{index}]")
        if identifier in result:
            raise EvidenceError(f"{label} contains duplicate resource ID {identifier}")
        result[identifier] = item
    return result


def account_id(value: object, label: str) -> str:
    if not isinstance(value, dict) or len(value) != 1:
        raise EvidenceError(f"{label} account selector is malformed")
    kind, item = next(iter(value.items()))
    if kind not in {"service_account", "user_account"} or not isinstance(item, dict):
        raise EvidenceError(f"{label} account selector has an unsupported kind")
    return nonempty(item.get("id"), f"{label}.account.id")


def provider_projection(artifact: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    exact(
        artifact,
        {
            "calls",
            "collection_id",
            "collector",
            "completed_at",
            "schema",
            "scope",
            "started_at",
            "whoami_principal_id",
        },
        "provider artifact",
    )
    if artifact["schema"] != PROVIDER_SCHEMA:
        raise EvidenceError("provider artifact schema is unsupported")
    collection_window(artifact, "provider artifact")
    collector = exact(
        artifact["collector"],
        {"boto3_version", "nebius_sdk_version", "source_sha256"},
        "provider artifact collector",
    )
    pins = contract["collector"]
    if (
        collector["source_sha256"] != pins["source_sha256"]
        or collector["nebius_sdk_version"] != pins["package_versions"]["nebius"]
        or collector["boto3_version"] != pins["package_versions"]["boto3"]
    ):
        raise EvidenceError("provider artifact was produced by an unreviewed adapter")
    scope = exact(
        artifact["scope"],
        {"backend_bucket_resource_id", "project_id", "tenant_id"},
        "provider artifact scope",
    )
    contract_scope = exact(
        contract["scope"],
        {
            "backend_bucket",
            "backend_bucket_resource_id",
            "backend_endpoint",
            "backend_region",
            "platform_state_key",
            "project_id",
            "tenant_id",
        },
        "provider/backend contract scope",
    )
    for field in scope:
        if scope[field] != contract_scope[field]:
            raise EvidenceError(f"provider artifact {field} differs from the repository contract")
    expected = exact(
        contract["expected"],
        {
            "authority_required_permits",
            "backend_collector_principal_id",
            "cluster_id",
            "kube_system_uid",
            "minimum_retention_days",
            "native_bucket_rules_sha256",
            "owner",
            "owner_required_permits",
            "platform",
            "protected_resource_ids",
            "provider_collector_principal_id",
            "receipt_operator",
            "receipt_required_permits",
            "s3_bucket_policy_sha256",
            "s3_canonical_owner_id",
        },
        "provider/backend contract expectations",
    )
    if artifact["whoami_principal_id"] != expected["provider_collector_principal_id"]:
        raise EvidenceError("provider collector identity differs from the repository contract")
    calls = exact(
        artifact["calls"],
        {
            "access_keys",
            "access_permits",
            "auth_public_keys",
            "bucket",
            "federated_credentials_by_project",
            "group_members",
            "groups_by_parent",
            "member_of",
            "profile",
            "project",
            "projects",
            "service_accounts_by_project",
            "static_keys",
            "tenant",
            "tenant_users",
            "tenants",
        },
        "provider calls",
    )
    profile = unary(
        calls["profile"],
        service="nebius.iam.v1.ProfileService",
        method="Get",
        request={},
        label="provider collector profile",
    )
    service_account_profile = profile.get("service_account_profile")
    user_profile = profile.get("user_profile")
    if isinstance(service_account_profile, dict) and user_profile in (None, {}):
        info = service_account_profile.get("info")
        if not isinstance(info, dict):
            raise EvidenceError("provider service-account profile omits account information")
        profile_principal_id, _, _ = metadata(info, "provider collector service account")
    elif isinstance(user_profile, dict) and service_account_profile in (None, {}):
        matches = [
            item.get("tenant_user_account_id")
            for item in user_profile.get("tenants", [])
            if isinstance(item, dict) and item.get("tenant_id") == scope["tenant_id"]
        ]
        if len(matches) != 1:
            raise EvidenceError("provider user profile does not resolve exactly one in-scope principal")
        profile_principal_id = nonempty(matches[0], "provider profile tenant principal")
    else:
        raise EvidenceError("provider collector profile is anonymous, ambiguous, or unsupported")
    if profile_principal_id != artifact["whoami_principal_id"]:
        raise EvidenceError("provider artifact identity is not derived from its raw profile response")
    tenant = unary(
        calls["tenant"],
        service="nebius.iam.v1.TenantService",
        method="Get",
        request={"id": scope["tenant_id"]},
        label="tenant get",
    )
    project = unary(
        calls["project"],
        service="nebius.iam.v1.ProjectService",
        method="Get",
        request={"id": scope["project_id"]},
        label="project get",
    )
    bucket = unary(
        calls["bucket"],
        service="nebius.storage.v1.BucketService",
        method="Get",
        request={"id": scope["backend_bucket_resource_id"]},
        label="bucket get",
    )
    tenant_id, tenant_parent, tenant_version = metadata(tenant, "tenant")
    project_id, project_parent, project_version = metadata(project, "project")
    bucket_id, bucket_parent, bucket_version = metadata(bucket, "bucket")
    if (
        tenant_id != scope["tenant_id"]
        or project_id != scope["project_id"]
        or bucket_id != scope["backend_bucket_resource_id"]
    ):
        raise EvidenceError("provider unary responses returned another resource")
    if project_parent != tenant_id or bucket_parent != project_id:
        raise EvidenceError("provider project/bucket hierarchy differs from the repository scope")
    page_size = pins["page_size"]
    max_pages = pins["max_pages_per_collection"]
    tenants = unique_resources(
        paged(
            calls["tenants"],
            service="nebius.iam.v1.TenantService",
            method="List",
            fixed_request={},
            page_size=page_size,
            max_pages=max_pages,
            item_field="items",
            label="tenant list",
        ),
        "tenant list",
    )
    projects = unique_resources(
        paged(
            calls["projects"],
            service="nebius.iam.v1.ProjectService",
            method="List",
            fixed_request={"parent_id": tenant_id},
            page_size=page_size,
            max_pages=max_pages,
            item_field="items",
            label="project list",
        ),
        "project list",
    )
    if tenant_id not in tenants or project_id not in projects:
        raise EvidenceError("provider scope is absent from its authoritative parent enumeration")
    tenant_inventory = []
    for item_id, item in tenants.items():
        _, item_parent, item_version = metadata(item, f"visible tenant {item_id}")
        tenant_inventory.append(
            {"parent_id": item_parent, "resource_id": item_id, "resource_version": item_version}
        )
    project_inventory = []
    for item_id, item in projects.items():
        _, item_parent, item_version = metadata(item, f"tenant project {item_id}")
        if item_parent != tenant_id:
            raise EvidenceError("project response is outside the enumerated tenant")
        project_inventory.append(
            {"parent_id": item_parent, "resource_id": item_id, "resource_version": item_version}
        )
    service_account_calls = calls["service_accounts_by_project"]
    if not isinstance(service_account_calls, dict) or set(service_account_calls) != set(projects):
        raise EvidenceError("service-account enumeration does not cover every tenant project")
    service_accounts: dict[str, dict[str, Any]] = {}
    for parent in sorted(projects):
        found = unique_resources(
            paged(
                service_account_calls[parent],
                service="nebius.iam.v1.ServiceAccountService",
                method="List",
                fixed_request={"parent_id": parent},
                page_size=page_size,
                max_pages=max_pages,
                item_field="items",
                label=f"service-account list {parent}",
            ),
            f"service-account list {parent}",
        )
        if set(service_accounts).intersection(found):
            raise EvidenceError("service-account resource ID appears under multiple projects")
        for item_id, item in found.items():
            _, item_parent, _ = metadata(item, f"service account {item_id}")
            if item_parent != parent:
                raise EvidenceError("service-account response is outside its enumerated project")
        service_accounts.update(found)
    tenant_users = unique_resources(
        paged(
            calls["tenant_users"],
            service="nebius.iam.v1.TenantUserAccountService",
            method="List",
            fixed_request={"parent_id": tenant_id},
            page_size=page_size,
            max_pages=max_pages,
            item_field="items",
            label="tenant-user list",
        ),
        "tenant-user list",
    )
    for item_id, item in tenant_users.items():
        _, item_parent, _ = metadata(item, f"tenant user {item_id}")
        if item_parent != tenant_id:
            raise EvidenceError("tenant-user response is outside its enumerated tenant")
    principals = {**service_accounts, **tenant_users}
    if len(principals) != len(service_accounts) + len(tenant_users):
        raise EvidenceError("provider principal resource IDs overlap")

    groups_by_parent = calls["groups_by_parent"]
    group_parents = {tenant_id} | set(projects)
    if not isinstance(groups_by_parent, dict) or set(groups_by_parent) != group_parents:
        raise EvidenceError("group enumeration does not cover the tenant and every tenant project")
    groups: dict[str, dict[str, Any]] = {}
    for parent in sorted(group_parents):
        found = unique_resources(
            paged(
                groups_by_parent[parent],
                service="nebius.iam.v1.GroupService",
                method="List",
                fixed_request={"parent_id": parent},
                page_size=page_size,
                max_pages=max_pages,
                item_field="items",
                label=f"group list {parent}",
            ),
            f"group list {parent}",
        )
        if set(groups).intersection(found):
            raise EvidenceError("group resource ID appears under multiple parents")
        for item_id, item in found.items():
            _, item_parent, _ = metadata(item, f"group {item_id}")
            if item_parent != parent:
                raise EvidenceError("group response is outside its enumerated parent")
        groups.update(found)

    group_members_calls = calls["group_members"]
    member_of_calls = calls["member_of"]
    permit_calls = calls["access_permits"]
    if not isinstance(group_members_calls, dict) or set(group_members_calls) != set(groups):
        raise EvidenceError("group-member enumeration is not exhaustive")
    if not isinstance(member_of_calls, dict) or set(member_of_calls) != set(principals):
        raise EvidenceError("member-of enumeration is not exhaustive")
    if not isinstance(permit_calls, dict) or set(permit_calls) != set(principals) | set(groups):
        raise EvidenceError("access-permit enumeration is not exhaustive")

    group_edges: set[tuple[str, str]] = set()
    for group_id in sorted(groups):
        members = paged(
            group_members_calls[group_id],
            service="nebius.iam.v1.GroupMembershipService",
            method="ListMembers",
            fixed_request={"parent_id": group_id},
            page_size=page_size,
            max_pages=max_pages,
            item_field="memberships",
            label=f"members of {group_id}",
        )
        for index, membership in enumerate(members):
            _, parent, _ = metadata(membership, f"members of {group_id}[{index}]")
            member_id = nonempty(membership.get("spec", {}).get("member_id"), "group member ID")
            if parent != group_id or member_id not in principals:
                raise EvidenceError("group membership points outside the enumerated principal set")
            group_edges.add((member_id, group_id))

    reverse_edges: set[tuple[str, str]] = set()
    for principal_id in sorted(principals):
        member_groups = paged(
            member_of_calls[principal_id],
            service="nebius.iam.v1.GroupMembershipService",
            method="ListMemberOf",
            fixed_request={"subject_id": principal_id},
            page_size=page_size,
            max_pages=max_pages,
            item_field="items",
            label=f"member-of {principal_id}",
        )
        for index, group in enumerate(member_groups):
            group_id, _, _ = metadata(group, f"member-of {principal_id}[{index}]")
            if group_id not in groups:
                raise EvidenceError("member-of response references a group outside the exhaustive group list")
            reverse_edges.add((principal_id, group_id))
    if reverse_edges != group_edges:
        raise EvidenceError("forward and reverse group membership enumerations differ")

    permits: list[dict[str, str]] = []
    for subject_id in sorted(set(principals) | set(groups)):
        items = paged(
            permit_calls[subject_id],
            service="nebius.iam.v1.AccessPermitService",
            method="List",
            fixed_request={"parent_id": subject_id},
            page_size=page_size,
            max_pages=max_pages,
            item_field="items",
            label=f"access permits {subject_id}",
        )
        for index, item in enumerate(items):
            permit_id, parent, version = metadata(item, f"access permits {subject_id}[{index}]")
            spec = item.get("spec")
            if parent != subject_id or not isinstance(spec, dict):
                raise EvidenceError("access permit subject differs from its exhaustive list parent")
            permits.append(
                {
                    "permit_id": permit_id,
                    "resource_id": nonempty(spec.get("resource_id"), "access permit resource_id"),
                    "resource_version": version,
                    "role": nonempty(spec.get("role"), "access permit role"),
                    "subject_id": subject_id,
                }
            )
    permits.sort(key=lambda item: canonical(item))

    credential_collections: dict[str, list[dict[str, Any]]] = {}
    access_key_owners: dict[str, str] = {}
    for field, service, method in (
        ("access_keys", "nebius.iam.v2.AccessKeyService", "ListByAccount"),
        ("auth_public_keys", "nebius.iam.v1.AuthPublicKeyService", "ListByAccount"),
    ):
        raw = calls[field]
        if not isinstance(raw, dict) or set(raw) != set(principals):
            raise EvidenceError(f"{field} enumeration is not exhaustive")
        normalized: list[dict[str, Any]] = []
        for principal_id in sorted(principals):
            account_kind = "service_account" if principal_id in service_accounts else "user_account"
            account_selector = {account_kind: {"id": principal_id}}
            items = paged(
                raw[principal_id],
                service=service,
                method=method,
                fixed_request={"account": account_selector},
                page_size=page_size,
                max_pages=max_pages,
                item_field="items",
                label=f"{field} {principal_id}",
            )
            for index, item in enumerate(items):
                key_id, _, version = metadata(item, f"{field} {principal_id}[{index}]")
                if account_id(item.get("spec", {}).get("account"), f"{field} {key_id}") != principal_id:
                    raise EvidenceError(f"{field} item is bound to another account")
                status = item.get("status", {})
                if not isinstance(status, dict):
                    raise EvidenceError(f"{field} {key_id} status is malformed")
                if field == "access_keys" and status.get("secret") not in (None, ""):
                    raise EvidenceError("provider evidence contains an access-key secret")
                public_access_key = status.get("aws_access_key_id") if field == "access_keys" else None
                if field == "access_keys":
                    public_access_key = nonempty(public_access_key, f"access key {key_id} public identifier")
                    if public_access_key in access_key_owners:
                        raise EvidenceError("provider evidence contains a duplicate public access-key identifier")
                    access_key_owners[public_access_key] = principal_id
                normalized.append(
                    {
                        "account_id": principal_id,
                        "access_key_id": public_access_key,
                        "credential_id": key_id,
                        "resource_version": version,
                        "status_sha256": hashlib.sha256(canonical(status)).hexdigest(),
                    }
                )
        credential_collections[field] = sorted(normalized, key=lambda item: canonical(item))

    static_calls = calls["static_keys"]
    if not isinstance(static_calls, dict) or set(static_calls) != set(service_accounts):
        raise EvidenceError("static-key enumeration is not exhaustive for every service account")
    static_items: list[dict[str, Any]] = []
    for principal_id in sorted(service_accounts):
        static_items.extend(
            paged(
                static_calls[principal_id],
                service="nebius.iam.v1.StaticKeyService",
                method="List",
                fixed_request={"parent_id": principal_id},
                page_size=page_size,
                max_pages=max_pages,
                item_field="items",
                label=f"static-key list {principal_id}",
            )
        )
    federated_calls = calls["federated_credentials_by_project"]
    if not isinstance(federated_calls, dict) or set(federated_calls) != set(projects):
        raise EvidenceError("federated-credential enumeration does not cover every tenant project")
    federated_items: list[dict[str, Any]] = []
    for parent in sorted(projects):
        items = paged(
            federated_calls[parent],
            service="nebius.iam.v1.FederatedCredentialsService",
            method="List",
            fixed_request={"parent_id": parent},
            page_size=page_size,
            max_pages=max_pages,
            item_field="items",
            label=f"federated-credential list {parent}",
        )
        for index, item in enumerate(items):
            _, item_parent, _ = metadata(item, f"federated credential {parent}[{index}]")
            if item_parent != parent:
                raise EvidenceError("federated credential is outside its enumerated project")
        federated_items.extend(items)
    additional_credentials: list[dict[str, str]] = []
    for kind, items in (("static_key", static_items), ("federated_credential", federated_items)):
        for index, item in enumerate(items):
            identifier, parent, version = metadata(item, f"{kind}[{index}]")
            expected_parents = set(service_accounts) if kind == "static_key" else set(projects)
            if parent not in expected_parents:
                raise EvidenceError(f"{kind} is outside the exhaustively enumerated tenant scope")
            additional_credentials.append(
                {
                    "credential_id": identifier,
                    "kind": kind,
                    "resource_version": version,
                    "spec_sha256": hashlib.sha256(canonical(item.get("spec", {}))).hexdigest(),
                }
            )

    identities = {
        "owner": expected["owner"],
        "platform": expected["platform"],
        "receipt_operator": expected["receipt_operator"],
    }
    for label, identity in identities.items():
        identity = exact(identity, {"group_ids", "principal_ids", "username"}, f"expected {label}")
        if not identity["principal_ids"] or identity["principal_ids"] != sorted(set(identity["principal_ids"])):
            raise EvidenceError(f"expected {label} principal set is empty or duplicated")
        if identity["group_ids"] != sorted(set(identity["group_ids"])):
            raise EvidenceError(f"expected {label} group set is duplicated")
        if not set(identity["principal_ids"]).issubset(principals):
            raise EvidenceError(f"expected {label} principal is absent from authoritative enumeration")
        for principal_id in identity["principal_ids"]:
            actual_groups = sorted(group_id for member_id, group_id in group_edges if member_id == principal_id)
            if actual_groups != identity["group_ids"]:
                raise EvidenceError(f"expected {label} group membership differs from provider evidence")

    authorities = contract["authorities"]
    authority_principals = []
    for label in ("provider_receipt", "backend_receipt", "manifest"):
        authority = exact(
            authorities[label],
            {"key_id", "principal_id", "public_key_path", "public_key_sha256"},
            f"{label} authority",
        )
        principal_id = nonempty(authority["principal_id"], f"{label} authority principal")
        if principal_id not in principals:
            raise EvidenceError(f"{label} authority is absent from the exhaustive principal enumeration")
        authority_principals.append(principal_id)
    if len(set(authority_principals)) != 3 or set(authority_principals).intersection(
        expected["platform"]["principal_ids"]
    ):
        raise EvidenceError("receipt/manifest authorities are not three distinct non-platform principals")

    protected = set(expected["protected_resource_ids"])
    scope_ancestors = {tenant_id, project_id, bucket_id}
    if not scope_ancestors.issubset(protected):
        raise EvidenceError("protected resource set omits a custody scope or inheritance ancestor")
    platform_subjects = set(expected["platform"]["principal_ids"]) | set(expected["platform"]["group_ids"])
    if not protected or any(
        item["subject_id"] in platform_subjects and item["resource_id"] in protected for item in permits
    ):
        raise EvidenceError("platform authority reaches an external custody resource")
    permit_edges = {(item["subject_id"], item["resource_id"], item["role"]) for item in permits}
    for label, field, identity_field in (
        ("owner", "owner_required_permits", "owner"),
        ("receipt operator", "receipt_required_permits", "receipt_operator"),
    ):
        required = expected[field]
        if not isinstance(required, list) or not required:
            raise EvidenceError(f"{label} required permit set is empty")
        identity = expected[identity_field]
        identity_subjects = set(identity["principal_ids"]) | set(identity["group_ids"])
        for item in required:
            entry = exact(item, {"resource_id", "role", "subject_id"}, f"{label} required permit")
            if entry["subject_id"] not in identity_subjects:
                raise EvidenceError(f"{label} required permit is not bound to its claimed identity")
            if (entry["subject_id"], entry["resource_id"], entry["role"]) not in permit_edges:
                raise EvidenceError(f"{label} required provider permit is absent")
    authority_permits = expected.get("authority_required_permits")
    if not isinstance(authority_permits, dict) or set(authority_permits) != {
        "backend_receipt",
        "manifest",
        "provider_receipt",
    }:
        raise EvidenceError("authority required-permit contract is incomplete")
    for label, principal_id in zip(
        ("provider_receipt", "backend_receipt", "manifest"), authority_principals, strict=True
    ):
        principal_groups = {group_id for member_id, group_id in group_edges if member_id == principal_id}
        authority_subjects = {principal_id} | principal_groups
        required = authority_permits[label]
        if not isinstance(required, list) or not required:
            raise EvidenceError(f"{label} authority required permit set is empty")
        for item in required:
            entry = exact(item, {"resource_id", "role", "subject_id"}, f"{label} required permit")
            if entry["subject_id"] not in authority_subjects:
                raise EvidenceError(f"{label} required permit is not bound to its provider identity")
            if (entry["subject_id"], entry["resource_id"], entry["role"]) not in permit_edges:
                raise EvidenceError(f"{label} required provider permit is absent")

    bucket_spec = bucket.get("spec")
    bucket_status = bucket.get("status")
    if not isinstance(bucket_spec, dict) or not isinstance(bucket_status, dict):
        raise EvidenceError("backend bucket omits provider-native spec/status")
    native_rules = bucket_spec.get("bucket_policy", {}).get("rules", [])
    if not isinstance(native_rules, list) or any(
        "anonymous" in rule for rule in native_rules if isinstance(rule, dict)
    ):
        raise EvidenceError("backend bucket has anonymous or malformed provider-native policy")
    if hashlib.sha256(canonical(native_rules)).hexdigest() != expected["native_bucket_rules_sha256"]:
        raise EvidenceError("backend provider-native policy differs from the reviewed rule set")
    if bucket_spec.get("versioning_policy") != "ENABLED" or bucket_status.get("state") != "ACTIVE":
        raise EvidenceError("backend bucket is not active with versioning enabled")

    resource_versions = {
        "backend_bucket": bucket_version,
        "project": project_version,
        "tenant": tenant_version,
    }
    projection = {
        "additional_credentials": sorted(additional_credentials, key=lambda item: canonical(item)),
        "credentials": credential_collections,
        "group_memberships": [list(edge) for edge in sorted(group_edges)],
        "groups": sorted(groups),
        "permits": permits,
        "principals": sorted(principals),
        "projects": sorted(project_inventory, key=lambda item: canonical(item)),
        "provider_resource_versions": resource_versions,
        "scope": scope,
        "visible_tenants": sorted(tenant_inventory, key=lambda item: canonical(item)),
    }
    return {
        "access_key_owners": access_key_owners,
        "collection_id": artifact["collection_id"],
        "completed_at": artifact["completed_at"],
        "projection": projection,
        "projection_sha256": hashlib.sha256(canonical(projection)).hexdigest(),
    }


def s3_response(calls: dict[str, Any], field: str, expected_request: dict[str, Any]) -> dict[str, Any]:
    item = exact(calls.get(field), {"method", "request", "response", "service"}, f"S3 {field}")
    expected_method = "head_object" if field == "head_object_after" else field
    if item["service"] != "s3" or item["method"] != expected_method or item["request"] != expected_request:
        raise EvidenceError(f"S3 {field} is not the exact source-pinned read operation")
    response = item["response"]
    if not isinstance(response, dict):
        raise EvidenceError(f"S3 {field} response is not an object")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if not isinstance(status, int) or status < 200 or status >= 300:
        raise EvidenceError(f"S3 {field} did not return a successful authoritative response")
    nonempty(response.get("ResponseMetadata", {}).get("RequestId"), f"S3 {field} request ID")
    return response


def terraform_address(resource: dict[str, Any], instance: dict[str, Any]) -> str:
    prefix = resource.get("module")
    base = f"{resource.get('type')}.{resource.get('name')}"
    if isinstance(prefix, str) and prefix:
        base = f"{prefix}.{base}"
    if "index_key" not in instance:
        return base
    index = instance["index_key"]
    if isinstance(index, str):
        return f"{base}[{json.dumps(index, ensure_ascii=True)}]"
    if isinstance(index, int) and not isinstance(index, bool):
        return f"{base}[{index}]"
    raise EvidenceError(f"Terraform state address {base} has an unsupported index key")


def state_projection(state_bytes: bytes) -> dict[str, Any]:
    try:
        state = json.loads(state_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError("platform Terraform state is not JSON") from error
    if not isinstance(state, dict) or state.get("version") != 4:
        raise EvidenceError("platform Terraform state is not version 4")
    lineage = nonempty(state.get("lineage"), "platform state lineage")
    serial = state.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 0:
        raise EvidenceError("platform state serial is invalid")
    resources = state.get("resources")
    if not isinstance(resources, list):
        raise EvidenceError("platform state resources are missing")
    all_addresses: set[str] = set()
    custody_addresses: set[str] = set()
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("mode") != "managed":
            continue
        instances = resource.get("instances")
        if not isinstance(instances, list):
            raise EvidenceError("managed state resource has no instance list")
        for instance in instances:
            if not isinstance(instance, dict):
                raise EvidenceError("managed state instance is not an object")
            address = terraform_address(resource, instance)
            if address in all_addresses:
                raise EvidenceError(f"platform state contains duplicate address {address}")
            all_addresses.add(address)
            if address in manifest_v2.STATIC_STATE or manifest_v2.DYNAMIC_ADDRESS_RE.fullmatch(address):
                custody_addresses.add(address)
    if not set(manifest_v2.STATIC_STATE).issubset(custody_addresses):
        raise EvidenceError("platform state omits a static retained custody address")
    return {
        "custody_addresses": sorted(custody_addresses),
        "custody_addresses_sha256": hashlib.sha256(canonical(sorted(custody_addresses))).hexdigest(),
        "custody_object_count": len(custody_addresses),
        "lineage": lineage,
        "serial": serial,
    }


def backend_projection(
    artifact: dict[str, Any], state_bytes: bytes, contract: dict[str, Any]
) -> dict[str, Any]:
    exact(
        artifact,
        {
            "calls",
            "caller_access_key_id",
            "collection_id",
            "collector",
            "completed_at",
            "schema",
            "scope",
            "started_at",
            "state",
        },
        "backend artifact",
    )
    if artifact["schema"] != BACKEND_SCHEMA:
        raise EvidenceError("backend artifact schema is unsupported")
    collection_window(artifact, "backend artifact")
    pins = contract["collector"]
    collector = exact(
        artifact["collector"],
        {"boto3_version", "botocore_version", "source_sha256"},
        "backend artifact collector",
    )
    if (
        collector["source_sha256"] != pins["source_sha256"]
        or collector["boto3_version"] != pins["package_versions"]["boto3"]
        or collector["botocore_version"] != pins["package_versions"]["botocore"]
    ):
        raise EvidenceError("backend artifact was produced by an unreviewed adapter")
    scope = exact(artifact["scope"], {"bucket", "endpoint", "key", "region"}, "backend artifact scope")
    scope_contract = contract["scope"]
    for field, contract_field in (
        ("bucket", "backend_bucket"),
        ("endpoint", "backend_endpoint"),
        ("key", "platform_state_key"),
        ("region", "backend_region"),
    ):
        if scope[field] != scope_contract[contract_field]:
            raise EvidenceError(f"backend artifact {field} differs from the repository contract")
    state = exact(artifact["state"], {"bytes", "etag", "sha256", "version_id"}, "backend artifact state")
    caller_access_key_id = nonempty(artifact["caller_access_key_id"], "backend caller access-key ID")
    provider_access_keys = contract.get("verified_provider_access_keys")
    if (
        not isinstance(provider_access_keys, dict)
        or provider_access_keys.get(caller_access_key_id)
        != contract["expected"]["backend_collector_principal_id"]
    ):
        raise EvidenceError("backend caller access-key identity is not bound to provider evidence")
    if state["bytes"] != len(state_bytes) or state["sha256"] != hashlib.sha256(state_bytes).hexdigest():
        raise EvidenceError("raw platform state bytes differ from the authoritative backend artifact")
    sha256(state["sha256"], "backend state SHA-256")
    bucket = scope["bucket"]
    key = scope["key"]
    version = nonempty(state["version_id"], "backend state version ID")
    calls = artifact["calls"]
    expected_calls = {
        "get_bucket_acl",
        "get_bucket_encryption",
        "get_bucket_policy",
        "get_bucket_policy_status",
        "get_bucket_versioning",
        "get_object",
        "get_object_legal_hold",
        "get_object_lock_configuration",
        "get_object_retention",
        "get_object_tagging",
        "head_bucket",
        "head_object",
        "head_object_after",
    }
    if not isinstance(calls, dict) or set(calls) != expected_calls:
        raise EvidenceError("backend evidence does not contain the exact authoritative S3 operation set")
    s3_response(calls, "head_bucket", {"Bucket": bucket})
    acl = s3_response(calls, "get_bucket_acl", {"Bucket": bucket})
    encryption = s3_response(calls, "get_bucket_encryption", {"Bucket": bucket})
    policy_response = s3_response(calls, "get_bucket_policy", {"Bucket": bucket})
    policy_status = s3_response(calls, "get_bucket_policy_status", {"Bucket": bucket})
    versioning = s3_response(calls, "get_bucket_versioning", {"Bucket": bucket})
    object_lock = s3_response(calls, "get_object_lock_configuration", {"Bucket": bucket})
    head = s3_response(calls, "head_object", {"Bucket": bucket, "Key": key})
    retained = s3_response(
        calls, "get_object_retention", {"Bucket": bucket, "Key": key, "VersionId": version}
    )
    legal_hold = s3_response(
        calls, "get_object_legal_hold", {"Bucket": bucket, "Key": key, "VersionId": version}
    )
    s3_response(calls, "get_object_tagging", {"Bucket": bucket, "Key": key, "VersionId": version})
    downloaded = s3_response(calls, "get_object", {"Bucket": bucket, "Key": key, "VersionId": version})
    after = s3_response(
        calls, "head_object_after", {"Bucket": bucket, "Key": key, "VersionId": version}
    )
    for response in (head, downloaded, after):
        if (
            response.get("VersionId") != version
            or response.get("ETag") != state["etag"]
            or response.get("ContentLength") != state["bytes"]
        ):
            raise EvidenceError("platform state version/ETag/length drifted across backend reads")
    if versioning.get("Status") != "Enabled":
        raise EvidenceError("custody backend bucket versioning is not enabled")
    rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules")
    if not isinstance(rules, list) or not rules:
        raise EvidenceError("custody backend has no server-side encryption rule")
    algorithms = {
        rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
        for rule in rules
        if isinstance(rule, dict)
    }
    if not algorithms or not algorithms.issubset({"AES256", "aws:kms"}):
        raise EvidenceError("custody backend encryption algorithm is unsupported")
    lock_config = object_lock.get("ObjectLockConfiguration", {})
    default_retention = lock_config.get("Rule", {}).get("DefaultRetention", {})
    if lock_config.get("ObjectLockEnabled") != "Enabled" or default_retention.get("Mode") != "COMPLIANCE":
        raise EvidenceError("custody backend lacks COMPLIANCE object lock")
    duration_days = default_retention.get("Days", 0) + default_retention.get("Years", 0) * 365
    if not isinstance(duration_days, int) or duration_days < contract["expected"]["minimum_retention_days"]:
        raise EvidenceError("custody backend default retention is below the reviewed minimum")
    retention = retained.get("Retention", {})
    retain_until = timestamp(retention.get("RetainUntilDate"), "state object RetainUntilDate")
    if retention.get("Mode") != "COMPLIANCE" or retain_until <= dt.datetime.now(dt.UTC) + dt.timedelta(
        days=contract["expected"]["minimum_retention_days"] - 1
    ):
        raise EvidenceError("platform state version lacks sufficient COMPLIANCE retention")
    if legal_hold.get("LegalHold", {}).get("Status") != "ON":
        raise EvidenceError("platform state version legal hold is not ON")
    if policy_status.get("PolicyStatus", {}).get("IsPublic") is not False:
        raise EvidenceError("custody backend policy is public or indeterminate")
    for grant in acl.get("Grants", []):
        grantee = grant.get("Grantee", {}) if isinstance(grant, dict) else {}
        if grantee.get("URI") in {
            "http://acs.amazonaws.com/groups/global/AllUsers",
            "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
        }:
            raise EvidenceError("custody backend ACL grants a global principal")
    owner_id = acl.get("Owner", {}).get("ID")
    if owner_id != contract["expected"]["s3_canonical_owner_id"]:
        raise EvidenceError("custody backend canonical owner differs from the repository contract")
    try:
        policy = json.loads(policy_response["Policy"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise EvidenceError("custody backend policy is not JSON") from error
    policy_sha = hashlib.sha256(canonical(policy)).hexdigest()
    if policy_sha != contract["expected"]["s3_bucket_policy_sha256"]:
        raise EvidenceError("custody backend policy differs from the repository-pinned boundary")
    derived_state = state_projection(state_bytes)
    projection = {
        "bucket_acl_owner": owner_id,
        "caller_access_key_id": caller_access_key_id,
        "bucket_encryption_algorithms": sorted(algorithms),
        "bucket_policy_sha256": policy_sha,
        "bucket_versioning": versioning["Status"],
        "object_lock": {
            "default_retention_days": duration_days,
            "legal_hold": legal_hold["LegalHold"]["Status"],
            "mode": default_retention["Mode"],
        },
        "scope": scope,
        "state": {
            **derived_state,
            "bytes": len(state_bytes),
            "etag": state["etag"],
            "sha256": state["sha256"],
            "version_id": version,
        },
    }
    return {
        "collection_id": artifact["collection_id"],
        "completed_at": artifact["completed_at"],
        "projection": projection,
        "projection_sha256": hashlib.sha256(canonical(projection)).hexdigest(),
    }
