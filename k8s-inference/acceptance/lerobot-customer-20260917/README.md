# Public LeRobot directory-to-directory client

`client.py` accepts a local LeRobot v3 directory, uploads its checked bundle and
scientific manifest, submits through the typed MCP tool or HTTP model route,
follows durable progress, downloads every output, safely unpacks it, and fully
reopens it with **LeRobot 0.6.1**. Local paths are interpreted only by this client;
they are never sent to Cosmos as remote paths. The input directory is unchanged.

Current status (2026-09-18): release149 completed **four dataset-mechanics
passes**: lighting edge-transfer and all-camera/all-episode environment V2V in
each of two cohorts, swapping HTTP and named MCP. Every output passed the full
pinned reader and both idempotent replays. The fifth, two-variant blur parent
`dc708f87-d0a2-4021-b645-7540cf0f4837` failed with public HTTP422 /
`PLATFORM_UPSTREAM_ERROR`; its static detail is “The control plane rejected or
could not process a delegated Cosmos request.” Its worker resources are
released. The runner stopped: malformed-input, worker-invalid-selection,
concurrency and cancellation probes **did not run**. This is not a passing
complete suite. No blind retry or further admission followed.

The unchanged release was CP source `8fe42b85f`, index `sha256:da50219255ae4af4975bba74c0221890cce1eaad4b28ac114d89ceb087e624e4`,
worker `df364675`. Original journals and all four outputs remain private under
`/home/tux/secure-handoff/lerobot-release-20260917/final-cohorts-r149/`.
[Run a local directory](#run-a-local-directory) for the usable CLI; see
[release evidence](RELEASE.md) for exact identities, observations and retained
release147 failure / release148 corrected debug history.

Mechanics are not semantic or physical qualification: visual review observed
object and trajectory drift, and the requested environment change was not
visibly achieved. Exact action/timestamp arrays and changed pixels do not prove
action-aligned imagery or suitable training data. Actual hosted LibreChat also
remains unqualified. A corrected-release follow-up requires explicit operator
GO; the original failed suite is never relabelled as a pass.

## Run a local directory

Run from `k8s-inference`. Use the control-plane lock for MCP/HTTP dependencies
and the worker's separate pinned reader environment (Python 3.12/3.13, FFmpeg):

```sh
uv sync --frozen --group dev --directory components/control-plane
uv sync --frozen --directory models/general-media/lerobot-augmentation/runtime

components/control-plane/.venv/bin/python \
  acceptance/lerobot-customer-20260917/client.py run \
  --dataset /path/to/lerobot-v3-directory \
  --request acceptance/lerobot-customer-20260917/lighting.json \
  --endpoint https://YOUR-PUBLIC-GATEWAY \
  --key-file /private/robotics-key.json \
  --reader-python models/general-media/lerobot-augmentation/runtime/.venv/bin/python \
  --output /private/lerobot-lighting-run \
  --protocol mcp --check-replay
```

The key file must be a regular, non-symlink `0600` file containing the key alone
or JSON with `secret`/`token`. Never put it on the command line. The output
directory must not already exist or be inside the source directory. Receipts and
artifacts are owner-only; signed download handles are used transiently, never
stored. Use `--protocol http` for
`POST /v1/models/cosmos3-lerobot-augmentation:submit`. Both modes use the
advertised public MCP artifact inspection/download tools. No admin API,
Kubernetes access, customer-key mutation, or global client configuration is
needed by this client.

The ordinary key and its owner must allow `cosmos3-lerobot-augmentation` and
`cosmos3-nano`, with `artifacts.write` in addition to the usual catalog,
inference/MCP, operation read/result/cancel scopes. Parent and its single active
delegated child share the concurrency slot; `max_concurrency=1` is supported.
The operator must grant these explicitly without changing budgets/limits or
rotating an existing customer key. `release_operator.py` is a separate operator
tool, not part of the customer client.

The supplied policies accept their local source from `--dataset`; they do not
need a manually constructed artifact pointer. A full scientific request is
also accepted as the template, with source/manifest pointers replaced by this
client's finalized uploads. Default client limits match the advertised 5 GiB
compressed / 8 GiB expanded bounds, with a one-hour polling deadline (at most
two hours). `--max-bytes` and `--max-expanded-bytes` optionally select smaller
local limits. The server's conservative workspace admission remains
authoritative: not every maximum-size selection/variant combination fits.

Outputs include `run.json` with operation/upload IDs, request, progress and
terminal views; `result.json`; checked archives; `variant-00/` (and subsequent
variants); and `variant-00-validation.json`. Every variant is checked, not just
the first. The worker result uses local labels such as `<parent>.variant-00`;
the client joins each label/index and exact content identity to the finalized
public UUID in the scientific output manifest. It preserves the original
result and downloads the manifest's public reference. Validations require every decoded camera frame, exact dtype/value
preservation of **all nonvideo fields**, identical episode/task/FPS structure,
correct provenance, and changed pixels in each selected stream. Untouched
streams are re-encoded: their per-frame mean absolute pixel error must be at
most 6/255, not byte-identical. Do not treat changed pixels as semantic success.

## Existing operation recovery and cancellation

There is no automatic admission or transport retry. Upload IDs are persisted
before content transfer; the parent idempotency key is persisted before submit.
An uncertain admission without a returned ID requires operator reconciliation,
not a new key or blind resubmission. Known parents can be resumed without any
new upload or admission:

```sh
components/control-plane/.venv/bin/python \
  acceptance/lerobot-customer-20260917/client.py resume \
  --key-file /private/robotics-key.json \
  --reader-python models/general-media/lerobot-augmentation/runtime/.venv/bin/python \
  --output /private/lerobot-lighting-run
```

Use `cancel` instead of `resume` to explicitly cancel that known parent and wait
until all recorded stage attempts release resources. `run --submit-only`
records admission without waiting. Concurrent writers to one run directory are
refused. A partial download or partial unpack remains evidence and is not
silently overwritten; reconcile it before retrying collection into a fresh
destination. Do not rerun `run` to recover a known operation.

## Bounded public acceptance

After the operator verifies every replica and freezes the exact release:

```sh
components/control-plane/.venv/bin/python \
  acceptance/lerobot-customer-20260917/run_acceptance.py \
  --dataset /private/public-robot-2x32 \
  --endpoint https://YOUR-PUBLIC-GATEWAY \
  --key-file /private/disposable-robotics-key.json \
  --reader-python /private/pinned-reader/bin/python \
  --release-receipt /private/verified-deployment.json \
  --output /private/lerobot-public-cohorts --include-blur
```

Two consecutive cohorts each run lighting edge-transfer on episode0/front and
native V2V environment variation on both episodes/cameras. HTTP and named MCP
are swapped between cohorts. The optional fifth parent produces **two complete
output datasets** with distinct seeds for blur-transfer on episode1/wrist. Thus
the positive matrix has five parents and six validated datasets; the runner
checks every output count and variant, including a regression that rejects a
corrupt second variant. All use35 steps / guidance6. Each parent has both in-flight and
terminal replay checks. A final additional parent tests running-stage
cancellation and an overlapping second admission tests the documented HTTP429
`concurrency_exceeded` response. Invalid `variants.count=0` must produce public
HTTP422, not authentication failure. An unexpected HTTP202 is preserved as an
actual admission, cancelled/settled by ID, and **does not** count as the expected
429 result. Failures stop new admissions; no automatic resubmission is made.

One additional deliberately invalid worker selection reuses the valid finalized
input artifacts, changing only `selection.episodes` to `[999]`. This ordinary
HTTP request must be admitted, then fail with `DATASET_INVALID`, the exact static
public detail and released worker resources. It is an explicitly expected
negative case, not a successful dataset. Its receipt separately records that
zero generation children still require the operator's exact-parent terminal
collector; the client cannot infer an empty child set from parent status. The
positive source dataset/policies are not modified and this case is never
automatically retried.

For an explicitly authorized corrected-release debug, `run_targeted_debug.py`
accepts the same arguments and runs only the two-variant MCP blur case followed
by those negative probes. It uses a new output directory and reports
`targeted_blur_probes_passed`, never a final-cohort pass. It stops on an unexpected
blur or probe failure and makes no automatic retry. This smaller diagnostic
does not replace the two unchanged-release cohorts after qualification.

The release receipt is hashed, not asserted to prove live stability by this
ordinary-key process. `observe.py` and the deployment owner separately verify
unchanged release/runtime identities, all parent/child Runs and Usage,
containers/logs/metrics, real GPU/scaling observations, and final settlement.
Only the operator revokes the disposable key/owner after all work settles.
Actual hosted LibreChat remains a separate gate; neither SDK calls nor this
client replace that evidence.

The fixture contains the pinned official model-generated robot clip's 64
frames and four original 16×29 action chunks, split into two 32-frame episodes
with two 640×352 views at 10fps. It is not recorded robot telemetry or calibrated
multi-camera data; observation.state contains explicit synthetic identifiers.
Native V2V can change trajectories and scene content. Even constrained transfer
can change fine details. Inspect every generated clip before training: exact
action arrays are **not proof of physically action-aligned augmented imagery**.

## Local evidence

Client/cohort/targeted-debug suite: **30 passed**. Real pinned-reader tests:
**2 passed** on the
two-episode/two-view fixture, including a full pack/unpack/rewrite/reopen loop
and all-camera/all-episode preparation. The rewrite test uses an explicitly
local FFmpeg brightness transform, not GPU inference or semantic qualification.
Ruff passes for all owned Python files.

```sh
components/control-plane/.venv/bin/python -m pytest -q \
  acceptance/lerobot-customer-20260917/test_client.py \
  acceptance/lerobot-customer-20260917/test_cohorts.py \
  acceptance/lerobot-customer-20260917/test_targeted_debug.py
FS2_LEROBOT_LOCAL_DATASET=/private/public-robot-2x32 \
  /private/pinned-reader/bin/python -m pytest -q \
  acceptance/lerobot-customer-20260917/test_reader.py
```

The reader tests skip unless their explicit dataset environment variable is
set. Initial negative local attempts are retained: a client unpack check
rejected legitimate directory entries in the worker tar; allowing safe normal
directories fixed it and the full reader regression then passed. LeRobot emits
upstream deprecation warnings during encoding; these are not silently called
clean GPU execution. No live public dataset pass is implied by these tests.

The first live status also corrected two acceptance-only diagnostics: public
stages are `active`, not `running`, and the running-cancellation probe requires
an unreleased attempt in `active_compute`; MCP context managers' nested
ExceptionGroup no longer hides the safe `operation_terminal_failure` code.
These fixes do not repair or relabel the actual worker/application failure.

The follow-up CP source adds a bounded static worker-error projection: only a
recognized LeRobot scientific-stage termination message is accepted; received
text is never copied into public detail. OOM/preemption/infrastructure/timeout
classification stays unchanged. Its new focused suite passed52 tests; existing
production observer plus the initial51 new cases passed116. The actual worker
negative case above must still verify this on the new deployed image.
