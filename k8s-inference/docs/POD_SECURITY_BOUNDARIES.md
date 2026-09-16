# Pod security and controller ownership

FS2 applies Kubernetes Pod Security Admission (PSA) at namespace boundaries.
Application namespaces enforce the `baseline` Pod Security Standard and report
`restricted` violations in audit records and admission warnings. Host-integrated
node agents use one explicitly annotated exception namespace.

| Namespace owner | Enforced state | Purpose |
| --- | --- | --- |
| Foundation: `fs2-system`, `fs2-data`, `fs2-models`, `fs2-observability` | `baseline`; `warn`/`audit=restricted` | Platform, database, model runtime, and namespaced observability workloads |
| Academic-assets and existing scientific namespaces named in deployment inputs | `baseline`; `warn`/`audit=restricted` | Scientific jobs and retained scientific namespaces |
| Workloads ModelExpress namespace | `baseline`; `warn`/`audit=restricted` | Optional ModelExpress control service |
| Reference-data module | `baseline`; `warn`/`audit=restricted` after CSI verification | Reference-data staging and status services on the RWX claim |
| Foundation: `fs2-node-observability` | `privileged`; `warn`/`audit=restricted` | Host-integrated, operator-owned node agents only |

The node-observability namespace is not a general workload destination. It has
no customer or model runtime service account. Adding a workload requires a
reviewed host-integration requirement, a digest-pinned image, bounded resources,
and a restricted-warning review.

## Ordered rollout

`deployment.pod_security.rollout_phase` makes admission changes separable from
workload movement. Advance only after the checks for the current phase pass:

1. `prepare`: create `fs2-node-observability`, move the GPU observer, DCGM
   exporter, node telemetry collector, and Prometheus node exporter into it,
   and create the reference-data RWX claim. Application namespaces remain
   unlabeled. Verify every moved DaemonSet has its desired number of Ready pods
   and bind that evidence through `host_agent_readiness_receipt_sha256`.
2. Copy the retained reference-data tree to `fs2-reference-data-rwx`, mounted
   with `ReadWriteMany` from `csi-mounted-fs-path-sc`. Record a non-secret
   migration receipt whose source and target tree SHA-256 values are equal.
3. `migrate-reference-data`: switch the stager and status Deployment to the RWX
   claim while reference data retains its temporary verification boundary.
   Verify the claim is Bound, status is Ready, and a read-only application probe
   can access the expected published data. Seal those results and supply their
   digest through `csi_readiness_receipt_sha256`.
4. `enforce`: apply `baseline` enforcement and restricted warn/audit labels to
   the foundation, reference-data, academic, ModelExpress, and explicitly listed
   existing scientific namespaces. A privileged Pod submitted to `fs2-models`
   must be rejected. Follow the negative probe with model-controller,
   scientific-job, database, telemetry, and inference smoke tests.

The existing-scientific-namespace input is a complete inventory, not a prefix
selector. A retained namespace must be added explicitly before `enforce`.

## Ordered rollback

Rollback is also phased; do not delete the exception namespace while an agent
still uses it:

1. `rollback-restore-host-agents`: remove application-namespace baseline
   enforcement and restore node agents to `fs2-observability` and `fs2-system`.
   The exception namespace remains present. Verify every restored DaemonSet is
   Ready and observability discovery still reaches it, then bind that evidence
   through `host_agent_restore_receipt_sha256`.
2. `rollback-remove-exception`: keep the agents in their restored namespaces
   and remove `fs2-node-observability` only after the first rollback phase has
   passed. The verified reference-data CSI claim remains in use; restoring the
   legacy host path is a separate, explicitly reviewed storage rollback.

Use the previously captured Terraform plans and Helm revision for application
rollback. Never jump directly from `enforce` to
`rollback-remove-exception`.

## Reference-data CSI gate

`prepare` retains the legacy read-only host path while creating the RWX claim.
Every later phase refuses to plan without a migration receipt bound to the
claim and equal source/target content identities. `migrate-reference-data`
switches consumers to the claim before `enforce` changes PSA; `enforce` also
refuses to plan without the post-switch readiness/access receipt. This makes
the storage transition observable and reversible without combining it with
the admission boundary.

## Model-controller ownership

The dynamic model controller does not own ServiceAccounts or DaemonSets.
Dynamic Deployments use the dedicated, non-token-mounted `fs2-model-runtime`
ServiceAccount provisioned by Terraform, and host-memory-residency declarations
are not published while the controller lacks DaemonSet authority.

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

The rollout evidence must include the exact deployment inputs, CSI class and
claim, migration receipt digest, DaemonSet readiness, namespace inventory,
negative privileged-Pod result, and application smoke-test results.
