# Image provenance and release governance (SAI-09)

Deployed application images must come from source anchored on a durable
release ref, carry a cosign signature bound to a verified release receipt, and
be enforced by admission, so that a registry compromise, a mis-pushed tag, or
an unreviewed local build cannot reach the platform namespaces unnoticed.

## Components and what each one actually enforces

| Piece | Where | Enforces | Does NOT enforce |
| --- | --- | --- | --- |
| Release-source gate | `inference-stack` (`release-gate`; enforced on `apply`) | `HEAD` is a clean checkout (tracked **and untracked** drift fails) reachable from `origin/main`, `origin/release/*`, an origin-verified `release/*`/`deploy/*` tag, or a bundle-verified local tag; receipt in `release-source.json`; `--allow-unreleased-source REASON` records an audited exception | Deploys made outside the wrapper |
| Durable anchor | `inference-stack anchor-release` | A `release/*`/`deploy/*` tag bundled into the private run root: full history, mode 0600, SHA-256 recorded, `git bundle verify` passed, and restore proven by a real clone that must resolve the tag to the expected commit. A local tag **without** a verified bundle never anchors | Public/remote publication (an owner decision) |
| Release receipt | `provenance.py receipt` | Digest ↔ source binding via the content-addressed chain (digest covers the OCI config; its revision label must be a 40-hex commit whose Git tree equals the source-tree label), ancestry to the anchor tag, bundle hash match, attestation subject = an image manifest of the same signed index (or an SPDX document naming the digest), then the receipt itself is cosign-signed (`sign-blob`, tlog disabled) | Anything for images without exact revision labels (they are refused) |
| Signing / verification | `provenance.py sign` / `verify` | `sign` refuses any reference without a signed, validated receipt; key-based cosign signatures with `--use-signing-config=false --new-bundle-format=false --tlog-upload=false` (the regional registry rejects the new bundle media type, and private repo names/digests must not reach the public Rekor log) | Signature ≠ provenance by itself: a signature without a receipt is artifact presence only |
| Admission: image rules | `policy.yaml` (`fs2-image-provenance`) | For Pods **and** Deployments/DaemonSets/StatefulSets/Jobs/CronJobs in `fs2-system`/`fs2-models`: digest pinning, registry prefix allow-list, and the platform-repository digest allow-list, so a direct `helm upgrade`/`kubectl apply` with a bad image fails at the workload write | Config-only changes that reuse allow-listed images |
| Admission: Helm release writes | `policy.yaml` (`fs2-helm-release-governance`) | Secrets of type `helm.sh/release.v1` in `fs2-system` may only be written by the `deploy-principals` recorded in the allow-list ConfigMap | This is a **compensating control**, not closure: a deploy-principal holder can still run direct `helm upgrade`, and arbitrary config-only `kubectl` writes are not gated |

**Closure prerequisite (owner-level IAM):** real enforcement of "one governed
deploy path" requires a distinct, automation-only, non-impersonable deploy
identity whose credentials are only reachable through the wrapper after the
gate and receipt checks, with interactive operators holding a role that cannot
write the platform namespaces. Today every operator authenticates as one
shared cluster-admin principal, so the deploy-principal list documents rather
than separates authority. Until that split exists, Terraform's no-drift plan
is the compensating detective control for config drift, and no claim is made
that admission alone gates every mutating deploy.

## Key management

The cosign release key pair lives in the operator-private run root (mode 0600,
never in Git): `$RUN_ROOT/../cosign/cosign.key`. Generate with
`COSIGN_PASSWORD="" cosign generate-key-pair` in that directory. The public
key is committed here as `cosign.pub`. Rotation: generate a new pair, re-sign
the receipts and currently deployed digests, replace `cosign.pub`, record the
rotation in a release receipt.

## Release procedure (extends `CUSTOMER_RELEASE_POLICY.md`)

Every application rollout — full `inference-stack apply` or Helm-only digest
bump — in order:

1. Commit the exact source and anchor it: push to `origin/main`/`release/*`,
   or (when public publication is owner-gated) tag it `release/*` or
   `deploy/*` and run `./inference-stack anchor-release --anchor-tag <tag>
   --var-file <tfvars> --run-root <run>`.
2. Build and publish the image; record the publish receipt in the run root.
3. Create the bound release receipt (verifies source/anchor/SBOM and signs the
   receipt): `python3 security/image-provenance/provenance.py receipt --image
   <repo>@sha256:<digest> --run-root <run> --repository <checkout>
   --anchor-tag <tag> --key <cosign.key>` (add `--sbom <spdx.json>` for images
   without BuildKit attestations, e.g. the website).
4. Sign the digest (refused without the receipt): `provenance.py sign --key
   <cosign.key> --public-key security/image-provenance/cosign.pub --run-root
   <run> <repo>@sha256:<digest>`.
5. Render and apply the allow-list **before** the rollout: `provenance.py
   render-allowlist --public-key … --run-root <run> --registry-prefix …
   --platform-repository-prefix … --image <ref> … --deploy-principal <user>`.
   Keep every digest still referenced by live Pods, frozen scientific-stage
   bindings, and the Helm rollback window; prune only drained digests.
6. Run the gate (`release-gate`, also automatic inside `apply`), deploy, then
   `provenance.py verify --public-key security/image-provenance/cosign.pub
   <ref>`.

## Negative verification

A non-allow-listed platform digest must be refused at admission. Safe probe
(full admission chain, nothing persisted):

```bash
kubectl -n fs2-system apply --dry-run=server -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: sai-09-negative-probe}
spec:
  restartPolicy: Never
  containers:
    - name: probe
      image: cr.example.invalid/fs2-platform/control-plane@sha256:0000000000000000000000000000000000000000000000000000000000000000
EOF
```

## Rollback (non-destructive first)

Denials are rolled back by demoting enforcement to observation, which keeps
the policies, the allow-list, and the audit trail in place:

```bash
kubectl patch validatingadmissionpolicybinding fs2-image-provenance \
  --type=json -p '[{"op":"replace","path":"/spec/validationActions","value":["Audit","Warn"]}]'
kubectl patch validatingadmissionpolicybinding fs2-helm-release-governance \
  --type=json -p '[{"op":"replace","path":"/spec/validationActions","value":["Audit","Warn"]}]'
```

Re-enable by restoring `["Deny", "Audit"]`. Deleting the policy objects and
the allow-list ConfigMap removes enforcement entirely and is a last resort
that requires the same authorization as disabling a security control.
Existing Pods are never affected by the policies; only new admissions are.

## Deliberate boundaries and residuals

- The exact digest allow-list applies to the platform repository prefix.
  Model runtime images (`…/fs2-models/…`) are enforced for digest pinning and
  registry origin only: scientific-stage bindings freeze historical digests at
  admission time, and an exact model allow-list would break legitimate frozen
  retries. Extending it requires feeding the list from the execution map.
- `pods/ephemeralcontainers` is not matched, so `kubectl debug` support
  workflows keep working for cluster admins.
- `parameterNotFoundAction: Deny` fails closed if the allow-list ConfigMap is
  deleted; Pod churn in the matched namespaces then stalls until it is
  restored from the run-root receipt.
- The website image is built in its repository's CI for validation only;
  publication happens operator-side, so website digests follow the same
  operator receipt/sign procedure. Current website images carry only a short
  revision label, which the receipt tool refuses; publishing full 40-hex
  revision labels (and an anchor for that repository) is an owner/landing-repo
  item. If publishing moves into CI, add the cosign step there with a
  CI-scoped key.
- A cryptographic signature check at admission (sigstore policy-controller) is
  the follow-up upgrade; until then the allow-list ConfigMap — renderable only
  from signed receipts and verified signatures — is the admission proxy.
- The transparency log is deliberately disabled for all signing paths; the
  regression suite asserts `--tlog-upload=false` on each of them.
