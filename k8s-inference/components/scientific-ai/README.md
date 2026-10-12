# Scientific AI application layer on Serverless 2.0

An **isolated migration pilot**, starting with GROMACS. Existing
Scientific AI routes, customer workloads, tenants, buckets, MCP tools and the
admin UI are unchanged. This is not a replacement API rollout or a claim of
customer/PHI readiness.

## Keep the ownership small

- **Scientific AI:** customer authorization, private workflow/storage manifests,
  scientific validation, native checkpoints, customer-object I/O and model-specific
  runtime compatibility.
- **Serverless:** admission, scheduling, current attempts, retries, cancellation,
  placement-time image choice, durable status/finalization and runtime isolation.
- **Provider SDK/IAM:** temporary credentials, renewal and actual storage permissions.

There is no new scheduler, user database, cloud credential issuer, operator or workflow engine.
The optional online object signer issues job-scoped application capabilities;
it does not issue customer API keys or cloud credentials.
The wheel includes the existing `fs2_gromacs` native worker from its original
source directory; it does not import or copy the old control plane. Its default
legacy checkpoint recipe and local/companion modes remain unchanged.

## Implemented

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Authenticated owner/attempt context, private scientific input, job-bound grant, immutable runtime map and manifests |
| `serverless.py` | S2 fleet discovery, private binding and idempotent execution/continuation transport; new fleet uses API 0.11.31 |
| `lifecycle.py` / `runner.py` | Authenticated uploader file bridge, epoch-fenced pointer acknowledgement and one native execution |
| `gateway.py` / `mcp_server.py` | REST and typed MCP compatibility, S2 key authentication, same application methods for both clients |
| `gateway_store.py` | Owner-scoped immutable artifacts and UUID compatibility references in customer object storage; no scheduling state |
| `broker.py` / `workload_identity.py` | Job capability plus signed, audience- and pod-bound S2 identity for direct S3 transfers, including multipart; no bucket credentials in GPU pods |
| `identity.py` | Per-job AWS SDK web-identity provider with renewal; no environment/static-key/metadata-service credential fallback |
| `storage.py` | Scoped S3 transfer, streaming SHA-256 validation, incremental closed-file objects and manifest-last publication |
| `gromacs.py` | Existing native GROMACS execution with durable application-managed checkpoints, fresh-workspace restore and transfer-only recovery |

The default application placement request is **RTX PRO 6000 Blackwell, then
L40S**, restricted to operator-approved clusters. Serverless makes the placement
decision. Each permitted GPU needs its own digest-pinned image in the immutable
runtime profile; the application verifies the *actual* attempt's image and GPU.
The test fixture's placeholder images are not deployment artifacts.

Use one compatibility ID only for builds explicitly qualified for native
checkpoint interchange: same GROMACS version, precision, enabled extensions and
scientific contract. Images may differ in GPU kernels. Record the actual image
on every attempt; do not substitute a compatibility ID for image provenance.
The exact 2026.4 SM120/SM89 pilot builds passed native RTX-to-L40S recovery with
a fresh scratch disk on another cluster; see the [live evidence](../../acceptance/serverless2-gromacs-20261011/LIVE-PILOT.md).
This does not qualify arbitrary versions/extensions. CUDA process snapshots are
not the recovery mechanism here.

Automatic cross-class recovery is a separate, operator opt-in on
`RuntimeProfile.recovery`: `on_infrastructure_loss="recover"`,
`max_recoveries=1`, `wait_s=300`, and optionally `deadline_s` (60–604800).
Unconfigured profiles keep legacy `retry`. The adapter requires Serverless to
advertise bounded recovery before submission; an older fleet returns a structured
503 rather than silently accepting unsupported recovery. Serverless owns loss
detection, fencing, replacement placement, root deadlines and cancellation.
Public Scientific AI operation handles and storage grants follow its successor.
The new-fleet deployment and live qualification are tracked in
[NEW-FLEET-RESULTS.md](../../acceptance/serverless2-gromacs-20261011/NEW-FLEET-RESULTS.md).
Earlier shared-fleet evidence remains historical; it does not qualify a later
image or deployment.

## Binding a request

1. Authenticate with the existing Scientific AI customer identity and model grant.
   Neither `Owner` nor `StorageGrant` is a public, caller-trusted authorization DTO.
2. Resolve a job-scoped customer storage grant and approved runtime revision.
   Provider IAM must enforce its bucket/prefix/actions; Python prefix checks alone
   do not constrain stolen credentials. Do not reuse a tenant-wide storage client.
3. Store the `GromacsRun` and provider configuration through the agreed protected
   binding mechanism. Submit only opaque references with `invoke_payload()`.
   Native filenames, clinical content, URLs, role details and credentials stay
   out of normal Serverless input/Job metadata.
4. At attempt start, resolve S2's authenticated owner/epoch/placement and projected
   token. `web_identity_session()` creates an isolated SDK credential provider;
   the mounted token is re-read on renewal without changing process-wide environment
   variables. `S3Objects` also accepts another **explicitly authorized**, scoped
   provider client where AWS federation is inapplicable. No automatic fallback exists.
5. Instantiate `GromacsApplication` with the approved private scratch path and
   GROMACS executable, the scoped `Artifacts`, and an authenticated `Lifecycle`.
   The process entrypoint must forward SIGTERM/SIGINT to `app.worker.stop`.
   Run `execute()` for new work or `execute(committed_checkpoint_ref)` for recovery.
6. Keep required files until the authenticated finalization acknowledgement.
   `DeliveryPending` means **not delivered**, even when native computation exited
   successfully. `finish_delivery()` retries export from retained approved scratch
   without recomputing. A committed result manifest also supports fresh-workspace
   recovery without calling the engine again.

Transient object/DNS failures receive bounded retries. A transport failure in
the final checkpoint is a delivery problem only when all scientific commands
and output checks have completed. Retain that exact successful native result
for CPU-only delivery; never claim its unacknowledged checkpoint was committed.
Failed/incomplete science, revoked access and corrupt output are not promoted.

The current worker preserves the existing typed native workflow (`jobs`, ordered
commands, explicit arguments/selections, bundles, file budgets and fourteen-day
execution budget). One application object executes one shard. Batch fan-out,
legacy artifact/API envelopes, existing tenant access and MCP/admin bindings are
not cut over by this slice; they stay on the current implementation until their
own acceptance paths pass. No scientific parameters are silently retuned.

## Objects and failure semantics

Within a **job-authorized** bucket/prefix, store immutable content as
`blobs/<sha256>` and `manifests/<sha256>`. A manifest contains file paths, lengths,
digests, native state, original input identity and attempt/runtime provenance.
It is private customer data, not shared telemetry. Files are uploaded/verified
before the manifest; the current S2 epoch must then atomically publish its pointer.
An upload from a stale worker may leave unreferenced objects, but cannot promote
the authoritative checkpoint. No application-local `latest.json` is used.

Closed trajectory segments are reused without reuploading unchanged bytes.
Verification currently performs a full object read-back, not a multipart ETag
comparison or trust in user metadata. That deliberately conservative path needs
measurement with large trajectories; provider-verified full-object checksums may
replace it after qualification. No customer data is copied to platform storage or
a shared model cache. Retention/garbage collection is not guessed by this library.

Epoch fencing is **not immediate cloud credential revocation**. A current grant's
remaining provider TTL may outlive pod removal; exact request-lifetime denial
requires provider enforcement or an online authorizer. No protected patient data
should use this pilot: BAA/service-scope review and the approved transient data
path (including scratch, spill, logs and snapshots) remain separate prerequisites.

## Tests and packaging

```sh
uv sync --frozen --group dev
uv run ruff check src tests
uv run pytest -q
uv run pytest -q ../../models/molecular-dynamics/gromacs/tests/test_runtime.py \
  ../../models/molecular-dynamics/gromacs/tests/test_inventory_cache.py \
  ../../models/molecular-dynamics/gromacs/tests/test_file_expansion.py
uv build --wheel
```

Build the wheel **before** building either container: the Dockerfiles install
`dist/*.whl`, not the live source tree. Check the installed wheel inside the
candidate image before deployment. Use `uv build --wheel` from this checkout;
the source-distribution build currently cannot resolve the reused GROMACS worker
outside this component directory. Do not publish that sdist as a standalone package.

Ordinary tests use explicit storage/lifecycle doubles or AWS SDK stubs. They cover
wrong tenant/user/job grants, placement/image mismatches, stale attempts, denied
and corrupted transfers, restore identity checks, temporary credential renewal
twice, withdrawal, no ambient credentials, transfer-only retry and legacy defaults.
These local tests do **not** prove cloud IAM enforcement or real multi-cluster
failover; the separate dated report records the live explicit recovery drill.

For the additional real-engine test, pre-pull an operator-approved image with
GROMACS 2026.4 at `/opt/gromacs/bin/gmx`, then:

```sh
SAI_GMX_DOCKER_IMAGE='<local-registry-reference>@sha256:<digest>' \
  uv run pytest -q -s tests/test_native_recovery.py
```

This runs a **fresh, synthetic 216-argon-particle CPU simulation**. It interrupts
after a durably published native checkpoint, hides the first workspace, restores
into another workspace, and completes 600,000 steps without repeating `grompp`.
No GPU, customer data, cloud credentials or external network is used by the engine.
Inputs are generated by the checked-in fixture with seed 1741; raw inputs,
checkpoints, trajectories, native logs and `native-acceptance.json` are retained
under pytest's test directory. Use a fresh `--basetemp` under a retained evidence
directory if those artifacts must survive normal `/tmp` cleanup.

## Remaining integration gates

See [the migration handoff](../../docs/SERVERLESS2-MIGRATION.md). In particular:

- The wire adapter now uses the delivered `GET /v1/fleet` execution shape, named
  profiles/identities, confidential bindings and portable resume request. An
  absent AWS trust setup can use an explicitly authorized confidential binding;
  it must not try the test fleet's placeholder role or ambient credentials.
  Discovery proves configuration availability, not execution correctness. The
  [upstream integration probes](../../docs/S2-INTEGRATION-GAPS-20261011.md)
  now pass finalization, resume and fallback contracts against 0.11.11.
- `FileLifecycle` was exercised against the actual 0.1.14 uploader. It waits for fenced
  durable pointer acknowledgement, not file existence. Result publication is also
  a recoverable checkpoint; S2 alone decides terminal success after main exits.
  Lost-ack drills and their exact runtime scope are recorded in the new-fleet
  report. Denied final-output outage qualification remains separate.
- The pilot authenticates existing **S2** internal keys and preserves UUID-shaped
  Scientific AI handles, artifact upload, submit, status, result, cancel and
  native-resume routes. This does not migrate existing production API keys/users.
- Exact optimized RTX/L40S images and the internal scoped storage path passed the
  shared-fleet drill. The signer additionally requires S2's projected pod JWT;
  see the follow-up receipt for that release's live evidence. External customer
  IAM remains unqualified. Do not touch the customer cluster or use customer keys.

Reference: [AWS SDK web-identity authentication](https://docs.aws.amazon.com/sdkref/latest/guide/access-assume-role-web.html).

## Pilot deployment and explicit limits

`deploy/` is an isolated Terraform application deployment into the s2pr2 control
cluster. It creates only its own Namespace, configuration Secret, Deployment,
Service and `/scientific-ai` HTTPRoute. It does not change GPU pools, shared
Serverless releases or customer routes. State and the 0600 configuration file
belong outside Git. `acceptance/serverless2-gromacs-20261011/pilot.py` prepares
internal bindings, registers the GPU-image map and drives fresh synthetic runs.

The online signer holds the configured storage credential in the trusted API,
not in GPU pods, then authorizes the active S2 attempt and one operation prefix
before issuing a 60-second object URL. It supports direct multipart transfer.
Previously issued URLs can remain valid for at most 60 seconds after termination;
this is not instantaneous provider revocation. No signing request bodies,
credentials or URLs are written to shared logs. The federation module remains
an alternative once a real external AWS trust setup is supplied and tested.

The broker requires **both** the private logical-job capability and the worker's
projected JWT in `X-Scientific-Workload-Token`. S2's existing workload-identity
mechanism mints/rotates it; Scientific AI does not issue a second pod credential.
The worker re-reads the projected file for every signing request, so rotation
does not require restarting a long simulation.

Configure a dedicated tenant identity, for example:

```hcl
identities = {
  scientific-ai-storage = {
    audience = "https://<scientific-ai-origin>/scientific-ai/internal/storage"
  }
}
```

The trusted API config pins that audience, each permitted region's OIDC issuer
and JWKS URL, and the tenant's namespace/identity reference. No discovery URL is
taken from an untrusted JWT. RS256 signature, expiry/not-before, issuer, audience,
subject, namespace, service account and signed pod name are verified against
S2's online current-attempt state. A previous pod cannot become its replacement
by supplying the replacement's name. Missing credentials fail closed; key-fetch
outages return retryable 503, not an authorization bypass.

This is [Kubernetes projected-token validation](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/),
using PyJWT, not hardware attestation. Offline JWT verification does not itself
re-check Pod/ServiceAccount deletion against Kubernetes; S2's current-attempt
state is the online withdrawal boundary, with up to 60 seconds of residual URL
validity. TokenReview-based immediate object revocation, compromised-node/process
isolation and external IAM are not claimed qualified by this change.

Resume accepts only `gpu_class` (one of the original runtime's qualified choices).
Legacy `job_id`, `performance_mode` and `max_wall_seconds` are rejected with 422
and field names before admission, rather than silently ignored. Their actual
implementation remains a migration gate; the production platform retains them.

The first pilot is **one native job per operation, one GPU**. Multi-job fan-out,
external-MPI, legacy admin integration, production identity migration, events,
changing continuation budgets and full extension/library qualification remain
on the existing production implementation. They are not removed or claimed
implemented in the replacement. It is not customer-ready or PHI-approved.
