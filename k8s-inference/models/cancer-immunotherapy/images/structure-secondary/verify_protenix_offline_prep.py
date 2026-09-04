#!/usr/bin/env python3
"""Verify the installed Protenix prep callback takes only the offline-none path."""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import tempfile

from runner import batch_inference


def main() -> None:
    callback = batch_inference.inputprep.callback
    source = inspect.getsource(callback)
    required_source = (
        'os.environ.get("FS2_MSA_MODE")',
        "use_msa=False",
        "use_template=False",
        "use_rna_msa=False",
    )
    if any(source.count(fragment) != 1 for fragment in required_source):
        raise SystemExit("installed Protenix wheel does not contain the exact offline-none patch")

    calls: list[dict[str, object]] = []

    def local_msa(input_json: str, out_dir: str, *, use_msa: bool, mode: str):
        calls.append(
            {
                "input_json": input_json,
                "out_dir": out_dir,
                "use_msa": use_msa,
                "mode": mode,
            }
        )
        if use_msa:
            raise AssertionError("offline-none prep attempted an MSA service path")
        return input_json, None

    def forbidden_service(*_args, **_kwargs):
        raise AssertionError("offline-none prep attempted a template or RNA-MSA service path")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        raw = root / "input.json"
        raw.write_text(
            json.dumps(
                [
                    {
                        "name": "fs2-offline-prep-check",
                        "sequences": [
                            {
                                "proteinChain": {
                                    "count": 1,
                                    "sequence": "MKTAYIAKQRQISFVKSHFSRQDILDLWIYHTQGYFP",
                                }
                            }
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        output = root / "output"
        output.mkdir()
        original = (
            batch_inference.update_infer_json,
            batch_inference.update_template_info,
            batch_inference.update_rna_msa_info,
        )
        batch_inference.update_infer_json = local_msa
        batch_inference.update_template_info = forbidden_service
        batch_inference.update_rna_msa_info = forbidden_service
        try:
            os.environ["FS2_MSA_MODE"] = "none"
            result = callback(
                input=str(raw),
                out_dir=str(output),
                msa_server_mode="protenix",
                hmmsearch_binary_path=None,
                hmmbuild_binary_path=None,
                seqres_database_path=None,
                nhmmer_binary_path=None,
                hmmalign_binary_path=None,
                hmmbuild_rna_binary_path=None,
                ntrna_database_path=None,
                rfam_database_path=None,
                rna_central_database_path=None,
                nhmmer_n_cpu=None,
            )
        finally:
            (
                batch_inference.update_infer_json,
                batch_inference.update_template_info,
                batch_inference.update_rna_msa_info,
            ) = original
            os.environ.pop("FS2_MSA_MODE", None)
        if result != str(raw) or len(calls) != 1 or calls[0]["use_msa"] is not False:
            raise SystemExit("installed Protenix prep did not remain on the offline-none path")

    print(
        json.dumps(
            {
                "schema": "fs2.nebius.ai/protenix-installed-offline-prep-check/v1",
                "msa": False,
                "templates": False,
                "rna_msa": False,
                "status": "passed",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
