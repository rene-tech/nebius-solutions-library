#!/usr/bin/env python3
"""Render versioned all-model ConfigMaps and the matching Helm values overlay."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_CONTROL_ROOT = Path(__file__).resolve().parents[1]
_SOLUTION_ROOT = _CONTROL_ROOT.parents[1]
sys.path[:0] = [str(_CONTROL_ROOT / "src"), str(_SOLUTION_ROOT / "catalog/runtime")]

from fs2_serve.live_release import (  # noqa: E402
    LiveReleaseError,
    load_and_render_live_release,
    write_json_atomic,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--catalog-root", type=Path, default=_SOLUTION_ROOT / "catalog/runtime")
    result.add_argument(
        "--repo-root",
        type=Path,
        default=_SOLUTION_ROOT / "catalog/runtime/packaged-repository",
    )
    result.add_argument(
        "--inventory", type=Path, default=_CONTROL_ROOT / "contracts/all-models-live-services.json"
    )
    result.add_argument("--configmaps-output", type=Path, required=True)
    result.add_argument("--helm-values-output", type=Path, required=True)
    result.add_argument(
        "--qualification-output",
        type=Path,
        help="Optional generated full-catalog qualification contract output.",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        release = load_and_render_live_release(
            catalog_root=args.catalog_root.resolve(),
            repo_root=args.repo_root.resolve(),
            inventory_path=args.inventory.resolve(),
        )
        write_json_atomic(
            args.configmaps_output.resolve(),
            {"apiVersion": "v1", "kind": "List", "items": list(release.config_maps)},
        )
        write_json_atomic(args.helm_values_output.resolve(), release.helm_values)
        if args.qualification_output is not None:
            write_json_atomic(
                args.qualification_output.resolve(),
                release.qualification_projection,
            )
    except LiveReleaseError as error:
        print(f"all-model live release: FAIL ({error})", file=sys.stderr)
        return 1
    print(
        "all-model live release: PASS "
        f"release_id={release.release_id} routes={len(release.routes)} "
        f"bindings={release.bindings_config_map_name} lean_routes={release.routes_config_map_name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
