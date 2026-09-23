# Regenerating the real-trajectory comparison

Keep this `analysis-inputs` directory beside the delivered `master/` and
`runs/<engine>/` directories. `spec.json` resolves its paths relative to itself,
not your shell's working directory. No original server directory is needed.

Use Python 3.12 with the pinned packages in `requirements.txt` and system
FFmpeg/ffprobe with the libx264 encoder (tested: FFmpeg 6.1.1). In an isolated
environment, run from any directory:

```bash
python /path/to/delivery/analysis-inputs/regenerate.py \
  --analysis-output /path/to/new-analysis \
  --video-output /path/to/new-videos
```

Both output directories must be new. Omitting `--video-output` performs analysis
only. The comparison reads the unmodified native trajectories, verifies every
frame/step/time and finite coordinate, and displays exactly 1,000 samples at
production-relative 1..1000 ps. No interpolation or synthesized frames are used.

All four clips share the canonical peptide reference, whole-molecule periodic
wrapping, heavy-atom Kabsch alignment, camera, 1 ps frame interval and playback
speed. Water is shown as translucent oxygen points within a fixed 13 Å display
radius; no solvent is removed from the raw trajectory or analysis. The 2×2 grid
uses the same frames and time scale as the four individual clips.

`packaging-receipt.json` records verified source-to-delivery hashes for every
analysis dependency. Historical source paths in that receipt are audit metadata,
not regeneration dependencies. Original workflow receipts may also contain
historical runtime paths; the relative spec selects the delivered files.
Packaging does not duplicate the raw trajectories and is not itself a scientific
equivalence, convergence, full raw-output inventory or platform readiness gate.
The parent's delivery manifest covers all native outputs, including files this
analysis does not read. Read the final report for native estimator, pressure,
electrostatics/dispersion, integrator and short-trajectory limitations.
