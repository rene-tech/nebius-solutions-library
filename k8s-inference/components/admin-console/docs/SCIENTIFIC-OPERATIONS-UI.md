# Scientific operations admin surface

The read-only `/admin/scientific-runs` and
`/admin/scientific-runs/:runId` routes isolate scientific workload concerns
from the existing generic inference-operation and model pages. They consume a
typed, same-origin BFF projection and do not read Kubernetes, PostgreSQL,
object storage, Prometheus, or licensed assets from the browser.

## Integration state

The production control-plane constructor instantiates the scientific admin
service and always registers its authenticated capability route. Each data
route is registered only when its real reader is bound. The browser first reads
the capability document, never selects a data source, and has no direct
database or object-store access.

Each source is represented independently. The current production bindings are:

| Source | State | Backing |
| --- | --- | --- |
| `scientific-catalog` | bound | global candidate evidence plus authorization-filtered, tenant-submittable profiles |
| `scientific-controller` | bound | durable PostgreSQL scientific batch and operation readers |
| `scientific-artifacts` | conditional | canonical artifact result service when object storage is configured |

An unbound source is marked unavailable by
`GET /admin/api/v1/scientific-capabilities`; its data route and navigation are
absent. A global model query projects delivered candidate evidence without
upgrading candidate-only documents into qualification evidence. A tenant-bound
query instead uses the controller's P0-E discovery path and returns only exact
profiles that pass that tenant's access, runtime, artifact, and scheduler
eligibility gates. A malformed or mismatched global candidate is isolated as
`unknown` with a bounded projection issue and cannot take down healthy rows.
The accepted durable controller adapter makes run routes available; a bound
artifact reader enriches run detail. Neither path uses fixtures or placeholder
rows.

`contracts/scientific-admin-fixture-v1.json` and the TypeScript fixtures are
restricted to component tests and explicit local Vite `fixture` mode. A
production build contains no fixture middleware or file-fixture fallback.
Independent run, artifact, and model source states remain visible: a failed or
stale source produces a bounded error or partial-data notice without hiding
healthy sections or manufacturing rows.

All demo fixture run IDs, timestamps, revisions, image digests, measurements,
and readiness states are synthetic UI examples. They are not deployment,
source qualification, access, or runtime evidence.

The authenticated BFF surface is capability-gated:

- `GET /admin/api/v1/scientific-capabilities` is present with the scientific
  admin service;
- `GET /admin/api/v1/scientific-models` is present only with the catalog
  reader; tenant operators are fixed to their tenant's authorized discovery
  view, while global operators may request a tenant view or the global
  candidate-evidence view;
- `GET /admin/api/v1/scientific-runs` and
  `GET /admin/api/v1/scientific-runs/{run_id}` are present only with the
  durable run reader and retain tenant-scoped authorization.

There is no cancellation mutation in this slice. The detail page displays the
immutable cancellation request/acknowledgement state and whether cancellation
would currently be permitted. An authorized command can be added only when
the scientific operation API publishes one.

## Truthfulness boundaries

Run and model presentations keep these facts independent:

- requested and policy-effective service class;
- durable queue identity, priority, and observed admission state;
- each attempt's actual Kueue admission time, resolved pool, ResourceFlavor,
  accelerator resource, and bounded pod/node/GPU counts when persisted;
- model, backend, source, image, and execution identity;
- standard versus academic access, non-secret receipt state, and the exact
  admission gate;
- fast-start tier and whether it was observed, declared, or unavailable;
- DAG stage state and every attempt, including retryable preemption;
- immutable input, checkpoint, output, and validation artifacts;
- lifecycle phase durations and GPU allocated, active, idle-by-cause,
  grace/drain, and reconciliation values;
- trace, log, and metrics launch availability.

Every GPU measurement has an explicit `measured`, `estimated`, or
`unavailable` evidence state and a source. The UI prints that state beside the
value. It does not infer idle time from an incomplete allocation boundary, and
an estimated allocation cannot receive a zero reconciliation delta.

`gpu-memory-snapshot-restore` is one exact fast-start tier, not a synonym for a
local image, local artifacts, runtime checkpoint, or warm replica. The fixture
does not claim GPU snapshot restore.

Native AlphaFold3 and native BindCraft/PyRosetta remain separate academic
backends. Their generated deployment-bound contract reports use `Granted`,
execution `Authorized`, and `request_time_license_receipt_required=false`.
Formal licence acceptance is displayed as an independent advisory state, not
an admission gate. The console never exposes credentials and identifies
OpenFold3 or FreeBindCraft only as explicit alternatives—not as the native
backend. Runtime/semantic qualification remains a separate readiness fact, so
authorized access does not make a candidate runtime appear qualified.

## Local verification

```bash
cd components/admin-console
npm ci
npm run typecheck
npm run test:run
npm run build
npm run build -- --mode fixture
```

The fixture build is for local browser acceptance only and must not be used as
a deployment image.
