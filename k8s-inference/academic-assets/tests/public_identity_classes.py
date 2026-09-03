"""Identity classes that must never appear in committed public evidence.

Content digests, sizes, counts, modes, resource kinds and semantic results are
evidence and stay. What must not ship is anything that identifies one live
deployment: registries, filesystems, volumes, projects, clusters and contexts.
Those belong in the private Task Deck evidence only.

Shared by the confidentiality scanner and the public-evidence tests so both
enforce the same definition.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

WITHHELD = "withheld"

# Field names whose value identifies a specific deployment. A committed file may
# omit them or set them to WITHHELD; a real value is a finding.
IDENTITY_FIELDS = frozenset(
    {
        "cluster_context",
        "cluster_id",
        "cluster_name",
        "context",
        "filesystem_id",
        "filesystem_name",
        "kubeconfig",
        "project_id",
        "project_name",
        "pv_name",
        "pv_uid",
        "pvc_uid",
        "registry_id",
        "repository",
        "volume_handle",
        "volume_name",
    }
)

# Values that are deployment identities wherever they appear.
IDENTITY_VALUE_PATTERNS = (
    (
        "opaque cloud resource ID",
        re.compile(
            r"\b(?:project|tenant|mk8scluster|mk8snodegroup|vpcnetwork|vpcsubnet|"
            r"computeinstance|computefilesystem|serviceaccount|containerregistry|registry)"
            r"-e[0-9a-z]{4,}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "kubernetes volume identity",
        re.compile(
            r"\bpvc-(?!0{8}-0{4}-0{4}-0{4}-0{12}\b)(?!1{8}-2{4}-3{4}-4{4}-5{12}\b)"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-",
            re.IGNORECASE,
        ),
    ),
    (
        # Any Kubernetes object UUID identifies one live cluster object. Example and
        # all-zero UUIDs are placeholders and are allowed.
        "kubernetes object UUID",
        re.compile(
            r"\b(?!0{8}-0{4}-0{4}-0{4}-0{12}\b)(?!1{8}-2{4}-3{4}-4{4}-5{12}\b)"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
    ),
    ("registry account path", re.compile(r"\bcr\.[a-z0-9-]+\.nebius\.cloud/\S+", re.IGNORECASE)),
    ("absolute developer home", re.compile("/" + r"(?:home|Users)/[A-Za-z0-9._-]+(?:/|\b)")),
    ("legacy private source layout", re.compile("platform/" + "fs2-serve" + r"(?:/|\b)")),
)

# Under these containers a plain "name" is also a deployment identity.
IDENTITY_PARENTS = frozenset(
    {
        "canonical_volume",
        "cluster",
        "cluster_cache",
        "environment",
        "private_cache",
        "private_registry",
        "retained_quarantine_volume",
        "target",
    }
)

_ALLOWED_PLACEHOLDERS = frozenset({WITHHELD, "", "not-exported"})


def walk(node: Any, path: str = "") -> Iterator[tuple[str, str, Any]]:
    """Yield (path, key, value) for every leaf in a JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if isinstance(value, (dict, list)):
                yield from walk(value, child)
            else:
                yield child, key, value
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child = f"{path}[{index}]"
            if isinstance(value, (dict, list)):
                yield from walk(value, child)
            else:
                yield child, path.rsplit(".", 1)[-1], value


def findings(document: Any) -> list[str]:
    """Return every deployment identity that would be exported by this document."""
    found: list[str] = []
    for path, key, value in walk(document):
        if not isinstance(value, str):
            continue
        parent = path.rsplit(".", 2)[-2] if path.count(".") >= 1 else ""
        identity_key = key in IDENTITY_FIELDS or (key == "name" and parent in IDENTITY_PARENTS)
        if identity_key and value not in _ALLOWED_PLACEHOLDERS:
            if not value.startswith("$") and not value.startswith("<"):
                found.append(f"{path}: identity field carries a live value: {value!r}")
                continue
        for label, pattern in IDENTITY_VALUE_PATTERNS:
            if pattern.search(value):
                found.append(f"{path}: {label}: {value!r}")
                break
    return found
