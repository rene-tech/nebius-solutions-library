# Cosmos media and LeRobot live qualification lane

`snapshot-media-compatibility.json` records actual H100 r7-restored runtime
compatibility with the new media adapter. It is deliberately not a public
customer acceptance receipt. `ADAPTER-CUTOVER.md` contains the separately
reviewed shared-template promotion plan.

## Final release146 bounded public result

`public-media-release146-summary.json` links the final immutable-image evidence:
20 first-attempt generations passed (four image/video compatibility cases and
two eight-case HTTP/MCP × HTTPS/upload × V2V/edge-transfer matrices). All outputs
were fully decoded and checksum-verified; in-flight and terminal replays kept
the same operation IDs. The ordinary upload-first path remained enabled.
An independently observed 90-second queued upload stayed CPU-only across fresh
metrics scrapes, then finalized normally.

The MCP-first and HTTP-first matrix cold starts were 38.225737 and 37.809772
seconds, respectively; end-to-end times were 42.546559 and 41.677109 seconds.
Both used existing cached preemptible H100s and confirmed r7 restore, not new
node provisioning. The first matrix's synchronous metrics-pair assertion failed
on a stale sample but the orchestration incorrectly continued. Six independent
advancing zero samples strictly before admission prove its actual cold state;
this does not make the preflight clean. The second launch explicitly required
a successful paired preflight. The harness negative is retained.

Three first-matrix operations have unavailable per-Pod/GPU attribution when
multiple ready Pods existed; zero-valued placeholders are not measured usage.
The second Pod's first-matrix logs were missed after natural deletion. Complete
other logs retain the explained TUN/iptables diagnostics reprinted at shutdown
by the immutable entrypoint, alongside successful CRIU/CUDA/unlock return codes.
No zero-warning or complete observability claim is made.

The canary had no outstanding work and was revoked at 16:21:23.926422 UTC;
an ordinary public read then returned 401. Cosmos naturally reached zero Pods,
zero demand and zero startup hold by 16:22:58.933 UTC, without manual scaling or
deletion. All media and receipts remain retained. An unused extra replica pulled
9.19 GB in 163.623 seconds and restored before normal shutdown; it served no
generation and is recorded as startup overhead, not per-request measured usage.

This qualifies only the tested bounded media paths. LeRobot remains unrouted;
public parent/child datasets, installed LibreChat/skills, Timothy's exact video,
physical action fidelity, concurrency and the full admin/usage release gate are
still incomplete. Combined customer readiness remains false.

## Retained earlier attempts

`public-media-failure-20260917.json` records the first real public cold request
after promotion. It failed with control-plane `runtime_protocol_error` despite
successful r7 restore and three adapter HTTP200 responses. No generated result
artifact reached the caller, and the remaining matrix was stopped. The initial
runner corrections and exact same-key operation recovery are retained explicitly;
this is neither a clean cohort nor a public media pass. The likely failure is
the gateway's JSON-only native semantic validation preceding binary result
artifactization. Release143 corrected that separate control-plane failure.

Release 143 corrected binary media handling and passed one complete eight-case
HTTP/MCP × HTTPS/upload × V2V/edge-transfer cohort with real artifact decode and
replay checks; `public-media-release143-intermediate.json` preserves its exact
operation/artifact identities, hashes, timings and limited scope. A subsequent
typed PNG compatibility call exposed an independent
fixed-default mismatch; `public-t2i-failure-20260917.json` records its HTTP422 and
the exact offline reproduction. Further admissions stopped at that failure.
The video cohort does not qualify the failing image tool or the full customer
workflow, and no LeRobot route was activated.

Release145 corrected typed and generic T2I contracts. The four cases in
`public-compatibility-release145.json` passed, including whole-JSON/base64 PNG,
legacy HTTP T2V, typed T2V and typed I2V using the generated PNG upload.
`public-media-release145-cohort1.json` records a subsequent complete eight-case
video matrix, all first attempts, with continuous runtime/adapter logs.
The second matrix's cold guard stopped before generation: its finalized input
upload was briefly counted as GPU demand and reactivated Cosmos. Exact evidence
is in `public-cold-guard-negative-release145.json`. Release146 corrected that
classification and reran the affected paths above; these twelve earlier
generation passes remain intermediate evidence, not final-digest qualification.

`public_compatibility.py` prepares four small supplemental cases: typed MCP T2I,
legacy HTTP T2V without a delivery override, typed MCP T2V, and typed MCP I2V
using the exact generated PNG through the existing tenant upload path. It
preflights all four public schemas before generation, verifies legacy JSON/base64
or whole-JSON artifact envelopes separately from binary artifacts, fully decodes
PNG/MP4 outputs, and checks both in-flight and terminal idempotent replay.
Settings stay at 256x256, seed 20260917, 35 steps and guidance 6; video is 33
frames at 20 fps without audio. This is a bounded compatibility test, not quality
or full customer qualification. Its default action is `plan`; actual runs require
the release owner's new-image GO, an exact expected digest and the existing
disposable canary. Any failure stops the remaining cases.

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
