# Scientific AI application layer on Serverless 2.0

An **isolated, pre-deployment implementation**, starting with GROMACS. Existing
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

There is no new scheduler, database, token issuer, operator or workflow engine.
The wheel includes the existing `fs2_gromacs` native worker from its original
source directory; it does not import or copy the old control plane. Its default
legacy checkpoint recipe and local/companion modes remain unchanged.

## Implemented

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Authenticated owner/attempt context, private scientific input, job-bound grant, immutable runtime map and manifests |
| `serverless.py` | Small submission/status/cancel client, opaque request references, capability check; the lifecycle port awaits the integrated S2 API |
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
The code supports this distinction, but RTX-to-L40S compatibility is **not yet
GPU-tested**. CUDA process snapshots are not the recovery mechanism here.

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

Ordinary tests use explicit storage/lifecycle doubles or AWS SDK stubs. They cover
wrong tenant/user/job grants, placement/image mismatches, stale attempts, denied
and corrupted transfers, restore identity checks, temporary credential renewal
twice, withdrawal, no ambient credentials, transfer-only retry and legacy defaults.
They do **not** prove cloud IAM enforcement or real multi-cluster failover.

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

- The seven capability keys and `execution` wire fields in `serverless.py` are
  **provisional**, aligned to S2-01..05. Supply the final discovery path explicitly;
  do not deploy the adapter against a server that ignores these fields.
- Implement `Lifecycle` against the S2 team's final authenticated current-attempt,
  checkpoint and finalization APIs. The only implementation here is a test double
  under `tests/`; there is deliberately no permissive production implementation.
- Connect protected binding resolution and the existing Scientific AI authorization
  and operation/API envelopes. No ephemeral in-memory grant store is introduced.
- Qualify exact optimized RTX/L40S image digests and real storage grants on the
  shared `s2pr2` fleet. Do not touch the current customer cluster or use customer keys.

Reference: [AWS SDK web-identity authentication](https://docs.aws.amazon.com/sdkref/latest/guide/access-assume-role-web.html).
