# Bounded LeRobot dataset worker

The worker rewrites complete LeRobot v3 datasets and reopens every output with
the pinned `lerobot==0.6.1` reader. Every camera frame is decoded, including
unselected cameras and episodes. Actions, states, timestamps, canonical indices,
episode boundaries and task strings are retained; visual streams are re-encoded
and are **not byte-identical**. Public acceptance additionally compares every
nonvideo value and the decoded unselected streams using
`../fixtures/validate_variant.py` (codec tolerance: mean absolute pixel error
at most 6/255 per unselected frame).

Fixed-rate input timestamps must equal `frame_index / fps` exactly in their
source float32 or float64 dtype. Jittered/noncanonical timestamps and task indices
not ordered by first occurrence are rejected before GPU work, rather than being
silently normalized by the pinned writer. Output automatic-feature dtypes are
retained from source metadata.

## Practical limits

- Uploaded compressed source and each compressed output artifact: 5 GiB.
- Expanded source and validated dataset inventory: 8 GiB, at most 100,000 files.
- Selected generation: 1–256 episodes, 1–8 cameras, including `all`; 16–400
  frames per selected episode; selected RGB dimensions follow the existing
  Cosmos geometry bounds. These generation limits do not constrain untouched
  episodes/cameras or full output integrity validation.
- The CPU stage has a 32 GiB ephemeral workspace budget. Before constructing a
  Cosmos client, the worker estimates the complete source, all variant datasets
  and archives, retained reference/child videos, largest-episode RGB scratch,
  and 2 GiB headroom, and checks available disk space. Oversized selections or
  variant counts fail before GPU work. This conservative estimate is not an
  encoder-size guarantee; actual artifact/inventory limits still apply.
- Reference and generated child videos retain their existing 512 MiB / 1 GiB
  bounds. Large accepted source files do not imply that all combinations of
  cameras, episodes and eight variants fit the workspace.
- Replacement tensors are retained for one episode at a time, not the complete
  dataset. Archive compression uses two threads. The stage's memory/disk limits
  remain authoritative; a maximum-bound throughput qualification is not claimed.

Cancellation is checked between localization, decoded frames, child operations,
rewrites and publication stages. Cancellation after the final child does not
publish a successful dataset. An individual network or codec call may finish
before a pending cancellation is observed; this is not instantaneous preemption.

## Appearance augmentation and evidence boundary

Use constrained `transfer` with edge conditioning for modest lighting or
background/appearance changes. Inspect all generated clips before training.
The retained real H100 example preserves coarse motion/layout but modifies fine
details. `video-to-video` can change trajectories and scene content: unchanged
action arrays alone do not make either mode an action-aligned training dataset.
Viewpoint/weather/object changes require their own task-specific review.

The realistic fixture uses the pinned public Cosmos robot clip's 64 frames and
four consecutive 16×29 action chunks, split into two 32-frame episodes. Its two
views come from the top/bottom regions of the public stacked image. These are
model-generated imagery/actions, not recorded telemetry or calibrated cameras;
fixture observation.state is explicitly synthetic episode/frame identification.
No physical action alignment or downstream robotics training quality is claimed.

```sh
/tmp/fs2-lerobot-venv/bin/python ../fixtures/build_robot_multiview.py \
  --video /private/official-robot.mp4 --actions /private/official-actions.json \
  --output /private/robot-multiview
```

The source hashes are enforced by the builder and recorded in source provenance.
The reader regression also covers a valid selected 16-frame front clip with an
untouched 8-frame episode and 128-pixel camera; full output reload must accept
those untouched streams. Local tests using retained H100 output validate dataset
mechanics, not a new public GPU workflow run. Public admission, child operations,
returned artifacts and cancellation require separate deployment acceptance.
