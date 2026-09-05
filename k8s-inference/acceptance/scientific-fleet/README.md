# Scientific fleet public acceptance runner

`run_acceptance.py` qualifies one model-owned canary through the same public
HTTP and bearer-token boundary available to a customer. It does not read
Kubernetes, object storage, Terraform state, or registry credentials and it
does not change a model route.

The runner reads an activation fragment's `model_id` and `public_fixtures`.
Paths are relative to the `k8s-inference` directory and must stay inside it:

```json
{
  "public_fixtures": {
    "request": "models/example/activation/public-request.json",
    "supporting_inputs": [
      {
        "role": "request-input-manifest",
        "path": "models/example/activation/input-manifest.json",
        "encoding": "canonical-json-newline"
      },
      {
        "role": "manifest-artifact",
        "name": "target_sequence",
        "path": "models/example/activation/target.fasta",
        "encoding": "raw"
      }
    ]
  }
}
```

For a direct input, declare exactly one `request-input-manifest` whose bytes
match the request's `input_manifest` digest and omit `manifest-artifact`
entries. For a scientific manifest, that declaration is the canonical
manifest template and each manifest entry has one `manifest-artifact` input.
`name` is recommended and binds to the manifest entry name; a declaration
without `name` is accepted only when its digest identifies exactly one entry.
The runner uploads every exact artifact, replaces template pointers with the
returned immutable public pointers, canonicalizes and uploads the resulting
manifest, then replaces the request pointer before submission.

`deterministic-tar-gzip-v1` is available for small, source-controlled public
fixtures that a runtime consumes as a tar workspace. The declaration's `path`
is the source file and `archive_path` is its fixed relative path in the tar.
The materializer fixes tar ownership, mode and timestamp plus every gzip header
field and emits byte-stable stored DEFLATE blocks, so the request can bind one
stable digest without checking in generated binary data. It is accepted only
for named `manifest-artifact` declarations.
Proteina-Complexa uses it to package the Apache-2.0 PD-L1 structure from the
pinned upstream source at
`assets/target_data/bindcraft_targets/PD-L1.pdb`; no checkpoint, credential, or
licensed PyRosetta package is part of the fixture.

`deterministic-tar-gzip-manifest-v1` covers the same boundary when one runtime
input needs several source-controlled files. Its JSON document lists a bounded
set of `source_path` and `archive_path` pairs; both paths stay inside their
respective repository/archive roots, duplicate archive members are rejected,
and the same byte-stable tar/gzip encoding is used. BoltzGen uses this form for
the exact projected public PD-L1 mmCIF plus its one-design campaign YAML.

BindCraft's public fixture is also deliberately bounded to one design, one GPU
shard, a fixed 67-residue binder length, the soluble MPNN lane, and seed
`912083`. The seed fixes the upstream trajectory, but the pinned ColabDesign
ProteinMPNN implementation initializes its own sampling key from process
entropy, so a single accepted winner is not bit-for-bit replay stable.

The content-addressed target is exactly human PD-L1 UniProt Q9NZQ7 residues
18..132, renumbered as PDB chain A residues 1..115. The all-human 4ZQK
PD-1/PD-L1 structure identifies I54, Y56, M115, A121, and Y123 as the central
hydrophobic core on PD-L1's PD-1-binding surface. In this target's coordinate
namespace the compact panel is `A37`, `A39`, `A98`, `A104`, and `A106`; its
maximum alpha-carbon span is 11.540 Angstrom. Supplying the canonical numbers
without the target's -17 numbering translation names unrelated residues and is
guarded against by repository tests. The numbering and sequence are anchored
to [UniProt Q9NZQ7](https://rest.uniprot.org/uniprotkb/Q9NZQ7.fasta), and the
five-residue core comes directly from the primary human-complex analysis
[PMCID PMC4752817](https://pmc.ncbi.nlm.nih.gov/articles/PMC4752817/).

This does not relax result validation: every exported winner must still pass
the pinned production filters and prove a measured atom-to-atom contact of at
most 4.0 Angstrom to at least one explicitly named crystallographic core
residue. All five residues must exist with their expected amino-acid identities
in the content-addressed input structure, the panel must remain spatially
compact, and the adapter's one-design trajectory budget remains bounded. An
offline replay of the exact runtime geometry algorithm against the captured
`425e393d` winner measured all five core residues within 2.676 Angstrom; the
content-addressed analysis is retained beside the H100 qualification evidence.

Use a token that can upload and invoke the selected model and read its
operation result. The token is accepted only through an environment variable:

```bash
export FS2_INFERENCE_TOKEN='...'
python3 acceptance/scientific-fleet/run_acceptance.py \
  --endpoint https://inference.example \
  --activation-fragment models/example/activation/fragment.json \
  --run-id qualification-20260904-01 \
  --receipt /secure/run/qualification-20260904-01.json
```

The output is mode `0600` canonical JSON. It contains only the endpoint host,
model and operation identities, API-provided timestamps, cold-start/runtime
attribution, queue decisions and admissions, execution identity, attempt
identity, and artifact digests. Bearer tokens, principal and tenant identity,
cookies, presigned handles, signed URLs, and object-store locations are never
copied from responses. An existing receipt is not overwritten unless
`--overwrite` is explicit.

The command returns nonzero and writes no receipt when an upload identity does
not match its bytes, the route rejects admission, the operation misses its
deadline or terminates unsuccessfully, the terminal result is inconsistent,
or semantic validation did not pass.

Run the offline fake-HTTP acceptance suite with:

```bash
acceptance/scientific-fleet/run_checks.sh
```

## Complete fleet acceptance and benchmark receipt

`run_fleet_acceptance.py` discovers the five primary activation fragments and
the five secondary public-acceptance records committed under `models/`, then
runs the single-model client above in separate child processes. `--max-parallel`
bounds concurrent customer submissions; the default is four and the accepted
range is 1–32. One failed model does not cancel the remaining models, and the
command exits nonzero after every discovered input reaches an outcome.

The bearer value remains only in `FS2_INFERENCE_TOKEN`. The fleet process puts
the environment-variable **name**, never its value, in child argv. Child
stdout/stderr is captured and not replayed. Per-model receipts and the canonical
aggregate are mode `0600`; each run gets its own mode `0700` directory beneath
the requested receipt root.

After the deployed routes are ready, run the whole current fleet with:

```bash
export FS2_INFERENCE_TOKEN='...'
python3 acceptance/scientific-fleet/run_fleet_acceptance.py \
  --endpoint https://inference.example \
  --run-id scientific-fleet-20260904-01 \
  --receipt-root /secure/fs2-acceptance \
  --max-parallel 8
```

This writes:

```text
/secure/fs2-acceptance/scientific-fleet-20260904-01/
├── aggregate.json
├── alphafold3.json
├── bindcraft.json
├── esmfold2.json
└── ... one receipt for every successful model
```

`aggregate.json` is compact, key-sorted canonical JSON. Its model rows pin the
input and per-model receipt digests and copy the API-provided cold-start,
runtime identity/timestamps/attempts, and queue/admission evidence. Exact GPU
occupied/idle accounting is copied when a per-model API receipt exposes one of
the recognized accounting projections. When it does not, the field is
explicitly `available: false`; the runner never estimates ledger data from
wall-clock timestamps. Failed rows contain only the model/input identity and a
stable non-secret failure code. Reusing a run ID fails before any network call
unless `--overwrite` is explicit.

## Promote successful public runs

`promote_qualifications.py` is the offline, reviewable bridge from fleet
acceptance to catalog qualification. Prefer to run it in the same repository
revision that supplied the acceptance inputs. The aggregate and every
successful per-model receipt must remain together, regular files with mode
`0600`.

The command verifies their exact bytes and projections, the model/variant and
full execution identity against the canonical profile and execution map, and a
successful scheduler admission for every stage selected by the run-specific
controller plan. Selected stages must be a non-empty, canonically ordered,
dependency-complete subset of the catalog stages; this preserves request-driven
optional stages such as BoltzGen affinity without treating an unrecognized or
disconnected stage set as evidence. A GPU decision must use the profile's exact
GPU count and ordered compatible-pool set; its successful admission must resolve
to one of those pools. CPU stages must have zero GPU resources.

First verify that the checked-out profile/map digest chain is current:

```bash
uv run --project components/control-plane \
  python components/control-plane/scripts/refresh_scientific_recipes.py --check
```

The default is a read-only plan:

```bash
python3 acceptance/scientific-fleet/promote_qualifications.py \
  --aggregate /secure/fs2-acceptance/<run-id>/aggregate.json
```

Review every per-model action and reason, then apply the same evidence:

```bash
python3 acceptance/scientific-fleet/promote_qualifications.py \
  --aggregate /secure/fs2-acceptance/<run-id>/aggregate.json \
  --write
```

If an unrelated model changed after acceptance and the deterministic refresh
therefore changed the fleet-wide `execution_map_sha256`, point the tool at a
read-only checkout of the exact accepted revision:

```bash
python3 acceptance/scientific-fleet/promote_qualifications.py \
  --aggregate /secure/fs2-acceptance/<run-id>/aggregate.json \
  --acceptance-repository-root /read-only/accepted-revision
```

This is not a general stale-evidence override. The aggregate input digest must
match that checkout, and the current and accepted model-owned documents and
that model's complete execution-map entry must be identical after normalizing
only the fleet-wide map digest (and an idempotent prior promotion). A changed
target model is skipped. The generated receipt retains both fleet map digests
and a digest of the unchanged model entry, so reviewers can audit why evidence
survived unrelated fleet drift. Run the read-only plan before adding `--write`.

Only successful rows whose complete evidence matches are changed. Failed or
stale rows stay `active` and are reported as `skip`; one mismatch cannot lend
qualification to another model. Each accepted model becomes `qualified` in
the canonical catalog and its model-owned profile projection. Primary
activation fragments also close `public_platform_run_required` and record the
public-accepted H100 state. The raw public receipt remains in operator custody;
its exact SHA-256 is recorded as `public_completion_receipt_sha256`.
On the next catalog load, the global scientific admin projection treats that
complete pinned profile as qualified without borrowing evidence from an online
serving lane. This is also what lets a mapped batch backend such as RFdiffusion
supersede a pre-promotion serving-lane identity mismatch truthfully.

For each promoted model the tool commits a secret-free, content-addressed
`activation/qualification/scheduler-eligibility-<sha256>.json` projection. It
chains the aggregate, acceptance input, execution map and execution identity to
the frozen scheduler decision and successful admissions without copying pod,
node, GPU, workload, operation or tenant identities. The exact bytes of that
file provide `scheduler_eligibility_receipt_sha256`. Catalog and owner
projections plus these receipts are written as one rollback-safe transaction.
Replaying the same aggregate is idempotent; conflicting evidence never replaces
an existing qualification.

After `--write`, review the generated files and run:

```bash
uv run --project components/control-plane \
  python components/control-plane/scripts/refresh_scientific_recipes.py --check
acceptance/scientific-fleet/run_checks.sh
models/cancer-immunotherapy/primary-fleet-activation/run_checks.sh
```

Do not substitute hand-authored digests when a live fleet receipt is absent.
The repository intentionally retains `active`/null evidence until an exact
public run exists.

The customer operation/result contract does not currently return the exact
per-attempt lifecycle rollup. The operator summary at
`GET /admin/api/v1/scientific-runs/{operation_id}` is useful for joining the
run, but its GPU measurements correctly remain unavailable until that admin
projection is connected to the lifecycle ledger. The authoritative existing
PostgreSQL join for post-run enrichment is:

```sql
SELECT subject.operation_id, subject.attempt_id, rollup.terminal,
       rollup.reconciled, rollup.quality, rollup.data_gaps,
       rollup.scheduler_occupied_gpu_seconds,
       rollup.device_allocated_gpu_seconds,
       rollup.active_gpu_seconds,
       rollup.occupied_idle_gpu_seconds,
       rollup.phase_gpu_seconds,
       rollup.reconciliation_delta_seconds
FROM fs2_telemetry_subjects AS subject
JOIN fs2_reporting_lifecycle_latest AS rollup USING (subject_id)
WHERE subject.operation_id = ANY ($1::uuid[])
ORDER BY subject.operation_id, subject.attempt_id;
```

Supply the operation IDs from `aggregate.json`; sum attempt rows per operation
only when every row is terminal and reconciled and `data_gaps` is empty.
Prometheus exposes the fleet-level equivalents as
`fs2_serve_lifecycle_clock_gpu_seconds_total{clock="scheduler_occupied"}` and
`fs2_serve_lifecycle_gpu_seconds_total` (with `phase="active_compute"` for
active time and non-active phases for occupied-idle analysis). These metrics
are cumulative by tenant/model, so use pre/post deltas for one fleet run.

## Ten-model cold-start and cache benchmark

`run_coldstart_benchmark.py` composes, rather than replaces, the fleet runner.
It runs at least three complete fleet repetitions, joins each operation to the
authenticated scientific run-detail and lifecycle projections, and groups
statistics only when execution image, execution identity, placement pool,
capacity type, environment cache tier, and observed fast-start tier match.

The benchmark records queue and admission/capacity wait, image pull, artifact
localization, restore, compile/warmup, active compute, accepted-to-terminal
semantic response, and operation runtime. A dedicated runtime/model-load value
remains unavailable until the controller exposes that boundary; the script
does not relabel total cold-start or artifact-load time. The asynchronous API
returns one terminal validated result, so the customer clock is named
`time_to_first_semantic_result_seconds`, never TTFT.

Exact GPU accounting is joined from
`/admin/api/v1/telemetry/workloads?operation_id=...`. Scheduler-occupied,
device-allocated, active, occupied-idle, and phase GPU seconds retain the
ledger's quality and reconciliation state. If that endpoint or an exact clock
is unavailable, the receipt says so and preserves no inferred zero.

Use the reviewed environment qualification set from the existing cold-start
tooling to pin H100, driver/CUDA, storage, pool, capacity, and eligible cache
state. An environment cache tier is capability evidence; it is not proof that
the attempt used that mechanism. The per-run admin `fast_start` observation is
recorded separately. Likewise, this scientific-batch receipt does not assign a
customer L1-L4 level because it has no model-endpoint-ready boundary.

Run only after all ten routes are deployed and the admin model preflight lists
all ten with `readiness=qualified` as published batch profiles. The runner
checks that state before submitting the first fleet operation and fails closed
if any profile is still a candidate:

```bash
export FS2_INFERENCE_TOKEN='...'
export FS2_ADMIN_TOKEN='...'
python3 acceptance/scientific-fleet/run_coldstart_benchmark.py \
  --endpoint https://inference.example \
  --repository-root "$PWD" \
  --receipt-root /secure/fs2-scientific-coldstart \
  --run-id scientific-coldstart-20260904-01 \
  --environment-qualifications /reviewed/environment-qualifications.json \
  --project-id project-e00rene \
  --region eu-north1 \
  --cluster-context k8s-inference-h100 \
  --reserved-pool-id h100-reserved-8x \
  --repetitions 3 \
  --max-parallel 8
```

Bearer values and the short-lived operator cookie remain memory-only. The run
directory is mode `0700`; fleet and benchmark receipts are mode `0600`. The
runner makes no model-configuration or capacity mutation, revokes its admin
session, verifies the before/after scientific-model snapshot digest, and exits
nonzero if any model fails or the snapshot changes. Public scientific Jobs are
already terminal when a fleet repetition returns, so there is no model floor
or scaling override to restore.

## Secondary structure fleet inputs

The five secondary/academic structure lanes have model-owned public acceptance
inputs under `models/structure/batch-adapters/*/activation/public-acceptance.json`:

- `esmfold2`
- `esmfold2-fast`
- `protenix-v2`
- `openfold3-openbind` (directory `openfold3`)
- `alphafold3`

Each record declares one canonical scientific manifest and one small public
payload. The payload bytes are the exact bounded input consumed by the H100
qualification renderer. AlphaFold 3 uploads only its public fold input; model
parameters remain deployment-owned and are never present in these records.
All records remain route-closed and require a completed public platform run.

Validate paths, schemas, digests, qualification-input equivalence, and runner
loading without contacting a cluster:

```bash
python3 acceptance/scientific-fleet/validate_secondary_inputs.py
```

In the control-plane development environment, also compile every request and
verified manifest entry through the production adapter registry:

```bash
uv run --project components/control-plane \
  python acceptance/scientific-fleet/validate_secondary_inputs.py \
  --compile-adapters
```

Once a profile and route have been activated by the serialized integration
owner, pass its record directly to `run_acceptance.py`, for example:

```bash
python3 acceptance/scientific-fleet/run_acceptance.py \
  --endpoint https://inference.example \
  --activation-fragment models/structure/batch-adapters/esmfold2/activation/public-acceptance.json \
  --run-id esmfold2-public-qualification-01 \
  --receipt /secure/run/esmfold2-public-qualification-01.json
```

The records pin the stable upstream model revision. The terminal receipt still
records the full runtime image, recipe, artifact-manifest, and execution
identity returned by the active profile; no candidate route is opened by these
fixtures.
