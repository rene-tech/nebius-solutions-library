#!/usr/bin/env python3
"""Fail closed on exact reviewed disposable Terraform plan contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

FS2_ROOT = Path(__file__).resolve().parents[3]
TARGET_CONTRACT_PATH = Path(
    os.environ.get(
        "K8S_INFERENCE_TARGET_CONTRACT_PATH",
        FS2_ROOT / "catalog/profiles/approved-targets.json",
    )
)
CAPACITY_CONTRACT_PATH = FS2_ROOT / "catalog/profiles/capacity-profiles.json"
TARGET_CONTRACT = json.loads(TARGET_CONTRACT_PATH.read_text(encoding="utf-8"))
CAPACITY_CONTRACT = json.loads(CAPACITY_CONTRACT_PATH.read_text(encoding="utf-8"))
APPROVED_TARGETS = TARGET_CONTRACT["targets"]
SOURCE_REGISTRY = TARGET_CONTRACT.get("source_registry")
DEFAULT_ACCEPTANCE_MODE = "full-catalog-zero"
ADMIN_MINIMAL_ZERO_MODE = "admin-minimal-zero"
REQUIRED_DRIVER_PRESET = "cuda13.0"
POOL_ADDRESSES = {
    "gpu_b300_1x": 'nebius_mk8s_v1_node_group.gpu["nebius-b300-preemptible-1x"]',
    "gpu_b300_8x": 'nebius_mk8s_v1_node_group.gpu["nebius-b300-preemptible-8x"]',
}

ALLOWED_TYPES = {
    "terraform_data",
    "nebius_iam_v1_service_account",
    "nebius_iam_v1_group",
    "nebius_iam_v1_group_membership",
    "nebius_iam_v1_access_permit",
    "nebius_registry_v1_registry",
    "nebius_compute_v1_filesystem",
    "nebius_vpc_v1_security_group",
    "nebius_vpc_v1_security_rule",
    "nebius_vpc_v1_allocation",
    "nebius_mk8s_v1_cluster",
    "nebius_mk8s_v1_node_group",
}

# These IDs are safety boundaries, not discovery targets. The verifier only
# compares local plan JSON strings and never queries either cluster.
DENYLISTED_IDS = {
    "mk8scluster-syntheticretained",  # synthetic retained platform fixture
    "mk8scluster-syntheticlegacy",  # synthetic prohibited legacy fixture
}

BASE_REQUIRED_ADDRESSES = {
    "terraform_data.target_contract",
    "nebius_iam_v1_service_account.nodepull",
    "nebius_iam_v1_group.target_registry_readers",
    "nebius_iam_v1_group_membership.nodepull_target_registry",
    "nebius_iam_v1_access_permit.nodepull_registry",
    "nebius_registry_v1_registry.images",
    "nebius_compute_v1_filesystem.cache",
    "nebius_vpc_v1_security_group.workers",
    "nebius_vpc_v1_security_rule.workers_private_ingress",
    "nebius_vpc_v1_security_rule.workers_egress",
    "nebius_mk8s_v1_cluster.validation",
    "nebius_mk8s_v1_node_group.system",
    *POOL_ADDRESSES.values(),
}

PUBLIC_EDGE_ADDRESSES = {
    "nebius_vpc_v1_security_rule.workers_public_edge_ingress[0]",
    "nebius_vpc_v1_allocation.gateway[0]",
}
REQUIRED_ADDRESSES = BASE_REQUIRED_ADDRESSES | PUBLIC_EDGE_ADDRESSES
EDGE_MODES = {
    "public": frozenset(PUBLIC_EDGE_ADDRESSES),
    "internal-only": frozenset(),
}

# The modes intentionally carry independent address and capacity contracts.
# Keeping the default release mode distinct prevents the smaller admin
# create/apply/destroy acceptance from silently weakening full-catalog DoD.
ACCEPTANCE_MODES = {
    DEFAULT_ACCEPTANCE_MODE: {
        "capacity_profile": "full_catalog",
        "gpu_floor_profile": "zero",
        "maximum_gpus": 22,
        "shared_cache_size_gib": 2048,
        "required_addresses": frozenset(BASE_REQUIRED_ADDRESSES),
    },
    ADMIN_MINIMAL_ZERO_MODE: {
        "capacity_profile": "minimal",
        "gpu_floor_profile": "zero",
        "maximum_gpus": 9,
        "shared_cache_size_gib": 128,
        "required_addresses": frozenset(BASE_REQUIRED_ADDRESSES),
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan_json", type=Path)
    parser.add_argument("--mode", choices=("create", "noop", "destroy"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-metadata", type=Path, required=True)
    parser.add_argument("--expected-project-id", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument(
        "--acceptance-mode",
        choices=tuple(ACCEPTANCE_MODES),
        default=DEFAULT_ACCEPTANCE_MODE,
        help=(
            "Exact reviewed topology. The default remains the release "
            "full_catalog/zero gate; admin-minimal-zero is additive."
        ),
    )
    parser.add_argument(
        "--public-edge-mode",
        choices=tuple(EDGE_MODES),
        default="public",
        help="Exact reviewed edge topology; internal-only admits no allocation or public ingress rule.",
    )
    parser.add_argument(
        "--forbidden-id",
        action="append",
        default=[],
        help="Known retained/prohibited resource ID; may be repeated.",
    )
    return parser.parse_args()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def expected_infrastructure_contract(
    project_id: str,
    source_commit: str,
    acceptance_mode: str = DEFAULT_ACCEPTANCE_MODE,
) -> dict[str, Any]:
    target = APPROVED_TARGETS[project_id]
    mode = ACCEPTANCE_MODES[acceptance_mode]
    capacity_profile = mode["capacity_profile"]
    gpu_floor_profile = mode["gpu_floor_profile"]
    capacity = CAPACITY_CONTRACT["capacity_profiles"][capacity_profile]
    floor = CAPACITY_CONTRACT["floor_profiles"][gpu_floor_profile]
    return {
        "schema": "fs2-serve.nebius.ai/terraform-infrastructure-contract/v1",
        "source_commit": source_commit,
        "target": {
            "project_id": project_id,
            "region": target["region"],
            "system_update_strategy": target["system_update_strategy"],
        },
        "source_registry": {
            "id": SOURCE_REGISTRY["id"],
            "project_id": SOURCE_REGISTRY["project_id"],
            "fqdn": SOURCE_REGISTRY["fqdn"],
        },
        "capacity": {
            "profile": capacity_profile,
            "floor_profile": gpu_floor_profile,
            "maximum_gpus": capacity["maximum_gpus"],
            "shared_cache_size_gib": capacity["shared_cache_size_gib"],
            "system": {
                "capacity": "regular",
                "platform": "cpu-d3",
                "preset": "8vcpu-32gb",
                "nodes": capacity["system_nodes"],
                "max_surge": target["system_update_strategy"]["max_surge"],
                "max_unavailable": target["system_update_strategy"]["max_unavailable"],
            },
            "gpu_b300_1x": {
                "capacity": "preemptible",
                "platform": "gpu-b300-sxm",
                "preset": "1gpu-24vcpu-346gb",
                "gpus_per_node": 1,
                "min_nodes": floor["gpu_1x_min_nodes"],
                "max_nodes": capacity["gpu_1x_max_nodes"],
                "driver_preset": REQUIRED_DRIVER_PRESET,
                "local_nvme": False,
            },
            "gpu_b300_8x": {
                "capacity": "preemptible",
                "platform": "gpu-b300-sxm",
                "preset": "8gpu-192vcpu-2768gb",
                "gpus_per_node": 8,
                "min_nodes": floor["gpu_8x_min_nodes"],
                "max_nodes": capacity["gpu_8x_max_nodes"],
                "driver_preset": REQUIRED_DRIVER_PRESET,
                "local_nvme": True,
            },
        },
    }


def validate_expected_inputs(project_id: str, source_commit: str) -> list[str]:
    errors: list[str] = []
    if project_id not in APPROVED_TARGETS:
        errors.append("expected project ID is not in the approved target contract")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        errors.append("expected source commit must be an exact lowercase Git SHA")
    return errors


def validate_run_metadata(
    path: Path,
    run_id: str,
    plan_json: Path,
    expected_project_id: str,
    expected_source_commit: str,
    acceptance_mode: str,
    public_edge_mode: str,
) -> list[str]:
    metadata = json.loads(path.read_text(encoding="utf-8"))
    expected_labels = {
        "owner": "k8s-elastic-inference-platform",
        "task": "fs2-terraform-recipe",
        "managed-by": "terraform",
        "environment": "fs2-disposable",
        "retention": "ephemeral",
        "run-id": run_id,
    }
    errors: list[str] = []
    if metadata.get("schema_version") != 1:
        errors.append("run metadata schema_version must be 1")
    if metadata.get("labels") != expected_labels:
        errors.append("run metadata ownership labels differ from the contract")
    if metadata.get("project_id") != expected_project_id:
        errors.append("run metadata project_id differs from the exact reviewed project")
    if metadata.get("source_commit") != expected_source_commit:
        errors.append(
            "run metadata source_commit differs from the exact reviewed commit"
        )
    mode = ACCEPTANCE_MODES[acceptance_mode]
    if metadata.get("capacity_profile") != mode["capacity_profile"]:
        errors.append(
            f"run metadata capacity_profile must be {mode['capacity_profile']}"
        )
    if metadata.get("gpu_floor_profile") != mode["gpu_floor_profile"]:
        errors.append(
            f"run metadata gpu_floor_profile must be {mode['gpu_floor_profile']}"
        )
    if metadata.get("public_edge_mode") != public_edge_mode:
        errors.append("run metadata public_edge_mode differs from the reviewed edge mode")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        errors.append("run metadata file mode must be 0600")
    if stat.S_IMODE(plan_json.stat().st_mode) != 0o600:
        errors.append("plan JSON file mode must be 0600")
    if stat.S_IMODE(path.resolve().parent.stat().st_mode) != 0o700:
        errors.append("run directory mode must be 0700")
    paths = metadata.get("paths", {})
    run_root = path.resolve().parent
    expected_paths = {
        "backend": str(run_root / "terraform.tfstate"),
        "kubeconfig": str(run_root / "kubeconfig"),
        "plan_json": str(plan_json.resolve()),
    }
    for key, expected in expected_paths.items():
        actual = paths.get(key)
        if actual != expected:
            errors.append(
                f"run metadata path {key!r}={actual!r}, expected {expected!r}"
            )
    return errors


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [part for item in value.values() for part in strings(item)]
    if isinstance(value, list):
        return [part for item in value for part in strings(item)]
    return []


def variable(document: dict[str, Any], name: str) -> Any:
    return document.get("variables", {}).get(name, {}).get("value")


def nested(value: dict[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def validate_plan_variables(
    document: dict[str, Any],
    expected_project_id: str,
    expected_source_commit: str,
    acceptance_mode: str,
    public_edge_mode: str,
) -> list[str]:
    mode = ACCEPTANCE_MODES[acceptance_mode]
    expected = {
        "project_id": expected_project_id,
        "source_commit": expected_source_commit,
        "capacity_profile": mode["capacity_profile"],
        "gpu_floor_profile": mode["gpu_floor_profile"],
        "gpu_driver_preset": REQUIRED_DRIVER_PRESET,
        "public_edge_mode": public_edge_mode,
    }
    errors: list[str] = []
    for name, expected_value in expected.items():
        if variable(document, name) != expected_value:
            errors.append(
                f"plan variable {name} differs from the exact release contract"
            )
    return errors


def validate_target_contract(
    values: dict[str, Any], expected_project_id: str, contract: dict[str, Any], public_edge_mode: str
) -> list[str]:
    target = values.get("input", {})
    selected = APPROVED_TARGETS[expected_project_id]
    expected = {
        "project_id": expected_project_id,
        "project_name": selected["project_name"],
        "region": selected["region"],
        "network_name": selected["network_name"],
        "subnet_name": selected["subnet_name"],
        "private_subnet_cidr": selected["private_subnet_cidr"],
        "system_update_strategy": selected["system_update_strategy"],
        "tenant_id": TARGET_CONTRACT["tenant_id"],
        "public_edge_mode": public_edge_mode,
    }
    errors = []
    for key, expected_value in expected.items():
        if target.get(key) != expected_value:
            errors.append(
                f"terraform_data.target_contract field {key!r} differs from the exact reviewed mapping"
            )
    return errors


def validate_public_edge(
    document: dict[str, Any],
    managed_by_address: dict[str, dict[str, Any]],
    side: str,
    expected_project_id: str,
    public_edge_mode: str,
) -> list[str]:
    source_cidrs = variable(document, "public_edge_source_cidrs")
    service_ports = variable(document, "public_edge_service_ports")
    if public_edge_mode == "internal-only":
        return [] if source_cidrs == [] else [
            "internal-only mode requires an empty public_edge_source_cidrs list"
        ]

    errors: list[str] = []
    if (
        not isinstance(source_cidrs, list)
        or not 1 <= len(source_cidrs) <= 8
        or any(not isinstance(item, str) for item in source_cidrs)
    ):
        errors.append("public edge source CIDRs are absent or outside the reviewed bound")
    expected_ports = [
        nested(service_ports or {}, "http", "listener_port"),
        nested(service_ports or {}, "https", "listener_port"),
        nested(service_ports or {}, "http", "target_port"),
        nested(service_ports or {}, "https", "target_port"),
        nested(service_ports or {}, "http", "node_port"),
        nested(service_ports or {}, "https", "node_port"),
    ]
    if any(value is None for value in expected_ports):
        errors.append("public edge service-port contract is incomplete")

    rule = (
        managed_by_address.get(
            "nebius_vpc_v1_security_rule.workers_public_edge_ingress[0]", {}
        )
        .get("change", {})
        .get(side)
        or {}
    )
    if (
        rule.get("access") != "ALLOW"
        or rule.get("protocol") != "TCP"
        or rule.get("type") != "STATEFUL"
        or rule.get("priority") != 90
        or nested(rule, "ingress", "source_cidrs") != source_cidrs
        or nested(rule, "ingress", "destination_ports") != expected_ports
    ):
        errors.append("public ingress rule differs from the exact variable-bound edge contract")

    allocation = (
        managed_by_address.get("nebius_vpc_v1_allocation.gateway[0]", {})
        .get("change", {})
        .get(side)
        or {}
    )
    target = (
        managed_by_address.get("terraform_data.target_contract", {})
        .get("change", {})
        .get(side)
        or {}
    ).get("input", {})
    if (
        allocation.get("parent_id") != expected_project_id
        or nested(allocation, "ipv4_public", "cidr") != "/32"
        or not isinstance(nested(allocation, "ipv4_public", "subnet_id"), str)
        or nested(allocation, "ipv4_public", "subnet_id") != target.get("subnet_id")
    ):
        errors.append("public IPv4 allocation differs from the exact target /32 contract")
    return errors


def validate_public_edge_outputs(
    document: dict[str, Any], mode: str, public_edge_mode: str
) -> list[str]:
    """Reject placeholder or nullable public identities outside internal-only mode."""
    if mode == "destroy":
        return []
    outputs = document.get("planned_values", {}).get("outputs", {})
    edge = outputs.get("public_edge_contract", {}).get("value")
    allocation_id = outputs.get("gateway_allocation_id", {}).get("value", "missing")
    public_cidr = outputs.get("gateway_public_cidr", {}).get("value", "missing")
    owned = outputs.get("owned_resource_ids", {}).get("value")
    if not isinstance(edge, dict):
        if mode == "create" and public_edge_mode == "public":
            return []
        return ["planned public_edge_contract output is absent or unknown"]
    errors: list[str] = []
    port_forward = edge.get("port_forward")
    expected_ports = variable(document, "public_edge_service_ports")
    expected_local_ports = variable(document, "port_forward_local_ports")
    if (
        not isinstance(expected_local_ports, dict)
        or set(expected_local_ports)
        != {"control_plane", "admin_console", "operator_proxy"}
        or any(
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1024 <= port <= 65535
            for port in expected_local_ports.values()
        )
        or len(set(expected_local_ports.values())) != 3
    ):
        return ["planned port_forward_local_ports variable is invalid"]
    if (
        edge.get("schema") != "fs2-serve.nebius.ai/public-edge/v1"
        or edge.get("mode") != public_edge_mode
        or edge.get("external_traffic_policy") != "Cluster"
        or edge.get("service_ports") != expected_ports
        or not isinstance(port_forward, dict)
        or port_forward.get("control_plane_service") != "fs2-serve-control-plane"
        or port_forward.get("control_plane_port") != 8080
        or port_forward.get("admin_console_service")
        != "fs2-serve-control-plane-admin-console"
        or port_forward.get("admin_console_port") != 8080
    ):
        errors.append("planned public_edge_contract output differs from the typed service contract")
        return errors
    if public_edge_mode == "internal-only":
        if (
            edge.get("transport") != "kubectl-port-forward"
            or any(
                edge.get(key) is not None
                for key in (
                    "public_origin",
                    "allocation_project_id",
                    "allocation_id",
                    "public_ipv4_address",
                )
            )
            or allocation_id is not None
            or public_cidr is not None
            or (
                mode == "noop"
                and (
                    not isinstance(owned, dict)
                    or owned.get("gateway_allocation") is not None
                )
            )
            or edge.get("security_group_destination_ports") != []
            or port_forward.get("enabled") is not True
            or port_forward.get("bind_address") != "127.0.0.1"
            or port_forward.get("application_origin")
            != f"http://localhost:{expected_local_ports['operator_proxy']}"
            or port_forward.get("operator_endpoint")
            != f"http://127.0.0.1:{expected_local_ports['operator_proxy']}"
            or port_forward.get("operator_proxy_port")
            != expected_local_ports["operator_proxy"]
            or port_forward.get("control_plane_local_port")
            != expected_local_ports["control_plane"]
            or port_forward.get("admin_console_local_port")
            != expected_local_ports["admin_console"]
        ):
            errors.append("internal-only outputs contain a public identity or wrong loopback contract")
    elif mode == "noop":
        public_ip = edge.get("public_ipv4_address")
        if (
            edge.get("transport") != "public-https"
            or not isinstance(edge.get("allocation_project_id"), str)
            or not isinstance(edge.get("allocation_id"), str)
            or re.fullmatch(r"vpcallocation-[a-z0-9]+", edge["allocation_id"]) is None
            or allocation_id != edge.get("allocation_id")
            or not isinstance(owned, dict)
            or owned.get("gateway_allocation") != edge.get("allocation_id")
            or not isinstance(public_ip, str)
            or public_cidr != f"{public_ip}/32"
            or edge.get("public_origin") != f"https://{public_ip}"
            or edge.get("security_group_destination_ports") is None
            or len(edge["security_group_destination_ports"]) != 6
            or port_forward.get("enabled") is not False
        ):
            errors.append("public no-op outputs lack the concrete run-owned allocation contract")
    return errors


def validate_concrete_topology(
    managed_by_address: dict[str, dict[str, Any]],
    side: str,
    contract: dict[str, Any],
    acceptance_mode: str,
) -> list[str]:
    def values(address: str) -> dict[str, Any]:
        return managed_by_address.get(address, {}).get("change", {}).get(side) or {}

    capacity = contract["capacity"]
    checks = {
        "nebius_compute_v1_filesystem.cache.size_gibibytes": (
            nested(values("nebius_compute_v1_filesystem.cache"), "size_gibibytes"),
            capacity["shared_cache_size_gib"],
        ),
        "nebius_mk8s_v1_node_group.system.fixed_node_count": (
            nested(values("nebius_mk8s_v1_node_group.system"), "fixed_node_count"),
            capacity["system"]["nodes"],
        ),
        "nebius_mk8s_v1_node_group.system.strategy.max_surge.count": (
            nested(
                values("nebius_mk8s_v1_node_group.system"),
                "strategy",
                "max_surge",
                "count",
            ),
            capacity["system"]["max_surge"],
        ),
        "nebius_mk8s_v1_node_group.system.strategy.max_unavailable.count": (
            nested(
                values("nebius_mk8s_v1_node_group.system"),
                "strategy",
                "max_unavailable",
                "count",
            ),
            capacity["system"]["max_unavailable"],
        ),
        f"{POOL_ADDRESSES['gpu_b300_1x']}.autoscaling.min_node_count": (
            nested(
                values(POOL_ADDRESSES["gpu_b300_1x"]),
                "autoscaling",
                "min_node_count",
            ),
            capacity["gpu_b300_1x"]["min_nodes"],
        ),
        f"{POOL_ADDRESSES['gpu_b300_1x']}.autoscaling.max_node_count": (
            nested(
                values(POOL_ADDRESSES["gpu_b300_1x"]),
                "autoscaling",
                "max_node_count",
            ),
            capacity["gpu_b300_1x"]["max_nodes"],
        ),
        f"{POOL_ADDRESSES['gpu_b300_1x']}.template.resources.preset": (
            nested(
                values(POOL_ADDRESSES["gpu_b300_1x"]),
                "template",
                "resources",
                "preset",
            ),
            capacity["gpu_b300_1x"]["preset"],
        ),
        f"{POOL_ADDRESSES['gpu_b300_8x']}.autoscaling.min_node_count": (
            nested(
                values(POOL_ADDRESSES["gpu_b300_8x"]),
                "autoscaling",
                "min_node_count",
            ),
            capacity["gpu_b300_8x"]["min_nodes"],
        ),
        f"{POOL_ADDRESSES['gpu_b300_8x']}.autoscaling.max_node_count": (
            nested(
                values(POOL_ADDRESSES["gpu_b300_8x"]),
                "autoscaling",
                "max_node_count",
            ),
            capacity["gpu_b300_8x"]["max_nodes"],
        ),
        f"{POOL_ADDRESSES['gpu_b300_8x']}.template.resources.preset": (
            nested(
                values(POOL_ADDRESSES["gpu_b300_8x"]),
                "template",
                "resources",
                "preset",
            ),
            capacity["gpu_b300_8x"]["preset"],
        ),
    }
    mode = ACCEPTANCE_MODES[acceptance_mode]
    profile_label = f"{mode['capacity_profile']}/{mode['gpu_floor_profile']}"
    errors = [
        f"{field} differs from the exact {profile_label} topology"
        for field, (actual, expected) in checks.items()
        if actual != expected
    ]
    maximum_gpus = (
        capacity["gpu_b300_1x"]["max_nodes"] * capacity["gpu_b300_1x"]["gpus_per_node"]
        + capacity["gpu_b300_8x"]["max_nodes"]
        * capacity["gpu_b300_8x"]["gpus_per_node"]
    )
    if (
        capacity["maximum_gpus"] != mode["maximum_gpus"]
        or maximum_gpus != mode["maximum_gpus"]
    ):
        errors.append(
            "infrastructure contract does not describe exactly "
            f"{mode['maximum_gpus']} GPUs for {profile_label}"
        )
    if capacity["shared_cache_size_gib"] != mode["shared_cache_size_gib"]:
        errors.append(
            "infrastructure contract filesystem is not exactly "
            f"{mode['shared_cache_size_gib']} GiB for {profile_label}"
        )
    return errors


def validate_output_contract(
    document: dict[str, Any], mode: str, contract: dict[str, Any]
) -> list[str]:
    if mode == "destroy":
        return []
    actual = (
        document.get("planned_values", {})
        .get("outputs", {})
        .get("infrastructure_contract", {})
        .get("value")
    )
    if actual != contract:
        return [
            "planned infrastructure_contract output differs from the exact contract"
        ]
    return []


def main() -> int:
    args = parse_args()
    document = json.loads(args.plan_json.read_text(encoding="utf-8"))
    errors = validate_expected_inputs(
        args.expected_project_id, args.expected_source_commit
    )
    contract: dict[str, Any] | None = None
    if not errors:
        contract = expected_infrastructure_contract(
            args.expected_project_id,
            args.expected_source_commit,
            args.acceptance_mode,
        )

    expected_actions = {
        "create": ["create"],
        "noop": ["no-op"],
        "destroy": ["delete"],
    }[args.mode]
    expected_prefix = f"fs2-disposable-{args.run_id}"
    managed = [
        change
        for change in document.get("resource_changes", [])
        if change.get("mode", "managed") == "managed"
    ]
    managed_by_address = {change.get("address", ""): change for change in managed}
    errors += validate_run_metadata(
        args.run_metadata,
        args.run_id,
        args.plan_json,
        args.expected_project_id,
        args.expected_source_commit,
        args.acceptance_mode,
        args.public_edge_mode,
    )
    errors += validate_plan_variables(
        document,
        args.expected_project_id,
        args.expected_source_commit,
        args.acceptance_mode,
        args.public_edge_mode,
    )

    for forbidden_id in DENYLISTED_IDS | set(args.forbidden_id):
        if forbidden_id and any(forbidden_id in value for value in strings(document)):
            errors.append("plan document references a denylisted resource ID")

    required_addresses = set(ACCEPTANCE_MODES[args.acceptance_mode]["required_addresses"])
    required_addresses.update(EDGE_MODES[args.public_edge_mode])
    addresses = set(managed_by_address)
    if addresses != required_addresses:
        errors.append(
            "managed address set differs from disposable contract: "
            f"missing={sorted(required_addresses - addresses)} "
            f"extra={sorted(addresses - required_addresses)}"
        )

    side = "after" if args.mode in {"create", "noop"} else "before"
    if contract is not None:
        target_change = managed_by_address.get("terraform_data.target_contract")
        if target_change is None:
            errors.append("terraform_data.target_contract is absent")
        else:
            errors += validate_target_contract(
                target_change.get("change", {}).get(side) or {},
                args.expected_project_id,
                contract,
                args.public_edge_mode,
            )
        errors += validate_concrete_topology(
            managed_by_address,
            side,
            contract,
            args.acceptance_mode,
        )
        errors += validate_public_edge(
            document,
            managed_by_address,
            side,
            args.expected_project_id,
            args.public_edge_mode,
        )
        errors += validate_public_edge_outputs(
            document,
            args.mode,
            args.public_edge_mode,
        )
        errors += validate_output_contract(document, args.mode, contract)

    for change in managed:
        address = change.get("address", "<unknown>")
        resource_type = change.get("type")
        actions = change.get("change", {}).get("actions", [])
        if resource_type not in ALLOWED_TYPES:
            errors.append(f"{address}: resource type {resource_type!r} is not allowed")
        if actions != expected_actions:
            errors.append(
                f"{address}: actions {actions!r}, expected {expected_actions!r}"
            )

        values = change.get("change", {}).get(side) or {}
        if resource_type == "terraform_data":
            continue
        name = values.get("name")
        if name is not None and not str(name).startswith(expected_prefix):
            errors.append(f"{address}: name {name!r} is outside {expected_prefix!r}")
        labels = values.get("labels")
        if labels is not None:
            expected_labels = {
                "environment": "fs2-disposable",
                "retention": "ephemeral",
                "run-id": args.run_id,
                "task": "fs2-terraform-recipe",
            }
            for key, expected in expected_labels.items():
                if labels.get(key) != expected:
                    errors.append(
                        f"{address}: label {key!r}={labels.get(key)!r}, expected {expected!r}"
                    )

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        f"PASS: {args.mode} plan contains exactly {len(managed)} disposable "
        "managed resources and the exact "
        f"{ACCEPTANCE_MODES[args.acceptance_mode]['capacity_profile']}/"
        f"{ACCEPTANCE_MODES[args.acceptance_mode]['gpu_floor_profile']} "
        f"infrastructure contract ({args.acceptance_mode}); edge={args.public_edge_mode}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
