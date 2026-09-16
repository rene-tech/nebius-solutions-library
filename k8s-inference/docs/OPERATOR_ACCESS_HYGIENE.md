# Operator access hygiene

The platform keeps operator access and credential material on separate,
least-privilege paths. These controls are required for every new deployment and
must be preserved during upgrades.

## Configuration contract

`deployment.cluster.control_plane_allowed_cidrs` is required and accepts one to
eight explicit IPv4 or IPv6 CIDRs. Use only the egress addresses of the
operator and automation networks that must reach the Kubernetes API. An empty
allowlist and a universal CIDR are not deployment defaults.

Terraform creates a dedicated handoff service account, viewer group, group
membership, and project-scoped `viewer` access permit. Its non-secret IDs are
available in the infrastructure `operator_handoff_contract` output. Create and
deliver an authentication key outside Terraform so private key material cannot
enter state. Do not add this identity to an editor, administrator, or
Kubernetes `cluster-admin` binding.

Generated passwords are Terraform ephemeral values. Kubernetes Secret
payloads use the provider's `data_wo` argument and an explicit
`data_wo_revision`; credential values therefore do not persist in state or
plans. The workloads access output contains only Kubernetes Secret references.
The wrapper resolves them in memory only when an operator requests an explicit
file handoff.

Each credential class has an independent positive generation under
`deployment.secrets.credential_generations`:

- `admin`
- `access`
- `database`
- `key_material`
- `registry`
- `grafana`

Increment only the class being rotated. Review the plan for the expected
write-only Secret updates and affected rollouts before applying it.

## Private run root and credential handoff

Every wrapper command recursively repairs the run root to owner-only
permissions and rejects symlinks beneath it. A credential handoff additionally
requires an explicit destination in an existing owner-only directory:

```bash
install -d -m 0700 /a/private/operator-handoff
./inference-stack output --var-file terraform.tfvars \
  --credential-file /a/private/operator-handoff/access.json
test "$(stat -c %a /a/private/operator-handoff/access.json)" = 600
```

The command prints a non-secret write receipt. Never redirect credential output
to a terminal, log, build artifact, or ticket. Remove the handoff file when the
recipient has imported it.

## Staged rollout and rollback

Before a shared rollout, reconcile the branch with the exact deployed source
and record the current Terraform state backup, cluster identity, control-plane
allowlist, IAM bindings, and application revisions. Apply in this order:

1. Confirm every required operator/automation egress address is in the new
   allowlist and keep the current session open as a rollback path.
2. Apply the infrastructure stage and verify a second connection through an
   allowed source before closing the first session.
3. Create the handoff key outside Terraform and verify it authenticates as the
   dedicated viewer identity.
4. Apply one credential generation change at a time, verifying the dependent
   service before continuing.

Rollback uses the recorded prior CIDR set and application revision. A
credential that has been disclosed is not rolled back: rotate it forward again
and revoke the superseded value.

## Verification

Run these checks without printing state, Secret data, or credential files:

```bash
test -z "$(find "$RUN_ROOT" -perm /044 -print -quit)"
test -z "$(find "$RUN_ROOT" -type d ! -perm 0700 -print -quit)"

kubectl auth can-i create pods --as="$HANDOFF_PRINCIPAL"
# expected: no

nebius mk8s cluster get --id "$CLUSTER_ID" --format json \
  | jq -e '.spec.control_plane.endpoints.public_endpoint.allowed_cidrs | length > 0'
```

Also prove that read-only cluster inventory still succeeds for the handoff
identity and run the normal landing, catalog, authorization, synchronous and
streaming inference, MCP, admin, operation, storage, queue, and observability
smoke tests after the serialized platform rollout.
