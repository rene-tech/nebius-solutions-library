# Cosmos media and LeRobot live qualification lane

`snapshot-media-compatibility.json` records actual H100 r7-restored runtime
compatibility with the new media adapter. It is deliberately not a public
customer acceptance receipt. `ADAPTER-CUTOVER.md` contains the separately
reviewed shared-template promotion plan.

## Reproduction inputs

- `render_media_preview.py` renders only task-owned ConfigMap/Deployment names,
  fixed digest-pinned Cosmos image, unique selectors, and read-only verified
  existing model cache. `--snapshot-bundle` applies the production transform
  with fallback disabled. Node/pool must be chosen from currently healthy,
  unallocated existing capacity; never copy historical placement blindly.
- `build_robot_fixture.py` expects two read-only files at
  `/fixtures/official-robot.mp4` and `/fixtures/official-actions.json`. It checks
  their pinned hashes before using the first 16 frames and matching 29D action
  chunk from the model repository's public generated AgiBotWorld example.
  It crops the upper camera to 640x352 without inventing state telemetry.
  Source provenance explicitly says this is model-generated, not real robot
  recording evidence.
- `qualify_media_dataset.py` runs in the published CPU worker image, checks
  exact LeRobot 0.6.1, builds a new fixture, sends bounded sequential media
  requests, checks MP4 digest/size headers, normalizes transfer resolution,
  writes full datasets, packages/extracts/reopens them, and compares action,
  state (when present), frame-index and timestamp values. Use `--all-transfer`
  for edge-controlled lighting and environment; omitting it tests V2V and
  transfer transport independently. Every run requires a new output directory.

The runtime digest is the registry manifest digest in the publication receipt,
not the Docker configuration ID. Local test command from the control-plane
directory:

```bash
.venv/bin/pytest -q \
  ../../acceptance/cosmos3-customer-20260917/test_media_preview.py \
  ../../models/general-media/tests/test_cosmos3_media_adapter.py \
  ../../models/general-media/tests/test_shared_cache_localization.py \
  ../../models/general-media/lerobot-augmentation/tests/test_scientific_adapter.py
```

## Interpretation

Four requests passed on the restored snapshot with no container restart and
both readers decoded all frames. The realistic transfer outputs visibly change
lighting and background while retaining coarse gripper motion/layout, but
fine objects are stylized. V2V changes the late gripper trajectory and does not
strongly follow the lighting request. Numeric action preservation must not be
reported as verified physical alignment of generated video with those actions.
Neither mode is evidence of downstream training benefit or real robot safety.

The initial gradient fixture established only data mechanics. A subsequent
realistic request exposed the default 64 MiB shared-memory failure, retained as
a failed run, before the bounded 2 GiB mount was added and retested. The original
preemptible node later stopped independently; cleanup waited for the cloud
STOPPED fence. These failures are not hidden retries or successful cohorts.

Public acceptance still requires root-approved canonical profile/execution-map
activation, customer-scoped upload/submit/poll/download, same-tenant delegated
child accounting, both output datasets reopened, admin/priority/cancellation
checks, and the unchanged release cohorts specified in CUSTOMER_RELEASE_POLICY.
