#!/usr/bin/env python3
"""Keep scientific profiles and their execution map on one digest chain.

``runtime_recipe_sha256`` hashes each primary adapter together with every shared
execution contract, including the localization contract and its schema. Editing
any of those changes the profile execution identity, which changes the matching
execution-map identity and finally the exact map digest rendered by Helm. This
tool derives that whole chain before it writes either contract.

Run with ``--check`` in CI to fail on drift, or without it to rewrite the values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]
SOLUTION_ROOT = CONTROL_PLANE_ROOT.parents[1]
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
EXECUTION_MAP_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-execution-map.json"
PROFILE_OWNER_GLOBS = (
    "models/**/activation/fragment.json",
    "models/**/activation/workload-profile.json",
)

PRIMARY_FRAGMENT_SCHEMA = "fs2.nebius.ai/primary-scientific-activation-fragment/v1"
SECONDARY_PROJECTION_SCHEMA = "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1"
PROFILE_MERGE_TARGET = "catalog/runtime/contracts/scientific-workload-profiles.json"
PENDING_RESULT_ERROR = "PUBLIC_ACCEPTANCE_PENDING"

sys.path.insert(0, str(CONTROL_PLANE_ROOT / "src"))

from fs2_serve.scientific_batch.adapters.common import (  # noqa: E402
    ScientificAdapterError,
    runtime_recipe_sha256,
)

_IDENTITY_FIELDS = (
    "model_revision",
    "runtime_image_digest",
    "runtime_recipe_sha256",
    "workload_recipe_sha256",
    "artifact_manifest_digest",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _helm_to_json_bytes(value: object) -> bytes:
    """Match Go ``encoding/json`` bytes used by Helm/Sprig ``toJson``.

    Python and Go both emit compact, key-sorted JSON for this contract, but
    their defaults differ for non-ASCII and HTML-sensitive characters. Go
    retains UTF-8 while escaping ``<``, ``>``, ``&``, U+2028, and U+2029.
    """

    rendered = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    for character, escape in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        rendered = rendered.replace(character, escape)
    return rendered.encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _replace(value: dict[str, Any], key: str, expected: object, label: str, drifted: list[str]) -> None:
    recorded = value.get(key)
    if recorded == expected:
        return
    drifted.append(f"{label}: {recorded} -> {expected}")
    value[key] = expected


def _derive_contracts(
    profiles_document: dict[str, Any],
    execution_map: dict[str, Any],
    *,
    solution_root: Path,
    recipe_digest: Callable[[Path, str], str],
) -> list[str]:
    raw_profiles = profiles_document.get("profiles")
    raw_models = execution_map.get("models")
    if not isinstance(raw_profiles, list) or not isinstance(raw_models, list):
        raise SystemExit("scientific profile or execution-map contract is malformed")
    profiles = {
        profile["model_id"]: profile
        for profile in raw_profiles
        if isinstance(profile, dict) and isinstance(profile.get("model_id"), str)
    }
    if len(profiles) != len(raw_profiles):
        raise SystemExit("scientific workload profiles must have unique string model IDs")

    drifted: list[str] = []
    for model_id, profile in profiles.items():
        identity = profile.get("execution_identity")
        workload = profile.get("workload")
        if not isinstance(identity, dict) or not isinstance(workload, dict):
            raise SystemExit(f"{model_id} has no execution identity or workload recipe")
        try:
            expected_runtime_recipe = recipe_digest(solution_root, model_id)
        except ScientificAdapterError as error:
            raise SystemExit(f"{model_id} has no refreshable runtime recipe: {error}") from error
        _replace(
            identity,
            "runtime_recipe_sha256",
            expected_runtime_recipe,
            f"{model_id} runtime_recipe_sha256",
            drifted,
        )
        _replace(
            identity,
            "workload_recipe_sha256",
            _sha256(workload),
            f"{model_id} workload_recipe_sha256",
            drifted,
        )

    derived_identities: dict[str, str | None] = {}
    for model_id, profile in profiles.items():
        identity = profile.get("execution_identity")
        if not isinstance(identity, dict) or any(field not in identity for field in _IDENTITY_FIELDS):
            raise SystemExit(f"{model_id} has an incomplete execution identity")
        identity_payload = {key: value for key, value in identity.items() if key != "execution_identity_sha256"}
        expected_identity = (
            None if any(value is None for value in identity_payload.values()) else _sha256(identity_payload)
        )
        _replace(
            identity,
            "execution_identity_sha256",
            expected_identity,
            f"{model_id} execution_identity_sha256",
            drifted,
        )
        derived_identities[model_id] = expected_identity

    mapped_profiles: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for model in raw_models:
        if not isinstance(model, dict) or not isinstance(model.get("model_id"), str):
            raise SystemExit("scientific execution-map models must have string model IDs")
        model_id = model["model_id"]
        if model_id in seen_models:
            raise SystemExit(f"scientific execution map repeats {model_id}")
        seen_models.add(model_id)
        mapped_profile = profiles.get(model_id)
        identity = derived_identities.get(model_id)
        if mapped_profile is None:
            raise SystemExit(f"execution-map model {model_id} has no profile identity")
        if identity is None and mapped_profile.get("state") != "candidate-unqualified":
            raise SystemExit(f"execution-map model {model_id} has no complete profile identity")
        _replace(
            model,
            "execution_identity_sha256",
            identity,
            f"{model_id} execution-map identity",
            drifted,
        )
        mapped_profiles.append(mapped_profile)

    execution_map_sha256 = hashlib.sha256(_helm_to_json_bytes(execution_map)).hexdigest()
    for profile in mapped_profiles:
        model_id = profile["model_id"]
        qualification = profile.get("qualification")
        if profile.get("state") == "candidate-unqualified" and qualification is None:
            continue
        if not isinstance(qualification, dict):
            raise SystemExit(f"execution-map model {model_id} has no qualification contract")
        _replace(
            qualification,
            "execution_map_sha256",
            execution_map_sha256,
            f"{model_id} qualification execution_map_sha256",
            drifted,
        )
    return drifted


def _stage_payload(path: Path, payload: bytes, purpose: str) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.{purpose}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _atomic_write(payloads: dict[Path, bytes]) -> None:
    """Stage all bytes and roll back completed replacements on an error."""

    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    retained: set[Path] = set()
    try:
        for path, payload in payloads.items():
            staged[path] = _stage_payload(path, payload, "new")
        for path in payloads:
            backups[path] = _stage_payload(path, path.read_bytes(), "rollback")
        try:
            for path, temporary_path in staged.items():
                os.replace(temporary_path, path)
                replaced.append(path)
        except BaseException as replacement_error:
            rollback_errors: list[str] = []
            for path in reversed(replaced):
                backup = backups[path]
                try:
                    os.replace(backup, path)
                except OSError as rollback_error:
                    retained.add(backup)
                    rollback_errors.append(f"{path}: {rollback_error}")
            if rollback_errors:
                locations = ", ".join(str(path) for path in sorted(retained))
                detail = "; ".join(rollback_errors)
                raise RuntimeError(
                    f"contract update failed and rollback was incomplete ({detail}); backups retained at {locations}"
                ) from replacement_error
            raise
    finally:
        for temporary_path in (*staged.values(), *backups.values()):
            if temporary_path not in retained:
                temporary_path.unlink(missing_ok=True)


def _profile_owner_payloads(
    profiles: dict[str, dict[str, Any]],
    *,
    solution_root: Path,
    drifted: list[str],
) -> dict[Path, bytes]:
    """Project refreshed profiles back to their model-owned source documents.

    The canonical profile set and the model-owned activation records are one
    contract. Updating only the aggregate leaves onboarding, qualification and
    deployment readers with different execution identities.
    """

    owners: dict[str, tuple[Path, dict[str, Any], dict[str, Any]]] = {}
    for pattern in PROFILE_OWNER_GLOBS:
        for path in sorted(solution_root.glob(pattern)):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SystemExit(f"scientific profile owner is unreadable: {path}") from error
            if not isinstance(document, dict):
                continue
            schema = document.get("schema")
            if schema == PRIMARY_FRAGMENT_SCHEMA:
                projection = document.get("profile_projection")
                if not isinstance(projection, dict) or projection.get("merge_target") != PROFILE_MERGE_TARGET:
                    raise SystemExit(f"scientific primary profile owner is malformed: {path}")
                owned_profile = projection.get("profile")
                model_id = document.get("model_id")
            elif schema == SECONDARY_PROJECTION_SCHEMA:
                if document.get("merge_target") != PROFILE_MERGE_TARGET:
                    raise SystemExit(f"scientific secondary profile owner is malformed: {path}")
                owned_profile = document.get("profile")
                model_id = owned_profile.get("model_id") if isinstance(owned_profile, dict) else None
            else:
                continue
            if not isinstance(model_id, str) or not isinstance(owned_profile, dict):
                raise SystemExit(f"scientific profile owner has no model profile: {path}")
            if model_id in owners:
                raise SystemExit(f"scientific profile owner is duplicated for {model_id}")
            owners[model_id] = (path, document, owned_profile)

    if not owners:
        return {}
    if set(owners) != set(profiles):
        missing = sorted(set(profiles) - set(owners))
        extra = sorted(set(owners) - set(profiles))
        raise SystemExit(f"scientific profile owner set differs (missing={missing}, extra={extra})")

    payloads: dict[Path, bytes] = {}
    for model_id, profile in profiles.items():
        path, document, owned_profile = owners[model_id]
        primary = document.get("schema") == PRIMARY_FRAGMENT_SCHEMA
        if owned_profile != profile:
            if primary:
                document["profile_projection"]["profile"] = profile
            else:
                document["profile"] = profile
            drifted.append(f"{model_id} model-owned profile projection")
            payloads[path] = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if primary:
            pending_result = _pending_result_payload(
                model_id,
                profile,
                document,
                solution_root=solution_root,
                drifted=drifted,
            )
            if pending_result is not None:
                result_path, result_payload = pending_result
                payloads[result_path] = result_payload
    return payloads


def _pending_result_payload(
    model_id: str,
    profile: dict[str, Any],
    fragment: dict[str, Any],
    *,
    solution_root: Path,
    drifted: list[str],
) -> tuple[Path, bytes] | None:
    """Refresh only a non-success example result, never acceptance evidence."""

    public_fixtures = fragment.get("public_fixtures")
    if not isinstance(public_fixtures, dict):
        return None
    relative = public_fixtures.get("result")
    if not isinstance(relative, str) or not relative:
        raise SystemExit(f"{model_id} primary fragment has no public result fixture")
    root = solution_root.resolve()
    result_path = (root / relative).resolve()
    try:
        result_path.relative_to(root)
    except ValueError as error:
        raise SystemExit(f"{model_id} public result fixture escapes the solution root") from error
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"{model_id} public result fixture is unreadable: {result_path}") from error
    if not isinstance(result, dict):
        raise SystemExit(f"{model_id} public result fixture is malformed: {result_path}")
    if (
        result.get("terminal_status") != "failed"
        or not isinstance(result.get("semantic_validation"), dict)
        or result["semantic_validation"].get("status") != "not-run"
        or not isinstance(result.get("error"), dict)
        or result["error"].get("code") != PENDING_RESULT_ERROR
    ):
        raise SystemExit(f"{model_id} public result is not a refreshable pending fixture: {result_path}")

    profile_identity = profile.get("execution_identity")
    accepted = fragment.get("accepted_evidence")
    execution = fragment.get("execution_projection")
    if not isinstance(profile_identity, dict) or not isinstance(accepted, dict) or not isinstance(execution, dict):
        raise SystemExit(f"{model_id} primary fragment has no refreshable execution identity")
    source = accepted.get("source")
    image = accepted.get("runtime_image")
    artifact_inputs = execution.get("artifact_identity_inputs")
    if (
        not isinstance(source, dict)
        or not isinstance(image, dict)
        or not isinstance(artifact_inputs, list)
        or not all(isinstance(value, str) for value in artifact_inputs)
    ):
        raise SystemExit(f"{model_id} primary fragment has malformed execution identity inputs")
    if profile_identity.get("model_revision") != source.get("revision") or profile_identity.get(
        "runtime_image_digest"
    ) != image.get("digest"):
        raise SystemExit(f"{model_id} profile identity differs from accepted source or image")

    executable_identity = {
        "model_id": model_id,
        "variant_id": execution.get("variant_id"),
        "model_revision": source.get("revision"),
        "runtime_image_digest": image.get("digest"),
        "runtime_recipe_sha256": profile_identity.get("runtime_recipe_sha256"),
        "workload_recipe_sha256": profile_identity.get("workload_recipe_sha256"),
        "model_artifact_manifest_digest": hashlib.sha256(
            json.dumps(sorted(artifact_inputs), separators=(",", ":")).encode("ascii")
        ).hexdigest(),
    }
    expected_identity = {
        **executable_identity,
        "execution_identity_sha256": _sha256(executable_identity),
    }
    if result.get("execution_identity") == expected_identity:
        return None
    current_identity = result.get("execution_identity")
    if not isinstance(current_identity, dict) or set(current_identity) != set(expected_identity):
        raise SystemExit(f"{model_id} pending public result execution identity is malformed")
    result["execution_identity"] = expected_identity
    drifted.append(f"{model_id} pending public result execution identity")
    return result_path, (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift instead of rewriting")
    options = parser.parse_args(argv)

    profile_document = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    execution_map = json.loads(EXECUTION_MAP_PATH.read_text(encoding="utf-8"))
    if not isinstance(profile_document, dict) or not isinstance(execution_map, dict):
        raise SystemExit("scientific profile and execution-map contracts must be JSON objects")
    drifted = _derive_contracts(
        profile_document,
        execution_map,
        solution_root=SOLUTION_ROOT,
        recipe_digest=runtime_recipe_sha256,
    )
    profiles = {
        profile["model_id"]: profile
        for profile in profile_document["profiles"]
        if isinstance(profile, dict) and isinstance(profile.get("model_id"), str)
    }
    owner_payloads = _profile_owner_payloads(
        profiles,
        solution_root=SOLUTION_ROOT,
        drifted=drifted,
    )

    if not drifted:
        print("scientific runtime recipes are current")
        return 0
    if options.check:
        for row in drifted:
            print(f"drift: {row}")
        print("run scripts/refresh_scientific_recipes.py to update them")
        return 1
    _atomic_write(
        {
            PROFILE_PATH: (json.dumps(profile_document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            EXECUTION_MAP_PATH: (json.dumps(execution_map, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            **owner_payloads,
        }
    )
    for row in drifted:
        print(f"updated {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
