# Public-edge execution capsule

Status: source contract only. No launcher was built, package was installed, or
apply was executed while authoring this document.

## Purpose

The capsule makes an accepted release—not a mutable checkout, caller `PATH`,
or command-line digest—the operator authority. It is used for internal-only and
public validation, planning, apply, destroy, status, output, and proxy flows
because deciding the edge mode and reading provider/state inputs must not
happen through an unaccepted binary or provider.

The fixed launcher accepts only these logical source/mode pairs:

- `inference-stack operator`
- `public-edge-verifier receipt-contract`
- `public-edge-verifier local-exec`
- `public-edge-verifier external`
- `edge-client-identity-verifier external`
- `jobset-chart-materializer external`
- `jobset-crd-upgrade local-exec`
- `jobset-release-verifier local-exec`
- `jobset-api-gate local-exec`
- `kueue-materializer local-exec`
- `kueue-admission-gate local-exec`
- `kueue-destroy-cleanup local-exec`

It accepts no source path, expected digest, Python path, Terraform path, or
provider path. It must be installed at
`/usr/local/libexec/fs2-public-edge-gate-launcher` as a static PIE owned by
`root:fs2-public-edge-capsule`, mode `2755`. The capsule group must have no
members. The canonical accepted manifest and bootstrap are fixed
`root:fs2-public-edge-capsule` mode `0440` files. The capsule Python executable
is a fixed mode `0550` file. Parent directories are root-owned and not writable
by group or other.

Each logical name is also fixed to its canonical relative release path in the
bootstrap. A signed manifest cannot relabel some other enrolled release file as
`inference-stack`, the membership verifier, or a Terraform helper.

The set-group-ID transition is deliberate. The launcher refuses when its real
and effective groups are equal, when the effective group is not the fixed
capsule group, or when that group appears in the caller's supplementary group
set. The bootstrap repeats that proof. A caller who directly starts Python and
sets the marker environment cannot acquire the effective group or read the
mode-0440 manifest. Systems mounted `nosuid`, or environments unable to provide
the dedicated no-member group, are unsupported and fail closed.

## Accepted package

Integration constructs a versioned package whose canonical manifest uses
`fs2-serve.nebius.ai/public-edge-execution-capsule/v1`. It binds:

- exact accepted Git commit and tree;
- versioned destination under `/opt/fs2/`;
- launcher and bootstrap SHA-256;
- a sorted, exhaustive path/SHA-256 inventory of every regular release file;
- the `inference-stack` and provider-membership verifier source records;
- exact awk, Bash, cat, crane, date, find, Git, grep, Helm, id, install, jq,
  kubectl, mktemp, Nebius, OpenSSL, Python, realpath, rm, sed, sha256sum, sleep,
  stat, tar, Terraform, timeout, tr, and wc executable records;
- every Terraform provider-mirror file and a CLI configuration that permits
  only that filesystem mirror; and
- a Platform-Security Ed25519 installation receipt over the whole payload.

No symlink is permitted in the installed release. Every parent and regular
file is root-owned, belongs only to root or the capsule group, and is not
group/world writable. At every apply, the bootstrap enumerates the installed
tree and compares the exact file set and every digest before executing source.
It pins accepted source, executable, provider, manifest, launcher, bootstrap,
and sealed Terraform-configuration descriptors. Child processes inherit only
those explicit descriptors. The accepted source replaces apply-time
`--terraform`, `--kubectl`, `--nebius`, and `--crane` values before even the
Terraform version probe.

The proposed source registry
`stages/foundation/trusted-public-edge-capsule-issuers.json` is intentionally
empty. A separate owner-approved commit must enroll exactly one issuer with
role `platform-security-public-edge-capsule`. Integration must install that
exact registry as protected root-owned
`/etc/fs2/public-edge-capsule-issuers.json` and a separately owner-issued,
root-owned `/etc/fs2/public-edge-capsule-acceptance.json`. The acceptance
policy binds the registry digest, accepted commit/tree, manifest digest, and
launcher/bootstrap digests plus the fixed package-verification OpenSSL digest.
The verifier stable-reads the manifest once, hashes those exact parsed bytes,
and invokes only the policy-pinned OpenSSL descriptor. It accepts neither authority path nor
expected digest from its caller. Ordinary apply inputs never select the
registry, policy, package, source, or tools.

## Reviewed build and installation gate

Perform these steps only from an independently accepted exact commit in an
isolated packaging environment. They were not performed for this source
candidate.

1. Build a new output path with
   `stages/foundation/scripts/build-public-edge-capsule-launcher.sh`. The build
   script invokes fixed `/usr/bin/cc`, requests static PIE and hardening flags,
   and refuses to replace an existing output.
2. Assemble the versioned release bundle and complete exhaustive manifest. Do
   not include a symlink, socket, device, secret, credential, state, plan,
   customer payload, or mutable cache.
3. Have Platform Security sign the canonical payload and have the independent
   package owner install the exact issuer registry and acceptance policy at
   their fixed `/etc/fs2` paths. The policy records exact commit/tree,
   manifest, registry, launcher, bootstrap, and package-verification OpenSSL
   digests.
4. Run `verify-public-edge-capsule-install.py` with absolute paths to only the
   bundle, manifest, launcher binary, and bootstrap. It opens the two fixed
   root authorities itself. Its only successful result is
   `VERIFIED_FOR_INSTALL`; an empty issuer registry, absent policy, caller
   substitute, or digest mismatch fails closed.
5. A privileged package owner—not Terraform—creates the no-member group and
   installs the new versioned tree, fixed Python copy, bootstrap, canonical
   manifest, and launcher with the ownership/modes above. The installer must
   first prove every destination is absent or belongs to the recorded previous
   package. Never merge a new bundle into an existing version directory.
6. Record the prior fixed-file package identities and new manifest/launcher
   digests. Switch fixed files atomically only after verification. Preserve the
   prior versioned tree and manifest for rollback; do not delete them in the
   rollout.
7. Invoke the documented `./inference-stack <command> ...` command. The checkout
   immediately re-executes the fixed launcher and injects an absolute default
   tfvars path when needed, so existing run-root, inference, operations,
   support/debugging, and secret-reference behavior remains available.

The package install is an integration action. It is not authorized merely by
this source candidate or by a manifest supplied by an ordinary caller.

## Rollback

Rollback is package selection, not mutation of an accepted version directory.
Restore the recorded prior fixed launcher/bootstrap/Python/manifest package as
one atomic unit, verify their exact recorded digests and modes, then rerun the
normal customer/operator smoke set. Do not combine a launcher or manifest from
one package with source/tools/providers from another.
