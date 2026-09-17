#!/usr/bin/env python3
"""Bind the v2 live manifest proof to raw v3 platform-state evidence.

The retained v2 live verifier already performs immediate UID,
resourceVersion and full-object hash reads.  This wrapper additionally requires
that its signed object inventory contains exactly the custody addresses derived
from the raw, version-fenced platform state.  No import or state transfer is
authorized: the platform state remains authoritative for every address.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import verify_sai07_custody_manifest_bundle as v1
import verify_sai07_custody_manifest_bundle_v2 as v2


class BundleV3Error(ValueError):
    pass


def validate(bundle: dict[str, object], query: dict[str, str], trust: dict[str, str]) -> dict[str, str]:
    state = bundle.get("platform_state")
    if not isinstance(state, dict):
        raise BundleV3Error("manifest bundle omits its platform-state inventory")
    objects = state.get("objects")
    if not isinstance(objects, list):
        raise BundleV3Error("manifest bundle platform-state objects are malformed")
    addresses: list[str] = []
    for index, item in enumerate(objects):
        if not isinstance(item, dict) or not isinstance(item.get("state_address"), str):
            raise BundleV3Error(f"platform-state object {index} omits its exact address")
        addresses.append(item["state_address"])
    if addresses != sorted(set(addresses)):
        raise BundleV3Error("manifest platform-state addresses must be sorted and unique")
    expected_addresses = json.loads(trust["custody_addresses_json"])
    if addresses != expected_addresses:
        raise BundleV3Error("signed manifest inventory differs from raw backend state addresses")
    if hashlib.sha256(v1.canonical(addresses)).hexdigest() != trust["state_addresses_sha256"]:
        raise BundleV3Error("manifest address aggregate differs from raw backend state")
    # v2's state-object hash is the signed, live-identity projection.  It is
    # not accepted as backend truth here: raw state independently supplies the
    # exact address set, and v2 immediately rereads every UID/RV/object hash.
    trust_for_v2 = dict(trust)
    trust_for_v2["state_objects_sha256"] = state.get("objects_sha256")
    return {
        **v2.validate(bundle, query, trust_for_v2),
        "platform_state_addresses_sha256": trust["state_addresses_sha256"],
        "platform_state_retained": "true",
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {"bundle_path", "verified_trust_json", "owner_kubeconfig_path", "owner_context"}
        if (
            not isinstance(query, dict)
            or set(query) != required
            or not all(isinstance(query[field], str) for field in required)
        ):
            raise BundleV3Error("external query fields differ from the v3 contract")
        trust = json.loads(query["verified_trust_json"])
        if not isinstance(trust, dict) or trust.get("valid") != "true":
            raise BundleV3Error("authoritative v3 trust result is absent or invalid")
        payload = v1.read_regular(Path(query["bundle_path"]), "manifest bundle")
        bundle = json.loads(payload)
        if not isinstance(bundle, dict) or v1.canonical(bundle) != payload:
            raise BundleV3Error("manifest bundle must be canonical JSON")
        key = v1.read_regular(Path(trust["authority_public_key_path"]), "manifest authority key", 65536)
        if hashlib.sha256(key).hexdigest() != trust["authority_public_key_sha256"]:
            raise BundleV3Error("manifest authority key differs after v3 trust verification")
        v1.verify_signature(bundle, key, trust["authority_key_id"])
        result = {
            **validate(bundle, query, trust),
            "bundle_sha256": hashlib.sha256(payload).hexdigest(),
        }
    except (
        BundleV3Error,
        v1.BundleError,
        v2.BundleV2Error,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"SAI-07 custody manifest v3 rejected: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
