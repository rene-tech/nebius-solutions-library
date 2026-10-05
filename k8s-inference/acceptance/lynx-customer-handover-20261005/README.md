# Lynx native GROMACS continuation: customer handover

Status: prepared runbook, **not a completed customer-performance qualification**.
The final L40S measurement and demo-owned public continuation/soak must fill the
release fields below before this is presented as tested. No customer API key is
used for the internal tests. The already-running customer recovery is untouched.

This supplements the existing [API contract](../../docs/SCIENTIFIC_BATCH_API.md)
and [continuation guide](../../models/molecular-dynamics/gromacs/CONTINUATION.md),
not a separate API or tenant-lifecycle implementation.

## What is being preserved and tested

The approved demo source is the original six-hour run's committed generation 71,
at native step 13,963,440 / 27.92688 ns. It contains 305 files / 82,674,112 bytes plus
the 130,375-byte manifest. The immutable source TPR SHA-256 is
`e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10`.
Every copied file is independently checksum-verified; the original bucket is
not changed. Customer files and signed URLs are never checked into Git.

The operator-authorized copy is private to the **existing demo-user tenant**,
under `runs/fs2-lynx-final-20261005/source`, not the shared demo seed. The copied
dataset is used for bounded native-continuation tests followed by a six-hour
demo soak, not a silent full 1 µs production run under demo credentials. The
demo 5 GB quota and Lynx 100 GB quota are different; neither is raised by this guide.

Import uses an ordinary demo-owned `run-workflow` with native
`restart_checkpoint`, the source TPR, and the retained native history. Once
that new operation has its own committed checkpoint and is terminal,
`POST /v1/operations/{id}:resume` tests normal same-owner recovery. No database
ownership change or cross-tenant artifact pointer is used.

The acceptance fixture may create a **separate finite-horizon TPR** to finish a
bounded test. It retains the original TPR byte-for-byte and runs `gmx check`
with zero tolerance: only `nsteps` may differ. The original 500,000,000-step /
1 µs target belongs to the customer's unchanged production protocol; a short
test is not evidence that this entire production run finished.

## Customer recovery must use the latest source, not the demo fixture

The running customer recovery is `a42479f9-5ee0-4ed4-869b-0a094357403f`.
**Do not cancel it.** Its original seven-day budget, hardware and performance
do not change when a successor is deployed. If it later fails or the customer
stops it, freshly retrieve its public operation status and checkpoint choices.
Use its latest committed checkpoint, not the older generation 71 demo copy.

If the existing tuning is appropriate, ordinary same-owner `:resume` below is
the simplest path and streams late history without creating another bundle.
If adopting a newly measured performance recipe, the offline
[`prepare_customer_import.py`](prepare_customer_import.py) helper prepares a new
ordinary `run-workflow` request. It does not submit, cancel, download or upload
anything by itself.

Provide these private inputs from the actual customer-owned operation:

- The fresh `GET /v1/operations/{id}/checkpoints` response, captured **after**
  terminal status, and the exact latest checkpoint artifact bytes it identifies.
- Every native file named by that checkpoint, downloaded to a private local
  directory and independently verified against its SHA-256 and size.
- The original **normalized/frozen** workflow parameters and original engine
  identity from that source's native result. A reconstructed request with
  different defaults is insufficient; the helper verifies the original recipe
  digest before preparing any changes.
- The final qualified performance settings. [`tuning.example.json`](tuning.example.json)
  contains the confirmed native finalist: eight threads, `nstlist=200`,
  `pin=auto`. Public delivered-throughput and soak acceptance remain separate.

### Where each input comes from

Ordinary same-owner `:resume` is public and does **not** need this offline
packaging helper or database access. A tuned new-operation import has an extra
provenance requirement: the source's exact normalized request. The current
public status/checkpoint/result APIs do not export that request automatically.

| Helper input | Existing customer path or prepared input |
| --- | --- |
| Latest checkpoint choices | `GET /v1/operations/{source_id}/checkpoints`; select `.jobs[]` by `job_id` after the source is failed/cancelled |
| Exact checkpoint bytes | Download `.jobs[].checkpoint.artifact_id` using `GET /v1/artifacts/{id}/content`; verify the pointer's SHA-256 and byte length before decoding |
| Native files | In that downloaded checkpoint, every `.files[]` has `path`, `sha256`, `size_bytes` and an `artifact.artifact_id`; download through the same artifact route and reconstruct the complete relative tree |
| Original TPR/target | Checkpoint `.state.active_step.tpr_sha256` and `.target_step`, independently matched against the original supplied TPR and agreed full target; do not substitute the demo's shortened TPR |
| Source native engine identity | Terminal `GET /v1/operations/{source_id}/result` → `output_manifest.artifact_id` → output-manifest entry with `semantic_type` `gromacs-workflow-result/v1` or `gromacs-failed-result/v1` → download its `artifact.artifact_id` and read `.engine_id` |
| Frozen parameters | The caller's saved exact request can be used only if normalization reproduces the checkpoint recipe hash. Otherwise the operator supplies the prepared frozen request below; no customer database access is needed |
| Qualified tuning | The provided tuning file, with its exact native/public acceptance scope stated in the final receipt |

The helper's `--source-engine-id` is the native result's **`engine_id`**, not
the outer result's `execution_identity.runtime_image_digest`. These are distinct
identities. A failed/cancelled run may lack a final native result after abrupt
loss; then use the operator-verified engine identity, never infer it from the
current catalog. Checkpoint metadata alone contains a recipe hash, not the
complete original request or engine identity.

For the current Lynx source, the required immutable inputs have already been
prepared privately at:

`/home/tux/secure-handoff/fs2-lynx-customer-handover-20261005/a424-frozen-source/`

- `source-frozen-parameters.json`: exact frozen `.fs2/request.json`, SHA-256
  `b9bb43f5e896cc85766892bdb10be3df4575f9eeedbddd4fa5f41d8dd5a25160`.
- `source-engine-id.txt`: exact native engine identity.
- `export-receipt.json` and `helper-verification.json`: tenant/operation-scoped
  read-only export and native recipe verification; no customer key was used.

Supply these private files with the customer handover; do not publish them to
Git or ask Lynx to query PostgreSQL. This operation was itself a continuation,
so its frozen request includes generated per-file bindings which are not
recoverable from the customer's original pre-continuation submit body alone.
The export used the existing repository decoder and an explicitly read-only
transaction, then verified the recipe against the exact running worker's
native metadata. Its remaining workflow is `mdrun` → `trjcat` → `check`; the
trajectory command already has an explicit file pattern and receives the
documented `nonempty: true` adaptation.

These frozen inputs stay valid while this exact operation runs. The export
does **not** make it terminal or freeze its latest checkpoint: after the
customer's chosen stop or an actual failure, retrieve fresh public checkpoint
choices and all files for that latest committed generation. The helper refuses
the still-running source. If the recovery lineage advances to another operation,
obtain that operation's matching frozen request instead of reusing these files.

The public API gap is the lack of a frozen-request export endpoint, not a
requirement for customer database credentials. The operator-prepared files
close it for this handover; no new API or broader access was introduced here.

```bash
read -r SOURCE_ENGINE_ID < /private/a424-frozen-source/source-engine-id.txt
python prepare_customer_import.py \
  --source-operation a42479f9-5ee0-4ed4-869b-0a094357403f \
  --job-id mas1-20e \
  --checkpoint-choices /private/latest-checkpoint-choices.json \
  --checkpoint /private/latest-platform-checkpoint.json \
  --original-parameters /private/a424-frozen-source/source-frozen-parameters.json \
  --source-engine-id "$SOURCE_ENGINE_ID" \
  --native-root /private/latest-native-files \
  --expected-tpr-sha256 e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10 \
  --expected-target-step 500000000 \
  --tuning /private/qualified-tuning.json \
  --output-prefix runs/gromacs-tuned-continuation \
  --output /private/prepared-customer-import
```

Run with the control-plane Python environment supplied by this checkout.
Replace the example source/job only if the actual terminal recovery lineage
has advanced; never select an older checkpoint just to make the helper pass.
The helper binds the latest pointer, source/job, full original TPR and target,
all copied files and the frozen recipe. It reuses the production continuation
logic to skip completed commands while retaining unfinished analysis. It
outputs deterministic `input.tar.gz`, `parameters.json` and `provenance.json`.
The bundle retains every original file in a source-history directory as well
as writable working copies, so old wrapper logs and checkpoints are not lost
when the new run updates them. Original TPR bytes and the full 500,000,000-step
target remain unchanged; no `convert-tpr` truncation is used for the customer.

Upload that bundle and its input manifest using the actual owner's API key.
Use the generated `parameters.json` as the submit body's parameters, rather
than the illustrative path names in the example below. The helper verifies
the combined file/byte envelope; if a very late complete history exceeds its
bundle bounds, use ordinary per-file `:resume` with unchanged tuning or request
an explicitly qualified import path. Never omit history to fit a limit.

### Preserve empty history without breaking final analysis

Some native trajectory parts are empty when a segment ends before the next
trajectory-output interval. The recorded demo source has 42 empty XTC parts
out of 70; native `trjcat` rejects an empty file. **Keep those original artifacts.**

For this tuned import, the helper explicitly adds `nonempty: true` to existing
file-pattern arguments following `-f` in unfinished `trjcat` and `eneconv`
commands. For example:

```json
{"id":"join-trajectory","command":"trjcat","args":["-f",{"files":"md.part*.xtc","nonempty":true},"-o","md.xtc"]}
```

It preserves the original patterns, directories, command order, other arguments
and expected outputs. It records every affected selector and its previous
setting in `provenance.json` under `analysis_file_selections`. It changes no
other analysis tool, literal filename, simulation physics or original artifact.
The selection is evaluated after simulation, so later resumed parts are included;
the helper does not bake in a list of files from the old checkpoint. Empty files
remain in both working files and the complete source-history archive.

Before submission:

1. Inspect `analysis_file_selections` and the generated `parameters.json`.
   Confirm that the `-f` patterns select the intended trajectory/energy parts,
   not the duplicate preserved source-history copies.
2. Confirm the **published** GROMACS request schema advertises the boolean
   `nonempty` file-selector field and the coordinated worker release has passed
   its public acceptance. Local schema/unit checks alone are not that gate.
3. Retain the full source inventory. Do not delete empty files, drop history, or
   shorten the TPR to force analysis to pass. If an old analysis command lists
   a literal filename already known to be empty in the source checkpoint, the
   helper stops **before packaging** and names that input. Prepare a reviewed
   selector for that command; the helper does not silently rewrite literals.
   Future generated files cannot be checked from the old checkpoint alone.

An input pattern with no nonempty matches still reports an explicit error; no
trajectory or scientific result is fabricated. Output cadence must produce at
least one usable frame for trajectory analysis. The generic helper does not
add an analysis step where the original workflow had none.

## Submit a tuned import through the public API

Use the existing [upload and manifest API](../../docs/SCIENTIFIC_BATCH_API.md#2-upload-your-inputs)
to upload a verified `gromacs-inputs` gzip-tar bundle into the submitting owner's
scope. In this example the bundle contains `source-history/simulation.tpr`,
`source-history/fs2-production.cpt`, and all retained source files. The manifest
points to that bundle. Do not place S3 credentials, shell commands, or signed
URLs in the request.

`import-request.example.json` illustrates the public body. Its manifest pointer
must be replaced with the real finalized manifest, and the tuning values must
match the final qualified recipe. The supplied eight-thread / `nstlist=200` /
`pin=auto` settings are the confirmed native finalist, **not a claim that the
public delivered-throughput/soak gate passed**. Pool assignment is operator-owned: the submit body does not accept an
invented GPU-selector field. Confirm `resolved_pool_id` in the admitted status
and receipt; a run on another pool is not the same performance measurement.

```bash
export SCIENTIFIC_AI_BASE_URL='https://89.169.99.188/v1'
# SCIENTIFIC_AI_KEY is supplied privately for the actual owner. Never paste it in Git.
curl --fail-with-body --silent --show-error --max-time 120 \
  -X POST "$SCIENTIFIC_AI_BASE_URL/models/gromacs:submit" \
  -H "Authorization: Bearer $SCIENTIFIC_AI_KEY" \
  -H 'Idempotency-Key: lynx-native-import-<unique-run-label>' \
  -H 'Content-Type: application/json' \
  --data-binary @import-request.json
```

`202` acknowledges admission only. Retain the returned operation ID. If the
HTTP response is lost, replay the **same** body and idempotency key; do not
launch a replacement with a fresh key. Per-request HTTP timeouts are not native
execution budgets and do not cancel the admitted simulation.

## Observe and continue an existing same-owner operation

```bash
curl --fail-with-body --silent --show-error --max-time 60 \
  "$SCIENTIFIC_AI_BASE_URL/operations/$OPERATION_ID" \
  -H "Authorization: Bearer $SCIENTIFIC_AI_KEY"

curl --fail-with-body --silent --show-error --max-time 60 \
  "$SCIENTIFIC_AI_BASE_URL/operations/$OPERATION_ID/checkpoints" \
  -H "Authorization: Bearer $SCIENTIFIC_AI_KEY"
```

Poll every 5–15 seconds, retain request IDs and distinguish `queued` from
`running`. Retry transient transport/429/5xx failures with bounded backoff;
honor `Retry-After`. A stopped browser/notebook or exhausted client wait budget
does not mean the server-side job failed. Query its status before any action.
Do not treat 401/403 as transient, and never submit duplicates to work around
queueing. Check the remote committed generation, saved native step and current
transfer phase; local `.cpt` existence alone does not prove durability.

When an operation is **failed or cancelled**, select its checkpoint job and
request a fresh budget:

```bash
curl --fail-with-body --silent --show-error --max-time 120 \
  -X POST "$SCIENTIFIC_AI_BASE_URL/operations/$SOURCE_OPERATION_ID:resume" \
  -H "Authorization: Bearer $SCIENTIFIC_AI_KEY" \
  -H 'Idempotency-Key: lynx-native-resume-<unique-run-label>' \
  -H 'Content-Type: application/json' \
  --data-binary @resume-request.example.json
```

Use the returned **new** operation ID from then on. The source stays terminal.
The `job_id` must be the actual source job (the import example uses
`production`; acceptance fixtures may use another job ID).

The resume schema accepts `job_id` and `max_wall_seconds`, **not tuning
overrides**. It preserves the source workflow arguments and native science.
For a changed performance recipe, use a separately qualified new-operation import
as above, keeping checkpoint/TPR/protocol lineage explicit. Do not pretend an
unsupported field such as `resume.mdrun_args` can change the deployed recipe.
An active customer operation cannot be resumed and is not cancelled for testing.

Results are downloaded via the operation result/output manifest and artifact
content endpoints in the API guide. In customer storage, generation manifests
live below `<output_prefix>/<operation_id>/<job_id>/attempt-NNN/`; their file
entries identify immutable objects and checksums. Verify checksums and retain
the complete native files, input identities, runtime digest and command history.

## Performance and release receipt

The relevant speed is **new durably completed ns /accepted-to-completed wall
time**, including queueing, startup, restore, simulation and export. Native
GROMACS `ns/day` is reported separately. Do not count already completed source
steps, retry work or a team's aggregate MPS throughput as new single-run speed.

Earlier unchanged-input L40S measurements were 200.98 ns/day by REST and
196.58 ns/day by raw MCP. They are retained as evidence, not rounded up to a
universal ≥200 ns/day result. The new target needs its own exact-release proof.

Before customer handover, record:

| Release field | Required evidence |
| --- | --- |
| Control-plane/companion image and engine image | Immutable digests from actual admitted run |
| L40S recipe | Threads, offloads, `nstlist`, pinning; unchanged TPR physics |
| Demo import operation and checkpoint | Real operation/job/generation IDs; saved step from native log |
| Public continuation | Real new operation ID; same-key replay returns same ID |
| Completed work | New step range, verified earlier history and new output artifacts |
| Throughput | Delivered ≥200 ns/day on two clean unchanged-release cohorts; native speed separately |
| Six-hour soak | Actual duration, committed progress, transfer timings, bucket growth and interruptions |
| Sibling availability | Public API responsiveness and untouched original customer operation |

Until these fields are filled from the qualification receipts, no blanket
"no more timeouts" or fourteen-day soak claim is supported. The
[lifetime and storage analysis](LIFETIME.md) states the precise limits and the
signed-handle fix; [demo qualification tooling](../lynx-demo-resume-20261005/prepare_acceptance.py)
preserves the source history for the actual test.
