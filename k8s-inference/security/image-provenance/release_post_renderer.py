#!/usr/bin/env python3
"""Helm post-renderer that stamps the current reviewed-source session.

The admission webhook, not these annotations, is the content authority: it
hashes each actual defaulted AdmissionReview object and requires the matching
owner-signed plan grant. The annotations bind the request to that plan's
source/session and make direct writes follow the same path as Helm SQL.
"""

from __future__ import annotations

import os
import sys

import yaml

ANNOTATIONS = {
    "security.fs2.nebius.ai/source-commit": "FS2_SOURCE_COMMIT",
    "security.fs2.nebius.ai/source-tree": "FS2_SOURCE_TREE",
    "security.fs2.nebius.ai/release-authorization": (
        "FS2_RELEASE_AUTHORIZATION_SHA256"
    ),
    "security.fs2.nebius.ai/release-session": "FS2_RELEASE_SESSION",
}


def render(payload: str) -> str:
    values = {}
    for annotation, variable in ANNOTATIONS.items():
        value = os.environ.get(variable, "")
        if not value:
            raise ValueError(f"required release binding {variable} is absent")
        values[annotation] = value
    documents = []
    for document in yaml.safe_load_all(payload):
        if not document:
            continue
        if not isinstance(document, dict):
            raise ValueError("Helm rendered a non-object YAML document")
        metadata = document.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Helm object metadata is not an object")
        annotations = metadata.setdefault("annotations", {})
        if not isinstance(annotations, dict):
            raise ValueError("Helm object annotations are not an object")
        for key, value in values.items():
            existing = annotations.get(key)
            if existing not in (None, value):
                raise ValueError(f"Helm object carries conflicting {key}")
            annotations[key] = value
        documents.append(document)
    if not documents:
        raise ValueError("Helm rendered no resources")
    return yaml.safe_dump_all(documents, sort_keys=True)


def main() -> int:
    try:
        sys.stdout.write(render(sys.stdin.read()))
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"release post-render refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
