# Image provenance and release governance (SAI-09)

Deployed application images must come from source anchored on a durable
release ref, carry a cosign signature bound to a verified release receipt, and
be enforced by admission, so that a registry compromise, a mis-pushed tag, or
an unreviewed local build cannot reach the platform namespaces unnoticed.

## Components and what each one actually enforces

| Piece | Where | Enforces | Does NOT enforce |
| --- | --- | --- | --- |
| Release-source gate | `inference-stack` (`release-gate`; enforced on `apply`) | `HEAD` is a clean checkout (tracked **and untracked** drift fails) reachable from `origin/main`, `origin/release/*`, an origin-verified `release/*`/`deploy/*` tag, or a bundle-verified local tag; latest state in `release-source.json`, every evaluation — including exceptions — appended to the hash-chained `release-source-history.jsonl`. Exceptions require a sanitized reason bound to a non-secret tracking ID (`incident:`/`change:`/`ticket:`/`task:`) plus a named approver (`--exception-approver` or `FS2_RELEASE_EXCEPTION_APPROVER`) | Deploys made outside the wrapper; this is a source-side check only and weaker than receipt anchoring. The chained log is tamper-EVIDENT (verify with `verify_chained_history`), not immutable — WORM storage of the log and binding approvers to real identities are owner infrastructure/IAM items |
| Durable anchor | `inference-stack anchor-release` | A `release/*`/`deploy/*` tag bundled into the private run root: full history, mode 0600, SHA-256 recorded, `git bundle verify` passed, and restore proven by a real clone that must resolve the tag to the expected commit. A local tag **without** a verified bundle never anchors. Anchors are **write-once**: identity-equal re-runs are idempotent; a moved tag, a changed/missing bundle, or an unrecorded file at the bundle path is refused | Public/remote publication (an owner decision) |
| Release receipt | `provenance.py receipt` | Digest ↔ source binding via the content-addressed chain: the **exactly one** linux/amd64 image manifest is resolved from the index (never "the first entry"), its config blob is fetched, hash-verified, platform-checked, and must carry exact 40-hex revision and source-tree labels; the anchor's current annotated **tag object** must equal the recorded target and be present in the verified bundle; ancestry and tree equality are proven inside a fresh clone restored **from the bundle**; SBOM evidence is the attestation selected for that exact amd64 manifest — a fetched, hash-verified (blob = layer digest) in-toto Statement (v0.1/v1) with `predicateType` exactly `https://spdx.dev/Document`, a named subject whose sha256 equals the amd64 manifest, and an SPDX-2.x predicate with valid unique SPDXIDs and a resolving SPDXRef-DOCUMENT DESCRIBES — or a standalone SPDX document that binds the digest as an exact SHA256 checksum or purl version on a described package (substring mentions never bind); the verified manifest/config/attestation/layer/statement digests are recorded and the receipt itself is cosign-signed and signature-verified before no-replace publication. Receipts are **write-once**: identity-equal re-runs re-verify the signature and return the original bytes; any difference is refused | Images without exact revision/tree labels (refused); superseding requires explicitly archiving the old receipt directory first |
| Signing / verification | `provenance.py sign` / `verify` | `sign` refuses any reference without a signed, validated receipt; key-based cosign signatures with `--use-signing-config=false --new-bundle-format=false --tlog-upload=false` (the regional registry rejects the new bundle media type, and private repo names/digests must not reach the public Rekor log) | Signature ≠ provenance by itself: a signature without a receipt is artifact presence only |
| Admission: image rules | `policy.yaml` (`fs2-image-provenance`) | For Pods **and** Deployments/DaemonSets/StatefulSets/Jobs/CronJobs in `fs2-system`/`fs2-models`: digest pinning, registry prefix allow-list, and the platform-repository digest allow-list, so a direct `helm upgrade`/`kubectl apply` with a bad image fails at the workload write | Config-only changes that reuse allow-listed images |
| Admission: Helm release writes | `policy.yaml` (`fs2-helm-release-governance`) | Secrets of type `helm.sh/release.v1` in `fs2-system` may only be written by the `deploy-principals` recorded in the allow-list ConfigMap | This is a **compensating control**, not closure: a deploy-principal holder can still run direct `helm upgrade`, and arbitrary config-only `kubectl` writes are not gated |

**Closure prerequisite (owner-level IAM):** real enforcement of "one governed
deploy path" requires a distinct, automation-only, non-impersonable deploy
identity whose credentials are only reachable through the wrapper after the
gate and receipt checks, with interactive operators holding a role that cannot
write the platform namespaces. Today every operator authenticates as one
shared cluster-admin principal, so the deploy-principal list documents rather
than separates authority. The same applies to the allow-list ConfigMap: the
governed renderer refuses unreceipted/unverified digests, but the ConfigMap is
**not mutation-protected** — any principal with ConfigMap update can add
digests or deploy principals directly, bypassing the renderer. Closure
therefore depends on (a) exclusive automation-identity RBAC on the allow-list
ConfigMap, (b) admission protection of the ConfigMap and of the policy objects
themselves, and (c) removal of human workload/ConfigMap-mutation/impersonation
rights — the parent program's deploy-identity proposal, an owner-gated IAM
decision. Until that split exists, Terraform's no-drift plan is the
compensating detective control for config drift, and no claim is made that
admission alone gates every mutating deploy or that the ConfigMap can only be
produced by the renderer.

**Coherent anchoring contract:** every *receipted* release requires a durable
`release/*`/`deploy/*` tag with a verified private bundle — including commits
that are also reachable from `origin/main` or another remote ref. Remote-ref
reachability satisfies the deploy-source gate, but only the anchor bundle
provides the self-contained restore proof a receipt binds to.

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

1. Commit the exact source, tag it `release/*` or `deploy/*`, and anchor it:
   `./inference-stack anchor-release --anchor-tag <tag> --var-file <tfvars>
   --run-root <run>`. The tag+bundle anchor is mandatory for every receipted
   release; additionally push to `origin/main`/`release/*` when public
   publication is permitted.
2. Build and publish the image; record the publish receipt in the run root.
3. Create the bound release receipt (resolves the exact linux/amd64 manifest
   and hash-verified config, proves the tag object and ancestry in a
   bundle-restored clone, validates the in-toto SPDX statement, retains the
   statement/SBOM bytes content-addressed under `release-sboms/`, signs the
   receipt, verifies the signature, and publishes no-replace):
   `python3 security/image-provenance/provenance.py receipt --image
   <repo>@sha256:<digest> --run-root <run> --repository <checkout>
   --anchor-tag <tag> --key <cosign.key> --public-key
   security/image-provenance/cosign.pub` (add `--sbom <spdx.json>` for images
   without BuildKit attestations, e.g. the website; the document must bind the
   digest as an exact SHA256 checksum or purl version on a described package).
4. Sign the digest (refused without the receipt): `provenance.py sign --key
   <cosign.key> --public-key security/image-provenance/cosign.pub --run-root
   <run> <repo>@sha256:<digest>`.
5. Assemble and sign the **complete inventory**
   (`fs2-serve.nebius.ai/release-inventory/v4`): enumerate the current live
   workloads in the matched namespaces, the Helm rollback window
   (`helm history` digests), and frozen scientific-stage bindings as its three
   `sources`, each with `observed_at` and the unique, structured
   `resource_ids` it was read from, plus the `cluster` identity, a
   `captured_at` timestamp, and the exact admission `scope` (cluster,
   namespaces, registry prefixes, platform repository prefix, deploy
   principals, verification-key SHA-256). Freshness is strictly bounded:
   future-dating beyond the 5-minute clock-skew bound is refused for
   `captured_at` AND every `observed_at`; the past bound defaults to 24h and
   `--max-inventory-age-hours` must be finite and within (0, 168] — nan/inf
   are refused. Drains can only remove **non-live** references: a drained
   image must be absent from `live_workloads` and present in a non-live
   source, with a reason bound to a tracking identifier — **an active image
   can never be drained**; owner scope decisions about live sibling programs
   belong in the admission policy's match scope, never in the inventory.
   `platform_images` must equal the source union minus the audited drains,
   which the renderer proves. Sign it: `cosign sign-blob
   --key <cosign.key> --use-signing-config=false --tlog-upload=false --yes
   --output-file inventory.json.sig inventory.json`. The renderer refuses to
   run without it and renders **only** the inventory set — extras, missing
   entries, and unreceipted or unsigned digests all abort:
   `provenance.py render-allowlist --public-key … --run-root <run>
   --inventory inventory.json --scope
   security/image-provenance/release-scope.json --registry-prefix …
   --platform-repository-prefix … --deploy-principal <user>` (optional
   `--image` arguments must equal the inventory exactly and exist only as a
   cross-check). The inventory additionally carries a strictly increasing
   integer `generation` and a typed collector
   (`{method: fs2-live-enumeration/v1, identity: <authenticated user>}`).
   Replay protection is a SIGNED chain, not a mutable file: every accepted
   render appends a cosign-signed head (sequence, generation, capture time,
   inventory hash, previous-head hash) under `release-inventory-heads/`,
   published no-replace via link(2); the whole chain is re-verified on every
   render (signatures over exact bytes, content-addressed names, dense
   sequences, prev-hash linkage, strictly increasing generations), so an
   older or forked signed inventory never replays and a tampered, unsigned,
   or spliced store fails closed — `render-allowlist` therefore requires
   `--key`. (Deleting the NEWEST head is locally undetectable, like deleting
   any local file: WORM/off-host anchoring of the head is the named owner
   gate, as for the gate history.) At render time the tool authoritatively
   re-observes the cluster through the AUTHENTICATED API (kubectl and helm
   required — unavailable enumeration fails closed): live platform images
   from Pods AND workload controllers (a scaled-to-zero or crash-looping
   Deployment counts with no Pod), the Helm rollback window re-derived from
   `helm history`/`helm get manifest` per revision, and frozen bindings
   re-fetched from the exact resources their signed identities name; every
   source's refs AND resource identities must equal the observation, the
   kubeconfig cluster identity must equal the scope's cluster, and the
   recorded collector identity must equal the authenticated identity — a
   signer cannot omit an active sibling image, drain a rollback revision the
   history still carries, or self-assert coverage. **Admission scope is
   never caller-chosen**: the committed `release-scope.json` is the single
   authority and it is OWNER-SIGNED — the detached
   `release-scope.json.sig` must verify against the pinned release key over
   the exact bytes parsed; no Git ref is consulted (local tracking refs are
   writable by any local process via `git update-ref` and are NOT
   authority). It ships EMPTY (signed), so rendering fails closed until the
   owner populates and signs the exact scope. The scope pins the committed
   admission policy manifest by SHA-256, the binding must select exactly the
   scope's namespaces with a single `In` expression, include `Deny` in its
   validationActions, and fail closed on the exact allow-list ConfigMap —
   and the LIVE ValidatingAdmissionPolicy and binding, fetched through the
   authenticated session, must equal the committed definitions on every
   enforced field (a missing, Audit-only, `NotIn`, or otherwise drifted live
   object refuses rendering). The scope's namespaces render into the
   ConfigMap `namespaces` key and the policy denies any request whose
   namespace is not listed — claimed and enforced coverage cannot drift. The signed inventory's `scope` must equal it exactly (a
   signer cannot substitute their own coverage), every CLI argument must
   equal it (a mistyped platform prefix cannot bypass platform-digest
   gating), the pinned key hash must equal its recorded key identity, every
   inventory reference must lie under its registry prefixes, and the
   ConfigMap renders FROM its values. The remaining residual is collector
   authenticity: the tooling cannot prove from source that the signer
   enumerated the cluster honestly, so the owner populating
   `release-scope.json` is also ratifying the collection procedure (an
   authenticated authoritative collector remains an owner infrastructure
   item, and rendering stays impossible until that owner sign-off exists).
6. Run the gate (`release-gate`, also automatic inside `apply`), deploy, then
   `provenance.py verify --public-key security/image-provenance/cosign.pub
   <ref>`.

## Evidence immutability and no-replace publication

Release evidence is write-once and published through no-replace primitives,
so neither a later run nor a crash or concurrent run can rewrite or truncate
what an earlier release proved:

- **Anchors** (`release-anchors.json` + content-addressed bundles): only
  annotated tag objects anchor (lightweight tags are refused). The bundle is
  built and fully verified (integrity, exact tag object via `list-heads`,
  real clone restore) in a same-filesystem staging directory, fsynced, then
  published to `release-anchors/<sha256>.bundle` via `link(2)` — atomic and
  **no-replace**, so there is no check-then-rename window; on a collision the
  surviving file is fully re-verified before it is trusted, and the published
  winner is re-hashed after publication. A malformed index fails closed (it
  is never treated as empty). An identity-equal re-run re-verifies hash,
  integrity, and tag placement and returns `idempotent: true`; a moved or
  recreated tag, a changed or missing bundle, or a conflicting file at the
  content address is refused; a crash remnant between publication and index
  commit is re-adopted only if it fully verifies. Superseding a release means
  anchoring a new tag.
- **Receipts** (`release-receipts/<digest>/receipt.json` + `.sig`): receipt
  and signature are staged together, the signature is verified, both files
  are fsynced, and the pair is published by claiming the final directory with
  `mkdir` — a true **no-replace** primitive; `rename(2)` would silently
  replace an injected empty target directory — then linking the staged files
  in with `link(2)` (also no-replace) and re-reading the published pair to
  verify it byte-matches the verified staged evidence. A lost claim falls
  back to verifying the occupant (idempotence or refusal); a failed signing
  leaves no partial published evidence and the same creation succeeds on
  retry; a crash mid-claim leaves a partial directory that every later load
  and publish refuses fail-closed. The staged pair is read ONCE, the
  signature is verified over exactly those bytes via private scratch copies,
  and the published files are written fresh from the verified bytes, so a
  staging-pathname swap between verification and publication publishes
  nothing. Every load reads the pair once through
  dirfd + `O_NOFOLLOW`,
  refuses non-regular files, hardlinked evidence, foreign owners, and
  group/other-accessible modes, verifies the signature over exactly the bytes
  it parses, and then **fully revalidates** the bindings against the bundle:
  tag object, anchor commit, source-commit ancestry, and source tree are
  re-proven in a fresh clone, and the SBOM subject must equal the recorded
  linux/amd64 manifest. Loads also re-prove REGISTRY content and retained
  evidence: the top/amd64/config bytes are re-fetched and byte-hash-verified
  against the recorded digests, the hash-verified config labels must equal
  the receipted source commit/tree, the attestation manifest and statement
  are re-selected and re-hash-verified with every recorded field compared,
  and the content-addressed statement/SBOM bytes retained under
  `release-sboms/` (published no-replace via link(2)) must byte-match; a
  standalone SPDX document is re-shape-validated and re-bound to the exact
  digest on every load. Recorded digest strings alone never authorize
  anything, on load, idempotent re-create, sign, or allow-list paths. Bundle
  git operations (`list-heads`, restore clones)
  never touch the mutable published pathname: the bundle is read once through
  the anomaly-checked reader, hash-verified, and git consumes a private
  snapshot of exactly those verified bytes, at creation, at load, and in the
  wrapper's release gate. Identity-equal re-creation returns the original bytes
  (`created_at` included); any difference is refused and the original is left
  untouched. Superseding requires explicitly archiving the old receipt
  directory first; the tool never overwrites. Symlinks are refused at every
  path component and the store must resolve inside the run root.
- **Registry content**: the top manifest is byte-hash-verified against the
  reference digest, the linux/amd64 manifest, config, attestation manifest,
  and statement against their descriptors; ambiguous duplicates — two amd64
  attestations, two SPDX predicate layers, two matching statement subjects,
  or repeated SPDX descriptions — are refused.
- **Gate evaluations**: `release-source.json` holds only the latest state for
  tooling; every evaluation, including exceptions, is appended to the
  hash-chained `release-source-history.jsonl` where each record commits to
  its predecessor AND a checkpoint (`…head.json`, rewritten on each append)
  commits to the record count and terminal hash, so earlier-record rewrites
  and **suffix truncation** are both detected — verification also runs before
  every append, failing the gate itself on a tampered history. Exception
  approvers must appear in the reviewed, scoped, expiring
  `release-approvers.json` allow-list (an empty list makes exceptions
  impossible), which is OWNER-SIGNED: the detached
  `release-approvers.json.sig` must verify against the committed release key
  over the exact bytes parsed, so an exception can never authorize its own
  authority mutation — a dirty-added or locally-committed approver fails
  signature verification, no Git ref is consulted, and producing a valid
  signature requires the owner-held private key, which never lives in the
  repository. The pair is tamper-evident, not immutable — WORM/off-host
  anchoring of the log and authenticating the caller as the approver identity
  are owner infrastructure/IAM items.
- **Registry signatures**: cosign appends signatures to a digest's `.sig`
  manifest; existing signatures are never replaced by re-signing.
- **Public inputs and single identities**: the standalone `--sbom` document
  is read exactly once through the same O_NOFOLLOW/fstat-checked reader (the
  parsed bytes are the hashed bytes), and public inputs — the committed
  verification key, standalone SBOMs — are refused when group/other-writable
  (chmod them 0644 after checkout under a group-writable umask; public
  inputs enforce mode bits within 0644 — no group/other write, no execute;
  private evidence enforces mode bits within 0600 EXACTLY, so a 0700
  executable file is refused, not accepted). Every
  evidence read walks EVERY path component from the root dirfd with
  openat(O_NOFOLLOW|O_DIRECTORY): a symlink at any ancestor, an
  other-writable or foreign-group-writable non-sticky ancestor, a
  foreign-owned ancestor, '.'/'..' components, and a device change (mount)
  inside the caller-owned tree are all refused — in provenance.py and in the
  wrapper's anchor-store/bundle reads alike. Every read is pre/post
  fstat-stable: the descriptor is checked again after the bytes are read and
  any size/mtime/ctime/nlink/mode change refuses them, so a same-inode
  overwrite during a multi-chunk read can never yield accepted mixed
  content; private evidence uniformly enforces mode bits within 0600,
  bundles included. Allow-list
  rendering pins ONE verification-key identity: the key bytes are read once
  into a private scratch copy used for every signature check and recorded in
  the ConfigMap annotation, and the recorded inventory hash is computed over
  the exact bytes that were signature-verified and parsed, never a re-read of
  the pathname.

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
- `pods/ephemeralcontainers` IS matched: `kubectl debug` injection is
  admission-governed like any other image, and keeps working with
  digest-pinned images from allow-listed registries (real isolated-kind
  admission tests prove the pinned injection is admitted and
  `evil.invalid/debug:latest` is denied; see tests/test_admission_kind.py).
- Kind boundary evidence (2026-09-16): as the configured cluster-admin
  principal, a direct config-only Pod patch, mutation of the allow-list
  ConfigMap, deletion of the VAP objects, and a Helm release-Secret write
  were all ACCEPTED (the foreign Helm principal was denied). These are
  explicit fail-closed owner/IAM gates — automation-only deploy identity,
  admission protection of the ConfigMap and policy objects, removal of human
  mutation/impersonation rights — and NOTHING in this source tree enforces
  them; do not read this component as claiming otherwise.
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
