# Pod security and controller ownership

FS2 applies Kubernetes Pod Security Admission (PSA) at namespace boundaries.
Application namespaces enforce the `baseline` Pod Security Standard and report
`restricted` violations in audit records and admission warnings. Host-integrated
node agents and the one retained CUDA checkpoint profile use two separately
admission-constrained exception namespaces.

| Namespace owner | Enforced state | Purpose |
| --- | --- | --- |
| Foundation: `fs2-system`, `fs2-data`, `fs2-models`, `fs2-observability` | `baseline`; `warn`/`audit=restricted` | Platform, database, model runtime, and namespaced observability workloads |
| Academic-assets and existing scientific namespaces named in deployment inputs | `baseline`; `warn`/`audit=restricted` | Scientific jobs and retained scientific namespaces |
| Workloads ModelExpress namespace | `baseline`; `warn`/`audit=restricted` | Optional ModelExpress control service |
| Reference-data module | `baseline`; `warn`/`audit=restricted` after CSI verification | Reference-data staging and status services on the RWX claim |
| Foundation: `fs2-node-observability` | `privileged`; `warn`/`audit=restricted` | Host-integrated, operator-owned node agents only |
| Foundation: `fs2-snapshot-operations` | `privileged`; `warn`/`audit=restricted` | One digest-pinned ESMFold2 donor/restore profile only |

The node-observability namespace is not a general workload destination. Two
fail-closed ValidatingAdmissionPolicies admit only four exact DaemonSet/service
account pairs and their Pods. Direct Pods and ephemeral containers are denied;
images, commands, host paths, mounts, host namespaces, and capabilities are
bounded. The snapshot namespace has a separate fail-closed policy and a fixed,
tokenless manager ServiceAccount/RoleBinding. It admits only the exact reviewed
runtime and tools digests, command, GPU resources, PVCs, mounts, and isolation
profile. Neither exception is selected by a caller-provided username or a
general workload label. These admission boundaries still apply if an unrelated
role is later widened.

## Ordered rollout

`deployment.pod_security.rollout_phase` makes admission changes separable from
workload movement. Advance only after the checks for the current phase pass:

1. At the serialized rollout slot, record the then-current stable Helm revision
   and image digests. Confirm `request_debug_enabled=false`. A historical Helm
   revision is never a cross-ticket rollback target.
2. `prepare`: create the admission-protected `fs2-node-observability` and
   `fs2-snapshot-operations` namespaces, the dedicated retained CSI
   driver/class, and the unused RWX claim. Dual-run the GPU observer, DCGM
   exporter, node telemetry collector, and Prometheus node exporter in both old
   and exception namespaces. Application namespaces remain unlabeled. Sign
   `exception-ready` only after immediate reads prove the exact UID,
   resourceVersion, spec hash, and readiness of all eight old/new agent sets,
   the exception admission/RBAC objects, and the retained claim identity.
3. `migrate-reference-data` consumes that signed state. Copy the retained tree
   to `fs2-reference-data-rwx`, switch the stager and status Deployment to the
   claim, and keep the temporary verification boundary. Sign
   `reference-data-ready` only after the claim is Bound, source and target tree
   identities match, status is Ready, and a read-only application probe passes.
4. `cleanup-legacy-resources` consumes `reference-data-ready`. Reconcile every
   retained model and App under the finite network profiles, then run the
   UID-fenced cleanup plan for controller-created NetworkPolicies,
   ServiceAccounts, and DaemonSets. It refuses referenced ServiceAccounts and
   Terraform-owned profile policies. Run the exact live inventory collector;
   sign `baseline-ready` only when all legacy-resource remainders, baseline
   incompatibilities, host paths, and unauthorized exception objects are zero.
5. `enforce` consumes `baseline-ready` and applies `baseline` enforcement plus
   pinned-minor restricted warn/audit labels to
   the foundation, reference-data, academic, ModelExpress, and explicitly listed
   existing scientific namespaces. A privileged Pod submitted to `fs2-models`
   must be rejected. Follow the negative probe with model-controller,
   arbitrary-UUID App, scientific-job, database, telemetry, and inference smoke
   tests. Sign `baseline-enforced` only after these checks pass.

The existing-scientific-namespace input is exactly the frozen set
`fs2-academic-poc` plus
`fs2-bioir-{boltz2,coverage,openfold,protenix,snapshot}`, not a prefix selector
or an optional empty list. The read-only inventory collector independently
compares that complete live scientific inventory before enforcement.
Historical hostPath launchers are replaced by CSI-only renderers that refuse a
live launch until the namespace-local claim is Bound to the retained class and
every required subpath passes a read-only probe. The privileged donor/restore
renderer has a separately admission-constrained exact-profile successor in
`fs2-snapshot-operations`; it cannot launch until its exact reference and
checkpoint claims have independently passed the same retained-content and
durability gates. These source contracts are not evidence that those claims
exist or contain data in a live cluster.

## Ordered rollback

Rollback is also phased; do not delete the exception namespace while an agent
still uses it:

1. `rollback-remove-enforcement` consumes `baseline-enforced` and removes
   application-namespace enforcement while exception agents remain Ready.
2. `rollback-restore-host-agents` consumes `enforcement-removed`, recreates the
   old-namespace agents while the exception copies continue running, and signs
   `host-agents-restored` only after all restored agents are Ready.
3. `rollback-remove-exception` consumes `host-agents-restored`, first refuses
   active snapshot Pods or unexported checkpoints, then removes the exception
   agents, admission bindings, and both exception namespaces and verifies
   absence. The retained reference-data CSI claim is not destroyed by this
   rollback.

Use plans and the stable Helm revision captured at the serialized rollout slot,
and reject any rollback candidate with request debugging enabled. Never use a
historical shared revision and never jump directly from `enforce` to an agent
restore or exception removal phase.

## Reference-data CSI gate

`prepare` retains the legacy read-only host path while creating the RWX claim
on `fs2-reference-data-retained-sc`. Its separate driver is rooted at
`/mnt/fs2-reference-data/csi-mounted-fs-path-data`, not the general model cache.
The StorageClass is post-rendered to `Retain`; the chart release and PVC use
`prevent_destroy`; the storage handoff must prove deletion is forbidden and
capacity is at least the 1611 GiB request.

Every later phase requires one short-lived v3 Ed25519 receipt whose single
signature covers the complete canonical bundle: reviewed signer identity and
key digest, cluster/run/kube-system UID, deployment nonce, exact prior and next
state, phase, one-time nonce, expiry, pinned PSA minor, six-namespace
inventory, PVC UID/class, dataset/revision/tree, retained filesystem, and live
observations. Each present observation carries the exact Kubernetes UID,
resourceVersion, and canonical object hash; absence observations carry no
substitutable identity. Baseline gates additionally bind complete live list
hashes for every relevant workload kind in every frozen namespace.

Receipt verification is an apply-time operation, never a replayable Terraform
data source. The foundation consumer re-reads every signed object and inventory
from the selected API server immediately before an atomic ConfigMap
resourceVersion compare-and-swap. The monotonic ledger is context- and
authority-bound, deletion-protected, admission-limited to exact rollout
identities, and stores the last receipt, nonce, sequence, state, and phase
authorization. The workloads stage can consume that exact authorization once;
owner or downstream replay, phase skipping, stale resourceVersions, spec/status
drift, inventory omission, context substitution, and concurrent ledger updates
all fail closed. A digest-shaped string or a valid signature without successful
live reconciliation and ledger consumption has no authority.

## Model-controller ownership

The dynamic model controller does not own ServiceAccounts or DaemonSets.
Dynamic Deployments use the dedicated, non-token-mounted `fs2-model-runtime`
ServiceAccount provisioned by Terraform. Host-memory-residency declarations
remain published: Terraform owns one finite holder per canonical model/pool,
and arbitrary App UUIDs only consume its signed receipt. The controller never
creates or mutates those DaemonSets.

The controller has no NetworkPolicy API endpoint and its Role has no
`networkpolicies` rule. It therefore cannot get, list, watch, create, patch, or
delete policy objects. Runtime isolation is supplied by a finite set of
Terraform-owned profiles selected by immutable
`fs2-serve.nebius.ai/network-profile` Pod labels. Standard profiles are bound
to exact service ports; ModelExpress profiles are bound to an exact reviewed
qualification and pool. Runtime App UUIDs are never policy object identities.

The profile contract is shared with the runtime-isolation implementation and
must be integrated as one reviewed lineage. Arbitrary App creation, update,
stale-workload cleanup, and finalizer cleanup must continue using only the
Deployment, Service, and ScaledObject permissions. Integration verification
must prove all six NetworkPolicy verbs are denied to the controller service
account while those App lifecycle paths remain functional.

## Verification

After `enforce`, inspect the namespace labels and exercise both negative and
positive paths:

```bash
kubectl get namespace \
  -L pod-security.kubernetes.io/enforce \
  -L pod-security.kubernetes.io/warn \
  -L pod-security.kubernetes.io/audit
```

The rollout evidence must include the exact deployment inputs, CSI driver/class
and claim UID, whole-bundle signature identity, pre/post ledger resourceVersion
and sequence, one-time consumption result, DaemonSet readiness, exact namespace
and live object/list hashes, bounded legacy cleanup UIDs, admission-policy
identity, negative privileged-Pod result, positive customer/App/inference
checks, and the slot-time stable rollback revision with request debugging
disabled.
