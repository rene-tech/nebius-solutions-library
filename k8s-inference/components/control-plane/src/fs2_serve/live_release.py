"""Render the retained all-model route overlay from one reviewed inventory.

The inventory intentionally repeats the catalog revision and HTTP interface.
That redundancy makes rollout fail closed when a model worker, catalog record,
or live Service handoff changes without the other two being reconciled.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fs2_serve_catalog.consumer import SERVING_BINDINGS_SCHEMA
from fs2_serve_catalog.loader import Catalog, CatalogError, load_catalog

from .qualification import QualificationError, build_qualification_projection

INVENTORY_SCHEMA = "fs2-serve.nebius.ai/all-models-live-services/v3"
LEAN_ROUTES_SCHEMA = "fs2-serve.nebius.ai/lean-routes/v3"
VARIANT_PROMOTIONS_SCHEMA = "fs2-serve.nebius.ai/model-variant-promotions/v4"
_IMAGE_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_MODEL_REVISION = re.compile(r"(?:[a-f0-9]{40}|sha256:[a-f0-9]{64})")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_MCP_TOOL = re.compile(r"[a-z][a-z0-9_]{0,63}")
_STORAGE_MODES = frozenset({"provider-block-pvc", "sfs-pvc", "local-nvme", "ephemeral-emptydir"})
_MAX_INVENTORY_BYTES = 256 * 1024
_MAX_ROUTES = 256


class LiveReleaseError(ValueError):
    """The all-model release input is incomplete or disagrees with catalog authority."""


@dataclass(frozen=True)
class LiveRelease:
    """Rendered immutable inputs for one atomic Helm rollout."""

    release_id: str
    catalog_digest: str
    inventory_digest: str
    bindings_config_map_name: str
    routes_config_map_name: str
    config_maps: tuple[dict[str, Any], ...]
    helm_values: dict[str, Any]
    routes: tuple[dict[str, Any], ...]
    qualification_projection: dict[str, Any]


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _exact(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise LiveReleaseError(f"{label} fields are invalid")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LiveReleaseError("live service inventory is unavailable") from exc
    if not raw or len(raw) > _MAX_INVENTORY_BYTES:
        raise LiveReleaseError("live service inventory size is invalid")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise LiveReleaseError("live service inventory contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise LiveReleaseError("live service inventory is not valid JSON") from exc
    return _exact(
        value,
        {"schema", "namespace", "routes", "qualification"},
        "live service inventory",
    )


def _catalog_interface(catalog: Catalog, model_id: str) -> tuple[str, str, dict[str, str], list[str], bool]:
    try:
        value = catalog.model(model_id).to_dict()
    except KeyError as exc:  # defensive for Catalog-compatible test doubles
        raise LiveReleaseError(f"live service names unknown model: {model_id}") from exc
    model = value["model"]
    runtime = value["runtime"]
    interface = value["interface"]
    return (
        model["source"]["revision"],
        runtime["image"]["digest"],
        dict(interface["endpoints"]),
        list(interface["policy"]["operations"]),
        bool(interface["mcp"]["discoverable"]),
    )


def _variant_runtime(catalog: Catalog, model_id: str, variant_id: object) -> tuple[str, str]:
    if not isinstance(variant_id, str) or _DNS_LABEL.fullmatch(variant_id) is None:
        raise LiveReleaseError(f"{model_id} variant ID is invalid")
    try:
        variant = catalog.model_variant(variant_id)
        fallback, profile = catalog.fallback_for_variant(variant_id)
    except CatalogError as exc:
        raise LiveReleaseError(f"{model_id} variant is not in the canonical fallback graph") from exc
    value = variant.to_dict()
    source = value["source"]
    runtime = value["runtime"]
    promotion = value["promotion"]
    if (
        variant.base_model_id != model_id
        or variant.exposed_model_id != model_id
        or variant.relationship != "exact-model"
        or variant.runtime_architecture != profile
        or fallback.relationship != "exact-model"
        or runtime["build_state"] != "built-attested"
        or not isinstance(runtime["device_capability"], str)
        or not runtime["device_capability"].endswith("-qualified")
        or promotion["route_exposed"] is not False
    ):
        raise LiveReleaseError(f"{model_id} variant is not an exact qualified runtime")
    revision = source["revision"]
    image = runtime["image_digest"]
    if not isinstance(revision, str) or not isinstance(image, str):
        raise LiveReleaseError(f"{model_id} variant runtime identity is incomplete")
    return revision, image


def _route(model_id: str, raw: object, *, namespace: str, catalog: Catalog) -> dict[str, Any]:
    item = _exact(
        raw,
        {
            "variant_id",
            "model_revision",
            "runtime_image_digest",
            "service",
            "storage_mode",
            "protocols",
            "operations",
            "mcp",
        },
        f"routes.{model_id}",
    )
    if _DNS_LABEL.fullmatch(model_id) is None:
        raise LiveReleaseError("live service model ID is invalid")
    revision = item["model_revision"]
    if not isinstance(revision, str) or _MODEL_REVISION.fullmatch(revision) is None:
        raise LiveReleaseError(f"{model_id} model revision is invalid")
    image = item["runtime_image_digest"]
    if not isinstance(image, str) or _IMAGE_DIGEST.fullmatch(image) is None:
        raise LiveReleaseError(f"{model_id} runtime image is not digest-pinned")

    service = _exact(item["service"], {"name", "port"}, f"routes.{model_id}.service")
    service_name = service["name"]
    port = service["port"]
    if (
        not isinstance(service_name, str)
        or _DNS_LABEL.fullmatch(service_name) is None
        or (service_name != model_id and not service_name.startswith(f"{model_id}-"))
    ):
        raise LiveReleaseError(f"{model_id} service name is invalid or unowned")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise LiveReleaseError(f"{model_id} service port is invalid")
    if item["storage_mode"] not in _STORAGE_MODES:
        raise LiveReleaseError(f"{model_id} storage mode is invalid")

    canonical_revision, canonical_image, endpoints, operations, mcp_discoverable = _catalog_interface(catalog, model_id)
    variant_id = item["variant_id"]
    if variant_id is None:
        expected_revision, expected_image = canonical_revision, canonical_image
    else:
        expected_revision, expected_image = _variant_runtime(catalog, model_id, variant_id)
    if revision != expected_revision:
        raise LiveReleaseError(f"{model_id} model revision differs from its selected catalog identity")
    if image != expected_image:
        raise LiveReleaseError(f"{model_id} runtime image differs from its selected catalog identity")
    if not isinstance(item["protocols"], dict) or item["protocols"] != endpoints:
        raise LiveReleaseError(f"{model_id} protocols differ from the canonical catalog")
    if not isinstance(item["operations"], list) or item["operations"] != operations:
        raise LiveReleaseError(f"{model_id} operations differ from the canonical catalog")

    mcp = _exact(item["mcp"], {"enabled", "tool_name", "description"}, f"routes.{model_id}.mcp")
    if mcp["enabled"] is not True or not mcp_discoverable:
        raise LiveReleaseError(f"{model_id} is not canonically MCP-discoverable")
    if not isinstance(mcp["tool_name"], str) or _MCP_TOOL.fullmatch(mcp["tool_name"]) is None:
        raise LiveReleaseError(f"{model_id} MCP tool name is invalid")
    description = mcp["description"]
    if not isinstance(description, str) or not 1 <= len(description) <= 240:
        raise LiveReleaseError(f"{model_id} MCP description is invalid")
    if any(marker in description.lower() for marker in ("http://", "https://", "token", "secret", "credential")):
        raise LiveReleaseError(f"{model_id} MCP description contains private routing material")

    return {
        "model_id": model_id,
        "variant_id": variant_id,
        "model_revision": revision,
        "runtime_image_digest": image,
        "service": {"namespace": namespace, "name": service_name, "port": port},
        "storage_mode": item["storage_mode"],
        "protocols": dict(sorted(endpoints.items())),
        "operations": operations,
        "mcp": mcp,
    }


def render_live_release(catalog: Catalog, inventory_path: Path) -> LiveRelease:
    """Validate one complete bounded service inventory and render versioned objects."""

    inventory = _load_json(inventory_path)
    if inventory["schema"] != INVENTORY_SCHEMA:
        raise LiveReleaseError("live service inventory schema is unsupported")
    namespace = inventory["namespace"]
    if namespace != "fs2-models":
        raise LiveReleaseError("live model services must remain in fs2-models")
    raw_routes = inventory["routes"]
    if not isinstance(raw_routes, dict) or not 1 <= len(raw_routes) <= _MAX_ROUTES:
        raise LiveReleaseError("live service route count is invalid")
    if set(raw_routes) != set(catalog.tested_model_ids):
        missing = sorted(set(catalog.tested_model_ids) - set(raw_routes))
        extra = sorted(set(raw_routes) - set(catalog.tested_model_ids))
        raise LiveReleaseError(f"live service inventory differs from tested catalog: missing={missing}, extra={extra}")

    routes = tuple(
        _route(model_id, raw_routes[model_id], namespace=namespace, catalog=catalog) for model_id in sorted(raw_routes)
    )
    tools = [route["mcp"]["tool_name"] for route in routes]
    if len(tools) != len(set(tools)):
        raise LiveReleaseError("live service inventory contains duplicate MCP tool names")
    try:
        qualification_projection = build_qualification_projection(
            catalog,
            routes,
            inventory["qualification"],
        )
    except QualificationError as exc:
        raise LiveReleaseError("live model qualification projection is contradictory") from exc

    inventory_digest = _sha256(inventory)
    release_id = _sha256({"catalog_digest": catalog.digest, "inventory_digest": inventory_digest})[:12]
    bindings_name = f"fs2-serve-serving-bindings-all-models-{release_id}"
    routes_name = f"fs2-serve-lean-routes-all-models-{release_id}"
    annotations = {
        "fs2.nebius.ai/catalog-sha256": catalog.digest,
        "fs2.nebius.ai/inventory-sha256": inventory_digest,
        "fs2.nebius.ai/release-id": release_id,
    }
    labels = {"app.kubernetes.io/part-of": "fs2-serve", "app.kubernetes.io/component": "model-routing"}
    bindings = {"schema": SERVING_BINDINGS_SCHEMA, "catalog_digest": catalog.digest, "bindings": {}}
    variants: dict[str, Any] = {
        "schema": VARIANT_PROMOTIONS_SCHEMA,
        "route_authority": "signed-live-evidence-only",
        "catalog_digest": catalog.digest,
        "attestor_policy_sha256": None,
        "promotions": {},
    }
    lean_routes = {
        "schema": LEAN_ROUTES_SCHEMA,
        "routes": list(routes),
        "qualification": qualification_projection,
    }
    config_maps = (
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": bindings_name,
                "namespace": "fs2-system",
                "labels": labels,
                "annotations": annotations,
            },
            "data": {
                "serving-bindings.json": canonical_json(bindings).decode("ascii"),
                "model-variant-promotions.json": canonical_json(variants).decode("ascii"),
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": routes_name, "namespace": "fs2-system", "labels": labels, "annotations": annotations},
            "data": {"lean-routes.json": canonical_json(lean_routes).decode("ascii")},
        },
    )
    helm_values = {
        "catalog": {
            "bindingsConfigMapName": bindings_name,
            "rolloutDigest": f"sha256:{catalog.digest}",
            "leanRoutes": {"enabled": True, "configMapName": routes_name, "key": "lean-routes.json"},
        }
    }
    return LiveRelease(
        release_id=release_id,
        catalog_digest=catalog.digest,
        inventory_digest=inventory_digest,
        bindings_config_map_name=bindings_name,
        routes_config_map_name=routes_name,
        config_maps=config_maps,
        helm_values=helm_values,
        routes=routes,
        qualification_projection=qualification_projection,
    )


def load_and_render_live_release(*, catalog_root: Path, repo_root: Path, inventory_path: Path) -> LiveRelease:
    try:
        catalog = load_catalog(catalog_root, repo_root=repo_root)
    except CatalogError as error:
        raise LiveReleaseError("canonical catalog is not internally consistent") from error
    return render_live_release(catalog, inventory_path)


def write_json_atomic(path: Path, value: object) -> None:
    """Write public release metadata atomically without replacing a symlink."""

    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise LiveReleaseError("release output path is not a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_json(value) + b"\n")
    temporary.replace(path)
