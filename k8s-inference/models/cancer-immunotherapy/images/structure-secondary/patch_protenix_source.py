#!/usr/bin/env python3
"""Apply the single audited Protenix v2 offline-prep source transformation."""

from pathlib import Path


path = Path("runner/batch_inference.py")
source = path.read_text(encoding="utf-8")
old = """    return preprocess_input(
        input_json=input,
        out_dir=out_dir,
        use_msa=True,
        use_template=True,
        use_rna_msa=True,
"""
new = """    # FS2 accepts only an explicit no-MSA or already-precomputed MSA
    # handoff. Neither mode may submit sequences to an external MSA server.
    fs2_msa_mode = os.environ.get("FS2_MSA_MODE")
    if fs2_msa_mode not in {"none", "precomputed"}:
        raise RuntimeError("FS2_MSA_MODE must be none or precomputed")

    return preprocess_input(
        input_json=input,
        out_dir=out_dir,
        use_msa=False,
        use_template=True,
        use_rna_msa=True,
"""
if source.count(old) != 1:
    raise SystemExit("expected exact Protenix v2 inputprep block was not found once")
path.write_text(source.replace(old, new), encoding="utf-8")
