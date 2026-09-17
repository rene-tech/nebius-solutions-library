# Public-edge execution capsule

Status: source contract only. No launcher was built, package was installed, or
apply was executed while authoring this document.

## Purpose

The capsule makes an accepted release—not a mutable checkout, caller `PATH`,
or command-line digest—the operator authority. It is used for internal-only and
public validation, planning, apply, destroy, status, output, and proxy flows.
Mutating/provider-aware commands enter brokered-cloud mode. `status`, `output`,
`proxy`, `debug-proxy`, `debug-view`, `debug-export`, `activate-debug`, and
`disable-debug` instead enter an authenticated
local-read-only mode with no Nebius token or refresh descriptor. That local
lane still uses the exact accepted source/tools and owner-protected retained
state. Proxy commands do not accept or receive a kubeconfig: an independently
enrolled root proxy broker retains credential custody, places raw upstream
transports in its private network namespace, and exposes only its scoped host
listener.

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
provider path. One activation unit holds an exact launcher, bootstrap, Python,
and manifest. The root-owned
`/usr/local/libexec/fs2-public-edge-current` symlink selects that unit, so the
entry point is `/usr/local/libexec/fs2-public-edge-current/launcher`. The
launcher is a static PIE owned by `root:fs2-public-edge-capsule`, mode `2755`;
the bootstrap and manifest are mode `0440`, and Python is mode `0550`. The
capsule group has no members. Every activation directory is root-owned mode
`0550`, and parent directories are not writable by group or other. The launcher
opens all four files relative to its already-open activation directory and
proves that the fixed symlink still selects its exact inode. A concurrent
activation can therefore never mix files from two releases.

Before any Python instruction executes, the static launcher hashes the opened
bootstrap and interpreter against digests compiled into the independently
accepted launcher. The interpreter is an ASLR-capable `ET_DYN` static PIE with
no `PT_INTERP`, `DT_NEEDED`, PLT/JMPREL, RPATH/RUNPATH, audit/filter, text
relocations, or external loader/library closure. Its single bounded dynamic
table may describe only the exact reviewed RELA closure: symbol-free relative
relocations and bounded in-image IRELATIVE resolvers. Relocation targets must
be in one non-executable writable load segment; the frozen table, pointer
storage, dynamic table, and relocation table are either read-only or covered
by one exact GNU RELRO range.

The runtime verifier joins a canonical, NUL-terminated module inventory and
payload to the actual CPython `_frozen` table and `PyImport_FrozenModules`
pointer through their exact symbols and relative relocations. Every required
section is file-backed and mapped congruently (`sh_offset - p_offset ==
sh_addr - p_vaddr`) inside one `PT_LOAD`; the inventory, payload, table, and
pointer-storage intervals are pairwise disjoint in both file and virtual
address space. Provenance and the detached review-attestation section are
non-ALLOC, disjoint from all load segments/control tables/other sections. The
normalized whole-artifact digest excludes only that exact attestation range,
avoiding a digest fixed point. A separately installed root-owned Ed25519 review
key authenticates the normalized artifact, actual module closure, and build
provenance before Python starts. Python then starts with `-I -S -B`; a compiled
prelude clears `sys.path`. Missing or unreviewed frozen code fails closed rather
than falling back to host runtime bytes.

This is not a build-selected digest convention. Neither launcher build command
accepts a reviewer key or reviewer-key digest. Runtime verification opens the
fixed `/etc/fs2/public-edge-frozen-runtime-review-key.bin` authority through a
protected root-owned parent chain, requires exact root ownership and mode 0444,
and verifies the detached `FS2ATT1` Ed25519 signature over the normalized whole
artifact plus the inventory, payload, and provenance digests. The separately
installed offline-verifier policy binds the same key digest, verifier,
interpreter, and static OpenSSL identities.

The signature is accepted only after semantic reconstruction. Both independent
verifiers require a sorted, unique, gap-free inventory; hash every declared
payload interval; reject unreviewed trailing payload; join every module name,
code pointer, size, package flag, and terminal row to the actual CPython
`_frozen` table through exact relative relocations; and bind
`PyImport_FrozenModules`, `_PyImport_FrozenBootstrap`,
`_PyImport_FrozenStdlib`, `_PyImport_FrozenTest`, and `PyImport_Inittab` to the
reviewed closure. Because the signed normalized artifact includes those actual
table, symbol, and relocation bytes, a build cannot substitute inert matching
sections while executing a different frozen or builtin authority.

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
`fs2-serve.nebius.ai/public-edge-execution-capsule/v2`. It binds:

- exact accepted Git commit and tree;
- versioned destination under `/opt/fs2/`;
- privileged installer, launcher, and bootstrap SHA-256;
- a sorted, exhaustive path/SHA-256 inventory of every regular release file;
- the `inference-stack` and provider-membership verifier source records;
- exact awk, Bash, cat, crane, date, find, Git, grep, Helm, id, install, jq,
  kubectl, mktemp, Nebius, OpenSSL, Python, realpath, rm, sed, sha256sum, sleep,
  stat, tar, Terraform, timeout, tr, and wc executable records;
- every Terraform provider-mirror file and a CLI configuration that permits
  only that filesystem mirror; and
- a fixed short-lived Nebius-auth broker socket, audience, TTL bounds, and
  source-owned authority registry; and
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

The accepted process never imports a Nebius profile file, caller `HOME`, or an
ambient `NEBIUS_*` secret. For brokered-cloud commands, the fixed root-owned broker socket returns a token
only as a sealed memfd plus an Ed25519 envelope binding exact profile selector,
broker authority/key, service-account subject, project, tenant, endpoint, audience,
commit/tree, manifest digest, request nonce, caller UID/GID, source-enrolled
operator identity, token digest, and issuance/expiry. The broker verifies the
request identity against Unix `SO_PEERCRED`; the bootstrap independently joins
the signed response to its real UID/GID and the authority's per-profile
operator matrix. Enrollment also binds the fixed root-owned broker executable
and config digests, expected peer UID/GID and peer mode, plus a nonzero external
runtime review of the service unit. No `/proc/<root-pid>/exe` read is required.
The source registry
`stages/foundation/trusted-public-edge-auth-broker-authorities.json` is empty
until an owner-approved authority is enrolled, so source alone cannot
authenticate. Terraform reads the token through the inherited
`/proc/self/fd/<n>` path; nested external/local-exec capsule entries request a
fresh envelope from the same broker because Terraform does not promise to
forward arbitrary parent descriptors.
Local-read-only commands deliberately skip the cloud-token broker exchange and
reject any inherited cloud token, auth envelope, or refresh descriptor. They
can inspect only the accepted run root owned by the invoking operator.
In brokered-cloud mode the token descriptor is excluded from the capsule's
global child descriptor set. It is added only when the exact child environment
carries the matching current signed delegated-auth envelope and token text;
signature verifiers, retained-state readers, and other helpers receive neither
the token descriptor nor its environment value. Refresh atomically replaces
the stable descriptor before the next exact authenticated child starts.
`status` and `output` remain entirely local. `proxy`, `debug-proxy`,
`debug-view`, and `debug-export` contact
the separately owned root proxy broker over its enrolled `SO_PEERCRED` Unix
socket, but receive only a signed session lease. They never receive a
Kubernetes credential or raw transport descriptor. The broker's signed lease
must bind an empty raw-host-listener set, its private network namespace inode,
the exact two namespace-local service transports, the ordinary scoped TCP
listener or caller-owned mode-0600 debug Unix socket, caller identity, cluster,
deployment contract, mode, and expiry. It also binds whether isolation was
derived for retained v2 state or embedded as the optional v1 sub-contract.
Enrollment and every signed response bind separate exact executable,
configuration, runtime-review, namespace-policy, and scope-policy digests.
Ordinary access remains the authenticated inference/MCP/admin lane; the
optional debug lane is default-off, exact tenant plus public model plus App,
read-only, and at most seven days. SAI-02 supplies the separate exact 90-day
request-record retention and purge dependency.
`debug-view` and `debug-export --debug-request-id <uuid>` are the supported
non-browser view/export adapters. They keep results on the root-authenticated
control channel and accept only the exact activation App list/detail paths;
each bounded JSON result is signed and binds its nonce, session, operation,
path, expiry, body digest, and audit-event digest. They export neither the
broker's Kubernetes credential nor its backend authorization bearer.
Activation install and disable serialize their complete signed-history
reconstruction, current-grant check, and append under an exclusive kernel lock
on the already-retained mode-0700 run-root directory. No lockfile or evidence
is deleted, replaced, or cleaned, and a competing lifecycle command fails
closed before either process can append a second current grant.
Signed heartbeats are sequence- and expiry-bound. The client converts expiry
to a monotonic deadline, rejects wall/monotonic divergence, and requires a
signed terminal receipt proving all listeners, connections, raw transports,
children, and the private namespace are gone at expiry or caller shutdown.
For debug mode the signed lease also binds the broker's non-exported
`x-fs2-debug-authorization` injection contract. The broker signs and injects
that assertion inside its private namespace on every permitted read. The
control plane verifies the enrolled broker key and exact activation, cluster,
session, expiry, App UUID, public model, tenant, and GET/HEAD method; a missing
or untrusted assertion is denied even on the ordinary admin route. Heartbeats
prove the assertion authority remains current and the signed terminal proves
it was revoked.
The accepted bootstrap retains a separate refresh agent. Every cloud/Terraform
operation requests a fresh lease with at least two hours remaining. Terraform
mutation is capped at 90 minutes. Before Terraform is spawned, the capsule must
obtain a signed `started` receipt from the fixed root-owned mutation-ledger
service. That service authenticates the capsule caller with `SO_PEERCRED`,
appends a hash-chained/WORM record under
`/var/lib/fs2/public-edge-mutation-settlements`, and refuses a conflicting
active predecessor. Successful completion requires a second signed external
`terminal` record referring to the exact started-receipt digest. Caller-owned
run-root files are evidence caches only: deleting them or forging a local
terminal JSON cannot clear the external fence. A crash, SIGKILL, timeout,
interruption, OS error, or nonzero apply therefore leaves the root-owned intent
pending until two-stage reconciliation completes. A root-owned signed external receipt
first proves stage-specific provider settlement, including honest zero-operation
proof for local and Kubernetes/Helm stages. Reconciliation then applies the
refresh-only plan to state, reacquires accepted secret slots only in memory,
requires a full exit-zero no-drift plan, and records exact state/output digests.
Resume requires a second external signature over that append-only evidence;
caller-owned reconciliation files are ignored.

The mutation-settlement authority registry is likewise empty in production
source. An enrolled authority must bind the protected settlement root, exact
project set, Ed25519 key, and a sorted, unique provider matrix. Every provider
entry binds its class (`terraform-local`, `nebius`, or `kubernetes-helm`),
endpoint, observer identity, executable/configuration digests, and independent
runtime-review digest. The same authority binds the mutation-ledger socket,
fixed root-owned executable and configuration paths/digests, peer UID/GID and
`SO_PEERCRED` mode, plus a nonzero service-runtime review. The ledger service
is an external Platform Security component and is not manufactured by this
repository. Until its reviewed implementation, registry enrollment, protected
storage, and signed receipts exist, mutation fails closed before Terraform.

Operator configuration may still name the Grafana, NGC, and NVCR environment
references. Values remain in a separate launcher parent and are returned once,
only for the four exact logical slots, as sealed memfds after accepted
configuration parsing. There is no generic uppercase-variable import;
`AWS_*`, `GITHUB_*`, `OPENAI_*`, loader, Python, Terraform, proxy, `HOME`, and
Nebius credential namespaces are rejected.

The proposed source registry
`stages/foundation/trusted-public-edge-capsule-issuers.json` is intentionally
empty. A separate owner-approved commit must enroll exactly one issuer with
role `platform-security-public-edge-capsule`. Integration must install that
exact registry as protected root-owned
`/etc/fs2/public-edge-capsule-issuers.json` and a separately owner-issued,
root-owned `/etc/fs2/public-edge-capsule-acceptance.json`. The acceptance
policy binds the registry digest, accepted commit/tree, manifest digest, and
installer/launcher/bootstrap digests plus the fixed package-verification
OpenSSL digest. Both signature boundaries require fully static OpenSSL ELF
bytes with neither `PT_INTERP` nor `PT_DYNAMIC`; pinning only an executable
while leaving its loader, libraries, or provider modules mutable is rejected.
The verifier stable-reads the manifest once, hashes those exact parsed bytes,
and invokes only the policy-pinned OpenSSL descriptor. It accepts neither authority path nor
expected digest from its caller. Ordinary apply inputs never select the
registry, policy, package, source, or tools.

## Reviewed build and installation gate

Perform these steps only from an independently accepted exact commit in an
isolated packaging environment. They were not performed for this source
candidate.

1. Build a new output path with exactly four arguments:
   `stages/foundation/scripts/build-public-edge-capsule-launcher.sh /absolute/new-launcher BOOTSTRAP_SHA256 /absolute/static-frozen-python STATIC_FROZEN_PYTHON_SHA256`. The build
   script invokes fixed `/usr/bin/cc`, requests static PIE and hardening flags,
   and refuses to replace an existing output.
   The independently reviewed frozen Python must already carry the canonical
   inventory, payload, actual CPython table, build-provenance, and detached
   attestation sections described above. Both the offline verifier
   `verify-public-edge-frozen-runtime.py` and the runtime header parse that
   closure; a caller-supplied review digest is not a build argument.
   Build the independently installed verifier launcher with the same exact
   runtime contract and four arguments:
   `stages/foundation/scripts/build-public-edge-frozen-runtime-verifier-launcher.sh /absolute/new-verifier-launcher VERIFIER_SOURCE_SHA256 /absolute/static-frozen-verifier-python FROZEN_VERIFIER_PYTHON_SHA256`.
   The script first asks the already accepted fixed verifier to validate those
   exact interpreter bytes, then emits an interpreter-free dependency-closed
   `-static-pie -fPIE` launcher. The installed policy and Python verifier both
   require that launcher to be `ET_DYN` with the bounded reviewed self-RELA
   closure; static `ET_EXEC` is not accepted for this launcher.
2. Assemble the versioned release bundle and complete exhaustive manifest. Do
   not include a symlink, socket, device, secret, credential, state, plan,
   customer payload, or mutable cache.
3. Have Platform Security sign the canonical payload and have the independent
   package owner install the exact issuer registry and acceptance policy at
   their fixed `/etc/fs2` paths. The policy records exact commit/tree,
   manifest, registry, launcher, bootstrap, and package-verification OpenSSL
   digests.
4. The package authority builds and installs the reviewed static C installer
   gate at `/usr/local/sbin/fs2-install-public-edge-capsule`, root-owned mode
   `0555`, using `build-public-edge-capsule-installer-launcher.sh`. Its accepted
   digest is recorded in the manifest/policy. It embeds the digests of mode-0444
   `/usr/local/libexec/fs2-public-edge-installer.py` and the no-`PT_INTERP`,
   hermetic static-PIE frozen-stdlib
   `/usr/local/libexec/fs2-public-edge-installer-python-static`. Build the gate
   with exactly four arguments:
   `stages/foundation/scripts/build-public-edge-capsule-installer-launcher.sh /absolute/new-installer INSTALLER_SOURCE_SHA256 /absolute/static-frozen-python STATIC_FROZEN_PYTHON_SHA256`.
   The same independent static-PIE/frozen-table verifier protects the installer
   runtime. The standalone
   `verify-public-edge-capsule-install.py` remains an audit-only preview; its
   `VERIFIED_FOR_INSTALL` output is never an installation authority.

   ```text
   /usr/local/sbin/fs2-install-public-edge-capsule --bundle /absolute/bundle --manifest /absolute/manifest.json --launcher-binary /absolute/launcher --bootstrap /absolute/bootstrap.py --python /absolute/static-frozen-python3
   ```
5. The privileged installer itself opens the two fixed root authorities. It
   rejects symlink arguments before any resolution, descriptor-walks and pins
   the exhaustive bundle, stable-opens every other input, verifies the signed
   receipt and policy-pinned installer/OpenSSL, and copies only from those same
   retained descriptors. It creates only absent files with `O_EXCL`. Release
   and activation bytes are first written under unique append-only attempt
   directories; a completion journal is written last and the complete payload
   is independently reread before one atomic no-replace rename installs the
   fixed identity. Every file and directory is fsynced in dependency order and
   both parents of cross-directory renames are fsynced. A crash leaves a preserved non-active attempt and a retry
   uses a new attempt. An already installed identity is reused only after an
   exact full-tree verification. It never reopens mutable caller paths for the
   later copy, deletes a partial attempt, or overwrites a destination.
6. After destination digest/mode/owner verification, the installer writes an
   exact install receipt into the activation unit and atomically switches the
   single current symlink with Linux `renameat2`. On upgrade it uses
   `RENAME_EXCHANGE`, then preserves the old symlink under a new
   `fs2-public-edge-previous-*` name. No prior version, activation unit, or
   pointer is deleted or overwritten. Prepared, exchanged, and pointer-preserved
   journals make retry finish or preserve either side of an interrupted exchange
   before the already-active fast path; no candidate or prior pointer is cleaned.
7. Invoke the documented `./inference-stack <command> ...` command. The checkout
   immediately re-executes the fixed launcher and injects an absolute default
   tfvars path when needed, so existing run-root, inference, operations,
   support/debugging, and secret-reference behavior remains available.

The package install is an integration action. It is not authorized merely by
this source candidate or by a manifest supplied by an ordinary caller.

## Rollback

Rollback is package selection, not mutation of an accepted version directory.
The preserved previous pointer and its install receipt identify the complete
prior activation unit. The package authority first restores an acceptance
policy for that exact signed manifest, verifies the retained release and unit,
then atomically exchanges the current pointer as a whole. It never copies
individual fixed files or combines launcher/manifest/Python/bootstrap from
different packages. The normal customer/operator smoke set remains mandatory.
