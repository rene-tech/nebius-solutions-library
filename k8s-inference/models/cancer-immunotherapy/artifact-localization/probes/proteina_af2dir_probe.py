#!/usr/bin/env python
"""Prove Proteina-Complexa loads AlphaFold2 parameters from the localized tree.

Proteina reaches AlphaFold2 through ColabDesign, which resolves
``AF2_DIR/params/params_<model>.npz`` and then ``AF2_DIR/params_<model>.npz``.
Neither form can read a tar archive, so an AF2_DIR pointing at
``alphafold_params_2022-12-06.tar`` fails at the first refold rather than at
startup. This probe takes the same resolution path with the same loader and the
same multimer model the evaluation stage selects.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

# ColabDesign builds the binder model with use_multimer=True, so these are the
# parameter sets an evaluate stage actually opens.
PROBE_MODELS = ("model_1_multimer_v3", "model_1_ptm")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--af2-dir", default=os.environ.get("AF2_DIR", ""))
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()

    started = time.monotonic()
    report: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/proteina-af2dir-probe/v1",
        "af2_dir": options.af2_dir,
        "node": os.environ.get("FS2_NODE_NAME", ""),
    }

    if not options.af2_dir:
        report["state"] = "failed"
        report["reason"] = "AF2_DIR is not set"
        _emit(report, options.report)
        return 1

    root = Path(options.af2_dir)
    if not root.is_dir():
        report["state"] = "failed"
        report["reason"] = f"{root} is not a directory"
        _emit(report, options.report)
        return 1

    entries = sorted(item.name for item in root.iterdir())
    archives = [name for name in entries if name.endswith((".tar", ".tar.gz", ".tgz", ".zip"))]
    report["entries"] = entries
    report["archives_present"] = archives
    if archives:
        report["state"] = "failed"
        report["reason"] = f"AF2_DIR still contains archives {archives}"
        _emit(report, options.report)
        return 1

    loaded: list[dict[str, object]] = []
    try:
        from colabdesign.af.alphafold.model import data as af_data
    except Exception as error:  # pragma: no cover - reported, not raised
        report["state"] = "failed"
        report["reason"] = f"colabdesign is unavailable in this runtime: {error}"
        _emit(report, options.report)
        return 1

    for model_name in PROBE_MODELS:
        model_started = time.monotonic()
        params = af_data.get_model_haiku_params(model_name=model_name, data_dir=options.af2_dir, fuse=True)
        if not params:
            report["state"] = "failed"
            report["reason"] = f"{model_name} resolved to empty parameters"
            _emit(report, options.report)
            return 1
        total = 0
        for module in params.values():
            for leaf in module.values():
                total += int(getattr(leaf, "size", 0))
        loaded.append(
            {
                "model_name": model_name,
                "module_count": len(params),
                "parameter_count": total,
                "load_seconds": round(time.monotonic() - model_started, 3),
            }
        )

    report["loaded_models"] = loaded
    report["state"] = "passed"
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    _emit(report, options.report)
    return 0


def _emit(report: dict[str, object], path: Path | None) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if path is not None:
        try:
            path.write_text(rendered + "\n", encoding="utf-8")
        except OSError as error:  # pragma: no cover - reported, not fatal
            print(f"could not write {path}: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
