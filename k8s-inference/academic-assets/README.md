# Licensed academic asset ingestion

Private, non-redistributable ingestion for the two licensed assets the cancer
immunotherapy workload needs:

| Asset | Model | What it is |
|---|---|---|
| `alphafold3` | `alphafold3` | Google's AlphaFold 3 model parameters (`af3.bin.zst`) |
| `pyrosetta-bindcraft` | `bindcraft` | The PyRosetta distribution native BindCraft requires |

Neither may be redistributed. Nothing licensed is ever committed to this
repository, baked into a container image, or placed in a general shared cache.

## Two independent axes

The single most important rule here is that these two questions have different
answers and neither implies the other.

**Use authorization** is the platform owner's grant to install and operate an
asset for this academic proof of concept. It activates the operational path and
is recorded in `contracts/*-use-authorization.json`. It carries no institution,
no representative and no signature, because it is not and must not be presented
as a licence acceptance.

**Formal licence acceptance** is what each licensor actually requires: a named
representative with authority to bind a specific academic institution accepts the
exact pinned terms and supplies entitlement evidence. It is reported separately
and is currently `FormalAcceptancePending`. It is never synthesized from
placeholder institution metadata, which is why `institution_id` is nullable
everywhere on the operational path.

An asset can therefore be fully usable for the authorized proof of concept while
formal acceptance is still outstanding, and the readiness projection says exactly
that rather than rounding either way.

## Delivery: mounted, never baked

Licensed bytes live on a tenant-private ReadWriteMany claim and are mounted
read-only. They are never embedded in an image. `resolve --for-image-embedding`
is refused unconditionally, and the contract cannot express an embedded delivery.

Access is by **shared non-root group**, not by matching user IDs:

| Object | Mode | Why |
|---|---|---|
| Volume root | `2770` | Owned by the asset group so a non-root installer can create asset directories |
| Asset directory | `0750` | Owner-writable so a new tree promotes atomically; group-traversable; no world access |
| Installed tree and files | `0550` / `0440` | Group-readable only. Never world-readable, never group-writable |

A consuming pod joins the group and mounts read-only:

```yaml
securityContext:
  supplementalGroups: [65532]
volumes:
  - name: academic-assets
    persistentVolumeClaim: { claimName: academic-assets-runtime-rwx, readOnly: true }
```

Consumers must **not** set `fsGroup`. Kubernetes fsGroup ownership management
rewrites the tree to group-writable `0660` files and world-traversable `2775`
directories, which is exactly the drift this contract exists to prevent.

BindCraft consumes a **preinstalled** `site-packages` tree via `PYTHONPATH`. The
1.67 GB wheel is never installed per request.

## One documented step

```bash
export FS2_ACADEMIC_ASSET_STATE_DIR=~/.local/state/fs2-academic/private-state
export FS2_ACADEMIC_GENERATION=poc-$(date -u +%Y%m%d)
export FS2_AF3_FILE=/path/to/af3.bin.zst
export FS2_PYROSETTA_WHEEL_FILE=/path/to/pyrosetta-2026.29+...-cp310-cp310-linux_x86_64.whl

# Verify and record locally.
scripts/ingest-approved-assets.sh

# Also stage onto the tenant volume and build the installed tree.
export FS2_ACADEMIC_STAGE_CACHE=1
export FS2_ACADEMIC_PROJECT_ID=... FS2_ACADEMIC_REGION=... FS2_ACADEMIC_CLUSTER_ID=...
scripts/ingest-approved-assets.sh
```

Artifact locations are passed by environment reference, so no licensed path ever
appears in argv, logs or receipts. Formal acceptance is optional and separate:
set `FS2_AF3_ACCEPTANCE` / `FS2_PYROSETTA_ACCEPTANCE` only when a real
institutional receipt exists. Templates for those live in `contracts/` and fail
validation while any `REPLACE_` marker remains.

## Readiness stages

`scripts/academic_assets.py status` reports, per asset:

1. `artifact` — bytes verified against the pinned size, magic, digest and a real
   structural check (zstd frame test; wheel `dist-info`, `METADATA` version and
   ABI tag).
2. `cache` — bytes present on the tenant-private claim, re-hashed on the cluster,
   with the observed volume identity and delivery modes.
3. `install` — the contracted installed tree, promoted atomically and
   import-verified in place. `NotApplicable` for assets consumed directly.
4. `runtime` — offline proof that a runtime can consume the mounted asset.
5. `deployment` and `semantic` — serving readiness, owned by the runtime
   onboarding work.

A generation is bound to an exact contract digest. Changing the contract makes
existing generations report `InvalidContract` until the evidence is replayed with
`scripts/replay-live-evidence.sh`. That is intentional fail-closed behaviour.

## Infrastructure

Terraform owns the namespace, the tenant-private claim, the retained quarantine
claim and the deny-egress policy. **A fresh deployment needs no manual step**:
`terraform apply` creates everything from the `academic_assets` block in tfvars,
which inherits project and region from `deployment.target`.

`scripts/adopt-live-resources.sh` is only for a claim that already holds verified
bytes, where recreation would provision an empty volume and discard them. It is
idempotent: addresses already in state are skipped, and objects that are not live
are left for `apply` to create.

## Portability

The contract holds policy and asset identity only. Project, region, cluster,
volume handle and registry are properties of a deployment, so they are observed
per environment and recorded in stage receipts and generated acceptance state,
never hard-coded in the reusable contract.

## Alternatives

OpenFold3 and the open binder lane are independent alternatives with their own
identities and results. They are never aliased to, and never satisfy, AlphaFold 3
or native BindCraft.

## Checks

```bash
./run_checks.sh
```

Runs contract and schema validation, the unit and executable script suites, the
anti-drift check that pins each stage validator to its published JSON Schema, and
a confidentiality scan of the working tree for licensed artifacts, credentials
and signed URLs.
