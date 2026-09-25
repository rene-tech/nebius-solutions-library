# Admitted pool recovery acceptance

This harness qualifies one bounded path: two real GROMACS umbrella windows,
submitted through the public named MCP tool by a disposable ordinary principal,
with an admitted first attempt stranded on the existing dead `h100-1x` pool.
It runs the unchanged pinned customer CLI for upload, submission and complete
artifact download. Additional MCP calls prove same-operation replay and
cancellation; Kubernetes and operator API reads supply independent observations.
No live recovery has been qualified merely by adding these scripts.

The native fixture is the existing archive
`/home/tux/fs2-alanine-analysis-20260924/umbrella/batches-01/batch-01/input.tar.gz`.
Its SHA-256 is
`208f9167db1c499e98b169cdc920bbe6ab955a2299418960e116372fc5e322ab`.
The runner selects unchanged `window-01` and `window-02` job objects, each with
the real 2 ns production protocol. It changes only the selected job list and
output transport to `platform-artifacts`; it never shortens or fabricates MD.
The ordinary client downloads every native artifact. Existing GROMACS semantic,
inventory, trajectory, geometry, topology and native pull-coordinate validators
then run against the original frozen delivery. This is not a PMF-convergence gate.

## Mutation boundary

`identity.py create` creates one new task tenant/owner and a GROMACS-only key,
copying ordinary scopes, concurrency, budgets and rate limits from an approved
local source key. Secret material stays in a new mode-0700 directory and a
mode-0600 key file. It never prints credentials or changes the source identity.
`identity.py retire` revokes only that key and disables that owner after all its
operations are terminal and all owned workloads are gone; artifacts are retained.

`run_acceptance.py run` submits the exact customer workload. The optional
`--inject-dead-first-attempt` adds a dead-pool affinity constraint to exactly one
task-owned first GPU Job while it is still suspended, before any Pod or Kueue
Workload exists. An atomic JSON patch tests UID, resourceVersion, name,
namespace and suspension. Every OR branch retains its original constraints.
The Job's original pool-preference annotation is preserved, allowing the
controller to render the next immutable attempt using healthy alternatives.

This race can be missed. The runner fails and cancels its accepted operation if
the Job already has a Pod/Workload or has resumed. A healthy-flavor admission
cannot pass as a dead-pool proof. The caller must preserve the failed receipt,
inspect it, and choose a new idempotency key/output directory for a new cohort.
No automatic retry creates another customer operation. Nodes, quotas, model
policy, customer Jobs, and Kueue admission status are never patched. The optional
CREATE-only exact-tenant webhook is implemented separately in
[INJECTOR.md](INJECTOR.md), with an independently reviewed deployment boundary.
This runner can verify an installed injector but never installs or removes it.

## Run

Use `/home/tux/.venvs/fs2-client-qualification-20260923/bin/python` for the runner.
Its installed MCP 2.2 client matches the released CLI. The system `python3`
provides PyYAML for reading autoscaler status. No package installation is needed.

Provision once, only when the release owner has authorized it:

```sh
/home/tux/.venvs/fs2-client-qualification-20260923/bin/python identity.py create \
  --kubeconfig /home/tux/secure-handoff/cosmos-stockholm-sandbox2-20260917.kubeconfig \
  --context fs2-remediation-sandbox2 \
  --source-key-file /home/tux/secure-handoff/fs2-md-engines-20260923/gromacs-isolated-key-private.json \
  --directory /home/tux/secure-handoff/admitted-pool-recovery-20260925/identity
```

The release owner supplies a private `release.json` with these required fields:

```json
{
  "source_revision": "EXACT_40_HEX_CANDIDATE_COMMIT",
  "control_plane_image": "registry/repository@sha256:EXACT_CANDIDATE_DIGEST",
  "runtime_image": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643",
  "client_image": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d"
}
```

The source-to-image build receipt remains the release owner's evidence. The
runner independently checks actual deployed image refs, ready replica counts,
Deployment specs, mounted ConfigMap digests, ClusterQueue and ResourceFlavor
specs before and after each cohort. It also requires observed started workers
to have the declared runtime image and an actual image ID.

Before submission, the public preflight checks the ordinary key, named tool,
published parameter schema, lifecycle tools and unchanged local fixture. It
creates no upload, inference or Kubernetes object:

```sh
/home/tux/.venvs/fs2-client-qualification-20260923/bin/python preflight.py \
  --key-file /home/tux/secure-handoff/admitted-pool-recovery-20260925/identity/key-private.json \
  --fixture /home/tux/fs2-alanine-analysis-20260924/umbrella/batches-01/batch-01 \
  --output /home/tux/secure-handoff/admitted-pool-recovery-20260925/preflight-candidate.json
```

```sh
/home/tux/.venvs/fs2-client-qualification-20260923/bin/python run_acceptance.py run \
  --kubeconfig /home/tux/secure-handoff/cosmos-stockholm-sandbox2-20260917.kubeconfig \
  --context fs2-remediation-sandbox2 \
  --key-file /home/tux/secure-handoff/admitted-pool-recovery-20260925/identity/key-private.json \
  --release-file /home/tux/secure-handoff/admitted-pool-recovery-20260925/release.json \
  --fixture /home/tux/fs2-alanine-analysis-20260924/umbrella/batches-01/batch-01 \
  --analysis-python /home/tux/.venvs/fs2-four-engine-20260923/bin/python \
  --delivery /home/tux/fs2-alanine-comparison-20260923/delivery-02 \
  --output /home/tux/secure-handoff/admitted-pool-recovery-20260925/cohort-01 \
  --idempotency-key admitted-pool-recovery-20260925-cohort-01 \
  --inject-dead-first-attempt
```

After the first clean result, repeat with only `cohort-02` and its new
idempotency key. Preserve the exact release, identity, native fixture and client.

```sh
/home/tux/.venvs/fs2-client-qualification-20260923/bin/python run_acceptance.py combine \
  /home/tux/secure-handoff/admitted-pool-recovery-20260925/cohort-01/receipt.json \
  /home/tux/secure-handoff/admitted-pool-recovery-20260925/cohort-02/receipt.json \
  --output /home/tux/secure-handoff/admitted-pool-recovery-20260925/two-cohorts.json
```

`--scenario cancel` uses a separate operation and cancels only after observing
real admission to the dead pool with an unscheduled owned Pod. It requires a
cancelled terminal result, released attempts and no remaining owned resources.

`--scenario no-spare` requires a real retry waiting with a Kueue
`QuotaReserved=False` insufficient-quota condition, no admission and no retry
churn for at least 120 seconds, followed by successful native completion when
capacity becomes available. Healthy available capacity makes this scenario
inconclusive/failed; the runner does not occupy the fleet or alter quotas to
manufacture it. Run only when this condition actually exists. The separate
`--scenario synthetic-no-eligible-capacity` exercises synthetic task-owned
eligibility loss/return via the optional injector; its receipt explicitly does
not claim physical fleet exhaustion. The ordinary recovery/cancellation cohorts
do not stand in for this evidence.

## Evidence and remaining scope

The runner requires a nonempty pool with all Nodes `Ready=Unknown` and
`NodeStatusUnknown` for at least 120 seconds, plus a fresh autoscaler group
`Unhealthy` record with zero ready/not-started/unregistered nodes, no scale-up,
and no target increase. Newly starting or scaled-to-zero pools do not qualify.
Both the retained public first-attempt failure and subsequent Job manifest
must prove `admitted_pool_unavailable` and pool exclusion. The observer checks
that old Jobs, Pods and Kueue reservations are absent when replacements appear.
The public recovery explanation must retain the failed pool, avoided and
eligible pools, original retry cap, at least 120 seconds admitted wait, and a
retry-not-before timestamp preceding the replacement's observed creation.

Operator observations bind tenant, principal, operation, immutable attempts,
Job/Pod/node/GPU correlations and frozen retry cap. GPU accounting is reconciled
against the lifecycle ledger by separate quota, scheduler, device and execution
clocks. Unknown measurements stay unknown; reservations and estimates are never
reported as measured billing. Raw credentials, caller fingerprints and native
inputs are excluded from the summary receipt. Full customer-client logs and
artifacts remain private and must not be committed.

The combined receipt deliberately says `customer_ready=false`: it establishes
two clean recovery cohorts for this bounded path. Cancellation, no-spare,
retained-service sibling checks, source/image provenance and the broader release
owner's capability inventory must accompany the final release verdict.

Local verification:

```sh
/home/tux/.venvs/fs2-client-qualification-20260923/bin/python -m pytest -q test_run_acceptance.py test_admission_injector.py
```

The three real-controller envelope tests require the control-plane venv instead
of the client venv; run the same test files with
`../../components/control-plane/.venv/bin/python` to execute them without skips.
