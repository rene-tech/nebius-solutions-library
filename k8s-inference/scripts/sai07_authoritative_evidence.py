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

import sai07_custody_state_semantics as state_semantics
import verify_sai07_custody_manifest_bundle_v2 as manifest_v2

PROVIDER_SCHEMA = "fs2-serve.nebius.ai/sai07-authoritative-provider-evidence/v3"
BACKEND_SCHEMA = "fs2-serve.nebius.ai/sai07-authoritative-backend-evidence/v3"
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


def require_active(value: dict[str, Any], label: str) -> None:
    status = value.get("status")
    if not isinstance(status, dict) or status.get("state") != "ACTIVE":
        raise EvidenceError(f"{label} is not active in authoritative provider evidence")


def transitive_group_closure(
    subject_id: str, memberships: set[tuple[str, str]]
) -> set[str]:
    parents: dict[str, set[str]] = {}
    for member_id, group_id in memberships:
        parents.setdefault(member_id, set()).add(group_id)
    closure: set[str] = set()
    pending = list(parents.get(subject_id, set()))
    while pending:
        group_id = pending.pop()
        if group_id == subject_id:
            raise EvidenceError("group membership graph contains a cycle")
        if group_id in closure:
            continue
        closure.add(group_id)
        pending.extend(parents.get(group_id, set()))
    for group_id in closure:
        if subject_id in transitive_group_closure_for_group(group_id, parents):
            raise EvidenceError("group membership graph contains a cycle")
    return closure


def transitive_group_closure_for_group(
    group_id: str, parents: dict[str, set[str]]
) -> set[str]:
    closure: set[str] = set()
    pending = list(parents.get(group_id, set()))
    while pending:
        parent = pending.pop()
        if parent in closure:
            continue
        closure.add(parent)
        pending.extend(parents.get(parent, set()))
    return closure


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
            "backend_access_group_id",
            "backend_collector_principal_id",
            "cluster_id",
            "external_kubernetes_authorization_resource_ids",
            "kube_system_uid",
            "minimum_retention_days",
            "namespace_inventory",
            "native_bucket_rules_sha256",
            "owner",
            "owner_required_permits",
            "platform",
            "persistent_volume_names",
            "protected_resource_ids",
            "provider_collector_principal_id",
            "receipt_operator",
            "receipt_required_permits",
            "s3_bucket_policy_sha256",
            "s3_canonical_owner_id",
        },
        "provider/backend contract expectations",
    )
    if (
        not isinstance(expected["namespace_inventory"], list)
        or not expected["namespace_inventory"]
        or expected["namespace_inventory"] != sorted(set(expected["namespace_inventory"]))
        or not all(isinstance(item, str) and item for item in expected["namespace_inventory"])
        or not isinstance(expected["persistent_volume_names"], list)
        or expected["persistent_volume_names"]
        != sorted(set(expected["persistent_volume_names"]))
        or len(expected["persistent_volume_names"]) != 8
        or not all(isinstance(item, str) and item for item in expected["persistent_volume_names"])
    ):
        raise EvidenceError("owner authority inventory is incomplete or non-canonical")
    epoch = exact(
        contract["custody_epoch"],
        {
            "epoch_id",
            "generation",
            "previous_activation_sha256",
            "principal_id",
            "required_group_ids",
            "required_permits",
            "retired_epochs",
            "retirement_mode",
            "status",
        },
        "custody epoch",
    )
    current_principal = nonempty(epoch["principal_id"], "custody epoch principal")
    if (
        artifact["whoami_principal_id"] != current_principal
        or expected["provider_collector_principal_id"] != current_principal
        or expected["backend_collector_principal_id"] != current_principal
    ):
        raise EvidenceError("provider/backend caller is not the unique current custody epoch principal")
    calls = exact(
        artifact["calls"],
        {
            "access_permits",
            "bucket",
            "group_members",
            "groups_by_parent",
            "member_of",
            "profile",
            "project",
            "projects",
            "service_accounts_by_project",
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
    if not isinstance(service_account_profile, dict) or user_profile not in (None, {}):
        raise EvidenceError("provider collector profile is not a unique service-account profile")
    info = service_account_profile.get("info")
    if not isinstance(info, dict):
        raise EvidenceError("provider service-account profile omits account information")
    profile_principal_id, profile_parent_id, profile_version = metadata(
        info, "provider collector service account"
    )
    require_active(info, "provider collector service account profile")
    if profile_parent_id != scope["project_id"]:
        raise EvidenceError("provider profile service account is outside the custody project")
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
    require_active(tenant, "custody tenant")
    require_active(project, "custody project")
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
    current_account = service_accounts.get(current_principal)
    if current_account is None:
        raise EvidenceError("current custody epoch principal is absent from service-account inventory")
    _, current_parent, current_version = metadata(
        current_account, "current custody epoch service account"
    )
    if current_parent != project_id or current_version != profile_version:
        raise EvidenceError("profile and project inventory do not prove one current service-account version")
    require_active(current_account, "current custody epoch service account")
    retired_epochs = epoch["retired_epochs"]
    if not isinstance(retired_epochs, list):
        raise EvidenceError("custody epoch retired generation list is malformed")
    retired_principals: list[str] = []
    retired_epoch_ids: list[str] = []
    retired_generations: list[int] = []
    for index, item in enumerate(retired_epochs):
        item = exact(
            item,
            {"epoch_id", "generation", "principal_id"},
            f"retired custody epoch {index}",
        )
        retired_principals.append(nonempty(item["principal_id"], "retired epoch principal"))
        retired_epoch_ids.append(nonempty(item["epoch_id"], "retired epoch ID"))
        generation = item["generation"]
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise EvidenceError("retired custody epoch generation is invalid")
        retired_generations.append(generation)
    current_generation = epoch["generation"]
    if (
        not isinstance(current_generation, int)
        or isinstance(current_generation, bool)
        or current_generation < 1
        or retired_generations != list(range(1, current_generation))
        or len(set(retired_epoch_ids)) != len(retired_epoch_ids)
        or len(set(retired_principals)) != len(retired_principals)
        or current_principal in retired_principals
    ):
        raise EvidenceError("custody epoch lineage is incomplete, duplicated, or non-monotonic")
    if current_generation == 1:
        if epoch["previous_activation_sha256"] is not None or retired_epochs:
            raise EvidenceError("initial custody epoch unexpectedly claims a predecessor")
    elif not SHA256_RE.fullmatch(str(epoch["previous_activation_sha256"])):
        raise EvidenceError("rotated custody epoch omits the prior activation digest")
    if (
        epoch["status"] != "active-reviewed"
        or epoch["retirement_mode"]
        != "authorization-denied-in-place-credentials-preserved"
    ):
        raise EvidenceError("custody epoch is not an active reviewed non-deleting transition")
    for principal_id in retired_principals:
        account = service_accounts.get(principal_id)
        if account is None:
            raise EvidenceError("retired custody epoch principal was deleted from provider inventory")
        _, retired_parent, _ = metadata(account, f"retired custody principal {principal_id}")
        if retired_parent != project_id:
            raise EvidenceError("retired custody epoch principal moved outside the custody project")
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
    if not isinstance(member_of_calls, dict) or set(member_of_calls) != set(principals) | set(groups):
        raise EvidenceError("principal/group member-of enumeration is not exhaustive")
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
            if parent != group_id or member_id not in set(principals) | set(groups):
                raise EvidenceError("group membership points outside the enumerated principal/group set")
            if member_id == group_id:
                raise EvidenceError("group membership graph contains a self-cycle")
            group_edges.add((member_id, group_id))

    reverse_edges: set[tuple[str, str]] = set()
    for subject_id in sorted(set(principals) | set(groups)):
        member_groups = paged(
            member_of_calls[subject_id],
            service="nebius.iam.v1.GroupMembershipService",
            method="ListMemberOf",
            fixed_request={"subject_id": subject_id},
            page_size=page_size,
            max_pages=max_pages,
            item_field="items",
            label=f"member-of {subject_id}",
        )
        for index, group in enumerate(member_groups):
            group_id, _, _ = metadata(group, f"member-of {subject_id}[{index}]")
            if group_id not in groups:
                raise EvidenceError("member-of response references a group outside the exhaustive group list")
            reverse_edges.add((subject_id, group_id))
    if reverse_edges != group_edges:
        raise EvidenceError("forward and reverse group membership enumerations differ")
    for group_id in groups:
        transitive_group_closure(group_id, group_edges)

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

    permit_edges = {
        (item["subject_id"], item["resource_id"], item["role"]) for item in permits
    }
    if len(permit_edges) != len(permits):
        raise EvidenceError("authoritative provider evidence contains duplicate permit edges")

    def effective_permits(principal_id: str) -> list[dict[str, str]]:
        subjects = {principal_id} | transitive_group_closure(principal_id, group_edges)
        return sorted(
            [
                {"resource_id": resource_id, "role": role, "subject_id": subject_id}
                for subject_id, resource_id, role in permit_edges
                if subject_id in subjects
            ],
            key=canonical,
        )

    current_group_closure = sorted(transitive_group_closure(current_principal, group_edges))
    current_effective_permits = effective_permits(current_principal)
    required_groups = epoch["required_group_ids"]
    required_permits = epoch["required_permits"]
    if (
        not isinstance(required_groups, list)
        or required_groups != sorted(set(required_groups))
        or current_group_closure != required_groups
    ):
        raise EvidenceError("current custody epoch group closure differs from the exact contract")
    if not isinstance(required_permits, list):
        raise EvidenceError("current custody epoch permit closure is malformed")
    normalized_required_permits = [
        exact(item, {"resource_id", "role", "subject_id"}, "custody epoch required permit")
        for item in required_permits
    ]
    if (
        normalized_required_permits != sorted(normalized_required_permits, key=canonical)
        or current_effective_permits != normalized_required_permits
    ):
        raise EvidenceError("current custody epoch effective permit closure differs from the exact contract")
    retired_proofs: list[dict[str, Any]] = []
    for item in retired_epochs:
        principal_id = item["principal_id"]
        group_closure = sorted(transitive_group_closure(principal_id, group_edges))
        retired_permits = effective_permits(principal_id)
        if group_closure or retired_permits:
            raise EvidenceError("retired custody epoch still has inherited or direct provider authority")
        retired_proofs.append(
            {
                "epoch_id": item["epoch_id"],
                "generation": item["generation"],
                "group_closure": group_closure,
                "permit_closure": retired_permits,
                "principal_id": principal_id,
                "service_account_preserved": True,
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
            actual_groups = sorted(transitive_group_closure(principal_id, group_edges))
            if actual_groups != identity["group_ids"]:
                raise EvidenceError(f"expected {label} transitive group closure differs from provider evidence")
    if expected["owner"]["principal_ids"] != [current_principal]:
        raise EvidenceError("Kubernetes custody owner is not bound to the unique current provider epoch")

    authority_permits = expected["authority_required_permits"]
    if not isinstance(authority_permits, dict) or set(authority_permits) != {
        "backend_receipt",
        "manifest",
        "provider_receipt",
    }:
        raise EvidenceError("authority required-permit contract is incomplete")
    authority_permit_resources: dict[str, set[str]] = {}
    for label, required in authority_permits.items():
        if not isinstance(required, list) or not required:
            raise EvidenceError(f"{label} authority required permit set is empty")
        normalized = [
            exact(
                item,
                {"resource_id", "role", "subject_id"},
                f"{label} required permit",
            )
            for item in required
        ]
        authority_permit_resources[label] = {
            nonempty(item["resource_id"], f"{label} permit resource ID")
            for item in normalized
        }

    authorities = exact(
        contract["authorities"],
        {"backend_receipt", "manifest", "provider_receipt"},
        "custody signing authorities",
    )
    authority_principals = []
    authority_resource_ids: set[str] = set()
    for label in ("provider_receipt", "backend_receipt", "manifest"):
        authority = exact(
            authorities[label],
            {
                "key_id",
                "principal_id",
                "protected_resource_ids",
                "public_key_path",
                "public_key_sha256",
                "signing_resource_id",
            },
            f"{label} authority",
        )
        principal_id = nonempty(authority["principal_id"], f"{label} authority principal")
        if principal_id not in principals:
            raise EvidenceError(f"{label} authority is absent from the exhaustive principal enumeration")
        authority_principals.append(principal_id)
        signing_resource_id = nonempty(
            authority["signing_resource_id"], f"{label} signing resource ID"
        )
        if signing_resource_id not in authority_permit_resources[label]:
            raise EvidenceError(
                f"{label} signing resource is not an exact provider permit target"
            )
        resources = authority["protected_resource_ids"]
        required_resources = sorted(
            {principal_id, signing_resource_id, *authority_permit_resources[label]}
        )
        if resources != required_resources:
            raise EvidenceError(
                f"{label} authority signing-resource closure is not derived from its "
                "principal and exact provider permit targets"
            )
        authority_resource_ids.update(resources)
    if len(set(authority_principals)) != 3 or set(authority_principals).intersection(
        expected["platform"]["principal_ids"]
    ):
        raise EvidenceError("receipt/manifest authorities are not three distinct non-platform principals")

    # All credential enumeration is intentionally forbidden. Access-key list
    # responses can include status.secret, and other credential classes cannot
    # prove which caller signed an S3 request. Bind S3 access to the unique
    # current epoch service account through an exact singleton group/policy
    # boundary and prove every prior epoch has zero inherited/direct authority.
    backend_group = nonempty(expected["backend_access_group_id"], "backend access group ID")
    backend_principal = nonempty(
        expected["backend_collector_principal_id"], "backend collector principal ID"
    )
    if backend_group not in groups or backend_principal not in principals:
        raise EvidenceError("backend collector singleton group is absent from authoritative inventory")
    if backend_principal != current_principal or backend_group not in current_group_closure:
        raise EvidenceError("backend boundary is not bound to the current custody epoch")
    backend_members = sorted(member for member, group in group_edges if group == backend_group)
    if backend_members != [backend_principal]:
        raise EvidenceError("backend access group is not the exact singleton collector boundary")

    external_kubernetes_resources = expected[
        "external_kubernetes_authorization_resource_ids"
    ]
    if (
        not isinstance(external_kubernetes_resources, list)
        or not external_kubernetes_resources
        or external_kubernetes_resources
        != sorted(set(external_kubernetes_resources))
        or not all(isinstance(item, str) and item for item in external_kubernetes_resources)
        or external_kubernetes_resources != [expected["cluster_id"]]
    ):
        raise EvidenceError(
            "external Kubernetes authorization closure must include its exact cluster resource"
        )
    identity_resource_ids = set().union(
        *(
            set(identity["principal_ids"]) | set(identity["group_ids"])
            for identity in identities.values()
        )
    )
    declared_permit_resources = {
        item.get("resource_id")
        for permit_set in (
            epoch["required_permits"],
            expected["owner_required_permits"],
            expected["receipt_required_permits"],
            *authority_permits.values(),
        )
        if isinstance(permit_set, list)
        for item in permit_set
        if isinstance(item, dict) and isinstance(item.get("resource_id"), str)
    }
    mandatory_protected = {
        tenant_id,
        project_id,
        bucket_id,
        backend_group,
        current_principal,
        *authority_principals,
        *authority_resource_ids,
        *external_kubernetes_resources,
        *identity_resource_ids,
        *epoch["required_group_ids"],
        *declared_permit_resources,
    }
    configured_protected = expected["protected_resource_ids"]
    if (
        not isinstance(configured_protected, list)
        or configured_protected != sorted(mandatory_protected)
    ):
        raise EvidenceError(
            "protected resource closure is not the exact source-mandated "
            "tenant/project/backend/signing/Kubernetes set"
        )
    protected = mandatory_protected
    platform_subjects = set(expected["platform"]["principal_ids"]) | set(expected["platform"]["group_ids"])
    if not protected or any(
        item["subject_id"] in platform_subjects and item["resource_id"] in protected for item in permits
    ):
        raise EvidenceError("platform authority reaches an external custody resource")
    for label, field, identity_field in (
        ("owner", "owner_required_permits", "owner"),
        ("receipt operator", "receipt_required_permits", "receipt_operator"),
    ):
        required = expected[field]
        if not isinstance(required, list) or not required:
            raise EvidenceError(f"{label} required permit set is empty")
        identity = expected[identity_field]
        identity_subjects = set(identity["principal_ids"]) | set(identity["group_ids"])
        if {
            item.get("resource_id")
            for item in required
            if isinstance(item, dict)
        } != set(external_kubernetes_resources):
            raise EvidenceError(
                f"{label} permit closure is not limited to the exact external "
                "Kubernetes authorization resource"
            )
        for item in required:
            entry = exact(item, {"resource_id", "role", "subject_id"}, f"{label} required permit")
            if entry["subject_id"] not in identity_subjects:
                raise EvidenceError(f"{label} required permit is not bound to its claimed identity")
            if (entry["subject_id"], entry["resource_id"], entry["role"]) not in permit_edges:
                raise EvidenceError(f"{label} required provider permit is absent")
    for label, principal_id in zip(
        ("provider_receipt", "backend_receipt", "manifest"), authority_principals, strict=True
    ):
        principal_groups = transitive_group_closure(principal_id, group_edges)
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

    def policy_subjects(value: object) -> set[str]:
        if isinstance(value, dict):
            return set().union(*(policy_subjects(item) for item in value.values()), set())
        if isinstance(value, list):
            return set().union(*(policy_subjects(item) for item in value), set())
        if isinstance(value, str) and value.startswith(
            ("group-", "serviceaccount-", "tenantuseraccount-")
        ):
            return {value}
        return set()

    if policy_subjects(native_rules) != {backend_group}:
        raise EvidenceError("backend provider-native policy is not limited to the singleton access group")
    if bucket_spec.get("versioning_policy") != "ENABLED" or bucket_status.get("state") != "ACTIVE":
        raise EvidenceError("backend bucket is not active with versioning enabled")

    resource_versions = {
        "backend_bucket": bucket_version,
        "project": project_version,
        "tenant": tenant_version,
    }
    epoch_projection = {
        "epoch_id": epoch["epoch_id"],
        "generation": current_generation,
        "group_closure": current_group_closure,
        "permit_closure": current_effective_permits,
        "previous_activation_sha256": epoch["previous_activation_sha256"],
        "principal_id": current_principal,
        "retired_epochs": retired_proofs,
        "retirement_mode": epoch["retirement_mode"],
    }
    epoch_sha256 = hashlib.sha256(canonical(epoch_projection)).hexdigest()
    projection = {
        "custody_epoch": epoch_projection,
        "group_memberships": [list(edge) for edge in sorted(group_edges)],
        "groups": sorted(groups),
        "permits": permits,
        "principals": sorted(principals),
        "projects": sorted(project_inventory, key=lambda item: canonical(item)),
        "provider_resource_versions": resource_versions,
        "scope": scope,
        "visible_tenants": sorted(tenant_inventory, key=lambda item: canonical(item)),
    }
    backend_boundary = {
        "backend_access_group_id": backend_group,
        "backend_bucket_resource_id": bucket_id,
        "backend_collector_principal_id": backend_principal,
        "custody_epoch_id": epoch["epoch_id"],
        "custody_epoch_sha256": epoch_sha256,
        "native_bucket_rules_sha256": expected["native_bucket_rules_sha256"],
    }
    return {
        "backend_boundary_sha256": hashlib.sha256(canonical(backend_boundary)).hexdigest(),
        "collection_id": artifact["collection_id"],
        "completed_at": artifact["completed_at"],
        "custody_epoch_sha256": epoch_sha256,
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
    custody_objects: list[dict[str, Any]] = []
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("mode") != "managed":
            continue
        provider_address = nonempty(
            resource.get("provider"), "managed state provider address"
        )
        custody_provider = provider_address.endswith('"].pod_security_custody')
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
            classified = (
                address in manifest_v2.STATIC_STATE
                or manifest_v2.DYNAMIC_ADDRESS_RE.fullmatch(address) is not None
            )
            if custody_provider and not classified:
                raise EvidenceError(
                    f"pod_security_custody provider has unclassified managed address {address}"
                )
            if classified and not custody_provider:
                raise EvidenceError(
                    f"retained custody address {address} is bound to another Terraform provider"
                )
            if classified:
                custody_addresses.add(address)
                try:
                    projected = state_semantics.state_instance_projection(
                        address,
                        nonempty(resource.get("type"), f"{address} resource type"),
                        instance,
                    )
                except state_semantics.StateSemanticsError as error:
                    raise EvidenceError(str(error)) from error
                identity = (
                    projected["api_version"],
                    projected["kind"],
                    projected["namespace"],
                    projected["name"],
                )
                if address in manifest_v2.STATIC_STATE:
                    if identity != manifest_v2.STATIC_STATE[address]:
                        raise EvidenceError(
                            f"raw state identity differs at static address {address}"
                        )
                else:
                    index_key = instance.get("index_key")
                    if not isinstance(index_key, str) or index_key != projected["name"]:
                        raise EvidenceError(
                            "dynamic custody address key does not equal its raw "
                            f"object name at {address}"
                        )
                    if projected["namespace"] != "fs2-models":
                        raise EvidenceError(
                            f"dynamic custody address escaped fs2-models at {address}"
                        )
                custody_objects.append(projected)
    if not manifest_v2.REQUIRED_STATIC_STATE.issubset(custody_addresses):
        raise EvidenceError("platform state omits an unconditional retained custody address")
    custody_objects.sort(key=lambda item: item["state_address"])
    if [item["state_address"] for item in custody_objects] != sorted(custody_addresses):
        raise EvidenceError("raw custody object projection is not exactly one-to-one with state")
    return {
        "all_managed_addresses_sha256": hashlib.sha256(
            canonical(sorted(all_addresses))
        ).hexdigest(),
        "all_managed_object_count": len(all_addresses),
        "custody_addresses": sorted(custody_addresses),
        "custody_addresses_sha256": hashlib.sha256(canonical(sorted(custody_addresses))).hexdigest(),
        "custody_objects": custody_objects,
        "custody_objects_sha256": hashlib.sha256(canonical(custody_objects)).hexdigest(),
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
    provider_boundary = sha256(
        contract.get("verified_backend_boundary_sha256"),
        "verified provider backend boundary SHA-256",
    )
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
        "provider_backend_boundary_sha256": provider_boundary,
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
