# Optional task-only admission injector

Nothing in these scripts deploys this injector. The release owner must review
the generated manifests and explicitly coordinate its installation and removal.
The guarded suspended-Job patch remains the first-trial option in README.md.
Do not enable both injection modes for one cohort.

The webhook matches only CREATE of `batch/v1` Jobs in `fs2-models`, exact tenant
`admitted-pool-recovery-20260925`, model `gromacs`, stage `workflow`, and one
configured shard (default `window-01`). It additionally validates the observed
controller/runtime service-account usernames, deterministic operation/workload/
attempt/Job identities, matching Pod-template ownership, suspension, GPU count,
unchanged runtime digest and original frozen annotations. All mismatches are
allowed without mutation. There is no API client, token, RBAC or outbound access.

On attempt 1 it appends `pool-id In [h100-1x]` to every required-affinity OR
branch. All original constraints and the qualified pool-preference annotation
remain unchanged. The controller alone records failure, deletes the old Job/
Pod/reservation and creates the next immutable attempt. Kueue considers required
affinity when selecting a ResourceFlavor; the acceptance runner still requires
actual dead-flavor admission, not just the injected constraint. See the
[Kueue flavor contract](https://kueue.sigs.k8s.io/docs/concepts/resource_flavor/).

The configuration is CREATE-only, fail-open (`failurePolicy: Ignore`, 2-second
timeout), reinvocation-idempotent, and stops injecting four hours after rendering.
It uses a 24-hour local CA/leaf pair with Service DNS SANs and a private ClusterIP
Service. These choices follow the
[Kubernetes admission contract](https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/).
Failure/open expiry means a cohort may run normally and fail its recovery gate;
it never means admission of unrelated customer work is denied.

The dedicated task namespace contains one CPU-only, non-root server with a
read-only filesystem, dropped capabilities, no token, immutable source/config
and TLS mounts, health probes, bounded resources and denied egress. Ingress is
limited to TLS port 8443; pass known API-server source CIDRs to narrow it further.
Do not guess those CIDRs. The default permits callers reaching the ClusterIP;
the server performs no external action regardless of caller-supplied objects.
No public Ingress, LoadBalancer, NodePort, RBAC or GPU resources are created.

## Render and inspect, without cluster writes

Run from this acceptance directory after the owner has supplied `release.json`:

```sh
python3 prepare_injector.py \
  --runtime-image "$(jq -r .runtime_image /home/tux/secure-handoff/admitted-pool-recovery-20260925/release.json)" \
  --pause-seconds 150 \
  --output /home/tux/secure-handoff/admitted-pool-recovery-20260925/injector-01
```

The server uses the gateway Dockerfile's trusted pinned public Python base,
`docker.io/library/python:3.13.15-alpine3.23@sha256:a3180613a9708f1cd59aa79a3dd82e8a6d3f3199d1d6e2c467a63687518872d3`,
and mounts only the reviewed stdlib server source. It does not build another image
or require private-registry credentials. Its default command is `/usr/local/bin/python`.
The generator writes mode-0600 files in a new mode-0700 directory outside the
worktree; `tls-secret-private.json`, `*-private.key` and the directory must never
be committed or attached as public evidence. `plan.json` contains no credentials.
Preserve source/config/manifest hashes in the evidence; do not print Secret data.

Review `namespace.json`, `server.json`, `webhook.json` and `plan.json`. Verify the
cluster/context explicitly and ensure both the namespace and named webhook are
absent before installing; do not adopt or overwrite existing resources. If a
new namespace cannot pull the exact public image, stop for the release owner's
approved route rather than copying credentials or changing the image silently.

After separate deployment authorization, the owner's ordering is:

1. Create only the exact generated task namespace.
2. Create its generated TLS Secret and server resources.
3. Wait for its Deployment to be ready; verify the Service endpoints and review
   the live selectors, image digest, source/config and no-token security context.
4. Install the exact named `webhook.json` last. It is cluster-scoped, but its
   selectors and CREATE rule must remain exactly as generated.
5. Run the existing ordinary-client command from README.md, replacing
   `--inject-dead-first-attempt` with
   `--admission-injector-plan /home/tux/secure-handoff/admitted-pool-recovery-20260925/injector-01/plan.json`.

The runner reads and verifies every non-secret injector resource before and after
each cohort. Source/config, image and resource identity must remain unchanged.
Two clean recovery cohorts must use the same injection plan as well as the same
release, ordinary key, fixture and client. A missing/expired/unavailable injector
cannot be passed as recovery evidence.

## Optional retry initialization pause

`--pause-seconds 150` adds `/bin/sleep 150` as an ordinary, non-restartable init
container only on the selected shard's attempt 2, using the unchanged GROMACS
runtime digest. The main command, env, mounts, resources and qualified pool list
are untouched. Its CPU/memory requests and limits are below the existing
`prepare-workspace` init; tests using the real controller envelope calculator
prove the effective reservation and digest are unchanged. The pinned GROMACS
image's `/bin/sleep` was also exercised locally as UID 1000 without network/GPU.

The runner requires observed scheduled Pod/container identity, the exact image
and sleep command, a successful init lasting at least 150 seconds and successful
completion of that same retry without attempt 3. The resulting receipt explicitly
sets `actual_scale_from_zero_proven=false`. This is an injected initialization
test, not evidence that a real node group scaled from zero.

## Separate synthetic eligibility-loss/return scenario

Instead of `--pause-seconds`, render a separate injector plan with
`--synthetic-no-eligible-capacity`. Do not change an injector between the two
clean recovery cohorts. Remove it first, then review/install the separate plan
for this additional scenario, using the same exact platform release.

This mode appends an impossible pool predicate to every required-affinity branch
of the selected shard's retry (attempt 2), retaining its qualified healthy-pool
predicate and original pool-preference annotation. Their conjunction matches
no ResourceFlavor and cannot match a node, even if someone adds the synthetic
label later. No reservation, quota, node or other operation is modified.

Use the normal runner with both `--admission-injector-plan PATH/plan.json` and
`--scenario synthetic-no-eligible-capacity`. It requires actual Kueue
`QuotaReserved=False` with a raw affinity/no-fit message, no admission, the same
Job/Workload/attempt identity and no retry churn continuously for at least 120
seconds. Only then may it remove the exact added predicate by atomic JSON patch,
guarded by UID, resourceVersion, name, namespace and suspension, and a fresh check
for no owned Pod/reservation/admission. Remove the predicate from both the Job
and its existing unreserved Workload's copied Pod template. The pinned Kueue
v0.17.8 [PodSet equivalence check](https://github.com/kubernetes-sigs/kueue/blob/v0.17.8/pkg/util/equality/podset.go)
does not compare affinity, so changing only the Job does not update that copy.
The Workload patch requires the same exact owner/UID, resourceVersion and status,
unchanged scientific Pod spec, no reservation/admission, and a successful server
dry-run. It removes only the injected expression; it never deletes the Workload
or mutates status. This is the explicitly approved task-only eligibility-return
injection, not controller recovery or physical capacity return.

Natural Kueue admission and the unchanged native workload must then succeed.
The receipt retains the original reasons, observed wait, exact removal patch
and `physical_capacity_exhaustion_claimed=false`. It does not satisfy or rename
the separate physical `no-spare` scenario. A patch race fails closed and the
runner cancels only its own accepted operation through public MCP.

### Disjoint parallel additional cases

After the two primary recovery cohorts, the owner may render two separately
named, exact-task instances with `--instance init --shard window-03
--pause-seconds 150` and `--instance synthetic --shard window-01
--synthetic-no-eligible-capacity`. The renderer creates separate names and
namespaces ending in `-init` and `-synthetic`, with their own short-lived TLS.
The server source, exact tenant/model/stage selectors and mutation guards are
unchanged. Install each new instance only after checking its exact namespace
and webhook are absent, using the same server-first/webhook-last sequence.

Run the initialization case with unchanged fixture windows `window-03 window-04`
and the synthetic case with `window-01 window-02`. `--concurrent-cohort PATH`
permits this pair only when their saved ordinary identity, platform release and
client agree, their window sets and injected shards are disjoint, and the first
case's recorded injector UIDs/configuration remain unchanged and ready. Both
instances retain exact task selectors; neither matches the other case's shards.
This does not relax the two primary cohorts' sequential unchanged-plan gate.

Cleanup each instance's exact name and namespace from its verified `plan.json`,
again webhook first. Do not remove the other case's instance while it is running.
These additional tests must not overlap a platform/seed rollout; recapture the
release after the owner announces the final revision.

## Cleanup and verification

The owner removes the webhook **first**, before stopping its server. Resolve the
exact name `admitted-pool-recovery-injector-20260925`, UID and task ownership label
with a read-only GET; compare with the runner's recorded injector resources. If
ownership/UID differs, stop. Delete only that named MutatingWebhookConfiguration
(never a selector, wildcard, or all-webhooks command), and confirm it is absent.

Next cancel any unfinished acceptance operation by its saved public operation ID
and poll for terminal status plus no owned Jobs/Pods/Workloads. The harness does
this automatically on its own failure; it does not remove the injector. Preserve
all failed receipts. Do not force-delete Jobs or reservations to make a gate pass.

Delete only the generated named server resources and TLS Secret in namespace
`admitted-pool-recovery-20260925`, after matching their UIDs/ownership. Inspect the
namespace for unexpected objects before deleting that exact task namespace; if
anything is not task-owned, stop. Confirm the webhook, Deployment/Pod, Service,
ConfigMap, Secret, ServiceAccount, NetworkPolicy and namespace are absent. These
ephemeral objects can be recreated from the private generated manifests; removal
does not delete the scientific tenant's native artifacts in `fs2-models`/storage.

Finally revoke only the task key/disable its owner with `identity.py retire` once
the lifecycle cleanup guard passes. Retain private local receipts/artifacts; TLS
expires after 24 hours. Report exactly what was removed and what was retained.
No physical node, queue/quota, model setting, customer operation or sibling key
is part of this cleanup.
