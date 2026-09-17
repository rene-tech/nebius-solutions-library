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
| Admission: Helm release writes | `policy.yaml` (`fs2-helm-release-governance`) | Secrets of type `helm.sh/release.v1` in `fs2-system` may only be written by the `deploy-principals` recorded in the allow-list ConfigMap; under the decided HELM_DRIVER=sql contract that list is EMPTY and every such write is denied outright | Config-only `kubectl` writes outside admission's matched resources are not gated; Terraform no-drift remains the detective control for those |

**Closure status (owner decisions 2026-09-16, DEFINED in source; live
application happens only at the separately authorized rollout window):** the
owner decided (a) an automation-only, short-lived, non-impersonable release
identity and a DISJOINT security identity (`iam-boundary.yaml` defines both,
with the release Role holding no Secret verbs under the HELM_DRIVER=sql
contract — a FEASIBLE contract: the scope's `helm_secret_writers` list MAY
be empty, an empty list renders an empty deploy-principals key that denies
every helm.sh/release.v1 Secret write outright, and the Helm backend is
OWNER AUTHORITY: scope `helm_storage` pins the driver and the non-secret
DSN identity (host:port/database?user), which the pinned runner REQUIRES
the live environment to match — a caller cannot point enumeration at an
alternate or empty backend, and the DSN reaches the deploy job only as a
file through the one owner-named credential CSI driver, per the committed
deploy-job.example.yaml custody definition), (b) admission protection of
the two parameter ConfigMaps through
`fs2-provenance-guard` (once applied, only the security identity writes
them), and (c) removal of human workload/ConfigMap-mutation/impersonation
rights, which the renderer's read-only IAM audit ENFORCES at every render —
rendering refuses while any non-exempt subject holds a forbidden identity
path, with allowances scoped to each identity's exact function. The policy
OBJECTS themselves are architecturally exempt from in-cluster admission
(anti-lockout), so their non-removability is the external provider/IAM arm:
attested by the ATTESTOR-SIGNED provider attestation and re-checked
detectively by the renderer's live-equality comparison. Until the rollout
window executes the live IAM/boundary application, the shared cluster-admin
reality persists on the cluster and NOTHING in this tree claims live
prevention before then; Terraform's no-drift plan remains the compensating
detective control for config-only drift outside admission's matched
resources.

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
   (`fs2-serve.nebius.ai/release-inventory/v7`): enumerate the current live
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
   `provenance.py render-allowlist --public-key … --key <cosign.key>
   --run-root <run> --inventory inventory.json --scope
   security/image-provenance/release-scope.json --attestation
   <provider-attestation.json> --attestation-key
   security/image-provenance/attestor.pub --registry-prefix …
   --platform-repository-prefix … --deploy-principal
   system:serviceaccount:<ns>:<name>` (`--key` signs the acceptance-chain
   head on success; deploy principals are AUTOMATION ServiceAccounts by
   owner decision — a human username is refused; the attestation verifies
   ONLY against the committed attestor.pub whose fingerprint is
   SOURCE-PINNED as `ATTESTATION_KEY_SHA256` — reviewed code, which the
   owner-signed scope must equal, so a release-key holder can never rotate
   the attestor. The provider arm is then enforced against LIVE
   provider-native answers, not a caller file: the owner-pinned provider
   CLI (scope tooling `nebius`) lists the access bindings of EVERY
   owner-enumerated ancestry level (`provider_parent_ids`) and every
   admin/editor/owner-class binding must name a subject the attestation
   enumerates, and the WORM bucket's live object-lock configuration must
   show immutability enabled with at least the required retention; optional
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
   from Pods AND every workload controller (Deployments, DaemonSets,
   StatefulSets, ReplicaSets, ReplicationControllers, Jobs, CronJobs — a
   scaled-to-zero or crash-looping controller counts with no Pod), the Helm
   rollback window re-derived from `helm list --all` with explicit
   `--offset` pagination (helm caps pages at 256 and `--max 0` is NOT
   unlimited) plus `helm history --max 10000`/`helm get manifest` per
   revision, and frozen bindings verified against their TRUE authority —
   the control plane's PostgreSQL stage bindings
   (`fs2_scientific_batches.state->'adapter_execution'->'stage_bindings'`),
   dumped read-only inside the scope-pinned control-plane workload, never a
   signer-chosen resource list or a swappable ConfigMap; every source's
   refs AND resource identities must equal the observation, the scope's
   `cluster` pins the kube-system namespace UID (server-assigned and
   immutable, unlike the client-editable kubeconfig cluster name), and the
   recorded collector identity must equal the authenticated identity — a
   signer cannot omit an active sibling image, drain a rollback revision the
   history still carries, or self-assert coverage. **Admission scope is
   never caller-chosen**: the committed `release-scope.json` is the single
   authority and it is OWNER-SIGNED — the detached
   `release-scope.json.sig` must verify against the release key over the
   exact bytes parsed, and the key itself is pinned by SHA-256 IN REVIEWED
   SOURCE (`RELEASE_KEY_SHA256`), so a co-located or caller-substituted
   cosign.pub can never verify anything; no Git ref is consulted (local
   tracking refs are writable by any local process via `git update-ref` and
   are NOT authority — the wrapper's release gate likewise only trusts
   branch anchors whose exact tip `git ls-remote` confirms against the real
   remote). The scope ships EMPTY and owner-signed: the signature verifies
   and the EMPTINESS fails closed until the owner populates and re-signs. The scope pins the committed
   admission policy manifest by SHA-256, the binding must select exactly the
   scope's namespaces with a single `In` expression, include `Deny` in its
   validationActions, and fail closed on the exact allow-list ConfigMap —
   and the LIVE ValidatingAdmissionPolicy and binding, fetched through the
   authenticated session, must equal the committed definitions on every
   enforced field — failurePolicy, paramKind/paramRef (selector included),
   matchPolicy, resourceRules AND excludeResourceRules, namespace/object
   selectors, matchConditions, variables, validations, auditAnnotations, and
   validationActions — so a missing, Audit-only, `NotIn`, exclude-all, or
   otherwise narrowed live object refuses rendering. The SECURITY-OWNED
   guard (`fs2-provenance-guard`) must be live and identical too: it
   restricts writes of the allow-list and guard-parameter ConfigMaps to the
   scope's `security_principals` (automation ServiceAccounts, DISJOINT from
   the deploy principals). Admission-configuration objects themselves are
   architecturally exempt from in-cluster admission (see the corrected
   boundary note under residuals): their non-removability is the EXTERNAL
   owner control, while the renderer's live-equality check — which also
   covers the Helm-governance objects and the guard-params CONTENT against
   the owner-signed scope — detects any drift, weakening, or deletion and
   refuses to render. Break-glass (validationActions changes) is a
   PROCEDURAL owner contract: reversible, never a deletion, authorized by an
   owner-signed bounded recovery document (`provenance.py verify-recovery`);
   `render-guard-params` renders the security-owned parameter ConfigMap,
   which the release renderer never emits. The owner-signed scope also PINS
   the frozen-binding surface (`frozen_bindings`): pinned bindings are
   fetched directly at render and must exist, so unlabeling — the label is
   an opt-in marker with no authority — can never silently drop coverage.
   Trust in the Git remote is likewise source-pinned: the wrapper's
   ls-remote verification asks the canonical URL pinned in reviewed source,
   never the locally mutable `remote.origin.url`. Acceptance-chain
   appends are serialized under an exclusive lock with the signature linked
   before the record, so concurrent renders cannot fork a sequence and a
   crash between links is retry-recoverable without deletion. The scope's
   namespaces render into the ConfigMap `namespaces` key and the policy
   denies any request whose namespace is not listed — claimed and enforced
   coverage cannot drift. MindEval is under IDENTICAL gates by owner
   decision: it runs in fs2-system, inside the enforced namespaces, and its
   digests must be receipted, signed, and inventoried like every other
   platform image — no carve-out, no drain. The signed inventory's `scope` must equal it exactly (a
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

## Rollback / break-glass (governed; never a direct patch, never deletion)

Enforcement changes are a SIGNED, bounded, reversible procedure executed by
the security identity — a direct `kubectl patch` is out-of-contract (it is
detected: every allow-list render refuses while live actions differ from the
committed definition, and after the owner IAM closure no other identity holds
the right at all):

1. The owner signs a recovery authorization (schema
   `fs2-serve.nebius.ai/admission-recovery/v2`) naming ONE target binding —
   pinned to the live cluster UID and the target's exact
   UID/resourceVersion/prior actions, single-use —
   exactly one sanctioned state: `[Audit, Warn]` (observe) or `[Deny, Audit]`
   (restore enforcement), bounded to at most 72h and a tracking identifier.
2. `provenance.py verify-recovery --recovery <doc> --public-key
   security/image-provenance/cosign.pub` verifies the signature over the
   exact bytes and prints the annotation value.
3. `provenance.py reconcile-boundary --public-key … --scope … --run-root …
   --recovery <doc>` emits the exact UID/resourceVersion-fenced annotated
   patch and prints the canonical PLAN-SHA256 over the BYTE-BOUND plan (a
   policy apply streams the verified bytes on stdin — the plan pins content,
   never a pathname). Execution is `--execute` PLUS an owner-signed
   `--authorization` document that EMBEDS the full byte-bound plan (argv +
   stdin digests; executed commands come only from the verified signed
   document, never from a local journal) and pins its PLAN-SHA256 and the
   LIVE kube-system UID (single-use via the chained consume ledger). Under
   the exclusive lock the plan is RECOMPUTED, the recovery fences and the
   IAM boundary are re-verified, and the authorization must match the
   recomputed plan exactly — live drift means the owner re-signs. The
   authenticated caller must be a scope security principal; the rollout
   window flag is only an ADDITIONAL guard; identity-path violations refuse
   execution outright. Sequence: journal intent, CONSUME, execute with
   per-entry idempotence (an entry whose target state already holds is
   skipped, so a crash after a successful patch never reruns a stale RV),
   post-check, journal complete keyed by the AUTHORIZATION hash. A crash
   after consume is finished with `--resume`, which requires RE-PRESENTING
   the original signed authorization (re-verified; plan taken from the
   document; stdin bytes from the content-addressed plan-inputs store
   checked against the SIGNED digests; live cluster UID re-checked) — never
   a second authorization and never journal-supplied commands. Ledgers and
   journals are chain-verified with head checkpoints on every read; a
   pre-checkpoint legacy ledger whose chain verifies is ADOPTED forward
   (checkpoint written, nothing deleted), and the append crash window
   (JSONL exactly one verified record ahead of the checkpoint) rolls the
   checkpoint forward — truncation and rewrites still fail closed.
   Recovery documents are additionally LIVE-CLUSTER-BOUND: their pinned
   cluster must equal the live kube-system namespace UID at plan time.

Deletion is never rollback. Existing Pods are never affected by the
policies; only new admissions are.

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
- CORRECTED (independent reviewer adjudication, 2026-09-16 — replaces an
  earlier FALSE claim in this section): Kubernetes admission INTENTIONALLY
  does not evaluate in-cluster admission policies or webhooks on writes or
  deletion of admission-configuration resources (ValidatingAdmissionPolicy,
  its bindings, and webhook configurations) — an anti-lockout design that
  avoids circular dependencies. The earlier kind evidence that VAP deletion
  succeeded as cluster-admin was therefore ARCHITECTURAL, not a missing
  rule, and NO in-cluster VAP or webhook can make the provenance policy
  objects non-removable (a webhook configuration is equally exempt and
  fail-open besides). The NON-REMOVABLE guarantee is exclusively the
  owner-approved EXTERNAL control (decision #4, confirmed 2026-09-16:
  designed and evidenced in source now, EXECUTED only at a separately
  authorized rollout window, reversibly, with no deletion): provider-held
  apiserver/static admission configuration, IAM/RBAC identity-path closure,
  and out-of-band WORM anchoring. The IAM proof must cover the owner's
  COMPLETE identity-path list for humans and the release identity — no
  admissionregistration or protected-parameter write/delete, and none of
  `impersonate`, ServiceAccount TOKEN creation (`serviceaccounts/token`),
  secrets access to stored SA credentials, RBAC `bind`, or RBAC `escalate`
  — because any one of those verbs reaches the security identity
  indirectly and voids the boundary. The security automation consumes a
  SIGNED, BOUNDED handoff (the rendered artifacts plus, for enforcement
  changes, the owner-signed recovery authorization), and recovery is ONLY
  the reversible Audit/Warn <-> Deny toggle — never deletion. In-cluster, the truthful posture is: the guard DENIES
  non-security writes to the two parameter ConfigMaps (ordinary resources
  admission fully evaluates; the ConfigMaps are DERIVED STATE, never
  authority — the renderer verifies live guard-params content against the
  owner-signed scope), and drift or deletion of ANY of the six policy
  objects is DETECTED at every render by the live-equality check
  (image-provenance, Helm-governance, and guard policies + bindings, all
  normalized over every narrowing field WITH API defaulting applied — a
  live GET returns persisted defaults such as `matchPolicy: Equivalent` and
  rule scope `*`, which compare equal to committed YAML that omits them),
  which refuses to render against a missing, weakened, or drifted object,
  and the live guard-params content must equal the owner-signed scope. The
  IDENTITY side of the external boundary is ENFORCED read-only at every
  render — and RE-AUDITED at the end of the render, shrinking the TOCTOU
  window: `_assert_iam_boundary` walks live (Cluster)RoleBindings across the
  scope AND security namespaces and refuses while any non-exempt subject
  holds a forbidden identity path — admission-configuration writes over the
  WILDCARD resource set (every current and future admission kind),
  impersonation of users/groups/serviceaccounts/uids/extras, ServiceAccount
  token minting, RBAC bind/escalate, protected-namespace secrets access,
  pods/exec-attach-ephemeral runtime credential theft, CSR
  create/approve/sign identity minting, and workload/ServiceAccount writes
  in the security identity's namespaces. Exemptions are NOT owner-arbitrary:
  only enumerated Kubernetes bootstrap identities and kube-system controller
  ServiceAccounts are exemptible, Group:system:masters never is, and the
  single bootstrap cluster-admin binding is tolerated only under the
  ATTESTOR-SIGNED provider attestation. Allowances inside the audit are
  FUNCTION-SCOPED, never identity-blanket: the deploy identity is permitted
  exactly the workload-write rule in the scope namespaces and the security
  identity exactly admission-configuration writes (its reconciler
  function); either principal holding any OTHER forbidden verb —
  impersonation, token minting, secret access included — is a violation
  like any other subject. The security principals
  themselves are verified LIVE: each ServiceAccount must exist and carry no
  long-lived token Secret (TokenRequest-only, so "short-lived automation
  identity" is checked against the cluster, not asserted). External
  (provider-console/etcd/node) paths are outside the RBAC surface and stay
  named owner-attestation items. `reconcile-boundary` is the security-owned
  runbook command: it plans re-application of drifted objects, reports
  identity-path violations, prints the canonical PLAN-SHA256, and emits the
  UID/resourceVersion-fenced annotated recovery patch; `--execute` is not an
  environment flag — it requires the caller's AUTHENTICATED identity to be a
  scope security principal AND the authorization's NAMED `executor`
  (rollout-authorization v3 binds execution and resume to ONE
  owner-designated automation identity — never whichever security principal
  shows up), an OWNER-SIGNED single-use rollout authorization
  EMBEDDING the byte-bound plan and pinning its hash and the cluster UID
  (consumed through a chained ledger), the attestor-signed provider
  attestation (whose anchored-heads snapshot and cluster pin are enforced
  UNDER the exclusive lock on every execute and resume;
  `--execute`/`--resume` refuse without it), zero identity-path violations,
  and it writes chained intent/complete journal records with a post-check
  that the applied state equals the authorized intent. RECOVERY SEQUENCING
  is fence-preserving by construction: when a recovery document is
  presented, its target binding is owned EXCLUSIVELY by the
  UID/resourceVersion-fenced patch — the patch comes FIRST in the plan, and
  any repair `kubectl apply` for OTHER drifted objects uses a filtered
  manifest that EXCLUDES the target binding, so the apply can never bump
  the target's resourceVersion and wedge the fence. That makes the
  authorized Audit/Warn -> Deny/Audit RESTORE completable: drift detection
  skips exactly the recovery target (everything else must equal committed),
  the fenced patch transitions it, and the post-check then requires the
  authorized actions plus the authorizing annotation. `--resume` completes
  ONLY a post-consume crash and re-binds everything live under the lock:
  the re-presented signed authorization (its embedded plan digest must
  equal the journaled intent's), the journaled caller (must equal the
  authorization's named executor), the original recovery document (required
  when the plan carries a recovery action; its hash must equal the intent's
  and the signed plan entry's annotation, and it must already be consumed),
  the live cluster UID, and a fresh plan recomputation — resume executes
  the RECOMPUTED still-outstanding entries, each of which must be one the
  owner signed; new live drift never executes under an old authorization,
  and signed entries the live state already satisfies are never replayed.
  A patch entry counts as already satisfied only when the live actions AND
  the recovery-authorization annotation match the signed entry (a
  same-actions state from any other patch is unaccounted drift and is
  re-patched under the pinned fences), and an apply entry is satisfied by
  policy equality itself. The full weaken -> restore -> crash -> resume
  sequence is exercised in the regression suite against a stateful fake API
  server with real resourceVersion-precondition semantics (CI-run; this is
  test evidence for the orchestration logic, not a live-cluster proof). The post-check accepts
  exactly ONE divergence from the committed policy: the recovery target
  carrying the authorized actions plus the authorizing annotation
  (everything else must equal the committed definitions) — so a sanctioned
  Audit/Warn break-glass can COMPLETE its own post-check while any
  unannotated weakening still refuses. Recovery authorizations (schema v2) pin the
  cluster UID, the target object's UID, resourceVersion, and prior actions,
  are single-use, and the emitted patch carries the UID/resourceVersion
  preconditions so the API server itself refuses replay against moved
  state. Every ledger and journal (consumption, publication, reconcile) is
  hash-chained AND head-checkpointed with the chain verified on every read
  and before every append — rewriting, splicing, and suffix truncation all
  fail closed, so a truncated consume ledger can never silently un-consume
  an authorization; `export-anchored-heads` emits the canonical chain-head
  snapshot for the owner's off-host WORM store (EVERY required chain is
  always enumerated, count 0 included) and `verify-anchored-heads` — the
  same enforcement that runs inside every render and every
  execute/resume — fails closed when a required chain is omitted from the
  snapshot, when local chains regress behind the anchored copy
  (whole-store deletion), when equal-length heads diverge (in-place
  rewrite), and when a LONGER local chain's element at the anchored
  position no longer hashes to the anchored head — the anchor must be a
  strict PREFIX, so history rewritten beneath new growth is refused, not
  just counted past. kubectl and helm are OWNER-PINNED absolute
  paths in the signed scope (`tooling`), executed with a from-scratch
  environment (only KUBECONFIG/HOME pass through, and the authenticated
  identity they select is then proven via whoami) — ambient PATH is never a
  trust root. The frozen authority is identity-bound end to end: the signed
  inventory records the authority workload UID + image (digest-pinned
  platform code; a same-name replacement changes the UID), the database
  name, the latest applied migration (version + sha256 from
  fs2_schema_migrations), and the presence of the immutability trigger, and
  every row identity carries a content digest
  (batch/<id>/rev/<n>/<sha12-of-bindings-jsonb>) — all compared against the
  live dump on every render — including the workload resourceVersion, the
  exact resolved pod (name + UID + resourceVersion; the dump execs into
  that pod, never the deployment alias; the pod is re-fetched BY NAME
  afterwards and the WORKLOAD's UID and resourceVersion are re-checked
  post-dump too), the pod's controller ownership chain (Pod -> ReplicaSet
  -> the exact Deployment UID, or StatefulSet -> Pod — copied labels never
  select a foreign pod) and the container that actually runs the
  workload's digest-pinned image, the database
  name/role/search_path/current_schema, the server identity (version
  digest, server_version_num, and the pg_control_system() system
  identifier — a cluster-unique physical identity, not just a version
  string), the latest applied migration, the trigger's
  relation/function/full-DEFINITION digests and enabled state (bound in
  code to the exact fs2_scientific_batches relation and its same-snapshot
  regclass OID), per-row FULL sha256 digests (batch/<id>/rev/<n>/<sha256>),
  and an aggregate rows digest — with EVERY database query executed inside
  one REPEATABLE READ read-only transaction, so identity facts and rows
  always come from a single database snapshot. The
  boundary-definition files in this directory are exactly that —
  DEFINITIONS, consistent with the residual note below: `iam-boundary.yaml`
  carries the Kubernetes-applicable subset (namespace, both automation
  ServiceAccounts with automountServiceAccountToken: false, minimal RBAC in
  which the RELEASE identity holds NO forbidden verb — no Secret access at
  all: Helm state uses HELM_DRIVER=sql and Secret provisioning is a
  separated owner/security duty) which becomes PREVENTIVE only when applied
  at the authorized rollout window, while the PROVIDER-HELD arm
  (system:masters certificate issuance, apiserver/static admission, etcd)
  enters as the ATTESTOR-SIGNED provider attestation (`--attestation` +
  `--attestation-key`, required by every render and by every
  execute/resume): a cluster-pinned, time-bounded document verified ONLY
  against the committed attestor.pub whose fingerprint is SOURCE-PINNED in
  reviewed code (`ATTESTATION_KEY_SHA256`, POPULATED; the owner-signed
  scope must carry the SAME value and it must differ from the release
  verification key — the pipeline cannot attest its own boundary and a
  release-key holder cannot rotate the attestor; the attestor PRIVATE key
  lives outside the release key directory, and moving its custody to the
  security owner is the remaining rollout-window step, stated, not
  claimed). The provider facts are then checked against the provider
  itself: the owner-pinned provider CLI enumerates the LIVE access
  bindings of every scope-enumerated ancestry level (cluster, folder,
  cloud — `provider_parent_ids`, so no level can be omitted) and every
  admin-class binding must name an attestation-enumerated subject
  (conditions never exempt; unparseable or unreachable answers fail
  closed), and the WORM bucket's LIVE object-lock configuration must show
  immutability enabled with at least the required default retention — a
  URI string alone proves nothing. The attestation's worm_store must equal
  the owner scope's `worm_store_uri`, and it embeds the latest off-host
  anchored-heads snapshot (every required chain enumerated; empty/omitted
  chains refuse), which the renderer and the reconciler enforce against
  the local chains with strict PREFIX continuity; checkpoint-less LEGACY
  ledgers are adopted ONLY when that signed anchor confirms their exact
  content, and a zero-count anchor adopts nothing. The shipped
  release-scope remains `scope: null` (fail-closed) until the owner
  ratifies the production values — that population is an owner act this
  tree does not perform. Identity hygiene is verified for BOTH
  automation identities (security and deploy): existence, automount
  disabled on the ServiceAccount AND explicitly on every pod running as
  it, no legacy token Secret, at least one REQUIRED pod-bound projection
  carrying the scope's EXACT `token_audience` with expirationSeconds <=
  3600 (projected tokens are pod-bound by the API server's TokenRequest
  boundObjectRef; direct serviceaccounts/token minting is a forbidden
  identity path for every subject), and NO other credential path on those
  pods — no Secret volumes and no env/envFrom Secret references. The same
  contract is enforced PREVENTIVELY in admission: the fs2-image-provenance
  policy denies automation-identity pods that automount, project a foreign
  audience, or mount Secret material, denies EVERY other pod that
  projects the automation token-audience, and — writer-scoped — denies any
  workload WRITTEN BY an automation identity that runs as a ServiceAccount
  outside the owner-enumerated `workload_service_accounts` ('default' and
  security principals are unenumerable), uses hostPath volumes, privileged
  containers, host network/PID/IPC namespaces, added capabilities or
  privilege escalation, direct nodeName scheduling, Secret volumes or
  Secret env values, or token projections outside the bounded automation
  audience; automation-identity pods may mount ONLY
  projected/configMap/emptyDir/downwardAPI volumes plus the one owner-named
  credential CSI driver. So the deploy identity's workload-create right
  cannot be pivoted into identity-token minting, stored-credential
  exfiltration, arbitrary-ServiceAccount scheduling, host/node access, or
  privileged execution; the security identity's RBAC is equally narrow
  (admission-object writes name-scoped to the three protected objects,
  ConfigMap writes namespaced and name-scoped to the two parameter
  ConfigMaps, no secret/pod/serviceaccount reads). The IAM audit itself
  matches subresource wildcards (`pods/*`, `*/token`) and enumerates
  Roles/RoleBindings in EVERY namespace, so cluster-effect grants (token
  minting, CSR, RBAC mutation, impersonation) in foreign namespaces are
  audited too. The wholesale
  kube-system ServiceAccount GROUP is not exemptible (controllers are
  exempted individually by name), Secret READS AND WRITES in protected
  namespaces are forbidden identity paths (exfiltration and legacy token
  minting), RBAC role/binding MUTATION is a forbidden delegation path,
  workload writes in the scope namespaces are permitted ONLY to the deploy
  identity itself (anyone else creating a pod there could mount the release
  ServiceAccount), and impersonation matching covers named userextras
  subresources. Remote verification is pinned END TO END, allowlist-style: the
  SOURCE-PINNED git binary (`/usr/bin/git`, never a PATH lookup) runs
  outside any repository with an environment built FROM SCRATCH (fixed
  system PATH; every git config source disabled; proxy/CA only from pinned
  constants) — so `url.*.insteadOf` rewrites, PATH/LD_PRELOAD interposition,
  and ambient proxy/TLS variables are all inert. Frozen bindings are
  verified against their TRUE authority: the control plane's PostgreSQL
  state (`fs2_scientific_batches.state->'adapter_execution'->
  'stage_bindings'`, protected by a database immutability trigger). The
  collector executes a READ-ONLY SELECT inside the owner-scope-pinned
  control-plane workload (its own asyncpg + DATABASE_URL; credentials never
  leave the pod) and the signed source's refs and `batch/<id>/rev/<n>`
  identities (now digest-suffixed) must equal the database enumeration
  exactly — ConfigMaps are
  at most a materialization, and a same-name ConfigMap swap changes nothing
  the database did not record. Crash remnants cannot wedge recovery and are
  DISTINGUISHABLE from attacker links: receipt publication writes a chained
  DURABLE journal intent (exact byte hashes + staging path) before the
  mkdir claim, staging is never deleted, hardlinked receipt files are
  accepted ONLY when the journal accounts for their exact bytes, and a
  SIGKILLed publication is ROLLED FORWARD from its own journaled staging on
  the next attempt; content-addressed stores (acceptance heads, SBOM
  evidence, bundles) carry their accounting in their names and signatures,
  and an orphaned head signature is VERIFIED over the deterministic payload
  and adopted rather than re-signed (real ECDSA is randomized).
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
