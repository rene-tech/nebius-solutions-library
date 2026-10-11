# Scientific AI → Serverless 2.0: implementation handoff

Updated 11 October 2026. **Application slice implemented locally; no customer
cutover or fleet deployment.** All existing platform features and routes remain
in service on the current implementation. This migration must not interrupt them.

## Agreed pilot

Start a **new GROMACS job using synthetic data**, rather than copying a customer
checkpoint. Prefer one RTX PRO 6000 Blackwell; fall back to L40S within the
operator's placement policy. Select the GPU-specific optimized image at actual
placement. Keep checkpoints/results in the approved customer bucket and scope
storage access to the request/attempt. Resume native checkpoints after abrupt
spot loss, including on another qualified GPU and cluster without the old PVC.
Use the shared `s2pr2` test fleet and existing internal QA identities only.

The S2 developer handoff is maintained locally at
`/home/tux/nebius-serverless-2/docs/dev-fleet/SCIENTIFIC-AI-EXECUTION-REQUIREMENTS-20261011.md`.
Its five requirements are agreed; integration is owned by the Serverless team.
The runtime source is the Solutions Library Serverless PR, not the historical
example GROMACS catalog in the Serverless notes repository.

## Implemented on the application side

[components/scientific-ai](../components/scientific-ai/README.md) is the new small
application package. It reuses the current GROMACS scientific worker and adds:

- private run manifests, job-bound grants, pinned runtime/image validation;
- opaque S2 submissions plus authoritative status/cancel passthrough;
- per-job AWS SDK web-identity refresh with no broad credential fallback;
- streaming customer-object transfers and verified manifest-last checkpoints;
- native recovery into a new workspace, with compatibility distinct from image identity;
- export-only recovery, including recovery after a lost finalization callback.

Only optional constructor/hooks were added to `fs2_gromacs.worker.Workflow`.
Existing CLI, checkpoint hashes, environment behavior and local/companion paths
keep their defaults. No customer image is rebuilt or deployed by source integration.

## Next steps after the S2 integration

| S2 requirement | Scientific AI integration action / acceptance |
| --- | --- |
| S2-01 identity/private bindings | Bind existing customer authorization to durable private manifests and provider-enforced job grants. Resolve S2's token only for the current attempt. Test real two-tenant denial, token renewal and the declared revocation bound. |
| S2-02 application I/O | Wire the protected worker inputs to `GromacsApplication`; prove the run/status/cancel/result paths need no platform bucket or tenant S3 key. |
| S2-03 durable attempt lifecycle | Implement the `Lifecycle` port against the integrated S2 API. Test restart, duplicate acknowledgment, partitioned stale worker and explicit cancellation. Do not build a second operation ledger/controller. |
| S2-04 placement/recovery | Qualify exact RTX and L40S builds, freeze one compatible scientific revision, then abruptly lose an RTX attempt and resume in an approved L40S cluster. Record actual GPU/image/cluster, steps lost/repeated/completed and ns/day. |
| S2-05 finalization/cleanup | Exercise denied/partial output writes and lost acknowledgments. Prove no false success or premature scratch deletion and transfer-only recovery on the approved storage profile. |

The last inspected fleet handover did not list RTX/L40S pools. Check live test
capacity before scheduling this gate; do not substitute H100 and call it passed.
Do not reprovision current Scientific AI customer nodes for migration testing.

Once the pilot passes, migrate the existing REST/MCP artifact/operation envelope,
batch fan-out, tenant access, usage/metrics, admin management and subsequent Apps
incrementally. Preserve each old path until its replacement passes the current
[customer release policy](../CUSTOMER_RELEASE_POLICY.md); customer-facing changes
are allowed where needed, but feature removal is not.

## Evidence and limitations

Component tests and the fresh real-engine local recovery test are recorded in
[the dated evidence](../acceptance/serverless2-gromacs-20261011/README.md).
These qualify local application logic and native CPU restart only. AWS STS/S3
wire behavior uses SDK stubs; lifecycle authority uses a test double. Live IAM,
GPU image compatibility/performance, multi-cluster recovery, request-lifetime
credential withdrawal, public API/MCP/client paths and PHI suitability remain
unqualified. No production key, bucket, cluster, DNS or customer job was changed.
