#!/usr/bin/env python3
"""Locate image mapping scalars in YAML without regex-shaped blind spots.

The release gate cannot depend on whichever YAML package happens to be present
on an operator host.  This small lexer therefore recognizes the YAML forms the
gate needs: block and flow mappings, quoted or plain ``image`` keys, quoted or
plain scalar values, comments, and block scalar bodies.  It deliberately fails
closed when an image value is absent, composite, aliased, tagged, or otherwise
not one exact scalar that can be rewritten without changing document shape.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


class YamlImageError(ValueError):
    """Raised when an image mapping cannot be closed over safely."""


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    start: int
    end: int
    line: int
    column: int


@dataclass(frozen=True)
class ImageScalar:
    reference: str
    start: int
    end: int
    line: int
    column: int


_BLOCK_SCALAR = re.compile(r"(?:^|:\s+|-\s+)[|>][0-9+-]*\s*$")


def _without_comment(line: str) -> str:
    single = False
    double = False
    escaped = False
    for index, character in enumerate(line):
        if double:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                double = False
        elif single:
            if character == "'" and index + 1 < len(line) and line[index + 1] == "'":
                continue
            if character == "'":
                single = False
        elif character == '"':
            double = True
        elif character == "'":
            single = True
        elif character == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _mask_block_scalar_bodies(source: str) -> str:
    """Replace block scalar payloads with spaces while preserving offsets."""

    result: list[str] = []
    block_indent: int | None = None
    for line in source.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = line[len(body) :]
        stripped = body.lstrip(" ")
        indent = len(body) - len(stripped)
        if block_indent is not None:
            if not stripped or indent > block_indent:
                result.append(" " * len(body) + newline)
                continue
            block_indent = None
        significant = _without_comment(body).rstrip()
        if _BLOCK_SCALAR.search(significant):
            block_indent = indent
        result.append(body + newline)
    return "".join(result)


def _quoted_token(source: str, start: int, line: int, column: int) -> Token:
    quote = source[start]
    index = start + 1
    if quote == "'":
        decoded: list[str] = []
        while index < len(source):
            character = source[index]
            if character == "'":
                if index + 1 < len(source) and source[index + 1] == "'":
                    decoded.append("'")
                    index += 2
                    continue
                return Token("quoted", "".join(decoded), start, index + 1, line, column)
            decoded.append(character)
            index += 1
        raise YamlImageError(f"line {line}: unterminated single-quoted scalar")

    escaped = False
    while index < len(source):
        character = source[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            raw = source[start : index + 1]
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise YamlImageError(
                    f"line {line}: invalid double-quoted scalar"
                ) from exc
            return Token("quoted", value, start, index + 1, line, column)
        index += 1
    raise YamlImageError(f"line {line}: unterminated double-quoted scalar")


def _tokens(source: str) -> list[Token]:
    masked = _mask_block_scalar_bodies(source)
    tokens: list[Token] = []
    index = 0
    line = 1
    column = 1
    while index < len(masked):
        character = masked[index]
        if character in " \t\r":
            index += 1
            column += 1
            continue
        if character == "\n":
            tokens.append(Token("newline", "\n", index, index + 1, line, column))
            index += 1
            line += 1
            column = 1
            continue
        if character == "#" and (
            index == 0 or masked[index - 1].isspace() or masked[index - 1] in "[{,"
        ):
            end = masked.find("\n", index)
            index = len(masked) if end < 0 else end
            continue
        if character in "{}[],:?":
            tokens.append(Token("punctuation", character, index, index + 1, line, column))
            index += 1
            column += 1
            continue
        if character in "'\"":
            token = _quoted_token(masked, index, line, column)
            consumed = masked[index : token.end]
            tokens.append(token)
            line += consumed.count("\n")
            if "\n" in consumed:
                column = len(consumed.rsplit("\n", 1)[1]) + 1
            else:
                column += len(consumed)
            index = token.end
            continue

        start = index
        start_column = column
        while index < len(masked):
            current = masked[index]
            if current.isspace() or current in "{}[],?":
                break
            if current == "#" and (
                index == start or masked[index - 1].isspace()
            ):
                break
            if current == ":":
                prefix = masked[start:index]
                following = masked[index + 1 : index + 2]
                if prefix == "image" or not following or following.isspace() or following in "{}[],":
                    break
            index += 1
        if index == start:
            tokens.append(Token("punctuation", character, index, index + 1, line, column))
            index += 1
            column += 1
            continue
        value = masked[start:index]
        tokens.append(Token("scalar", value, start, index, line, start_column))
        column += index - start
    return tokens


def image_key_lines(source: str) -> list[int]:
    """Return every lexically valid image mapping key location."""

    tokens = _tokens(source)
    result: list[int] = []
    for index, token in enumerate(tokens):
        if token.kind == "scalar" and token.value.startswith(("&", "*")):
            raise YamlImageError(
                f"line {token.line}: YAML anchors and aliases are not permitted"
            )
        if (
            token.kind in {"scalar", "quoted"}
            and token.value == "image"
            and _mapping_colon(tokens, index) is not None
        ):
            result.append(token.line)
    return result


def _mapping_colon(tokens: list[Token], key_index: int) -> int | None:
    following = key_index + 1
    if (
        following < len(tokens)
        and tokens[following].kind == "punctuation"
        and tokens[following].value == ":"
    ):
        return following
    previous = key_index - 1
    if not (
        previous >= 0
        and tokens[previous].kind == "punctuation"
        and tokens[previous].value == "?"
    ):
        return None
    while following < len(tokens) and tokens[following].kind == "newline":
        following += 1
    if (
        following < len(tokens)
        and tokens[following].kind == "punctuation"
        and tokens[following].value == ":"
    ):
        return following
    return None


def image_scalars(source: str) -> list[ImageScalar]:
    """Return exact scalar spans for all image mapping values or fail closed."""

    tokens = _tokens(source)
    result: list[ImageScalar] = []
    flow_depth = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "scalar" and token.value.startswith(("&", "*")):
            raise YamlImageError(
                f"line {token.line}: YAML anchors and aliases are not permitted"
            )
        if token.kind == "punctuation" and token.value in "[{":
            flow_depth += 1
        elif token.kind == "punctuation" and token.value in "]}":
            flow_depth = max(0, flow_depth - 1)
        colon_index = _mapping_colon(tokens, index)
        if not (
            token.kind in {"scalar", "quoted"}
            and token.value == "image"
            and colon_index is not None
        ):
            index += 1
            continue

        value_index = colon_index + 1
        while value_index < len(tokens) and tokens[value_index].kind == "newline":
            if flow_depth == 0:
                raise YamlImageError(
                    f"line {token.line}: image value must be one inline scalar"
                )
            value_index += 1
        if value_index >= len(tokens) or tokens[value_index].kind not in {
            "scalar",
            "quoted",
        }:
            raise YamlImageError(
                f"line {token.line}: image value is absent or is not a scalar"
            )
        value = tokens[value_index]
        if not value.value or value.value[0] in "!&*|>":
            raise YamlImageError(
                f"line {token.line}: image value uses an unsupported YAML indirection"
            )
        result.append(
            ImageScalar(value.value, value.start, value.end, value.line, value.column)
        )
        index = value_index + 1
    return result


def rewrite_image_scalars(source: str, replacements: dict[tuple[int, int], str]) -> str:
    """Replace validated scalar spans without disturbing surrounding YAML."""

    output = source
    for (start, end), value in sorted(replacements.items(), reverse=True):
        output = output[:start] + value + output[end:]
    return output
