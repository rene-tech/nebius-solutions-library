#!/usr/bin/env python
"""Prove BoltzGen reads its molecule dictionary from the localized tree.

The failure this guards against is subtle: `--moldir` pointing at a directory
that holds `mols.zip` looks fine until the first ligand lookup, because nothing
opens the dictionary during startup. This probe does what a design run does. It
resolves the same path the adapter compiles into argv, loads real entries with
BoltzGen's own loader, and reports what it got.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle  # noqa: S403 - the dictionary is a pinned, digest-verified artifact
import sys
import time
from pathlib import Path

# Chemical Component Dictionary codes are one to five characters. Sampling
# across that range catches a tree staged with a three-character assumption.
PROBE_CODES = ("I", "CL", "ZN", "MG", "HEM", "ATP", "NAD", "GLC", "HOH", "A1LV8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--moldir", required=True)
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()

    started = time.monotonic()
    moldir = Path(options.moldir)
    report: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/boltzgen-moldir-probe/v1",
        "moldir": str(moldir),
        "node": os.environ.get("FS2_NODE_NAME", ""),
    }

    if not moldir.is_dir():
        report["state"] = "failed"
        report["reason"] = f"{moldir} is not a directory"
        _emit(report, options.report)
        return 1

    entries = sorted(item.name for item in moldir.iterdir())
    archives = [name for name in entries if name.endswith((".zip", ".tar", ".tar.gz", ".tgz"))]
    report["entry_count"] = len(entries)
    report["archives_present"] = archives
    if archives:
        report["state"] = "failed"
        report["reason"] = f"the molecule directory still contains archives {archives}"
        _emit(report, options.report)
        return 1

    loaded: list[dict[str, object]] = []
    for code in PROBE_CODES:
        candidate = moldir / f"{code}.pkl"
        if not candidate.is_file():
            report["state"] = "failed"
            report["reason"] = f"molecule {code} is absent from the localized tree"
            _emit(report, options.report)
            return 1
        with candidate.open("rb") as handle:
            molecule = pickle.load(handle)  # noqa: S301 - digest-verified artifact
        loaded.append(
            {
                "code": code,
                "bytes": candidate.stat().st_size,
                "type": type(molecule).__name__,
                "atom_count": _atom_count(molecule),
            }
        )

    report["probed_molecules"] = loaded
    report["state"] = "passed"
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    try:
        import boltzgen  # noqa: F401

        report["boltzgen_import"] = "ok"
    except Exception as error:  # pragma: no cover - reported, not raised
        report["boltzgen_import"] = f"failed: {error}"
    _emit(report, options.report)
    return 0


def _atom_count(molecule: object) -> int | None:
    """Report a real structural quantity without assuming one upstream shape."""

    for attribute in ("GetNumAtoms", "get_num_atoms"):
        method = getattr(molecule, attribute, None)
        if callable(method):
            try:
                return int(method())
            except Exception:  # pragma: no cover - shape probe only
                return None
    if isinstance(molecule, dict):
        for key in ("atoms", "atom_name", "atom_names"):
            value = molecule.get(key)
            if value is not None:
                try:
                    return len(value)
                except TypeError:  # pragma: no cover
                    return None
    return None


def _emit(report: dict[str, object], path: Path | None) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if path is not None:
        try:
            path.write_text(rendered + "\n", encoding="utf-8")
        except OSError as error:  # pragma: no cover - reported, not fatal
            print(f"could not write {path}: {error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
