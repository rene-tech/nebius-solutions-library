# Pod security and controller ownership

FS2 applies Kubernetes Pod Security Admission (PSA) at namespace boundaries.
Application namespaces enforce the `baseline` Pod Security Standard and report
`restricted` violations in both audit records and admission warnings.

| Namespace owner | Enforcement | Purpose |
| --- | --- | --- |
| Foundation: `fs2-system`, `fs2-data`, `fs2-models`, `fs2-observability` | `baseline`; `warn`/`audit=restricted` | Platform, database, model runtime, and namespaced observability workloads |
| Academic-assets module | `baseline`; `warn`/`audit=restricted` | Scientific jobs using private RWX claims |
| Workloads ModelExpress namespace | `baseline`; `warn`/`audit=restricted` | Optional ModelExpress control service |
| Foundation: `fs2-node-observability` | `privileged`; `warn`/`audit=restricted` | Host-integrated, operator-owned node agents only |
| Reference-data module | `privileged`; `warn`/`audit=restricted` | Temporary host-path compatibility boundary |

The two `privileged` namespaces are explicit exceptions, not general workload
destinations. They carry a `security.fs2.nebius.ai/pod-security-exception`
annotation, are created by Terraform, and do not receive customer or model
runtime service accounts.

## Node-agent exception

The GPU allocation observer, DCGM exporter, node log collector, and Prometheus
node exporter require host integration that the Baseline policy forbids. They
run in `fs2-node-observability`; the application-facing observability services
remain in the baseline-enforced `fs2-observability` namespace. ServiceMonitor
discovery explicitly includes the node-agent namespace.

Adding another workload to this exception namespace requires a reviewed
host-integration requirement, a digest-pinned image, a dedicated service
account, bounded resources, and a restricted-warning review. Ordinary
Deployments, Jobs, model runtimes, and scientific jobs must not use it.

## Reference-data exception

Reference-data staging currently mounts the retained shared data tree through
a node path. Its dedicated namespace therefore remains an explicit exception
until the existing bytes can be adopted through the RWX CSI class without a
copy, replacement, or path change. The exit gate is a reviewed no-replacement
Terraform plan, a read-only content identity check before and after the mount
change, and successful status plus scientific preprocessing probes. Only then
may the namespace move to `enforce=baseline`.

## Model-controller ownership

The dynamic model controller does not own ServiceAccounts or DaemonSets:

- Dynamic Deployments use the dedicated, non-token-mounted
  `fs2-model-runtime` ServiceAccount provisioned by Terraform. Render bundles
  exclude ServiceAccounts. Previously controller-owned per-model accounts are
  no longer referenced and remain only until Kubernetes garbage-collects them
  with their existing ModelDeployment owner.
- Host-memory-residency declarations are not published to the dynamic
  controller while it lacks DaemonSet authority. Regional-cache and
  GPU-resident mechanisms retain their existing qualification paths.
- The controller can read and create NetworkPolicies. Patching or deleting an
  existing policy requires its exact name in
  `modelController.networkPolicyResourceNames`; Kubernetes cannot apply
  `resourceNames` to collection-level create requests.

This preserves controller-rendered policy creation while preventing the
controller from changing or deleting unrelated namespace policies.

## Rollout and rollback

Apply the foundation namespace and node-agent placement changes before rolling
the workloads/control-plane release. Confirm all node agents are Ready in the
exception namespace, then confirm application namespaces show `baseline` in:

```bash
kubectl get namespace \
  -L pod-security.kubernetes.io/enforce \
  -L pod-security.kubernetes.io/warn \
  -L pod-security.kubernetes.io/audit
```

A privileged test Pod submitted to `fs2-models` must be rejected. Follow that
negative probe with model-controller, scientific-job, database, telemetry, and
inference smoke tests.

Rollback uses the previously captured foundation/workloads plans and control
plane Helm revision. Restore node agents to their former namespaces before
removing the exception namespace. Do not remove PSA labels as a shortcut for a
workload regression; either correct the workload or execute the complete,
recorded rollback.
