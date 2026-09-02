# Scientific operations admin surface

The read-only `/admin/scientific-runs` and
`/admin/scientific-runs/:runId` routes isolate scientific workload concerns
from the existing generic inference-operation and model pages. They consume a
typed, same-origin BFF projection and do not read Kubernetes, PostgreSQL,
object storage, Prometheus, or licensed assets from the browser.

## Integration state

The backend integration is deliberately not part of this change. The
provisional read contract is recorded in
`contracts/scientific-admin-fixture-v1.json`; TypeScript fixtures implement it
for component tests and local Vite `fixture` mode. A production build contains
no fixture middleware. Until the control plane implements the three read
routes, the page uses the existing fail-closed `DataBoundary` and reports the
missing view without manufacturing data.

All fixture run IDs, timestamps, revisions, image digests, measurements, and
readiness states are synthetic UI examples. They are not deployment, source
qualification, access, or runtime evidence.

The future BFF routes are:

- `GET /admin/api/v1/scientific-runs`
- `GET /admin/api/v1/scientific-runs/{run_id}`
- `GET /admin/api/v1/scientific-models`

There is no cancellation mutation in this slice. The detail page displays the
immutable cancellation request/acknowledgement state and whether cancellation
would currently be permitted. An authorized command can be added only when
the scientific operation API publishes one.

## Truthfulness boundaries

Run and model presentations keep these facts independent:

- requested and policy-effective service class;
- durable queue identity, priority, and observed admission state;
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
backends. Each requires a verified receipt before admission, never exposes the
credential, and identifies OpenFold3 or the open binder workflow only as an
explicit alternative—not as the native backend.

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
