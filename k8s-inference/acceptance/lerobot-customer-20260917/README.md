# Public LeRobot directory-to-directory client

`client.py` accepts a local LeRobot v3 directory, uploads its checked bundle and
scientific manifest, submits through the typed MCP tool or HTTP model route,
follows durable progress, downloads every output, safely unpacks it, and fully
reopens it with **LeRobot 0.6.1**. Local paths are interpreted only by this client;
they are never sent to Cosmos as remote paths. The input directory is unchanged.

Release151 (2026-09-18) passed the bounded public suite: two lighting/environment
cohorts plus two-variant blur, **five successful parent workflows and six
validated datasets**, both replays, malformed422, worker-invalid selection,
concurrency429 and CPU-stage cancellation. A separate operator-assisted probe
also cancelled a publicly confirmed running child through its parent. All
admitted work settled; no GPU-kernel interruption claim is made.

Use the ordinary-key CLI below. Dataset mechanics are not semantic or physical
qualification: representative visual review found object/trajectory drift and
the requested environment change was not visibly achieved. Exact action arrays
do not prove action-aligned imagery or training suitability. Hosted LibreChat
remains unqualified. See [release evidence](RELEASE.md) for the scoped verdict,
exact images, retained negative attempts and operator grant/cleanup receipts.

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
HTTP request must be admitted, then fail with `INVALID_REQUEST`, the exact static
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

## Release151 evidence and operator-only cancellation

The base suite ran01:48:03.898–01:59:50.046 UTC on unchanged CP source
`440d7c607`, index
`sha256:ab2f802727b29b60a301870771610ec5b9851e2d92f837ee422c52b7fd78f214`,
worker `sha256:1eeb26e239243c3c088b1ce9cb184f6cde9639100a6583d0d39b45d787cd85a1`.
Its private `final-cohorts-r151/acceptance.json` has SHA256
`dc70d3199bcb00ff1cfb62573a53df466b7f0fc42b55a347811c7a4628ea8f58`.
Twelve delegated generations produced the six fully checked datasets. Nine of
those generation records lack exact per-operation GPU identity; unavailable
identity is not measured zero or evidence of complete GPU accounting.

The base client was the source at `440d7c607` (including corrected assertions
from `109025990`/`cf30ce185`). After that suite, helper-only commit `3e49468a6`
fixed parsing of bare native `OperationView.operation="generate-media"` versus
the scientific dict envelope. The server/runtime/config did not change, and no
new full cohort is claimed for this client-only correction. Its actual server
DTO regression and the complete local acceptance suite passed38 tests.

Extra cancellation evidence remains separate from the pure-public base suite.
The first extra parent completed before its late observer began. The second
captured a running child, but the private helper's native-DTO error delayed
cancellation until all children had finished. Both failed/inconclusive receipts
remain untouched. A newly authorized probe after the tested parser fix used a
watcher-ready handshake before admission: parent
`13bcffd2-d8c5-45c9-9608-921e85c10881`, child
`9e82e652-f7f4-44e4-b405-42957d3f67f8`. The same ordinary key GET-verified that
child running immediately before parent cancel at02:10:30.162142 UTC; both then
became cancelled and the CPU attempt released resources. Child discovery was
operator-assisted; immediate GPU-kernel preemption is unverified.

The retained private final command, run from `k8s-inference`, was:

```sh
components/control-plane/.venv/bin/python \
  /home/tux/secure-handoff/lerobot-release-20260917/run_active_child_cancel_dto_fixed.py
```

That helper's source SHA256 is
`e550bf369155b6739da6faedc8f1c81154fc65b568901ddfdfe9d5b99fb97603`.
It requires a prior watcher-ready receipt and refuses an already-used output
directory; this is an operator evidence pointer, **not another customer command
or authorization to rerun it**. Its final receipt is
`active-child-cancellation-r151-dto-fixed/run.json`, SHA256
`e83bf5ad6c953dd41cfe27d5fbb0fda32fb95e349fad14b8003110b830e19a71`,
under the same owner-only handoff directory. No keys or raw private receipts
are committed. Earlier release147/149 backend failures and both release150
harness-error aggregates remain linked from [release history](RELEASE.md).

## Local evidence

Client/cohort/targeted-debug/continuation suite: **38 passed**. Real pinned-reader tests:
**2 passed** on the
two-episode/two-view fixture, including a full pack/unpack/rewrite/reopen loop
and all-camera/all-episode preparation. The rewrite test uses an explicitly
local FFmpeg brightness transform, not GPU inference or semantic qualification.
Ruff passes for all owned Python files.

```sh
components/control-plane/.venv/bin/python -m pytest -q \
  acceptance/lerobot-customer-20260917/test_client.py \
  acceptance/lerobot-customer-20260917/test_cohorts.py \
  acceptance/lerobot-customer-20260917/test_targeted_debug.py \
  acceptance/lerobot-customer-20260917/test_probe_continuation.py
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
production observer plus the initial51 new cases passed116. Release151's admitted
invalid-selection case verified the actual static `INVALID_REQUEST` detail and
released resources; the operator independently confirmed zero child operations.
