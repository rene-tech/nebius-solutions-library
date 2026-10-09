# Four-engine molecular-motion videos — 24 September 2026

Both requested versions are delivered. This acceptance covers the two movies
and four short native continuations, not a new platform-wide release or a claim
of cross-engine force-field equivalence or equilibrium convergence.

## Files in the owner's bucket

Prefix: `s3://renes-bucket/four-engine-alanine-20260923/`.

| File | What it shows | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `four-engine-presentation-smoothed.mp4` | Original 1 ns experiment, display-only smoothing | 3,563,238 | `6563f46b42150c063d00fcf9e134284487f4e2a0f4b2d74a61a204d8d1c7d79e` |
| `four-engine-dense-slow-motion.mp4` | New 20 ps continuations, actual 20 fs samples | 3,137,528 | `b5bfec35c3303d5c154ca0adb3b4e15c06927e9611bcc94e99e50f33bdd185c1` |

Both are 25-second, 1440×1440 H.264/yuv420p MP4s with 1,000 frames at 40 fps
and fast-start metadata. Four matching panels are labeled GROMACS, NAMD,
AMBER and LAMMPS. The same fixed camera, view scale and representation are used
for each engine. Heavy atoms are sticks; solvent oxygen points fade near the
view boundary, and hidden hydrogens remain in all raw scientific files.

The objects were created conditionally without overwriting existing keys.
Both returned version 1; full-object GET readbacks matched the SHA-256 values
above. Bucket policy and ACLs were not changed. The original comparison video,
phi/psi plot and complete frozen delivery were not replaced.

## Which version to use

**Presentation:** shows the original 1–1,000 ps samples with progressive proper
rigid alignment, a five-frame triangular display filter and visual bond-length
projection. It is prominently labeled **VISUALLY SMOOTHED**. The display
second-difference jitter metric fell by approximately 72–75% across the engines.
That is a visualization metric, not a change in molecular dynamics. These
averaged positions are not physical intermediate states and must never be used
for energy, dihedral or other scientific measurements. Alanine chirality was
checked; no force-field-validity claim is made for display averages.

**Dense slow motion:** preferred for examining real internal molecular motion.
Each engine actually integrated another 10,000 steps at the original 2 fs
timestep, saving coordinates every 10 steps. This is 50 times finer sampling
than the original 1 ps output. The movie contains the 1,000 positive samples
from 0.02 to 20.00 ps of each continuation, with **no coordinate averaging,
interpolation or generated frames**. Only periodic-boundary handling and
proper rigid viewing alignment are applied. Playback is 0.8 ps per video second.
These are new dynamics, not reconstructed missing frames from the first movie.

## Actual native execution and validation

The unchanged hosted customer SDK/typed API submitted each native workflow once
using ordinary qualification credentials. All four operations completed on the
existing H100 pool and released their compute. No quota, concurrency limit,
runtime image, deployment, node pool or unrelated workload was changed.

| Engine | Operation | Native origin | Passed receipt | Maximum constrained-distance error |
| --- | --- | --- | --- | ---: |
| GROMACS | `da92902e-9420-41e7-b02a-e65b9ac07da2` | step 500000 / 1000 ps | `dense/gromacs/validation-01.json` | 1.908e-5 Å |
| NAMD | `f88b4e14-9ae0-4930-b314-23e59f8591a1` | step 605000 / 1210 ps | `dense/namd/validation-02.json` | 1.492e-5 Å |
| AMBER | `d2f82bde-b3a9-44ac-9c4f-669dc96e9cbe` | local step 0 / 1200 ps | `dense/amber/validation-02.json` | 1.493e-5 Å |
| LAMMPS | `199127a9-c7be-4a57-b99c-c58c29499bce` | step 600000 / 1200 ps | `dense/lammps/validation-01.json` | 2.949e-5 Å |

All retain the canonical 6,598-atom ff14SB/TIP3P system, 6,588 constrained
distances, atom ordering and original physical settings. Native validators
check actual steps/times, finite coordinates/cells, source checkpoint and
topology hashes, command/runtime identity, and artifact inventories. Optional
native initial frames are preserved but excluded from the common 1,000-frame
movie. Native float32 timestamp precision is recorded, not mistaken for a
changed sampling interval. AMBER steps are not claimed to be stored in NetCDF.

File restarts are not promised to reproduce stochastic trajectories bitwise.
AMBER and LAMMPS Langevin random streams restart with the recorded seed; NAMD
retains its native COM-velocity removal. LAMMPS's new step-600000 SHAKE setup
projection was independently checked against this exact source checkpoint and
topology, not waived using earlier boundary evidence. The dense movie does not
claim new thermodynamic convergence or a throughput benchmark.

The LAMMPS Pod disappeared before direct observation. Retained lifecycle/DCGM
evidence identifies its actual GPU; direct per-Pod driver and imageID capture
remain unavailable. The exact caller-published runtime digest and native CUDA
dispatch evidence are retained. No missing observation is invented.

Two offline validation harness defects were corrected while retaining the
failed receipts (AMBER scalar NetCDF time reading and the NAMD validation
harness). No failed simulation, extra native run or loosened scientific
tolerance is hidden by those fixes. See the lane reports for full details.

## Reproduction and evidence

Implementation: [method and commands](MOTION-VIDEOS-20260924.md),
[GROMACS/AMBER continuation preparation](../../qualification/dense-motion-20260924/GROMACS-AMBER.md),
and the NAMD/LAMMPS preparation/validation helper in that same directory.
The renderer and its regression suite contain 74 passing tests; native-lane
tests are recorded separately and are not substituted for real GPU evidence.

Owner-local evidence root: `/home/tux/fs2-alanine-videos-20260924/`.
Raw native inputs/results and commands are under `dense/<engine>/`;
`dense-spec.json` binds all actual paths and time origins.
Each movie directory also contains four individual clips, previews and a
rendering receipt. All ten encoded clips were fully decoded without errors;
frame counts, timestamps, dimensions and pixel format were checked, and both
four-panel previews were visually inspected. Proper rigid viewing fits preserve
within-frame bond lengths to less than 2.0e-15 Å. Both rendering receipts confirm
their input hashes remained unchanged.

| Evidence | SHA-256 |
| --- | --- |
| `presentation-01/receipt.json` | `aaf55f93f518f0cece33275df34dd679fb29a6340eeb25dd5b38bd81dfd6b110` |
| `dense-video-01/receipt.json` | `ee2619385045535b24912ec3e21b603fa333c8a09f0603f0f10fa8f7c301d3e8` |
| `dense-spec.json` | `c8aee3462d3015b3bf5d0b392fa9b1af1b5f66647988f27df751d3204f2466b5` |

Rendering source identities are `d34cd0905714fcc7667fcfebf222388f8aeabd7b`
(presentation) and `e047673dbcb853b3d63a66e693b90a1df7d4fc81` (dense), with
per-file hashes in each receipt. Native-worker integration and documentation
follow on the existing `agent/fs2-gromacs-r20260923` branch; no new branches
were created. The original `delivery-02` integrity check covered all 548 files
(1,525,211,038 bytes). Raw scientific data remain the authority for analysis.

The unrelated LibreChat default-model blocker is intentionally unchanged.
