#!/usr/bin/env python3
"""Independent semantic YAML image closure for the SAI-24 release gate.

The span lexer is intentionally not trusted as the semantic authority.  This
module uses the independently maintained PyYAML parser to reject YAML graph
features that can change mapping meaning, walks the composed node graph, and
then requires its image multiset to equal the lexer's rewrite targets.
"""

from __future__ import annotations

from collections import Counter
import os
from .execution_toolchain import (
    ToolchainError,
    environment_toolchain,
    validate_current_python,
)
try:
    import yaml
    from yaml.events import AliasEvent
    from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
except ImportError as exc:  # pragma: no cover - exercised by installer preflight
    raise RuntimeError(
        "PyYAML 6.0.2 is required by the independent image semantic gate"
    ) from exc

from .yaml_image_references import ImageScalar, YamlImageError, image_scalars


PY_YAML_VERSION = "6.0.2"


class SemanticYamlError(ValueError):
    """Raised when semantic YAML cannot be proven safe for image rewriting."""


def _mapping_key(key: Node, *, location: str) -> str | None:
    if not isinstance(key, ScalarNode):
        return None
    if key.value == "<<":
        raise SemanticYamlError(f"{location}: YAML merge keys are not permitted")
    return key.value


def _walk(node: Node, *, location: str, images: list[str]) -> None:
    if isinstance(node, MappingNode):
        seen_keys: set[str] = set()
        for index, pair in enumerate(node.value):
            key, value = pair
            child_location = f"{location}.mapping[{index}]"
            key_value = _mapping_key(key, location=child_location)
            if key_value is not None:
                if key_value in seen_keys:
                    raise SemanticYamlError(
                        f"{child_location}: duplicate mapping key {key_value!r}"
                    )
                seen_keys.add(key_value)
            if key_value == "image":
                if (
                    not isinstance(value, ScalarNode)
                    or value.tag != "tag:yaml.org,2002:str"
                ):
                    raise SemanticYamlError(
                        f"{child_location}: image must be one string scalar"
                    )
                if not value.value:
                    raise SemanticYamlError(
                        f"{child_location}: image scalar must not be empty"
                    )
                images.append(value.value)
            _walk(value, location=child_location, images=images)
        return
    if isinstance(node, SequenceNode):
        for index, value in enumerate(node.value):
            _walk(value, location=f"{location}[{index}]", images=images)


def semantic_image_references(source: str) -> list[str]:
    """Parse YAML independently and return semantic image scalar values."""

    if os.environ.get("FS2_EXTERNAL_CAPSULE_ACTIVE") == "1":
        try:
            lock_path, trust_path = environment_toolchain()
            validate_current_python(lock_path=lock_path, trust_path=trust_path)
        except ToolchainError as exc:
            raise SemanticYamlError(
                f"independent parser runtime is not trusted: {exc}"
            ) from exc
    if getattr(yaml, "__version__", None) != PY_YAML_VERSION:
        raise SemanticYamlError(
            f"independent parser must be PyYAML {PY_YAML_VERSION}"
        )
    try:
        events = list(yaml.parse(source, Loader=yaml.SafeLoader))
        if any(
            isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None
            for event in events
        ):
            raise SemanticYamlError("YAML anchors and aliases are not permitted")
        documents = list(yaml.compose_all(source, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        raise SemanticYamlError(f"independent YAML parse failed: {exc}") from exc

    images: list[str] = []
    for index, document in enumerate(documents):
        if document is not None:
            _walk(document, location=f"document[{index}]", images=images)
    return images


def independently_validated_image_scalars(source: str) -> list[ImageScalar]:
    """Return rewrite spans only when two independent parsers agree exactly."""

    try:
        lexical = image_scalars(source)
    except YamlImageError as exc:
        raise SemanticYamlError(str(exc)) from exc
    semantic = semantic_image_references(source)
    lexical_values = [scalar.reference for scalar in lexical]
    if Counter(lexical_values) != Counter(semantic):
        raise SemanticYamlError(
            "semantic YAML image closure differs from lexical rewrite targets"
        )
    return lexical
