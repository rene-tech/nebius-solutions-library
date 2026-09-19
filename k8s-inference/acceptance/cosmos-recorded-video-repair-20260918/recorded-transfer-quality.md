# Recorded transfer quality: frozen eight-case comparison

All eight public requests returned valid640×480,64frame,25FPS videos. The
comparison exposes a tradeoff, not a customer-ready robotics verdict: stronger
unanchored changes follow the robot's broad trajectory but also recolor objects
and materials; first-frame anchoring preserves appearance better while weakening
the requested lighting change. No defaults were changed after observing results.

## Inputs and fixed experiment

Two recorded ALOHA coffee episodes are derived from
[`lerobot/aloha_static_coffee`](https://huggingface.co/datasets/lerobot/aloha_static_coffee/tree/b144896feb1f37398a862927b22cd3abdf005a6b),
revision`b144896feb1f37398a862927b22cd3abdf005a6b`, with retained source actions
and states. The underlying research is [ALOHA](https://arxiv.org/abs/2304.13705).
These are bounded64frame segments, not a reproduction of the paper's policy
training or evaluation.

Each episode was submitted through the ordinary scientist10 MCP principal,
using the same35steps,seed20260918,prompt,exact dimensions,FPS and controls
guidance1.5. The experiment was frozen before admission as a sequence of
explicit changes: text guidance6→3, first-chunk conditioning0→1, then add blur
to edge. Native image`5e2680aa…`, model revision`7a312c868…`, and strict snapshot
owner remained unchanged on control-plane release173.

The starting guidance choice is informed by the
[Diffusers Cosmos3 transfer documentation](https://huggingface.co/docs/diffusers/en/api/pipelines/cosmos3).
The deployed semantics are checked against
[the pinned vLLM-Omni recipe](https://github.com/vllm-project/vllm-omni/blob/eb11446b7f2e30ca582f8aff3afe12e9a2e66f6c/recipes/cosmos3/Cosmos3-Nano.md),
not inferred from a newer runtime.

## Whole-clip image-space measurements

`compare_video_motion.py` checks exact source/output geometry, frame count and
FPS before analysis. It compares all63adjacent-frame transitions per episode
using Farneback optical flow at320×240 analysis resolution, retaining pixels
whose source motion exceeds0.75pixels. Endpoint error is in **analysis pixels**,
not robot-space units. Lighting and texture affect this proxy; no pass threshold
is invented from these two episodes.

| Explicit setting | Endpoint error, episodes0 /1 | Flow direction cosine, episodes0 /1 | Active model time, episodes0 /1 |
| --- | --- | --- | --- |
| Edge, guidance6, first0 |0.963 /0.703 |0.956 /0.971 |34.80 /34.68 s |
| Edge, guidance3, first0 |0.874 /0.603 |0.956 /0.973 |33.34 /35.35 s |
| Edge, guidance3, first1 |0.625 /0.504 |0.972 /0.978 |35.70 /34.32 s |
| Edge+blur, guidance3, first1 |0.618 /0.464 |0.977 /0.984 |51.94 /52.74 s |

Adding blur after first-frame anchoring produced a small further improvement in
these proxies but cost roughly50%more active inference time. It is not yet a
justified universal default. Accepted-to-complete times ranged34.46–95.80 s;
some requests used a warm Pod, others waited41.60–48.58 s for activation.
These are not homogeneous empty-node cold-start benchmarks.

## Visual assessment and limits

Same-index frames0,16,32,48,63 were independently inspected for both episodes:

- Unanchored edge variants broadly follow the recorded arm motion but strongly
  recolor the coffee machine, cup and work surface. Appearance is not a strict
  lighting-only transformation.
- First-frame anchoring keeps the initial scene and robot appearance much closer
  to the recording and follows its broad motion, but the intended cooler-lighting
  augmentation is substantially weaker.
- Edge+blur offers similar visual preservation; fine robot contacts, gripper
  geometry, occlusions and action-label validity are not established by these
  grids or flow estimates.
- No camera calibration, robot masks, contact ground truth or downstream policy
  evaluation was performed. Numeric action-array preservation must never be
  presented as proof that labels remain physically correct for generated video.

The final edge+blur operation`ec9b6d8f-22a2-42e1-bb41-b674fa7da93c` completed,
but its public runtime attribution was null with`gpu_count:0`. Multiple restored
Pods were observed. No GPU was guessed, and zero must not be interpreted as
measured zero GPU consumption. Seven other requests have joined Pod/GPU strict
restore witnesses. This observability gap remains a separate release defect.

## Reproducible evidence

Protected evidence root:
`scientific-qualification-20260918/lerobot-admission-diagnosis/`.

- Frozen input manifest:`transfer-quality-v1/manifest.json`, SHA256
  `091b18400df680540679327d9588fcdbc19bfc8318a2a6c1dec745f82de818e4`.
- Every admission, terminal operation and media artifact:
  `public-mode-quality-episode{0,1}-*-r1/`.
- Complete metrics:`transfer-quality-analysis-r1/summary.json`, SHA256
  `f7d3555025d6438f71b8a6f184f5ce1c15177f0ed0cdd54393a5caf45df88f8f`.
- Episode0visual grid:`transfer-quality-contact-episode0/comparison.png`, SHA256
  `8dabec1c00e2fc45f58d1649d66342b5345a8dc536b53f0f2ed0f27b84d469bd`.
- Episode1visual grid:`transfer-quality-contact-episode1/comparison.png`, SHA256
  `ce98cf41d9b82d5ac3bc0ff81f4bd467d71badd908497bf1f3ae263264651f1c`.

This experiment is evidence for artifact correctness, limited visual behavior
and image-space tradeoffs. It does not qualify robot training, action-policy
generation, physical safety, burst concurrency or the actual LibreChat path.
