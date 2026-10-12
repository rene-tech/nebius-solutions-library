# Scientific AI 2 migration fleet

Owner: Rene. Task Deck: `fs2-serverless2-isolated-fleet-r20261012`.
This is a **new, independent deployment**, not a change to the customer fleet.
Infrastructure provisioning started on 12 October 2026; see `deployment.json`
for the latest verified state. Provisioning does not qualify customer migration.
Live test findings, exact failed-job cleanup and remaining gates are recorded in
[QUALIFICATION.md](QUALIFICATION.md).

## Source and configuration

- Serverless source: `rene-tech/nebius-solutions-library`, branch
  `serverless-inference-solution`, commit
  `184e24f80de3a5d489c0a17ee0e7fa4c109de789`.
- Isolated execution worktree:
  `/home/tux/worktrees/scientific-ai-s2-fleet-20261012/k8s-inference`.
- Fleet input: [terraform.tfvars.example](terraform.tfvars.example). This is the
  actual non-secret deployment configuration, passed through `TFVARS`; it is not
  a second implementation of Serverless. Model definitions remain runtime API
  state, not infrastructure settings.
- Provider locks are retained alongside this file. The tested upstream operator
  checkout used Nebius `0.6.67`; a clean unpinned init selected `0.6.68`, so the
  existing operator locks were restored **before planning/applying**. No provider
  upgrade was included.
- Terraform CLI: `1.16.5`; Nebius CLI: `0.12.287`, profile `sandbox2`.

Keep the configuration on `scientific-ai-serverless2-migration` until the owner
authorizes integration. The Serverless branch/PR and shared development checkout
remain owned by the Serverless team. Do not run fleet commands in that checkout.
The existing absolute repository-local Git guards were inspected and preserved;
the helper refused to replace their existing `core.hooksPath`.

## Topology and bounds

| Cluster | Project | Region | Purpose |
| --- | --- | --- | --- |
| `scientific-ai2-control` | `project-e00rene` | `eu-north1` | API, console, database consumers, fleet admission |
| `scientific-ai2-north` | `project-e00rene` | `eu-north1` | H100, H200 and L40S workers |
| `scientific-ai2-south` | `project-e07hsk0rma00kghvpcrxy2` | `eu-south1` | RTX PRO 6000 Blackwell workers |

Rene explicitly approved regular CPU system nodes and managed services on
12 October. Each cluster has two regular `cpu-d3/8vcpu-32gb` system nodes.
All six GPU pools use spot follow-price, prohibit reservation consumption and
start at zero. North ceilings reproduce the existing 38-GPU hardware mix:
eight 1x H100 nodes, two 8x H100 nodes, eight 1x Intel L40S nodes, one 4x AMD
L40S node and two 1x H200 nodes. South initially allows four 1x RTX nodes.
These are **configured ceilings, not reserved or measured available capacity**.

The H100 full-node pool owns a new GPU cluster on `fabric-2`, independently of
the existing customer's GPU cluster on that fabric. None of these GPU presets
is configured with local NVMe. Shared model-cache filesystems are 2 TiB north
and 512 GiB south. GDS is disabled pending kernel qualification. The managed
PostgreSQL instance initially has one host; this is not a database-HA claim.

The only bootstrap tenant is `system`, with `development` (admin) and `qa`
keys. `storage = "external"` creates no customer bucket/S3 key. Tests must use
application-managed I/O and the existing internal workspace where necessary.
Do not run the stock managed-storage `hello-run` verifier unchanged against
this tenant. Customer identities, keys, data and history are not imported here.

## State and safe execution

The new versioned state bucket is `scientific-ai2-tfstate`. Its service account
has object-editor access on that bucket only. The credentials file is
`/home/tux/.config/scientific-ai2/tfstate.env`, mode 0600. Never print or commit it.
Cloud state is `cloud/fleet.tfstate`; platform states are
`platform/north.tfstate`, `platform/south.tfstate`, `platform/control.tfstate`.
The new cloud workspace was `default` with **no state file** before the first
plan. The reviewed initial plan contained 59 creates, zero changes/deletions,
zero existing-cluster node-group targets and zero customer-storage resources.

From the isolated Serverless solution directory:

```sh
export TFVARS=/home/tux/worktrees/scientific-ai-s2-gromacs-20261011/k8s-inference/deployments/scientific-ai2/terraform.tfvars.example
./stack.sh clusters
./stack.sh preflight
./stack.sh plan cloud -out=/private/path/cloud.tfplan
# Review the saved plan's exact resource names/actions, then:
./stack.sh tf cloud -- apply /private/path/cloud.tfplan
```

Restore the supplied cloud/platform locks into their respective roots before a
new init. Use the existing `stack/bootstrap/state-bucket.sh` only for this fleet;
it has already run. Do not issue another state key without a reason. Saved plans
and raw state contain credentials and belong in private, mode-0700 storage.

After cloud resources are ready, mirror the pinned five component images from
`cr.eu-north1.nebius.cloud/e00dh8c3thpm0ptfea` to the fleet's **own** registry using
`tools/images.sh copy`, then compare source/destination digests. Per-cluster image
caches remain enabled for cross-project pulls into south. Pass `NGC_API_KEY`
through the environment when applying the platform; never copy unrelated pilot
keys or put credentials in the input file.

Plan/apply the platform for `north` and `south` first (independent states, can
run concurrently), then `control`. Record any source patch required by this
fresh deployment and coordinate it with the Serverless source owner; do not
quietly patch a live shared fleet.

## Networking and security boundaries

Existing subnets are reused; new public gateway allocations belong only to this
fleet. No old allocation, load balancer, DNS record, HTTPRoute or listener is
changed. `forge.nebius.cloud` remains intentionally unavailable.

The initial cloud apply created south and the supporting services, but north
and control failed because the reused VPC could not allocate the default `/16`
Service ranges. The Serverless owner added optional per-cluster `service_cidrs`;
existing fleets still plan with no changes. North/control now request `["/20"]`,
letting VPC choose free 4,096-address blocks. South keeps its original default.
The repair plan contains only 11 remaining creates, no updates or replacements.
Do not change Service ranges on an existing cluster: they are immutable.

The control Kubernetes API starts with the operator egress allow-list. Workers
initially allow authenticated public API access so that cross-region control
traffic can bootstrap. After actual requests exist, derive control-plane source
addresses from the workers' API audit logs and restrict one worker at a time,
verifying operator access, MultiKueue and job submission after each change.
Do not infer API source addresses from an external IP-checking website. See
Serverless `docs/OPERATIONS.md`, “API endpoint allow-list”. This bootstrap state
is not approval for protected patient data or customer cutover.

The first direct north-worker API check succeeded at 17:54 UTC. Its audit event
reports operator `serviceaccount-i00pafr0ydvbaxj952` at **195.242.18.145** (not the
46.243.145.28 address initially selected from prior operator notes). The control
allow-list is corrected in a separate in-place cloud update after creation;
no Kubernetes API authorization is bypassed. Recheck this address if the
operator runner or NAT routing changes.

The update completed at 18:01 UTC and three consecutive control-cluster node
queries passed. All three clusters have two Ready CPU nodes; all six GPU pools
were created at zero. Both worker platform plans contain 118 creates and no
changes/deletions, each verified against its exact new API-server endpoint.

## Preservation and acceptance

Before provisioning, the existing customer cluster
`mk8scluster-e00j5z9te7x5dd9g6a` had 27 Ready nodes / 38 Ready GPUs, and
`https://89.169.99.188` returned HTTP 200. Only read-only checks are authorized
there in this task. Preserve the shared s2pr2 fleet too.

Verify new node readiness, storage/cache pulls, TLS, API/console authentication,
regional admission, real GPU execution and operation finalization on the exact
release. Record cold-node delays separately from runtime speed. Then qualify
GROMACS and its REST/MCP compatibility layer on this fleet before using it for
customer traffic. Old pilot measurements do not qualify the new deployment.

No customer migration, legacy-key import, customer-bucket copying, live capacity
draining, IP reassignment or DNS switch is part of this bootstrap. These require
the later staged migration plan. Do not destroy the new fleet as test cleanup:
Rene requested it to remain for migration. Temporary verification jobs alone can
be cleaned after their evidence is retained.
