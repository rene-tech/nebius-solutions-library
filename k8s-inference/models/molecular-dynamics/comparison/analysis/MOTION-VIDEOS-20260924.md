# Smoother presentation and genuine dense native motion

Delivered files, native results and verification:
[24 September result](MOTION-VIDEO-RESULT-20260924.md).

The owner authorized two distinct derivatives of the canonical four-engine MD
delivery. These do not replace the frozen 23 September evidence, plots or raw
trajectories, and they do not qualify unrelated platform or LibreChat features.

## Two deliberately different movies

| Version | Physical interval | Coordinates | Playback |
| --- | --- | --- | --- |
| Presentation | Original production samples at 1–1000 ps | Clearly labeled five-frame display averaging after progressive rigid alignment, with visual bond-length correction | 1000 frames / 40 fps / 25 s |
| Dense slow motion | New 20 ps native continuations from each engine's final state | Actual coordinates saved every 20 fs, with rigid display alignment only; no averaging or interpolation | 1000 frames / 40 fps / 25 s = 0.8 ps per video second |

Both use the same fixed camera and scale across four 720×720 panels (1440×1440
comparison). Heavy atoms are shown as sticks; hydrogens remain in the native
data but are hidden to reduce clutter. Water oxygen points fade smoothly between
5 and 7 Å instead of popping at a hard viewing boundary. The tighter 15 Å view
improves molecular legibility; it is only a display crop, not solvent deletion.

### Presentation is not a physical trajectory

`motion_video.py --mode presentation` applies consecutive proper rotations
(determinant +1), a centered 1:2:3:2:1 temporal filter, then iterative visual
bond-length projection to the filtered original bond lengths. This avoids
collapsed bonds from Cartesian averaging. Alanine chirality is explicitly
checked. Bond angles and intermediate configurations are not asserted to be
physical. Do not compute energies, dihedrals or scientific statistics from this
movie. All scientific analysis continues to use the unchanged native outputs.
Water is not temporally averaged, so periodic image changes cannot create false
water motion through the peptide. Its display opacity is smoothly cropped.

Display-displacement and second-difference metrics document reduced jitter;
these are not velocities, accelerations or molecular observables. The original
native-frame movie remains available as an unsmoothed reference.

### Dense slow motion is genuinely sampled, not reconstructed

Native workers retain the original ff14SB/TIP3P topology, 2 fs integration step,
temperature/pressure targets, constraints, cutoffs and long-range settings.
They continue each original engine state for 10000 steps, saving every 10 steps.
The renderer strictly checks all 1000 nonzero native frame times, available
stored steps, atom count, finite coordinates and cell geometry. It never inserts
missing frames. AMBER step numbers may be derived from native stored time, and
are not falsely labeled as independently stored steps.

File restarts are not assumed to preserve stochastic streams or initialization
virials exactly. Native receipts record the exact source restart, image, command,
operation and any engine-specific limitations. These are new trajectories after
the original experiment, not recovered intermediate frames from that experiment.
The native continuation validators own force-field/constraint/runtime acceptance;
readable coordinates alone do not establish that acceptance.

## Reproduce

Use the existing pinned CPU analysis environment and FFmpeg. New output
directories are mandatory; source hashes are checked again after rendering.

```bash
python motion_video.py --mode presentation \
  --master /delivery-02/master --analysis /delivery-02/analysis \
  --output /new/presentation

python motion_video.py --mode dense --master /delivery-02/master \
  --spec /new/dense-spec.json --output /new/dense-video
```

Dense spec `runs` contains all four entries with `engine`, `trajectory`,
`origin_time_ps`, `origin_step`, `canonical_to_native: "identity"`, and
`validation_receipt` (a passed native-workflow receipt). Optional
`trajectory_format` follows the existing native reader. Use the actual native
origins, not a common guessed clock; 0.02–20 ps is elapsed continuation time.

Tests: `python -m unittest discover -s tests -v`. Geometry/encoder unit fixtures
are synthetic and not evidence of GPU simulation. Native evidence and rendering
receipts for this task live under `/home/tux/fs2-alanine-videos-20260924`.

Methods: [GROMACS progressive fitting](https://manual.gromacs.org/current/onlinehelp/gmx-trjconv.html)
and [VMD display smoothing](https://www.ks.uiuc.edu/Training/TutorialsOverview/vmd/tutorial-html/node3.html).
The implementation keeps these visualization choices explicit rather than
silently altering the scientific records.
