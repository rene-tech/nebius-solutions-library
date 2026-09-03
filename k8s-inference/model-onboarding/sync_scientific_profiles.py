#!/usr/bin/env python3
"""Deterministically render canonical scientific profiles from ModelDefinitions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import compile_model

ROOT = Path(__file__).resolve().parent.parent
DECLARATION_ROOT = Path(__file__).resolve().parent / "declarations/cancer-immunotherapy"
TARGET = ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
RECEIPT_TARGET = ROOT / "catalog/runtime/contracts/scientific-source-candidate-receipts.json"
PROFILE_SCHEMA = ROOT / "catalog/runtime/schema/scientific-workload-profile.schema.json"
PROFILE_SET_SCHEMA = ROOT / "catalog/runtime/schema/scientific-workload-profiles.schema.json"


def render() -> bytes:
    profiles = []
    pairs: set[tuple[str, str]] = set()
    tools: set[str] = set()
    repositories: set[str] = set()
    default_models: set[str] = set()
    for path in sorted(DECLARATION_ROOT.glob("*.json")):
        declaration = compile_model.load_declaration(path)
        compile_model._validate_collisions(declaration, ROOT)
        profile = compile_model.compile_scientific_profile(declaration, ROOT)
        pair = (profile["model_id"], profile["variant_id"])
        tool = profile["interface"]["mcp"]["tool_name"]
        repository = profile["execution_identity"]["runtime_image_repository"]
        if pair in pairs:
            raise compile_model.OnboardingError(f"duplicate scientific profile: {pair}")
        if tool in tools:
            raise compile_model.OnboardingError(f"duplicate scientific MCP tool: {tool}")
        if repository in repositories:
            raise compile_model.OnboardingError(f"duplicate scientific runtime repository: {repository}")
        if profile["default_variant"] and profile["model_id"] in default_models:
            raise compile_model.OnboardingError(f"duplicate default scientific variant: {profile['model_id']}")
        pairs.add(pair)
        tools.add(tool)
        repositories.add(repository)
        if profile["default_variant"]:
            default_models.add(profile["model_id"])
        profiles.append(profile)
    document = {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profiles/v1",
        "profiles": sorted(profiles, key=lambda item: (item["model_id"], item["variant_id"])),
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def render_profile_set_schema() -> bytes:
    """Embed the canonical item schema so collection validation is fail-closed."""

    profile_schema = json.loads(PROFILE_SCHEMA.read_text(encoding="utf-8"))
    embedded = {
        key: value
        for key, value in profile_schema.items()
        if key not in {"$schema", "$id", "$defs", "title"}
    }
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://fs2-serve.nebius.ai/schema/scientific-workload-profiles/v1",
        "title": "Scientific workload profile set",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "profiles"],
        "properties": {
            "schema": {"const": "fs2-serve.nebius.ai/scientific-workload-profiles/v1"},
            "profiles": {"type": "array", "items": {"$ref": "#/$defs/profile"}},
        },
        "$defs": {"profile": embedded, **profile_schema["$defs"]},
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def render_receipts() -> bytes:
    receipts = []
    for path in sorted(DECLARATION_ROOT.glob("*.json")):
        declaration = compile_model.load_declaration(path)
        model = declaration["model"]
        source = model["source"]
        batch = declaration["batch"]
        receipts.append(
            {
                "model_id": model["id"],
                "variant_id": model["variant_id"],
                "upstream_name": model["display_name"],
                "backend_identity": "native-upstream",
                "status": "candidate",
                "qualification_state": "source-qualified",
                "observation_method": "source-qualification-record",
                "source": {
                    "kind": source["kind"],
                    "repository": source["repository"],
                    "revision": source["revision"],
                    "review_url": source["review"]["url"],
                },
                "access_profile": batch["access_profile"],
                "access_state": batch["access_state"],
                "notes": source["review"]["summary"],
            }
        )
    document = {
        "schema": "fs2-serve.nebius.ai/scientific-source-candidate-receipts/v1",
        "observed_on": "2026-09-02",
        "receipts": sorted(receipts, key=lambda item: (item["model_id"], item["variant_id"])),
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    expected_receipts = render_receipts()
    expected_schema = render_profile_set_schema()
    if args.check:
        if not TARGET.is_file() or TARGET.read_bytes() != expected:
            raise SystemExit("scientific workload profiles are not synchronized")
        if not RECEIPT_TARGET.is_file() or RECEIPT_TARGET.read_bytes() != expected_receipts:
            raise SystemExit("scientific source receipts are not synchronized")
        if not PROFILE_SET_SCHEMA.is_file() or PROFILE_SET_SCHEMA.read_bytes() != expected_schema:
            raise SystemExit("scientific workload profile set schema is not synchronized")
        print(f"scientific workload profiles: PASS ({len(json.loads(expected)['profiles'])})")
        return 0
    TARGET.write_bytes(expected)
    RECEIPT_TARGET.write_bytes(expected_receipts)
    PROFILE_SET_SCHEMA.write_bytes(expected_schema)
    print(f"wrote {TARGET}")
    print(f"wrote {RECEIPT_TARGET}")
    print(f"wrote {PROFILE_SET_SCHEMA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
