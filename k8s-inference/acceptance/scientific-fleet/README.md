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
