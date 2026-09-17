#!/usr/bin/env python3
"""Execute one source-pinned tool without PATH or Python environment inheritance."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Tool execution is implemented by the externally anchored bootstrap. This
# historical repository-local launcher is importable for source review only.
if __name__ == "__main__":
    print("use the external capsule bootstrap exec-tool command", file=sys.stderr)
    raise SystemExit(1)

raise ImportError(
    "repository-local tool execution is retired; use the external capsule bootstrap"
)

try:
    from .execution_toolchain import (
        ToolchainError,
        validate_current_python,
        validate_source_file,
        validated_tool,
    )
except ImportError:
    from execution_toolchain import (
        ToolchainError,
        validate_current_python,
        validate_source_file,
        validated_tool,
    )


def main() -> int:
    print(
        "repository-local tool execution is permanently disabled; use the external capsule",
        file=sys.stderr,
    )
    return 1

    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True, choices=("helm", "kubectl"))
    parser.add_argument("--trust", required=True, type=Path)
    parser.add_argument("--toolchain", required=True, type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        validate_current_python(lock_path=args.toolchain, trust_path=args.trust)
        validate_source_file(
            "security/run_reviewed_tool.py",
            source_root=Path(__file__).resolve().parent.parent,
            lock_path=args.toolchain,
            trust_path=args.trust,
        )
        executable, _ = validated_tool(
            args.tool, lock_path=args.toolchain, trust_path=args.trust
        )
        arguments = list(args.arguments)
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PATH", "PYTHONHOME", "PYTHONPATH"}
        }
        os.execve(executable, [str(executable), *arguments], environment)
    except (OSError, ToolchainError) as exc:
        print(f"reviewed tool execution: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
