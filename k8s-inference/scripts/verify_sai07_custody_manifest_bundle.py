#!/usr/bin/env python3
"""Verify a signed manifest bundle for the standalone SAI-07 custody root."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "fs2-serve.nebius.ai/sai07-custody-manifest-bundle/v1"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
MAX_SIZE = 8 * 1024 * 1024
MAX_OBJECTS = 256
ALLOWED_KINDS = {
    ("apps/v1", "DaemonSet"),
    ("v1", "ConfigMap"),
    ("v1", "Secret"),
    ("v1", "ServiceAccount"),
    ("rbac.authorization.k8s.io/v1", "Role"),
    ("rbac.authorization.k8s.io/v1", "RoleBinding"),
    ("rbac.authorization.k8s.io/v1", "ClusterRole"),
    ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"),
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy"),
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding"),
    ("networking.k8s.io/v1", "NetworkPolicy"),
}
REQUIRED_OBJECTS = {
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-pod-security-custody-boundary",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-pod-security-custody-boundary",
    ),
    ("v1", "Secret", "fs2-system", "fs2-pod-security-token-anchor"),
    ("v1", "ServiceAccount", "fs2-system", "fs2-pod-security-rollout-custodian"),
    ("v1", "ServiceAccount", "fs2-system", "fs2-pod-security-metadata-reader"),
    (
        "rbac.authorization.k8s.io/v1",
        "Role",
        "fs2-models",
        "fs2-pod-security-secret-metadata-reader",
    ),
    (
        "rbac.authorization.k8s.io/v1",
        "RoleBinding",
        "fs2-models",
        "fs2-pod-security-secret-metadata-reader",
    ),
    (
        "rbac.authorization.k8s.io/v1",
        "Role",
        "fs2-system",
        "fs2-pod-security-token-anchor-metadata-reader",
    ),
    (
        "rbac.authorization.k8s.io/v1",
        "RoleBinding",
        "fs2-system",
        "fs2-pod-security-token-anchor-metadata-reader",
    ),
    (
        "rbac.authorization.k8s.io/v1",
        "Role",
        "fs2-system",
        "fs2-pod-security-metadata-reader-token-request",
    ),
    (
        "rbac.authorization.k8s.io/v1",
        "RoleBinding",
        "fs2-system",
        "fs2-pod-security-metadata-reader-token-request",
    ),
    (
        "rbac.authorization.k8s.io/v1",
        "Role",
        "fs2-system",
        "fs2-pod-security-rollout-token-request",
    ),
    (
        "rbac.authorization.k8s.io/v1",
        "RoleBinding",
        "fs2-system",
        "fs2-pod-security-rollout-token-request",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-node-observability-configs",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-node-observability-configs",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-node-observability-pods",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-node-observability-pods",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-node-observability-daemonsets",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-node-observability-daemonsets",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-snapshot-exact-profile",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-snapshot-exact-profile",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-pod-security-rollout-token-request",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-pod-security-rollout-token-request",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-pod-security-enforcement-fence",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-pod-security-enforcement-fence",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-pod-security-legacy-cleanup-fence",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-pod-security-legacy-cleanup-fence",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicy",
        "",
        "fs2-pod-security-rollout-ledger",
    ),
    (
        "admissionregistration.k8s.io/v1",
        "ValidatingAdmissionPolicyBinding",
        "",
        "fs2-pod-security-rollout-ledger",
    ),
    ("v1", "ConfigMap", "fs2-system", "fs2-pod-security-rollout-ledger"),
}


class BundleError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_regular(path: Path, label: str, limit: int = MAX_SIZE) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise BundleError(f"{label} path must be absolute without parent traversal")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise BundleError(f"{label} is not a bounded regular file")
        payload = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if len(payload) != before.st_size or (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise BundleError(f"{label} changed during its descriptor-fenced read")
        return payload
    finally:
        os.close(descriptor)


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BundleError(f"{label} fields differ from the v1 contract")
    return value


def verify_signature(bundle: dict[str, Any], public_key: bytes, key_id: str) -> None:
    signature = exact(bundle.get("signature"), {"algorithm", "key_id", "value"}, "signature")
    if signature["algorithm"] != "ed25519" or signature["key_id"] != key_id:
        raise BundleError("manifest signature authority differs")
    try:
        raw_signature = base64.b64decode(signature["value"], validate=True)
    except (binascii.Error, TypeError) as error:
        raise BundleError("manifest signature is not strict base64") from error
    if len(raw_signature) != 64:
        raise BundleError("Ed25519 signature must be exactly 64 bytes")
    unsigned = dict(bundle)
    del unsigned["signature"]
    descriptors: list[int] = []
    try:
        for label, payload in (("bundle", canonical(unsigned)), ("signature", raw_signature), ("key", public_key)):
            descriptor = os.memfd_create(f"fs2-sai07-custody-{label}", flags=0)
            descriptors.append(descriptor)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise BundleError("descriptor-fenced signature write made no progress")
                offset += written
            os.fsync(descriptor)
        completed = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                f"/proc/self/fd/{descriptors[2]}",
                "-rawin",
                "-in",
                f"/proc/self/fd/{descriptors[0]}",
                "-sigfile",
                f"/proc/self/fd/{descriptors[1]}",
            ],
            check=False,
            capture_output=True,
            timeout=10,
            pass_fds=tuple(descriptors),
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    if completed.returncode != 0:
        raise BundleError("whole manifest-bundle signature verification failed")


def validate(bundle: dict[str, Any], query: dict[str, str]) -> dict[str, str]:
    exact(
        bundle,
        {
            "schema",
            "cluster_id",
            "run_id",
            "kube_system_uid",
            "issued_at",
            "expires_at",
            "owner",
            "platform_exclusion",
            "iam_boundary_sha256",
            "objects_sha256",
            "objects",
            "signature",
        },
        "manifest bundle",
    )
    if bundle["schema"] != SCHEMA:
        raise BundleError("manifest-bundle schema is unsupported")
    if bundle["cluster_id"] != query["cluster_id"] or bundle["kube_system_uid"] != query["kube_system_uid"]:
        raise BundleError("manifest bundle belongs to another cluster")
    owner = exact(bundle["owner"], {"username", "groups"}, "owner")
    excluded = exact(bundle["platform_exclusion"], {"username", "groups"}, "platform exclusion")
    if (
        owner["username"] != query["owner_username"]
        or owner["groups"] != [query["owner_group"]]
        or excluded["username"] != query["platform_username"]
        or excluded["groups"] != [query["platform_group"]]
        or owner["username"] == excluded["username"]
        or any(str(value).startswith("system:") for value in (owner["username"], excluded["username"]))
    ):
        raise BundleError("custody owner and excluded platform identities differ from the contract")
    if bundle["iam_boundary_sha256"] != query["iam_boundary_sha256"] or not SHA256_RE.fullmatch(
        str(bundle["iam_boundary_sha256"])
    ):
        raise BundleError("external IAM boundary receipt digest differs")
    issued = dt.datetime.fromisoformat(str(bundle["issued_at"]).removesuffix("Z") + "+00:00")
    expires = dt.datetime.fromisoformat(str(bundle["expires_at"]).removesuffix("Z") + "+00:00")
    now = dt.datetime.now(dt.UTC)
    if issued.tzinfo != dt.UTC or expires.tzinfo != dt.UTC or issued > now + dt.timedelta(seconds=30) or now > expires:
        raise BundleError("manifest bundle is not currently valid")
    if now - issued > dt.timedelta(minutes=15) or expires - issued > dt.timedelta(hours=1):
        raise BundleError("manifest bundle freshness or lifetime exceeds the contract")
    objects = bundle["objects"]
    if not isinstance(objects, list) or not 1 <= len(objects) <= MAX_OBJECTS:
        raise BundleError("manifest object count is outside the bounded contract")
    if hashlib.sha256(canonical(objects)).hexdigest() != bundle["objects_sha256"]:
        raise BundleError("manifest object aggregate digest differs")
    manifests: dict[str, dict[str, Any]] = {}
    existing_imports: dict[str, str] = {}
    observed: set[tuple[str, str, str, str]] = set()
    for index, entry_raw in enumerate(objects):
        entry = exact(entry_raw, {"manifest", "live_identity"}, f"objects[{index}]")
        manifest = entry["manifest"]
        if not isinstance(manifest, dict) or set(manifest) - {"apiVersion", "kind", "metadata", "spec", "data", "type", "immutable", "automountServiceAccountToken", "secrets", "imagePullSecrets", "rules", "roleRef", "subjects"}:
            raise BundleError("custody manifest contains an unsupported field")
        api_version = manifest.get("apiVersion")
        kind = manifest.get("kind")
        metadata = manifest.get("metadata")
        if (api_version, kind) not in ALLOWED_KINDS or not isinstance(metadata, dict):
            raise BundleError("custody manifest kind is outside the allowlist")
        name = metadata.get("name")
        namespace = metadata.get("namespace", "")
        identity = (api_version, kind, namespace, name)
        if not all(isinstance(value, str) for value in identity) or not name or identity in observed:
            raise BundleError("custody manifest identity is incomplete or duplicated")
        if set(metadata) - {"name", "namespace", "labels", "annotations"}:
            raise BundleError("custody desired metadata contains live or destructive fields")
        labels = metadata.get("labels")
        if not isinstance(labels, dict) or labels.get("security.fs2.nebius.ai/custody-owner") != "external":
            raise BundleError("every custody manifest must carry the external-owner label")
        if kind == "Secret":
            annotations = metadata.get("annotations")
            if (
                identity not in REQUIRED_OBJECTS
                or labels
                != {
                    "security.fs2.nebius.ai/custody-owner": "external",
                    "security.fs2.nebius.ai/role": "token-anchor",
                }
                or not isinstance(annotations, dict)
                or set(annotations)
                != {"security.fs2.nebius.ai/custody-epoch-sha256"}
                or not SHA256_RE.fullmatch(
                    str(annotations["security.fs2.nebius.ai/custody-epoch-sha256"])
                )
                or manifest.get("immutable") is not True
                or manifest.get("type") != "Opaque"
                or manifest.get("data", {}) != {}
                or "stringData" in manifest
            ):
                raise BundleError("only the immutable empty token-anchor Secret is permitted")
        if kind == "ServiceAccount" and (
            manifest.get("automountServiceAccountToken") is not False
            or manifest.get("secrets", []) != []
            or manifest.get("imagePullSecrets", []) != []
        ):
            raise BundleError("custody ServiceAccounts must be tokenless and reference-free")
        if identity == (
            "rbac.authorization.k8s.io/v1",
            "Role",
            "fs2-models",
            "fs2-pod-security-secret-metadata-reader",
        ) and manifest.get("rules") != [
            {"apiGroups": [""], "resources": ["secrets"], "verbs": ["list"]}
        ]:
            raise BundleError("metadata reader Role must grant only Secret list")
        if identity == (
            "rbac.authorization.k8s.io/v1",
            "Role",
            "fs2-system",
            "fs2-pod-security-token-anchor-metadata-reader",
        ) and manifest.get("rules") != [
            {"apiGroups": [""], "resources": ["secrets"], "verbs": ["list"]}
        ]:
            raise BundleError("token-anchor metadata Role must grant only Secret list")
        if identity == (
            "rbac.authorization.k8s.io/v1",
            "Role",
            "fs2-system",
            "fs2-pod-security-metadata-reader-token-request",
        ) and manifest.get("rules") != [
            {
                "apiGroups": [""],
                "resourceNames": ["fs2-pod-security-metadata-reader"],
                "resources": ["serviceaccounts/token"],
                "verbs": ["create"],
            }
        ]:
            raise BundleError("metadata token Role must grant only the exact TokenRequest edge")
        if identity == (
            "rbac.authorization.k8s.io/v1",
            "Role",
            "fs2-system",
            "fs2-pod-security-rollout-token-request",
        ) and manifest.get("rules") != [
            {
                "apiGroups": [""],
                "resourceNames": ["fs2-pod-security-rollout-custodian"],
                "resources": ["serviceaccounts/token"],
                "verbs": ["create"],
            }
        ]:
            raise BundleError("receipt token Role must grant only the exact TokenRequest edge")
        if identity == (
            "rbac.authorization.k8s.io/v1",
            "RoleBinding",
            "fs2-models",
            "fs2-pod-security-secret-metadata-reader",
        ) and (
            manifest.get("roleRef")
            != {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "fs2-pod-security-secret-metadata-reader",
            }
            or manifest.get("subjects")
            != [
                {
                    "kind": "ServiceAccount",
                    "name": "fs2-pod-security-metadata-reader",
                    "namespace": "fs2-system",
                }
            ]
        ):
            raise BundleError("Secret metadata RoleBinding subject or role differs")
        if identity == (
            "rbac.authorization.k8s.io/v1",
            "RoleBinding",
            "fs2-system",
            "fs2-pod-security-token-anchor-metadata-reader",
        ) and (
            manifest.get("roleRef")
            != {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "fs2-pod-security-token-anchor-metadata-reader",
            }
            or manifest.get("subjects")
            != [
                {
                    "kind": "ServiceAccount",
                    "name": "fs2-pod-security-metadata-reader",
                    "namespace": "fs2-system",
                }
            ]
        ):
            raise BundleError("token-anchor metadata RoleBinding subject or role differs")
        if identity == (
            "rbac.authorization.k8s.io/v1",
            "RoleBinding",
            "fs2-system",
            "fs2-pod-security-metadata-reader-token-request",
        ) and (
            manifest.get("roleRef")
            != {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "fs2-pod-security-metadata-reader-token-request",
            }
            or manifest.get("subjects")
            != [
                {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Group",
                    "name": "fs2-pod-security-receipt-custodians",
                }
            ]
        ):
            raise BundleError("metadata TokenRequest RoleBinding subject or role differs")
        if identity == (
            "rbac.authorization.k8s.io/v1",
            "RoleBinding",
            "fs2-system",
            "fs2-pod-security-rollout-token-request",
        ) and (
            manifest.get("roleRef")
            != {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "fs2-pod-security-rollout-token-request",
            }
            or manifest.get("subjects")
            != [
                {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Group",
                    "name": "fs2-pod-security-receipt-custodians",
                }
            ]
        ):
            raise BundleError("receipt TokenRequest RoleBinding subject or role differs")
        if kind in {"Role", "ClusterRole"}:
            rules = manifest.get("rules")
            if not isinstance(rules, list):
                raise BundleError("custody RBAC rules are missing")
            for rule in rules:
                if not isinstance(rule, dict):
                    raise BundleError("custody RBAC rule is malformed")
                verbs = set(rule.get("verbs", []))
                resources = set(rule.get("resources", []))
                api_groups = set(rule.get("apiGroups", []))
                if "*" in verbs | resources | api_groups or verbs & {"impersonate", "bind", "escalate"}:
                    raise BundleError("custody RBAC may not contain wildcard or pivot authority")
                if "secrets" in resources and identity not in {
                    (
                        "rbac.authorization.k8s.io/v1",
                        "Role",
                        "fs2-models",
                        "fs2-pod-security-secret-metadata-reader",
                    ),
                    (
                        "rbac.authorization.k8s.io/v1",
                        "Role",
                        "fs2-system",
                        "fs2-pod-security-token-anchor-metadata-reader",
                    ),
                }:
                    raise BundleError("only the bounded metadata reader may list Secrets")
                if "serviceaccounts/token" in resources and not rule.get("resourceNames"):
                    raise BundleError("TokenRequest authority must be exact-name bounded")
        if kind == "NetworkPolicy" and namespace == "fs2-models":
            spec = manifest.get("spec")
            if not isinstance(spec, dict) or set(spec.get("policyTypes", [])) != {"Ingress", "Egress"} or spec.get("ingress", []) != [] or spec.get("egress", []) != []:
                raise BundleError("adopted legacy NetworkPolicy must be deny-only")
        live_identity = exact(
            entry["live_identity"],
            {"present", "uid", "resource_version", "object_sha256"},
            f"objects[{index}].live_identity",
        )
        if live_identity["present"] is True:
            if not all(
                isinstance(live_identity[field], str) and live_identity[field]
                for field in ("uid", "resource_version", "object_sha256")
            ) or not SHA256_RE.fullmatch(live_identity["object_sha256"]):
                raise BundleError("custody adoption identity is incomplete")
        elif live_identity != {
            "present": False,
            "uid": None,
            "resource_version": None,
            "object_sha256": None,
        }:
            raise BundleError("an absent custody object may not carry a substitutable identity")
        key = "/".join(identity)
        manifests[key] = manifest
        if live_identity["present"]:
            import_components = [f"apiVersion={api_version}", f"kind={kind}"]
            if namespace:
                import_components.append(f"namespace={namespace}")
            import_components.append(f"name={name}")
            existing_imports[key] = ",".join(import_components)
        observed.add(identity)
    if not REQUIRED_OBJECTS.issubset(observed):
        raise BundleError("manifest bundle omits mandatory custody, ledger, token, or fence objects")
    custody_policy = manifests[
        "/".join(
            (
                "admissionregistration.k8s.io/v1",
                "ValidatingAdmissionPolicy",
                "",
                "fs2-pod-security-custody-boundary",
            )
        )
    ]
    custody_spec = custody_policy.get("spec")
    if not isinstance(custody_spec, dict) or custody_spec.get("failurePolicy") != "Fail":
        raise BundleError("external custody admission must fail closed")
    custody_rules = custody_spec.get("matchConstraints", {}).get("resourceRules", [])
    if not any(
        isinstance(rule, dict)
        and rule.get("apiGroups") == [""]
        and rule.get("apiVersions") == ["v1"]
        and rule.get("operations") == ["CREATE", "UPDATE", "DELETE"]
        and "secrets" in rule.get("resources", [])
        for rule in custody_rules
    ):
        raise BundleError("external custody admission omits token-anchor Secret writes")
    custody_expressions = [
        item.get("expression")
        for item in custody_spec.get("validations", [])
        if isinstance(item, dict) and isinstance(item.get("expression"), str)
    ]
    anchor_fragments = (
        "fs2-pod-security-token-anchor",
        "request.operation == 'CREATE'",
        "request.userInfo.username !=",
        "object.metadata.namespace == 'fs2-system'",
        "object.immutable == true",
        "object.type == 'Opaque'",
        "object.data == {}",
        "!has(object.stringData)",
        "security.fs2.nebius.ai/custody-epoch-sha256",
        "security.fs2.nebius.ai/role",
        "token-anchor",
        "authentication.kubernetes.io/credential-id",
    )
    if any(
        not any(fragment in expression for expression in custody_expressions)
        for fragment in anchor_fragments
    ):
        raise BundleError(
            "external custody admission does not enforce the exact immutable empty token anchor"
        )
    token_policy = manifests[
        "/".join(
            (
                "admissionregistration.k8s.io/v1",
                "ValidatingAdmissionPolicy",
                "",
                "fs2-pod-security-rollout-token-request",
            )
        )
    ]
    token_spec = token_policy.get("spec")
    if not isinstance(token_spec, dict) or token_spec.get("failurePolicy") != "Fail":
        raise BundleError("TokenRequest admission must fail closed")
    rules = token_spec.get("matchConstraints", {}).get("resourceRules")
    if rules != [
        {
            "apiGroups": [""],
            "apiVersions": ["v1"],
            "operations": ["CREATE"],
            "resources": ["serviceaccounts/token"],
        }
    ]:
        raise BundleError("TokenRequest admission scope differs from the exact subresource")
    condition_expressions = {
        item.get("expression")
        for item in token_spec.get("matchConditions", [])
        if isinstance(item, dict)
    }
    if condition_expressions != {
        "request.name in ['fs2-pod-security-metadata-reader','fs2-pod-security-rollout-custodian']"
    }:
        raise BundleError("TokenRequest admission must match exactly the two bounded readers")
    validations = token_spec.get("validations")
    validation_expressions = {
        item.get("expression") for item in validations or [] if isinstance(item, dict)
    }
    required_fragments = (
        "fs2-pod-security-receipt-custodians",
        "authentication.kubernetes.io/credential-id",
        "https://kubernetes.default.svc",
        "expirationSeconds <= 600",
        "has(object.spec.boundObjectRef)",
        "object.spec.boundObjectRef.apiVersion == 'v1'",
        "object.spec.boundObjectRef.kind == 'Secret'",
        "object.spec.boundObjectRef.name == 'fs2-pod-security-token-anchor'",
        "object.spec.boundObjectRef.uid != ''",
    )
    if not validation_expressions or any(
        not any(
            fragment in expression
            for expression in validation_expressions
            if isinstance(expression, str)
        )
        for fragment in required_fragments
    ):
        raise BundleError("TokenRequest admission omits external identity, lifetime, or boundObjectRef")
    return {
        "valid": "true",
        "objects_sha256": bundle["objects_sha256"],
        "manifests_json": json.dumps(manifests, sort_keys=True, separators=(",", ":")),
        "existing_imports_json": json.dumps(
            existing_imports, sort_keys=True, separators=(",", ":")
        ),
        "object_count": str(len(manifests)),
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        required = {
            "bundle_path",
            "public_key_path",
            "public_key_sha256",
            "key_id",
            "cluster_id",
            "kube_system_uid",
            "owner_username",
            "owner_group",
            "platform_username",
            "platform_group",
            "iam_boundary_sha256",
        }
        if not isinstance(query, dict) or set(query) != required or not all(isinstance(query[key], str) for key in required):
            raise BundleError("external query fields differ from the v1 contract")
        payload = read_regular(Path(query["bundle_path"]), "manifest bundle")
        public_key = read_regular(Path(query["public_key_path"]), "manifest public key", 65536)
        if hashlib.sha256(public_key).hexdigest() != query["public_key_sha256"]:
            raise BundleError("manifest public key digest differs")
        bundle = json.loads(payload)
        if not isinstance(bundle, dict) or canonical(bundle) != payload:
            raise BundleError("manifest bundle must be canonical JSON")
        verify_signature(bundle, public_key, query["key_id"])
        result = validate(bundle, query)
    except (BundleError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"SAI-07 custody manifest rejected: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
