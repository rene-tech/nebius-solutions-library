#!/usr/bin/env python3
"""Sign, verify, and allow-list fs2-serve platform image digests (SAI-09).

Every digest published for the platform namespaces must be cosign-signed with
the operator release key and recorded in the admission allow-list ConfigMap
before it is deployed. The transparency log is intentionally disabled: image
repositories and digests of a private deployment must not be published to a
public Rekor instance.

Subcommands:
  render-allowlist  Render the fs2-image-provenance-allowlist ConfigMap.
  sign              cosign-sign one or more digest references with a key file.
  verify            cosign-verify one or more digest references with a public key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat as stat_module
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DIGEST_REFERENCE_PATTERN = re.compile(r"^[a-z0-9.\-]+(?::\d+)?/\S+@sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ALLOWLIST_NAME = "fs2-image-provenance-allowlist"
ALLOWLIST_NAMESPACE = "fs2-system"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/release-receipt/v2"
IN_TOTO_STATEMENT_TYPES = (
    "https://in-toto.io/Statement/v0.1",
    "https://in-toto.io/Statement/v1",
)


class ProvenanceError(RuntimeError):
    """A bounded, operator-actionable provenance failure."""


def validate_digest(digest: str) -> str:
    if not DIGEST_PATTERN.match(digest):
        raise ProvenanceError(
            f"not an exact sha256 digest: {digest!r}; expected sha256:<64 hex>"
        )
    return digest


def validate_digest_reference(reference: str) -> str:
    if not DIGEST_REFERENCE_PATTERN.match(reference):
        raise ProvenanceError(
            f"not a digest-pinned image reference: {reference!r}; "
            "expected <registry>/<repository>@sha256:<64 hex>"
        )
    return reference


def validate_registry_prefix(prefix: str) -> str:
    if not prefix or "@" in prefix or " " in prefix or "\n" in prefix:
        raise ProvenanceError(f"invalid registry prefix: {prefix!r}")
    if not prefix.endswith("/"):
        raise ProvenanceError(
            f"registry prefix must end with '/' so it cannot match a longer "
            f"host or repository name by accident: {prefix!r}"
        )
    return prefix


def render_allowlist(
    registry_prefixes: Sequence[str],
    platform_repository_prefix: str,
    platform_digests: Sequence[str],
    helm_secret_writers: Sequence[str] = (),
    namespaces: Sequence[str] = (),
    token_audience: str = "",
    automation_service_accounts: Sequence[str] = (),
    workload_service_accounts: Sequence[str] = (),
    deploy_credential_csi_driver: str = "",
    deploy_credential_spc: str = "",
    workload_csi_drivers: Sequence[str] = (),
) -> dict:
    """Render the admission allow-list ConfigMap consumed by policy.yaml.

    Besides the image provenance keys, the ConfigMap carries the identity
    keys the fs2-image-provenance policy enforces on pods: `token-audience`
    (the ONLY audience a serviceAccountToken projection may carry in the
    scope namespaces on automation pods, and the audience nobody ELSE may
    project), `automation-service-accounts` (`<namespace>:<name>` rows for
    the deploy and security identities, whose pods must run token-hardened),
    and `workload-service-accounts` (the owner-enumerated ServiceAccounts
    that automation-written workloads may run as). The `deploy-principals`
    key names the Helm release-Secret writers and MAY be empty: under the
    HELM_DRIVER=sql contract the owner sets an empty list and the
    fs2-helm-release-governance policy then denies every helm.sh/release.v1
    Secret write outright — the empty list is the STRONGER state, never an
    error.
    """
    if not registry_prefixes:
        raise ProvenanceError("at least one --registry-prefix is required")
    if not platform_digests:
        raise ProvenanceError("at least one --platform-digest is required")
    for principal in helm_secret_writers:
        if not principal.strip() or "\n" in principal:
            raise ProvenanceError(
                f"invalid Helm secret writer principal: {principal!r}"
            )
    prefixes = [validate_registry_prefix(prefix) for prefix in registry_prefixes]
    platform_prefix = validate_registry_prefix(platform_repository_prefix)
    digests = sorted({validate_digest(digest) for digest in platform_digests})
    for namespace in namespaces:
        if not NAMESPACE_PATTERN.match(str(namespace)):
            raise ProvenanceError(f"invalid namespace: {namespace!r}")
    if not TOKEN_AUDIENCE_PATTERN.match(str(token_audience)):
        raise ProvenanceError(
            f"invalid token audience: {token_audience!r}; the admission "
            "token constraints fail closed without the exact scope audience"
        )
    for label, accounts in (
        ("automation", automation_service_accounts),
        ("workload", workload_service_accounts),
    ):
        for account in accounts:
            namespace, _, name = str(account).partition(":")
            if not NAMESPACE_PATTERN.match(
                namespace
            ) or not NAMESPACE_PATTERN.match(name):
                raise ProvenanceError(
                    f"invalid {label} service account row: {account!r} "
                    "(expected <namespace>:<name>)"
                )
        if not accounts:
            raise ProvenanceError(
                f"the {label} service-account list is required; the "
                "admission identity constraints fail closed without it"
            )
    if not re.match(
        r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$",
        str(deploy_credential_csi_driver),
    ):
        raise ProvenanceError(
            "the deploy credential CSI driver name is required; the "
            "automation-pod volume allowlist fails closed without it"
        )
    if not re.match(
        r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$",
        str(deploy_credential_spc),
    ):
        raise ProvenanceError(
            "the deploy credential SecretProviderClass name is required; an "
            "unpinned provider class could serve foreign credential material"
        )
    for driver in workload_csi_drivers:
        if not re.match(
            r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$", str(driver)
        ):
            raise ProvenanceError(
                f"invalid workload CSI driver name: {driver!r}"
            )
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": ALLOWLIST_NAME,
            "namespace": ALLOWLIST_NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "fs2-image-provenance",
                "app.kubernetes.io/part-of": "fs2-serve",
                "security.fs2.nebius.ai/finding": "sai-09",
            },
        },
        "data": {
            "registry-prefixes": "\n".join(prefixes),
            "platform-repository-prefix": platform_prefix,
            "platform-digests": "\n".join(digests),
            "deploy-principals": "\n".join(sorted(set(helm_secret_writers))),
            "namespaces": "\n".join(sorted(set(namespaces))),
            "token-audience": str(token_audience),
            "automation-service-accounts": "\n".join(
                sorted(set(map(str, automation_service_accounts)))
            ),
            "workload-service-accounts": "\n".join(
                sorted(set(map(str, workload_service_accounts)))
            ),
            "deploy-credential-csi-driver": str(deploy_credential_csi_driver),
            "deploy-credential-spc": str(deploy_credential_spc),
            "workload-csi-drivers": "\n".join(
                sorted(set(map(str, workload_csi_drivers)))
            ),
        },
    }


def render_guard_params(security_principals: Sequence[str]) -> dict:
    """Render the security-owned guard parameter ConfigMap.

    This is a SEPARATE artifact from the release allow-list: the guard
    policy's parameters are applied and mutated only by the security
    automation identity, so the identity that deploys releases can never
    edit who guards the controls.
    """
    for principal in security_principals:
        if not AUTOMATION_PRINCIPAL_PATTERN.match(str(principal)):
            raise ProvenanceError(
                f"invalid security principal: {principal!r}"
            )
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": GUARD_PARAMS_NAME,
            "namespace": ALLOWLIST_NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "fs2-image-provenance",
                "app.kubernetes.io/part-of": "fs2-serve",
                "security.fs2.nebius.ai/finding": "sai-09",
            },
        },
        "data": {
            "security-principals": "\n".join(sorted(set(security_principals))),
        },
    }


def receipt_path(run_root: Path, digest: str) -> Path:
    # One directory per digest so the receipt and its signature publish
    # together in a single atomic directory rename.
    return (
        run_root
        / "release-receipts"
        / validate_digest(digest).split(":", 1)[1]
        / "receipt.json"
    )


def _open_evidence_descriptor(path: Path) -> int:
    """Open evidence by walking EVERY path component from the root dirfd.

    Each ancestor is opened with openat(O_NOFOLLOW|O_DIRECTORY) relative to
    its parent's descriptor, so a symlink at ANY component — not only the
    final parent — is refused, as are '.'/'..' components. Every ancestor
    must be a real directory owned by the caller or root and must not be
    writable by others, nor by a group other than the caller's own primary
    group (the user-private-group idiom), unless it is sticky like /tmp.
    Once the walk enters a caller-owned directory, a device change (a mount
    grafted into the evidence tree) is refused. The returned FILE descriptor
    is opened O_NOFOLLOW from the final validated directory descriptor; the
    caller fstat-checks its own invariants on it.
    """
    resolved = path if path.is_absolute() else Path(os.getcwd()) / path
    parts = resolved.parts
    dir_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        device = os.fstat(dir_fd).st_dev
        inside_caller_tree = False
        for component in parts[1:-1]:
            if component in (".", ".."):
                raise ProvenanceError(
                    f"evidence path must not contain '.' or '..': {path}"
                )
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=dir_fd,
                )
            except OSError as error:
                raise ProvenanceError(
                    "cannot open evidence path component safely (symlink, "
                    f"missing, or not a directory): {component!r} in {path}"
                ) from error
            os.close(dir_fd)
            dir_fd = next_fd
            status = os.fstat(dir_fd)
            if not stat_module.S_ISDIR(status.st_mode):
                raise ProvenanceError(
                    f"evidence path component is not a directory: "
                    f"{component!r} in {path}"
                )
            if status.st_uid not in (0, os.getuid()):
                raise ProvenanceError(
                    f"evidence path ancestor has a foreign owner: "
                    f"{component!r} in {path}"
                )
            sticky = bool(status.st_mode & stat_module.S_ISVTX)
            if status.st_mode & 0o002 and not sticky:
                raise ProvenanceError(
                    f"evidence path ancestor is other-writable: "
                    f"{component!r} in {path}"
                )
            if (
                status.st_mode & 0o020
                and not sticky
                and status.st_gid != os.getgid()
            ):
                raise ProvenanceError(
                    "evidence path ancestor is writable by a foreign group: "
                    f"{component!r} in {path}"
                )
            if inside_caller_tree and status.st_dev != device:
                raise ProvenanceError(
                    "evidence path crosses a mount inside the caller-owned "
                    f"tree: {component!r} in {path}"
                )
            device = status.st_dev
            if status.st_uid == os.getuid():
                inside_caller_tree = True
        if parts[-1] in (".", ".."):
            raise ProvenanceError(
                f"evidence path must not contain '.' or '..': {path}"
            )
        try:
            return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as error:
            raise ProvenanceError(
                f"cannot open evidence file safely (symlink or missing): {path}"
            ) from error
    finally:
        os.close(dir_fd)


def _read_evidence_bytes(
    path: Path, private: bool = True, allow_hardlinks: bool = False
) -> bytes:
    """Read evidence via a secure component walk and refuse anomalies.

    Every path component down to the file is opened without following
    symlinks and validated (see _open_evidence_descriptor), and the OPEN
    DESCRIPTOR is fstat-checked: it must be a regular file with link count 1,
    owned by the caller; private evidence must have no group/other access,
    public inputs (e.g. the committed verification key) must at least not be
    group/other writable. The bytes returned are read from that descriptor
    exactly once, so what is verified is what is parsed and hashed.
    """
    fd = _open_evidence_descriptor(path)
    try:
        status = os.fstat(fd)
        if not stat_module.S_ISREG(status.st_mode):
            raise ProvenanceError(f"evidence path is not a regular file: {path}")
        if status.st_nlink != 1 and not allow_hardlinks:
            # Content-addressed/signature-verified stores opt out: their
            # bytes are verified against independent expectations, and a
            # SIGKILL between link(2) and staging cleanup legitimately leaves
            # nlink=2 remnants that may never be deleted and must not wedge
            # recovery.
            raise ProvenanceError(
                f"evidence file has link count {status.st_nlink}: {path}; "
                "hardlinked evidence is refused"
            )
        if status.st_uid != os.getuid():
            raise ProvenanceError(f"evidence file has a foreign owner: {path}")
        permissions = stat_module.S_IMODE(status.st_mode)
        if private and permissions & ~0o600:
            raise ProvenanceError(
                f"evidence file mode {oct(permissions)} exceeds 0600: {path}; "
                "private evidence permits owner read/write only — no group/"
                "other access and no execute bits"
            )
        if not private and permissions & ~0o644:
            raise ProvenanceError(
                f"public input file mode {oct(permissions)} exceeds 0644: "
                f"{path}; no write beyond the owner and no execute bits"
            )
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        # Post-read stability: a same-inode overwrite during a multi-chunk
        # read would yield mixed content that no single version ever had. The
        # SAME descriptor is fstat-checked again and the inode must be
        # byte-for-byte stable across the read, or the bytes are refused.
        after = os.fstat(fd)
        if (
            len(payload) != status.st_size
            or after.st_size != status.st_size
            or after.st_mtime_ns != status.st_mtime_ns
            or after.st_ctime_ns != status.st_ctime_ns
            or after.st_nlink != status.st_nlink
            or after.st_mode != status.st_mode
            or after.st_uid != status.st_uid
        ):
            raise ProvenanceError(
                f"evidence file changed while being read: {path}; torn or "
                "concurrently rewritten evidence is refused"
            )
        return payload
    finally:
        os.close(fd)


# The release verification key is pinned by fingerprint IN REVIEWED SOURCE,
# breaking the circularity of a key that sits next to (and would otherwise
# authenticate) the authority files it verifies: a caller-selected or
# co-located substitute key never verifies anything, because its hash cannot
# equal this constant. Rotating the key is an owner action: commit the new
# cosign.pub AND this constant together through review.
RELEASE_KEY_SHA256 = (
    "56919b309fb65821c8a7d317730ed18613fe9b2a7e52295fce4fffb12c63a208"
)
# SOURCE-PINNED attestor key fingerprint — POPULATED. The provider
# attestation verifies ONLY against the committed attestor.pub whose SHA-256
# equals this reviewed constant — the owner-signed scope must carry the SAME
# value, so a release-key holder can never rotate the attestor by re-signing
# the scope (attestor designation goes through CODE REVIEW, independent of
# the release key). The attestor PRIVATE key lives outside the release
# pipeline's key directory (separate custody path; transferring it to the
# security owner's HSM/workstation is the rollout-window step and rotation
# is a reviewed commit of attestor.pub + this constant together).
ATTESTATION_KEY_SHA256 = (
    "78090e41cde862bc91bc61c1b65041bf113ad5bc641690578ba4184f14bda687"
)
# ORIGINATION of the attestor key, reviewed alongside the fingerprint. The
# committed key above was generated INSIDE the remediation session as a
# BOOTSTRAP PLACEHOLDER: a trust anchor originated by the release/remediation
# path is self-bootstrapped, so it is NOT trusted — every attestation-
# consuming path refuses while this reads "bootstrap-placeholder". Production
# acceptance requires the OWNER to generate and custody the real attestor
# key and to replace attestor.pub, ATTESTATION_KEY_SHA256, and this constant
# (to "owner-originated") together in ONE reviewed commit at ratification.
# The placeholder key itself is never deleted or rotated unilaterally.
ATTESTATION_KEY_PROVENANCE = "bootstrap-placeholder"
RESUME_GRACE_DAYS = 7


class _PinnedPublicKey:
    """One safe read of the verification key, reused for every check.

    The key bytes are read once (O_NOFOLLOW, anomaly-checked) and written to a
    private scratch copy; every cosign invocation and every recorded key hash
    then refer to that single identity, so a mid-run swap of the original file
    cannot make the verified key differ from the recorded one.
    """

    def __init__(
        self, public_key_path: str, expected_sha256: str | None = None
    ) -> None:
        # Default pin: the source-pinned RELEASE key. Callers verifying the
        # SEPARATE attestor key pass the scope-pinned fingerprint instead —
        # there is no unpinned mode.
        self._source = Path(public_key_path)
        self._expected_sha256 = expected_sha256 or RELEASE_KEY_SHA256
        self._holder: tempfile.TemporaryDirectory[str] | None = None
        self.path = ""
        self.sha256 = ""

    def __enter__(self) -> "_PinnedPublicKey":
        key_bytes = _read_evidence_bytes(self._source, private=False)
        self._holder = tempfile.TemporaryDirectory(prefix=".fs2-pubkey-")
        copy = Path(self._holder.name) / "cosign.pub"
        copy.write_bytes(key_bytes)
        copy.chmod(0o600)
        self.path = str(copy)
        self.sha256 = hashlib.sha256(key_bytes).hexdigest()
        if self.sha256 != self._expected_sha256:
            self._holder.cleanup()
            self._holder = None
            raise ProvenanceError(
                f"verification key {self._source} (sha256 {self.sha256}) does "
                "not match the pinned key fingerprint "
                f"{self._expected_sha256}; a substituted key never verifies "
                "anything — rotate keys through review, updating the key "
                "file and the pinned fingerprint together"
            )
        return self

    def __exit__(self, *exc_info) -> None:
        if self._holder is not None:
            self._holder.cleanup()


def _verify_blob_bytes(
    public_key_path: str,
    payload: bytes,
    signature: bytes,
    verifier,
    context: str,
) -> None:
    """Verify a signature over EXACTLY the bytes the caller will parse.

    cosign re-reads files, so the already-read bytes are written to private
    scratch copies and verified there; the caller then parses the same byte
    string it passed in, eliminating any verify/parse divergence.
    """
    run_verifier = verifier or (
        lambda command: subprocess.run(list(command), check=True, capture_output=True)
    )
    with tempfile.TemporaryDirectory(prefix=".fs2-verify-") as scratch:
        payload_copy = Path(scratch) / "payload"
        signature_copy = Path(scratch) / "payload.sig"
        payload_copy.write_bytes(payload)
        payload_copy.chmod(0o600)
        signature_copy.write_bytes(signature)
        signature_copy.chmod(0o600)
        try:
            run_verifier(
                receipt_verify_blob_command(
                    public_key_path, payload_copy, signature_copy
                )
            )
        except subprocess.CalledProcessError as error:
            raise ProvenanceError(
                f"signature verification failed for {context}"
            ) from error


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _run_capture(command: Sequence[str], input_text: str | None = None) -> str:
    result = subprocess.run(
        list(command),
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
    )
    return result.stdout


def _verified_tool_bytes(name: str, spec: dict) -> None:
    """The tool binary's CONTENT is the trust root, not its path.

    The file at the owner-pinned absolute path must be a regular,
    non-symlink, non-world/group-writable file whose exact bytes hash to the
    owner-pinned digest — a repointed or overwritten binary never runs.
    """
    path = Path(str(spec["path"]))
    if path.is_symlink() or not path.is_file():
        raise ProvenanceError(
            f"pinned tool {name} at {path} is not a regular file; refusing"
        )
    status = os.stat(path, follow_symlinks=False)
    if status.st_mode & 0o022:
        raise ProvenanceError(
            f"pinned tool {name} at {path} is group/world-writable; refusing"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != str(spec["sha256"]):
        raise ProvenanceError(
            f"pinned tool {name} at {path} hashes to {digest}, not the "
            f"owner-pinned {spec['sha256']}; a substituted binary never runs"
        )


def _pinned_live_runner(owner_scope: dict):
    """Build the live-API runner from OWNER-PINNED tooling, never ambient.

    kubectl/helm/nebius are resolved from the owner-signed scope's absolute
    paths AND digest-verified against the scope's pinned binary hashes at
    first use. The environment is built from scratch: fixed system PATH,
    HOME pointed at /nonexistent, private freshly-created XDG/Helm state
    directories (nothing ambient under a caller's HOME is consulted), an
    EXPLICIT KUBECONFIG required (no ambient fallback identity), and
    NEBIUS_IAM_TOKEN passed through as the read-only provider credential.

    The Helm storage backend is OWNER AUTHORITY under the fixed SQL
    contract: HELM_DRIVER must be sql and the DSN must parse FULLY
    (anchored, query string included) with its non-secret identity
    (host:port/database?user) equal to the scope pin and every query
    parameter inside the safe allowlist — libpq-style query keys such as
    host/hostaddr/port/dbname/user/options/service, which can redirect or
    reshape the backend, are refused outright.
    """
    tooling = owner_scope["tooling"]
    if not os.environ.get("KUBECONFIG"):
        raise ProvenanceError(
            "KUBECONFIG must be set explicitly; an ambient HOME-derived "
            "identity is never a trust root — fails closed"
        )
    private_state = tempfile.mkdtemp(prefix=".fs2-runner-state-")
    os.chmod(private_state, 0o700)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "KUBECONFIG": os.environ["KUBECONFIG"],
        "XDG_CACHE_HOME": os.path.join(private_state, "cache"),
        "XDG_CONFIG_HOME": os.path.join(private_state, "config"),
        "XDG_DATA_HOME": os.path.join(private_state, "data"),
        "HELM_CACHE_HOME": os.path.join(private_state, "helm-cache"),
        "HELM_CONFIG_HOME": os.path.join(private_state, "helm-config"),
        "HELM_DATA_HOME": os.path.join(private_state, "helm-data"),
    }
    if os.environ.get("NEBIUS_IAM_TOKEN"):
        environment["NEBIUS_IAM_TOKEN"] = os.environ["NEBIUS_IAM_TOKEN"]
    if os.environ.get("HELM_DRIVER") != "sql":
        raise ProvenanceError(
            "the owner-fixed Helm storage contract is the SQL driver, but "
            "HELM_DRIVER is not 'sql'; enumeration against any other "
            "backend silently misreads the release history — fails closed"
        )
    dsn = os.environ.get("HELM_DRIVER_SQL_CONNECTION_STRING", "")
    match = HELM_DSN_PATTERN.match(dsn)
    if match is None:
        raise ProvenanceError(
            "HELM_DRIVER_SQL_CONNECTION_STRING is missing or not a fully "
            "parsable PostgreSQL DSN (anchored, query string included); "
            "the SQL-backed Helm enumeration fails closed"
        )
    query = match.group("query") or ""
    for pair in filter(None, query.split("&")):
        key = pair.partition("=")[0].strip().lower()
        if key not in HELM_DSN_ALLOWED_QUERY_KEYS:
            raise ProvenanceError(
                f"Helm DSN query parameter {key!r} is not in the safe "
                "allowlist; libpq-style overrides (host/hostaddr/port/"
                "dbname/user/options/service/search_path/...) can redirect "
                "or reshape the backend — fails closed"
            )
    live_identity = (
        f"{match.group('host')}:{match.group('port') or '5432'}/"
        f"{match.group('database')}?user={match.group('user')}"
    )
    helm_storage = owner_scope["helm_storage"]
    if live_identity != helm_storage["connection_identity"]:
        raise ProvenanceError(
            f"the live Helm SQL backend {live_identity!r} does not equal "
            "the owner-pinned helm_storage.connection_identity; a "
            "caller-selected alternate backend never serves the release "
            "history — fails closed"
        )
    environment["HELM_DRIVER"] = "sql"
    environment["HELM_DRIVER_SQL_CONNECTION_STRING"] = dsn
    verified_tools: set[str] = set()

    def runner(command: Sequence[str], input_text: str | None = None) -> str:
        argv = list(command)
        if argv and argv[0] in ("kubectl", "helm", "nebius"):
            name = argv[0]
            if name not in verified_tools:
                _verified_tool_bytes(name, tooling[name])
                verified_tools.add(name)
            argv[0] = str(tooling[name]["path"])
        result = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            input=input_text,
            env=environment,
        )
        return result.stdout

    return runner


def _git_capture(repository: Path, *arguments: str) -> str:
    return _run_capture(["git", "-C", str(repository), *arguments])


SPDX_PREDICATE = "https://spdx.dev/Document"
SLSA_PREDICATE_PREFIX = "https://slsa.dev/provenance"
IN_TOTO_MEDIA_TYPE = "application/vnd.in-toto+json"


def _resolve_amd64_image(reference: str, capture) -> tuple[dict, str, str, dict]:
    """Resolve the exact linux/amd64 image manifest, config, and labels.

    Returns (top_manifest, amd64_manifest_digest, config_digest, labels). For a
    multi-platform index the linux/amd64 image manifest must exist exactly
    once; the config blob is fetched, hash-verified against its digest, and
    must itself declare linux/amd64, so the labels provably belong to the
    manifest the SBOM subject names.
    """
    repository = reference.rsplit("@", 1)[0]
    top_text = capture(["crane", "manifest", reference])
    top_sha = hashlib.sha256(top_text.encode("utf-8")).hexdigest()
    if f"sha256:{top_sha}" != reference.rsplit("@", 1)[1]:
        raise ProvenanceError(
            f"fetched top manifest of {reference} hashes to sha256:{top_sha}, "
            "not the reference digest"
        )
    top = json.loads(top_text)
    if "manifests" in top:
        image_entries = [
            entry
            for entry in top.get("manifests", [])
            if entry.get("annotations", {}).get("vnd.docker.reference.type")
            != "attestation-manifest"
        ]
        amd64_entries = [
            entry
            for entry in image_entries
            if entry.get("platform", {}).get("architecture") == "amd64"
            and entry.get("platform", {}).get("os") == "linux"
        ]
        if len(amd64_entries) != 1:
            raise ProvenanceError(
                f"index {reference} must contain exactly one linux/amd64 image "
                f"manifest, found {len(amd64_entries)}"
            )
        amd64_digest = amd64_entries[0]["digest"]
        amd64_text = capture(["crane", "manifest", f"{repository}@{amd64_digest}"])
        amd64_sha = hashlib.sha256(amd64_text.encode("utf-8")).hexdigest()
        if f"sha256:{amd64_sha}" != amd64_digest:
            raise ProvenanceError(
                f"fetched linux/amd64 manifest of {reference} hashes to "
                f"sha256:{amd64_sha}, not its descriptor digest {amd64_digest}"
            )
        amd64_manifest = json.loads(amd64_text)
    else:
        amd64_digest = reference.rsplit("@", 1)[1]
        amd64_manifest = top
    config_digest = (amd64_manifest.get("config") or {}).get("digest", "")
    if not str(config_digest).startswith("sha256:"):
        raise ProvenanceError(
            f"image manifest {amd64_digest} of {reference} has no config digest"
        )
    config_text = capture(["crane", "blob", f"{repository}@{config_digest}"])
    config_sha = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
    if f"sha256:{config_sha}" != config_digest:
        raise ProvenanceError(
            f"fetched config of {reference} hashes to sha256:{config_sha}, "
            f"not its digest {config_digest}"
        )
    config = json.loads(config_text)
    if config.get("architecture") != "amd64" or config.get("os") != "linux":
        raise ProvenanceError(
            f"config {config_digest} of {reference} declares "
            f"{config.get('os')}/{config.get('architecture')}, expected linux/amd64"
        )
    labels = (config.get("config", {}) or {}).get("Labels") or {}
    return top, amd64_digest, config_digest, labels


def _validated_attestation_evidence(
    reference: str, top: dict, amd64_digest: str, capture
) -> dict | None:
    """Validate the BuildKit attestation structure, not just its annotation.

    Selects the attestation whose subject is exactly the linux/amd64 image
    manifest (never merely the first index entry); the fetched attestation
    manifest must carry an in-toto layer with the SPDX predicate, and the
    fetched, hash-verified in-toto statement must name that exact subject.
    """
    repository = reference.rsplit("@", 1)[0]
    attestation_entries = [
        entry
        for entry in top.get("manifests", [])
        if entry.get("annotations", {}).get("vnd.docker.reference.type")
        == "attestation-manifest"
    ]
    if not attestation_entries:
        return None
    matching = [
        entry
        for entry in attestation_entries
        if entry.get("annotations", {}).get("vnd.docker.reference.digest")
        == amd64_digest
    ]
    if not matching:
        raise ProvenanceError(
            f"index {reference} carries attestation manifests, but none whose "
            f"subject is the linux/amd64 image manifest {amd64_digest}"
        )
    if len(matching) > 1:
        raise ProvenanceError(
            f"index {reference} carries {len(matching)} attestation manifests "
            f"for the linux/amd64 image manifest {amd64_digest}; ambiguous "
            "attestations are refused"
        )
    attestation_digest = matching[0]["digest"]
    subject_manifest_digest = amd64_digest
    attestation_text = capture(
        ["crane", "manifest", f"{repository}@{attestation_digest}"]
    )
    attestation_sha = hashlib.sha256(attestation_text.encode("utf-8")).hexdigest()
    if f"sha256:{attestation_sha}" != attestation_digest:
        raise ProvenanceError(
            f"fetched attestation manifest of {reference} hashes to "
            f"sha256:{attestation_sha}, not its descriptor digest "
            f"{attestation_digest}"
        )
    attestation_manifest = json.loads(attestation_text)
    spdx_layers = []
    slsa_layer_digest = None
    for layer in attestation_manifest.get("layers", []):
        if layer.get("mediaType") != IN_TOTO_MEDIA_TYPE:
            continue
        predicate = layer.get("annotations", {}).get("in-toto.io/predicate-type", "")
        if predicate == SPDX_PREDICATE:
            spdx_layers.append(layer.get("digest"))
        elif predicate.startswith(SLSA_PREDICATE_PREFIX) and slsa_layer_digest is None:
            slsa_layer_digest = layer.get("digest")
    if not spdx_layers:
        raise ProvenanceError(
            f"attestation manifest {attestation_digest} of {reference} carries "
            f"no {IN_TOTO_MEDIA_TYPE} layer with predicate {SPDX_PREDICATE}"
        )
    if len(spdx_layers) > 1:
        raise ProvenanceError(
            f"attestation manifest {attestation_digest} of {reference} carries "
            f"{len(spdx_layers)} SPDX predicate layers; ambiguous attestations "
            "are refused"
        )
    spdx_layer_digest = spdx_layers[0]
    # The layer annotation alone proves nothing: fetch the blob, prove it is
    # the content the layer digest names, and validate it as a real in-toto
    # Statement carrying an SPDX document about this exact image.
    statement_text = capture(["crane", "blob", f"{repository}@{spdx_layer_digest}"])
    statement_sha256 = hashlib.sha256(statement_text.encode("utf-8")).hexdigest()
    if f"sha256:{statement_sha256}" != spdx_layer_digest:
        raise ProvenanceError(
            f"fetched SPDX statement of {reference} hashes to "
            f"sha256:{statement_sha256}, not the layer digest {spdx_layer_digest}"
        )
    try:
        statement = json.loads(statement_text)
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"SPDX attestation layer of {reference} is not valid JSON"
        ) from error
    if not isinstance(statement, dict) or statement.get("_type") not in IN_TOTO_STATEMENT_TYPES:
        found = statement.get("_type") if isinstance(statement, dict) else type(statement).__name__
        raise ProvenanceError(
            f"SPDX attestation layer of {reference} is not an in-toto "
            f"Statement (got _type={found!r})"
        )
    if statement.get("predicateType") != SPDX_PREDICATE:
        raise ProvenanceError(
            f"attestation statement of {reference} carries predicateType "
            f"{statement.get('predicateType')!r}, expected {SPDX_PREDICATE}"
        )
    subject_hex = subject_manifest_digest.split(":", 1)[1]
    matching_subjects = [
        subject
        for subject in statement.get("subject", []) or []
        if isinstance(subject, dict)
        and (subject.get("digest") or {}).get("sha256") == subject_hex
    ]
    if not matching_subjects:
        raise ProvenanceError(
            f"SPDX attestation of {reference} does not name the image manifest "
            f"{subject_manifest_digest} as a subject"
        )
    if len(matching_subjects) > 1:
        raise ProvenanceError(
            f"SPDX attestation of {reference} names the image manifest "
            f"{subject_manifest_digest} as {len(matching_subjects)} subjects; "
            "ambiguous subjects are refused"
        )
    if not str(matching_subjects[0].get("name", "")).strip():
        raise ProvenanceError(
            f"SPDX attestation subject for {reference} has no name"
        )
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict) or not predicate:
        raise ProvenanceError(
            f"SPDX attestation of {reference} has an empty or non-object predicate"
        )
    _validate_spdx_shape(predicate, f"SPDX predicate of {reference}")
    return {
        "attestation_manifest_digest": attestation_digest,
        "subject_manifest_digest": subject_manifest_digest,
        "spdx_layer_digest": spdx_layer_digest,
        "statement_sha256": statement_sha256,
        "slsa_layer_digest": slsa_layer_digest,
        "spdx_sha256": None,
        "spdx_subject_digest": None,
        "_evidence_bytes": statement_text.encode("utf-8"),
    }


SPDXID_PATTERN = re.compile(r"^SPDXRef-[A-Za-z0-9.\-]+$")


def _validate_spdx_shape(document: dict, context: str) -> list[dict]:
    """Require a real SPDX 2.x document shape and return the described packages.

    Beyond field presence this enforces valid and unique package SPDXIDs, and
    that the document describes (via `documentDescribes` or a
    SPDXRef-DOCUMENT → package DESCRIBES relationship) only packages that
    actually exist in the document.
    """
    if str(document.get("spdxVersion", "")) not in ("SPDX-2.2", "SPDX-2.3"):
        raise ProvenanceError(
            f"{context} has unsupported spdxVersion {document.get('spdxVersion')!r}"
        )
    if document.get("SPDXID") != "SPDXRef-DOCUMENT":
        raise ProvenanceError(f"{context} lacks SPDXID SPDXRef-DOCUMENT")
    if document.get("dataLicense") != "CC0-1.0":
        raise ProvenanceError(f"{context} lacks the required dataLicense CC0-1.0")
    if not str(document.get("name", "")).strip():
        raise ProvenanceError(f"{context} lacks a document name")
    if not str(document.get("documentNamespace", "")).strip():
        raise ProvenanceError(f"{context} lacks a documentNamespace")
    creation = document.get("creationInfo")
    if (
        not isinstance(creation, dict)
        or not str(creation.get("created", "")).strip()
        or not isinstance(creation.get("creators"), list)
        or not creation["creators"]
    ):
        raise ProvenanceError(
            f"{context} lacks creationInfo with created and creators"
        )
    packages = document.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ProvenanceError(f"{context} describes no packages")
    package_ids: dict[str, dict] = {}
    for package in packages:
        if not isinstance(package, dict):
            raise ProvenanceError(f"{context} contains a non-object package")
        spdx_id = str(package.get("SPDXID", ""))
        if not SPDXID_PATTERN.match(spdx_id):
            raise ProvenanceError(
                f"{context} contains a package with invalid SPDXID {spdx_id!r}"
            )
        if spdx_id in package_ids:
            raise ProvenanceError(
                f"{context} contains duplicate package SPDXID {spdx_id}"
            )
        if not str(package.get("name", "")).strip():
            raise ProvenanceError(
                f"{context} package {spdx_id} has no name"
            )
        if not str(package.get("downloadLocation", "")).strip():
            raise ProvenanceError(
                f"{context} package {spdx_id} has no downloadLocation"
            )
        package_ids[spdx_id] = package
    described_ids = [
        str(identifier) for identifier in document.get("documentDescribes") or []
    ]
    for relationship in document.get("relationships", []) or []:
        if (
            isinstance(relationship, dict)
            and relationship.get("relationshipType") == "DESCRIBES"
            and relationship.get("spdxElementId") == "SPDXRef-DOCUMENT"
        ):
            described_ids.append(str(relationship.get("relatedSpdxElement", "")))
    if not described_ids:
        raise ProvenanceError(
            f"{context} has no SPDXRef-DOCUMENT DESCRIBES relationship or "
            "documentDescribes entry"
        )
    if len(described_ids) != len(set(described_ids)):
        raise ProvenanceError(
            f"{context} describes the same element more than once; ambiguous "
            "descriptions are refused"
        )
    described_packages = []
    for identifier in described_ids:
        if identifier not in package_ids:
            raise ProvenanceError(
                f"{context} describes {identifier}, which is not a package in "
                "the document"
            )
        described_packages.append(package_ids[identifier])
    return described_packages


def _package_names_exact_digest(package: dict, digest_hex: str) -> bool:
    """Exact identity only: a SHA256 checksum value or a purl version part.

    Substring matches anywhere else are deliberately rejected; the digest must
    appear as the package's declared SHA256 checksum or as the exact
    `@sha256:<hex>` version of a defined external subject locator.
    """
    for checksum in package.get("checksums") or []:
        if (
            isinstance(checksum, dict)
            and checksum.get("algorithm") == "SHA256"
            and checksum.get("checksumValue") == digest_hex
        ):
            return True
    for external in package.get("externalRefs") or []:
        if not isinstance(external, dict):
            continue
        if external.get("referenceType") not in ("purl", "locator"):
            continue
        locator = str(external.get("referenceLocator", ""))
        if "@" not in locator:
            continue
        version = locator.rsplit("@", 1)[1].split("?", 1)[0]
        if version == f"sha256:{digest_hex}":
            return True
    return False


def _validated_spdx_document(digest: str, sbom_path: Path) -> dict:
    """Parse, shape-check, and exactly subject-bind a standalone SPDX document.

    `digest` is the exact linux/amd64 RUNTIME manifest digest: for a
    multi-platform image the document must name the manifest that actually
    runs, never the top index, whose digest also covers foreign platforms.

    The document is read exactly once through the anomaly-checked O_NOFOLLOW
    reader; the SAME byte string is parsed and hashed into the receipt, so a
    pathname swap between parsing and hashing cannot record a hash for bytes
    that were never validated.
    """
    sbom_bytes = _read_evidence_bytes(sbom_path, private=False)
    try:
        document = json.loads(sbom_bytes)
    except json.JSONDecodeError as error:
        raise ProvenanceError(f"unreadable SPDX document: {sbom_path}") from error
    if not isinstance(document, dict) or not str(
        document.get("spdxVersion", "")
    ).startswith("SPDX-"):
        raise ProvenanceError(f"{sbom_path} is not an SPDX JSON document")
    described = _validate_spdx_shape(document, f"SPDX document {sbom_path}")
    digest_hex = digest.split(":", 1)[1]
    if not any(
        _package_names_exact_digest(package, digest_hex) for package in described
    ):
        raise ProvenanceError(
            f"SPDX document {sbom_path} does not bind the image digest "
            f"{digest} as an exact SHA256 checksum or subject locator on a "
            "described package; substring mentions do not count"
        )
    return {
        "attestation_manifest_digest": None,
        "subject_manifest_digest": None,
        "spdx_layer_digest": None,
        "statement_sha256": None,
        "slsa_layer_digest": None,
        "spdx_sha256": hashlib.sha256(sbom_bytes).hexdigest(),
        "spdx_subject_digest": digest,
        "_evidence_bytes": sbom_bytes,
    }


def _read_anchor_store_strict(run_root: Path) -> dict:
    """Read the anchor store fail-closed: a malformed store is a refusal."""
    anchors_file = run_root / "release-anchors.json"
    if anchors_file.is_symlink():
        raise ProvenanceError(f"anchor store must not be a symlink: {anchors_file}")
    if not anchors_file.is_file():
        raise ProvenanceError(f"missing release anchor store: {anchors_file}")
    try:
        anchors = json.loads(_read_evidence_bytes(anchors_file))
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"release anchor store is malformed and fails closed: {anchors_file}"
        ) from error
    if not isinstance(anchors, dict):
        raise ProvenanceError(
            f"release anchor store is malformed and fails closed: {anchors_file}"
        )
    return anchors


@contextmanager
def _verified_bundle_snapshot(
    run_root: Path, bundle_path: Path, expected_sha256: str, context: str
) -> Iterator[Path]:
    """Yield a private snapshot of the bundle bound to its verified bytes.

    The bundle is read exactly once through the anomaly-checked reader and
    hash-verified; every subsequent git operation (list-heads, clone) runs
    against a private copy of those verified bytes, so a same-user pathname
    swap between hashing and git use cannot make the verified bytes differ
    from the bytes git consumes.
    """
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise ProvenanceError(
            f"anchor bundle for {context} is missing or a symlink: {bundle_path}"
        )
    bundle_bytes = _read_evidence_bytes(bundle_path, allow_hardlinks=True)
    if hashlib.sha256(bundle_bytes).hexdigest() != expected_sha256:
        raise ProvenanceError(
            f"anchor bundle for {context} no longer matches its recorded "
            f"SHA-256: {bundle_path}"
        )
    with tempfile.TemporaryDirectory(dir=run_root, prefix=".bundle-snap-") as scratch:
        snapshot = Path(scratch) / "bundle-snapshot"
        snapshot.write_bytes(bundle_bytes)
        snapshot.chmod(0o600)
        yield snapshot


def _retained_sbom_path(run_root: Path, sha256_hex: str, suffix: str) -> Path:
    return run_root / "release-sboms" / f"{sha256_hex}{suffix}"


def _retain_sbom_evidence(
    run_root: Path, sha256_hex: str, payload: bytes, suffix: str
) -> Path:
    """Retain content-addressed SBOM/statement bytes, no-replace, verified.

    The bytes are staged 0600 on the same filesystem, fsynced, published with
    link(2) (no-replace: a pre-existing file is never overwritten), and the
    published winner is re-read through the anomaly-checked reader and must
    byte-match both its content address and the payload.
    """
    directory = run_root / "release-sboms"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ProvenanceError(f"SBOM evidence store is a symlink: {directory}")
    final = _retained_sbom_path(run_root, sha256_hex, suffix)
    with tempfile.TemporaryDirectory(dir=directory) as staging:
        candidate = Path(staging) / "candidate"
        candidate.write_bytes(payload)
        candidate.chmod(0o600)
        _fsync_file(candidate)
        try:
            os.link(candidate, final)
        except FileExistsError:
            pass
        else:
            _fsync_dir(directory)
    published = _read_evidence_bytes(final, allow_hardlinks=True)
    if (
        hashlib.sha256(published).hexdigest() != sha256_hex
        or published != payload
    ):
        raise ProvenanceError(
            f"retained SBOM evidence at {final} does not match its content "
            "address; investigate the tamper before trusting or replacing it"
        )
    return final


def _reproven_sbom_evidence(
    run_root: Path, sha256_hex: str, suffix: str, context: str
) -> bytes:
    """Read retained content-addressed SBOM evidence and re-prove its hash."""
    retained = _retained_sbom_path(run_root, sha256_hex, suffix)
    if not retained.is_file() or retained.is_symlink():
        raise ProvenanceError(
            f"retained SBOM evidence for {context} is missing: {retained}; a "
            "receipt without its content-addressed evidence authorizes nothing"
        )
    payload = _read_evidence_bytes(retained, allow_hardlinks=True)
    if hashlib.sha256(payload).hexdigest() != sha256_hex:
        raise ProvenanceError(
            f"retained SBOM evidence for {context} no longer matches its "
            f"content address: {retained}"
        )
    return payload


def create_release_receipt(
    reference: str,
    run_root: Path,
    repository: Path,
    anchor_tag: str,
    key_path: str,
    public_key_path: str,
    sbom_path: Path | None = None,
    capture=_run_capture,
) -> dict:
    """Bind an image digest to its source, durable anchor, and SBOM evidence.

    The chain is content-addressed end to end: the signed image digest covers
    the exact linux/amd64 manifest whose hash-verified config carries the
    source commit/tree labels; the commit must be reachable from the durable
    anchor tag — whose current annotated tag OBJECT must equal the recorded
    target and be present in the verified bundle — and the label tree must
    equal the Git tree of the label commit inside a clone restored from that
    bundle; SBOM evidence must name this exact manifest as its subject. Deep
    checks run here; the receipt and its verified cosign signature are then
    published together through a no-replace mkdir claim plus link(2), so
    existing evidence at the published path is never replaced and a partial
    claim fails closed on every later load.
    """
    reference = validate_digest_reference(reference)
    digest = reference.rsplit("@", 1)[1]
    if not anchor_tag.startswith("refs/tags/"):
        anchor_tag = f"refs/tags/{anchor_tag}"
    anchors = _read_anchor_store_strict(run_root)
    anchor = anchors.get(anchor_tag)
    if not isinstance(anchor, dict) or not anchor.get("restore_tested"):
        raise ProvenanceError(
            f"anchor {anchor_tag} has no restore-tested bundle receipt; run "
            "inference-stack anchor-release first"
        )
    bundle = Path(str(anchor.get("bundle_path", "")))
    tag_target = _git_capture(repository, "rev-parse", anchor_tag).strip()
    if _git_capture(repository, "cat-file", "-t", tag_target).strip() != "tag":
        raise ProvenanceError(
            f"anchor {anchor_tag} is not an annotated tag object; lightweight "
            "tags are never receipted"
        )
    anchor_commit = _git_capture(
        repository, "rev-parse", f"{anchor_tag}^{{commit}}"
    ).strip()
    # The exact annotated tag OBJECT must be unchanged, not merely its peeled
    # commit: a deleted-and-recreated tag at the same commit is a different
    # object and is refused before anything is signed.
    if tag_target != anchor.get("tag_target"):
        raise ProvenanceError(
            f"anchor {anchor_tag} tag object changed: recorded "
            f"{anchor.get('tag_target')}, the tag now resolves to {tag_target}; "
            "a recreated tag is never receipted — anchor a new tag"
        )
    if anchor_commit != anchor.get("commit"):
        raise ProvenanceError(
            f"anchor {anchor_tag} moved: bundle receipt binds {anchor.get('commit')}, "
            f"the tag now resolves to {anchor_commit}"
        )
    top, amd64_digest, config_digest, labels = _resolve_amd64_image(
        reference, capture
    )
    source_commit = labels.get("org.opencontainers.image.revision", "")
    source_tree = labels.get("ai.nebius.fs2-serve.source-tree", "")
    if not COMMIT_PATTERN.match(source_commit):
        raise ProvenanceError(
            f"image {reference} does not carry an exact 40-hex "
            "org.opencontainers.image.revision label; rebuild it from an "
            "anchored commit before receipting"
        )
    if not COMMIT_PATTERN.match(source_tree):
        raise ProvenanceError(
            f"image {reference} does not carry an exact 40-hex "
            "ai.nebius.fs2-serve.source-tree label; rebuild it with the "
            "release tooling before receipting"
        )
    # Prove commit/tree/anchor ancestry against a fresh clone restored from
    # the durable bundle itself, so the recovery evidence is self-contained
    # rather than trusting the current working repository's object store. Both
    # git operations run against one private snapshot of the hash-verified
    # bundle bytes, never against the mutable published pathname.
    with (
        _verified_bundle_snapshot(
            run_root, bundle, str(anchor.get("sha256", "")), reference
        ) as snapshot,
        tempfile.TemporaryDirectory(dir=run_root) as scratch,
    ):
        bundle_heads = _run_capture(
            ["git", "bundle", "list-heads", str(snapshot)]
        )
        if f"{tag_target} {anchor_tag}" not in {
            line.strip() for line in bundle_heads.splitlines()
        }:
            raise ProvenanceError(
                f"verified bundle {bundle} does not carry {anchor_tag} at tag "
                f"object {tag_target}"
            )
        restore = Path(scratch) / "restore.git"
        _run_capture(
            ["git", "clone", "--quiet", "--bare", str(snapshot), str(restore)]
        )
        restored_tag_target = _git_capture(restore, "rev-parse", anchor_tag).strip()
        if restored_tag_target != tag_target:
            raise ProvenanceError(
                f"bundle restore resolves {anchor_tag} to tag object "
                f"{restored_tag_target}, expected {tag_target}"
            )
        restored_anchor_commit = _git_capture(
            restore, "rev-parse", f"{anchor_tag}^{{commit}}"
        ).strip()
        if restored_anchor_commit != anchor_commit:
            raise ProvenanceError(
                f"bundle restore resolves {anchor_tag} to "
                f"{restored_anchor_commit}, expected {anchor_commit}"
            )
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(restore),
                "merge-base",
                "--is-ancestor",
                source_commit,
                restored_anchor_commit,
            ],
            capture_output=True,
            text=True,
        )
        if ancestry.returncode != 0:
            raise ProvenanceError(
                f"image source commit {source_commit} is not reachable from "
                f"anchor {anchor_tag} ({anchor_commit}) in the restored "
                "bundle; anchor the lineage first"
            )
        restored_tree = _git_capture(
            restore, "rev-parse", f"{source_commit}^{{tree}}"
        ).strip()
    if source_tree != restored_tree:
        raise ProvenanceError(
            f"image {reference} source-tree label {source_tree} does not match "
            f"the Git tree {restored_tree} of its revision in the restored bundle"
        )

    sbom_evidence = _validated_attestation_evidence(
        reference, top, amd64_digest, capture
    )
    if sbom_evidence is None and sbom_path is not None:
        # The runtime identity is the exact linux/amd64 manifest, not the
        # multi-platform index: a standalone document must name IT.
        sbom_evidence = _validated_spdx_document(amd64_digest, Path(sbom_path))
    if sbom_evidence is None:
        raise ProvenanceError(
            f"image {reference} carries no validated in-toto SPDX attestation "
            "and no --sbom document was provided; generate an SPDX SBOM first"
        )
    evidence_bytes = sbom_evidence.pop("_evidence_bytes")
    if sbom_evidence.get("statement_sha256"):
        _retain_sbom_evidence(
            run_root, sbom_evidence["statement_sha256"], evidence_bytes,
            ".intoto.json",
        )
    else:
        _retain_sbom_evidence(
            run_root, sbom_evidence["spdx_sha256"], evidence_bytes, ".spdx.json"
        )

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "image": reference,
        "digest": digest,
        "image_manifest": {
            "amd64_manifest_digest": amd64_digest,
            "config_digest": config_digest,
        },
        "source": {"commit": source_commit, "tree": source_tree},
        "anchor": {
            "mode": "bundle",
            "tag": anchor_tag,
            "tag_target": tag_target,
            "commit": anchor_commit,
            "restored_commit": anchor_commit,
            "bundle_path": str(bundle),
            "bundle_sha256": anchor["sha256"],
        },
        "sbom": sbom_evidence,
    }
    return _publish_receipt(
        run_root, reference, receipt, key_path, public_key_path, capture
    )


def _existing_receipt_or_conflict(
    run_root: Path,
    final_dir: Path,
    reference: str,
    receipt: dict,
    public_key_path: str,
    capture,
) -> dict:
    """Idempotence with full revalidation: bytes equal or refuse.

    The existing signature is cryptographically re-verified over the exact
    read bytes — never trusted on existence — and the recorded bindings are
    revalidated before the identity comparison decides between idempotent
    return and refusal.
    """
    path = final_dir / "receipt.json"
    signature = final_dir / "receipt.json.sig"
    if (
        path.is_symlink()
        or signature.is_symlink()
        or not path.is_file()
        or not signature.is_file()
    ):
        raise ProvenanceError(
            f"partial receipt evidence already exists for {reference} at "
            f"{final_dir}; refusing to overwrite — restore or archive the "
            "existing evidence first"
        )
    receipt_bytes = _read_receipt_evidence(run_root, path)
    signature_bytes = _read_receipt_evidence(run_root, signature)
    try:
        _verify_blob_bytes(
            public_key_path,
            receipt_bytes,
            signature_bytes,
            lambda command: capture(command),
            f"existing receipt of {reference}",
        )
    except ProvenanceError as error:
        raise ProvenanceError(
            f"existing receipt signature for {reference} fails verification; "
            "receipts are immutable — refusing to overwrite; investigate the "
            "tamper"
        ) from error
    try:
        existing = json.loads(receipt_bytes)
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"existing receipt for {reference} is unreadable; receipts are "
            "immutable — refusing to overwrite"
        ) from error
    validate_receipt_binding(existing, reference, run_root, capture)
    comparable_existing = {k: v for k, v in existing.items() if k != "created_at"}
    comparable_new = {k: v for k, v in receipt.items() if k != "created_at"}
    if comparable_existing != comparable_new:
        raise ProvenanceError(
            f"a different release receipt already exists for {reference} at "
            f"{path}; receipts are immutable — investigate the conflict and, "
            "only if superseding is intended, archive the old receipt and "
            "signature before creating a new one"
        )
    return existing


def _roll_forward_partial_publication(
    run_root: Path, final_dir: Path, digest: str
) -> bool:
    """Complete a SIGKILLed publication from ITS OWN journaled staging.

    Retry bytes can never equal the crashed attempt's bytes (the receipt
    carries created_at and real ECDSA signatures are randomized), so a
    partial claim is rolled forward from the DURABLE staging directory the
    journal intent recorded for this digest: the staged artifacts are read
    back, verified against the journaled hashes, required to byte-match any
    piece already published, and the missing links are completed (signature
    first). Returns True when final_dir now holds the completed OLD
    publication; the caller then runs the ordinary idempotence/conflict
    verification against it. Nothing is ever deleted or replaced.
    """
    if final_dir.is_symlink() or not final_dir.is_dir():
        return False
    journal = _publication_journal_path(run_root)
    intent: dict | None = None
    completed: set[str] = set()
    for record in _read_chained_records(journal):
        if record.get("digest") != digest:
            continue
        if record.get("phase") == "intent":
            intent = record
        elif record.get("phase") == "complete":
            completed.add(str(record.get("receipt_sha256")))
    if intent is None or str(intent.get("receipt_sha256")) in completed:
        return False
    staging = final_dir.parent / ".staging" / str(intent.get("staging", ""))
    receipt_source = staging / "publish.json"
    signature_source = staging / "publish.json.sig"
    if (
        not staging.is_dir()
        or staging.is_symlink()
        or not receipt_source.is_file()
        or not signature_source.is_file()
    ):
        return False
    receipt_bytes = _read_evidence_bytes(receipt_source, allow_hardlinks=True)
    signature_bytes = _read_evidence_bytes(
        signature_source, allow_hardlinks=True
    )
    if (
        hashlib.sha256(receipt_bytes).hexdigest() != intent.get("receipt_sha256")
        or hashlib.sha256(signature_bytes).hexdigest()
        != intent.get("signature_sha256")
    ):
        return False
    receipt_file = final_dir / "receipt.json"
    signature_file = final_dir / "receipt.json.sig"
    for existing, expected in (
        (receipt_file, receipt_bytes),
        (signature_file, signature_bytes),
    ):
        if existing.is_symlink():
            return False
        if existing.exists() and _read_evidence_bytes(
            existing, allow_hardlinks=True
        ) != expected:
            return False
    _link_no_replace_or_adopt(signature_source, signature_file, signature_bytes)
    _link_no_replace_or_adopt(receipt_source, receipt_file, receipt_bytes)
    _fsync_dir(final_dir)
    _append_chained_record(
        journal,
        {
            "phase": "complete",
            "digest": digest,
            "receipt_sha256": intent.get("receipt_sha256"),
            "rolled_forward": True,
            "at": datetime.now(UTC).isoformat(),
        },
    )
    return True


def _publication_journal_path(run_root: Path) -> Path:
    return run_root / "release-receipts" / ".publication-journal.jsonl"


def _journal_accounts_for(run_root: Path, sha256_hex: str) -> bool:
    """True when the chained publication journal accounts for these bytes.

    Hardlinked receipt files are accepted ONLY when a journaled publication
    intent recorded their exact hash: that is what distinguishes the durable
    staging remnants of a legitimate (possibly SIGKILLed) publication from an
    attacker-planted hardlink, which no journal entry accounts for.
    """
    for record in _read_chained_records(_publication_journal_path(run_root)):
        if sha256_hex in (
            record.get("receipt_sha256"),
            record.get("signature_sha256"),
        ):
            return True
    return False


def _read_receipt_evidence(run_root: Path, path: Path) -> bytes:
    """Receipt-store read: hardlinks accepted only with journal accounting."""
    payload = _read_evidence_bytes(path, allow_hardlinks=True)
    status = os.stat(path, follow_symlinks=False)
    if status.st_nlink != 1 and not _journal_accounts_for(
        run_root, hashlib.sha256(payload).hexdigest()
    ):
        raise ProvenanceError(
            f"evidence file has link count {status.st_nlink} and NO "
            f"journaled publication accounts for its bytes: {path}; an "
            "unaccounted hardlink is refused"
        )
    return payload


def _publish_receipt(
    run_root: Path,
    reference: str,
    receipt: dict,
    key_path: str,
    public_key_path: str,
    capture,
) -> dict:
    """Publish receipt+signature no-replace; existing evidence is write-once.

    The receipt is written to a same-filesystem staging directory, signed,
    the signature is VERIFIED, and both files are fsynced. Publication then
    claims the final directory with mkdir — a true no-replace primitive that
    fails even against an injected EMPTY directory, unlike rename(2), which
    would silently replace one — and links the staged files in with link(2),
    which is also no-replace. A publisher that loses the claim verifies the
    winner through the idempotence/conflict check instead of overwriting it,
    and the winning publisher re-reads the published bytes and requires them
    to equal the verified staged bytes. A crash can leave a partial claim,
    which every subsequent load and publish refuses fail-closed as partial
    evidence; nothing ever replaces existing published state.
    """
    path = receipt_path(run_root, receipt["digest"])
    final_dir = path.parent
    parent = final_dir.parent
    _assert_receipt_tree_safe(run_root, final_dir)
    if final_dir.exists():
        _roll_forward_partial_publication(run_root, final_dir, receipt["digest"])
        return _existing_receipt_or_conflict(
            run_root, final_dir, reference, receipt, public_key_path, capture
        )
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_receipt_tree_safe(run_root, final_dir)
    staging_root = parent / ".staging"
    staging_root.mkdir(mode=0o700, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(dir=staging_root, prefix=f"{receipt['digest'][7:19]}-")
    )
    if True:
        staged_receipt = staging / "receipt.json"
        staged_signature = staging / "receipt.json.sig"
        staged_receipt.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        staged_receipt.chmod(0o600)
        capture(
            [
                "cosign",
                "sign-blob",
                "--key",
                key_path,
                "--use-signing-config=false",
                "--tlog-upload=false",
                "--yes",
                "--output-file",
                str(staged_signature),
                str(staged_receipt),
            ]
        )
        staged_signature.chmod(0o600)
        # One read of the staged pair; the signature is verified over EXACTLY
        # those bytes via private scratch copies (never by re-reading the
        # staging pathnames), and the files that get published are written
        # fresh from the verified bytes — a swap of the staging pathname
        # between verification and publication can publish nothing.
        staged_receipt_bytes = _read_evidence_bytes(staged_receipt)
        staged_signature_bytes = _read_evidence_bytes(staged_signature)
        _verify_blob_bytes(
            public_key_path,
            staged_receipt_bytes,
            staged_signature_bytes,
            lambda command: capture(command),
            f"staged receipt of {reference}",
        )
        publish_receipt = staging / "publish.json"
        publish_signature = staging / "publish.json.sig"
        publish_receipt.write_bytes(staged_receipt_bytes)
        publish_receipt.chmod(0o600)
        publish_signature.write_bytes(staged_signature_bytes)
        publish_signature.chmod(0o600)
        _fsync_file(publish_receipt)
        _fsync_file(publish_signature)
        _fsync_dir(staging)
        # DURABLE publication intent BEFORE the claim: the journal records
        # the exact bytes and the staging path, so (a) a SIGKILL at ANY later
        # point leaves a state a retry can roll forward from, and (b) the
        # permanent nlink=2 of the never-deleted staging remnants is
        # journal-accounted, distinguishing it from attacker hardlinks.
        _append_chained_record(
            _publication_journal_path(run_root),
            {
                "phase": "intent",
                "digest": receipt["digest"],
                "receipt_sha256": hashlib.sha256(staged_receipt_bytes).hexdigest(),
                "signature_sha256": hashlib.sha256(
                    staged_signature_bytes
                ).hexdigest(),
                "staging": staging.name,
                "at": datetime.now(UTC).isoformat(),
            },
        )
        try:
            os.mkdir(final_dir, mode=0o700)
        except OSError:
            # The claim failed: a concurrent publisher won, an earlier
            # SIGKILLed attempt left a partial claim, or something was
            # injected. A PARTIAL directory whose journal intent matches is
            # ROLLED FORWARD; anything else is verified as the winner or
            # refused — never replaced.
            _roll_forward_partial_publication(
                run_root, final_dir, receipt["digest"]
            )
            return _existing_receipt_or_conflict(
                run_root,
                final_dir,
                reference,
                receipt,
                public_key_path,
                capture,
            )
        _link_no_replace_or_adopt(
            publish_signature,
            final_dir / "receipt.json.sig",
            staged_signature_bytes,
        )
        _link_no_replace_or_adopt(
            publish_receipt, final_dir / "receipt.json", staged_receipt_bytes
        )
        _fsync_dir(final_dir)
        _fsync_dir(parent)
        # Staging is DURABLE (never deleted): the published files keep
        # nlink=2 permanently, accounted by the journal intent above.
        published_receipt = _read_receipt_evidence(
            run_root, final_dir / "receipt.json"
        )
        published_signature = _read_receipt_evidence(
            run_root, final_dir / "receipt.json.sig"
        )
        if (
            published_receipt != staged_receipt_bytes
            or published_signature != staged_signature_bytes
        ):
            raise ProvenanceError(
                f"published receipt for {reference} at {final_dir} does not "
                "byte-match the verified staged evidence; investigate the "
                "tamper before trusting or replacing it"
            )
        _append_chained_record(
            _publication_journal_path(run_root),
            {
                "phase": "complete",
                "digest": receipt["digest"],
                "receipt_sha256": hashlib.sha256(staged_receipt_bytes).hexdigest(),
                "at": datetime.now(UTC).isoformat(),
            },
        )
        return receipt


def receipt_verify_blob_command(
    public_key_path: str, receipt: Path, signature: Path
) -> list[str]:
    return [
        "cosign",
        "verify-blob",
        "--key",
        public_key_path,
        "--signature",
        str(signature),
        "--insecure-ignore-tlog=true",
        str(receipt),
    ]


def validate_receipt_binding(
    receipt: dict, reference: str, run_root: Path, capture=_run_capture
) -> None:
    """Refuse signing/allow-listing unless the receipt fully binds the digest.

    Nothing recorded is taken on faith: the digest identity, the SBOM subject
    == the recorded linux/amd64 manifest, the durable bundle artifact (exists,
    hash-matches, still carries the anchor tag at its recorded target), and —
    via a fresh clone restored from the bundle — the tag object, the anchor
    commit, the source-commit ancestry, and the source tree are all
    re-proven on every load. Registry content is re-proven too: the top,
    linux/amd64, and config bytes, source labels, and — for attestation
    receipts — the attestation manifest and statement are re-fetched and
    hash-verified against the recorded digests, and the retained
    content-addressed SBOM/statement evidence must byte-match; recorded
    digest strings alone never authorize anything.
    """
    reference = validate_digest_reference(reference)
    digest = reference.rsplit("@", 1)[1]
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise ProvenanceError(f"unsupported release receipt schema for {reference}")
    if receipt.get("image") != reference or receipt.get("digest") != digest:
        raise ProvenanceError(
            f"release receipt does not bind {reference}; it names "
            f"{receipt.get('image')!r}"
        )
    source = receipt.get("source", {})
    if not COMMIT_PATTERN.match(str(source.get("commit", ""))) or not COMMIT_PATTERN.match(
        str(source.get("tree", ""))
    ):
        raise ProvenanceError(
            f"release receipt for {reference} lacks an exact source commit and tree"
        )
    anchor = receipt.get("anchor", {})
    if anchor.get("mode") != "bundle" or not str(anchor.get("tag", "")).startswith(
        "refs/tags/"
    ):
        raise ProvenanceError(
            f"release receipt for {reference} lacks a durable bundle anchor"
        )
    if not COMMIT_PATTERN.match(str(anchor.get("restored_commit", ""))) or anchor.get(
        "restored_commit"
    ) != anchor.get("commit"):
        raise ProvenanceError(
            f"release receipt for {reference} lacks restore evidence matching "
            "the anchor commit"
        )
    if not SHA256_PATTERN.match(str(anchor.get("bundle_sha256", ""))):
        raise ProvenanceError(
            f"release receipt for {reference} lacks the anchor bundle SHA-256"
        )
    bundle = Path(str(anchor.get("bundle_path", "")))
    # Re-prove the recorded source identity against the bundle itself: a
    # receipt naming a commit, tree, or tag object the durable artifact does
    # not actually contain is refused, whatever its other fields claim. The
    # bundle is read once, hash-verified against the receipt, and every git
    # operation runs against a private snapshot of those verified bytes so a
    # same-user pathname swap cannot diverge verified and consumed bytes.
    with (
        _verified_bundle_snapshot(
            run_root, bundle, str(anchor["bundle_sha256"]), reference
        ) as snapshot,
        tempfile.TemporaryDirectory(dir=run_root) as scratch,
    ):
        heads = _run_capture(["git", "bundle", "list-heads", str(snapshot)])
        expected = f"{anchor.get('tag_target')} {anchor.get('tag')}"
        if expected not in {line.strip() for line in heads.splitlines()}:
            raise ProvenanceError(
                f"anchor bundle for {reference} does not carry "
                f"{anchor.get('tag')} at {anchor.get('tag_target')}"
            )
        restore = Path(scratch) / "revalidate.git"
        _run_capture(
            ["git", "clone", "--quiet", "--bare", str(snapshot), str(restore)]
        )
        restored_target = _git_capture(restore, "rev-parse", anchor["tag"]).strip()
        restored_commit = _git_capture(
            restore, "rev-parse", f"{anchor['tag']}^{{commit}}"
        ).strip()
        if restored_target != anchor.get("tag_target") or restored_commit != anchor.get(
            "commit"
        ):
            raise ProvenanceError(
                f"anchor bundle for {reference} restores {anchor['tag']} to "
                f"{restored_target} ({restored_commit}), not the recorded "
                f"{anchor.get('tag_target')} ({anchor.get('commit')})"
            )
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(restore),
                "merge-base",
                "--is-ancestor",
                source["commit"],
                restored_commit,
            ],
            capture_output=True,
            text=True,
        )
        if ancestry.returncode != 0:
            raise ProvenanceError(
                f"receipt source commit {source['commit']} for {reference} is "
                "not reachable from the anchor in the restored bundle"
            )
        restored_tree = _git_capture(
            restore, "rev-parse", f"{source['commit']}^{{tree}}"
        ).strip()
        if restored_tree != source["tree"]:
            raise ProvenanceError(
                f"receipt source tree {source['tree']} for {reference} does "
                f"not match the tree {restored_tree} of its commit in the "
                "restored bundle"
            )
    image_manifest = receipt.get("image_manifest", {})
    amd64_digest = str(image_manifest.get("amd64_manifest_digest") or "")
    config_digest = str(image_manifest.get("config_digest") or "")

    sbom = receipt.get("sbom", {})
    attestation = str(sbom.get("attestation_manifest_digest") or "")
    subject = str(sbom.get("subject_manifest_digest") or "")
    spdx_layer = str(sbom.get("spdx_layer_digest") or "")
    statement_sha = str(sbom.get("statement_sha256") or "")
    spdx = str(sbom.get("spdx_sha256") or "")
    spdx_subject = str(sbom.get("spdx_subject_digest") or "")

    def _is_digest(value: str) -> bool:
        return value.startswith("sha256:") and bool(
            SHA256_PATTERN.match(value.split(":", 1)[1])
        )

    if not _is_digest(amd64_digest) or not _is_digest(config_digest):
        raise ProvenanceError(
            f"release receipt for {reference} lacks the exact linux/amd64 "
            "manifest and config digests"
        )
    attestation_bound = (
        _is_digest(attestation)
        and _is_digest(subject)
        and subject == amd64_digest
        and _is_digest(spdx_layer)
        and bool(SHA256_PATTERN.match(statement_sha))
        and spdx_layer == f"sha256:{statement_sha}"
    )
    spdx_bound = bool(SHA256_PATTERN.match(spdx)) and spdx_subject == amd64_digest
    if not attestation_bound and not spdx_bound:
        raise ProvenanceError(
            f"release receipt for {reference} lacks SBOM evidence bound to the "
            "exact linux/amd64 manifest (in-toto subject == recorded amd64 "
            "manifest) or a subject-checked SPDX document"
        )

    # Registry re-proof: the recorded digests must still name the exact
    # content the registry serves, re-fetched and byte-hash-verified now.
    top, live_amd64, live_config, labels = _resolve_amd64_image(
        reference, capture
    )
    if live_amd64 != amd64_digest or live_config != config_digest:
        raise ProvenanceError(
            f"registry re-proof failed for {reference}: the index now "
            f"resolves to manifest {live_amd64} / config {live_config}, not "
            f"the receipted {amd64_digest} / {config_digest}"
        )
    if (
        labels.get("org.opencontainers.image.revision") != source["commit"]
        or labels.get("ai.nebius.fs2-serve.source-tree") != source["tree"]
    ):
        raise ProvenanceError(
            f"registry re-proof failed for {reference}: the hash-verified "
            "config labels no longer match the receipted source commit/tree"
        )
    if attestation_bound:
        evidence = _validated_attestation_evidence(
            reference, top, live_amd64, capture
        )
        if evidence is None:
            raise ProvenanceError(
                f"registry re-proof failed for {reference}: the receipted "
                "attestation evidence is no longer present in the index"
            )
        for field in (
            "attestation_manifest_digest",
            "subject_manifest_digest",
            "spdx_layer_digest",
            "statement_sha256",
        ):
            if str(sbom.get(field) or "") != str(evidence.get(field) or ""):
                raise ProvenanceError(
                    f"registry re-proof failed for {reference}: re-validated "
                    f"attestation {field} {evidence.get(field)!r} does not "
                    f"equal the receipted {sbom.get(field)!r}"
                )
        retained = _reproven_sbom_evidence(
            run_root, statement_sha, ".intoto.json", reference
        )
        if retained != evidence["_evidence_bytes"]:
            raise ProvenanceError(
                f"retained statement evidence for {reference} does not "
                "byte-match the re-fetched registry statement"
            )
    else:
        document_bytes = _reproven_sbom_evidence(
            run_root, spdx, ".spdx.json", reference
        )
        try:
            document = json.loads(document_bytes)
        except json.JSONDecodeError as error:
            raise ProvenanceError(
                f"retained SPDX evidence for {reference} is not valid JSON"
            ) from error
        described = _validate_spdx_shape(
            document, f"retained SPDX evidence for {reference}"
        )
        digest_hex = live_amd64.split(":", 1)[1]
        if not any(
            _package_names_exact_digest(package, digest_hex)
            for package in described
        ):
            raise ProvenanceError(
                f"retained SPDX evidence for {reference} no longer binds the "
                "digest as an exact SHA256 checksum or subject locator"
            )


def _assert_receipt_tree_safe(run_root: Path, final_dir: Path) -> None:
    """Refuse symlinks at every component of the receipt publication path."""
    receipts_parent = final_dir.parent
    for component in (run_root, receipts_parent, final_dir):
        if component.is_symlink():
            raise ProvenanceError(f"receipt path component is a symlink: {component}")
    if receipts_parent.exists():
        expected = run_root.resolve(strict=True) / receipts_parent.name
        if receipts_parent.resolve() != expected:
            raise ProvenanceError(
                f"receipt store escapes the run root: {receipts_parent}"
            )


def load_bound_receipt(
    run_root: Path,
    reference: str,
    public_key_path: str,
    verifier=None,
    capture=_run_capture,
) -> dict:
    """Load a receipt, verify its cosign signature, then verify its bindings.

    The receipt and signature are read once through dirfd/O_NOFOLLOW with
    anomaly checks, the signature is verified over exactly those bytes, the
    same bytes are parsed, and the parsed bindings are then fully revalidated
    against the durable bundle artifact.
    """
    reference = validate_digest_reference(reference)
    path = receipt_path(run_root, reference.rsplit("@", 1)[1])
    _assert_receipt_tree_safe(run_root, path.parent)
    signature = path.parent / (path.name + ".sig")
    if not path.parent.is_dir() or not path.is_file() or not signature.is_file():
        raise ProvenanceError(
            f"no signed release receipt for {reference} at {path}; create one "
            "with provenance.py receipt before signing or allow-listing"
        )
    receipt_bytes = _read_receipt_evidence(run_root, path)
    signature_bytes = _read_receipt_evidence(run_root, signature)
    try:
        _verify_blob_bytes(
            public_key_path,
            receipt_bytes,
            signature_bytes,
            verifier,
            f"release receipt of {reference}",
        )
    except ProvenanceError as error:
        raise ProvenanceError(
            f"release receipt signature verification failed for {reference}; "
            "the receipt is not trustworthy"
        ) from error
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError as error:
        raise ProvenanceError(f"unreadable release receipt at {path}") from error
    validate_receipt_binding(receipt, reference, run_root, capture)
    return receipt


INVENTORY_SCHEMA = "fs2-serve.nebius.ai/release-inventory/v7"
COLLECTOR_METHOD = "fs2-live-enumeration/v1"
INVENTORY_CHECKPOINT_SCHEMA = "fs2-serve.nebius.ai/inventory-checkpoint/v1"
SCOPE_SCHEMA = "fs2-serve.nebius.ai/release-scope/v12"
ROLLOUT_AUTHORIZATION_SCHEMA = "fs2-serve.nebius.ai/rollout-authorization/v3"
PROVIDER_ATTESTATION_SCHEMA = "fs2-serve.nebius.ai/provider-attestation/v3"
# Provider roles that amount to control over the managed cluster or its IAM.
# Any live provider access binding matching this pattern must name a subject
# the SIGNED attestation explicitly enumerates — an unenumerated admin is a
# violation. Bindings carrying conditions are NOT exempted: the role class
# decides, fail-closed.
PROVIDER_ADMIN_ROLE_PATTERN = re.compile(r"(admin|editor|owner)", re.IGNORECASE)
PROVIDER_PARENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
WORM_MIN_RETENTION_DAYS = 30
HELM_DSN_PATTERN = re.compile(
    r"^postgres(?:ql)?(?:\+[a-z0-9]+)?://(?P<user>[^:@/]+)(?::[^@]*)?"
    r"@(?P<host>[^:/?#]+)(?::(?P<port>\d+))?/(?P<database>[^?#/]+)"
    r"(?:\?(?P<query>[^#]*))?$"
)
RECOVERY_SCHEMA = "fs2-serve.nebius.ai/admission-recovery/v2"
GUARD_PARAMS_NAME = "fs2-security-guard-params"
PROTECTED_POLICY_NAMES = (
    "fs2-image-provenance",
    "fs2-helm-release-governance",
    "fs2-provenance-guard",
)
RECOVERY_MAX_VALIDITY_HOURS = 72
INVENTORY_SOURCES = (
    "live_workloads",
    "helm_rollback_window",
    "frozen_scientific_bindings",
)
DRAIN_REASON_PATTERN = re.compile(
    r"^(incident|change|ticket|task):[A-Za-z0-9][A-Za-z0-9._/-]{1,63}"
    r"( [\x20-\x7e]{1,160})?$"
)
INVENTORY_MAX_AGE_HOURS = 24
INVENTORY_MAX_AGE_HOURS_LIMIT = 168
_CLOCK_SKEW_SECONDS = 300
CLUSTER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._:-]{0,255}$")
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
PRINCIPAL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@:/._-]{0,255}$")
# Owner decision (2026-09-16): deploy principals are AUTOMATION-ONLY — a
# Kubernetes ServiceAccount identity, never a human user. Enforcement is
# ADDITIVE deny (admission + this contract), not credential revocation.
AUTOMATION_PRINCIPAL_PATTERN = re.compile(
    r"^system:serviceaccount:[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?:"
    r"[a-z0-9]([a-z0-9-]{0,251}[a-z0-9])?$"
)
SCOPE_FIELDS = (
    "cluster",
    "namespaces",
    "registry_prefixes",
    "platform_repository_prefix",
    "deploy_principals",
    "helm_secret_writers",
    "helm_storage",
    "workload_service_accounts",
    "deploy_credential_csi_driver",
    "deploy_credential_spc",
    "workload_csi_drivers",
    "security_principals",
    "verification_key_sha256",
    "attestation_key_sha256",
    "policy_sha256",
    "iam_boundary_sha256",
    "iam_exempt_subjects",
    "token_audience",
    "stage_binding_authority",
    "tooling",
    "run_root",
    "provider_endpoint",
    "provider_principal",
    "provider_cluster_id",
    "provider_parent_ids",
    "provider_readonly_roles",
    "worm_store_uri",
    "worm_bucket",
)
PROVIDER_ENDPOINT_PATTERN = re.compile(r"^https://[a-z0-9.-]+(?::\d+)?$")
# Query parameters a Helm SQL DSN may carry: TLS/timeout/name hints only.
# Anything else — host/hostaddr/port/dbname/user/password/service/options/
# search_path — can REDIRECT or reshape the backend and is refused outright.
HELM_DSN_ALLOWED_QUERY_KEYS = frozenset(
    {"sslmode", "sslrootcert", "connect_timeout", "application_name"}
)
# Every tamper-evident local chain MUST be covered by the off-host anchor: a
# snapshot that omits a chain anchors nothing for it, so verification refuses
# snapshots that do not enumerate all of these explicitly (count 0 included).
REQUIRED_ANCHOR_CHAINS = (
    "acceptance-heads",
    "consumed",
    "publication-journal",
    "reconcile-journal",
)
RECOVERY_ANNOTATION = "security.fs2.nebius.ai/recovery-authorization"
TOKEN_AUDIENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:/._-]{1,127}$")
BINARY_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9/._-]{1,255}$")
WORKLOAD_REF_PATTERN = re.compile(
    r"^(deployment|statefulset)/[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$"
)
# The ONLY exemptible identities: enumerated Kubernetes bootstrap identities
# plus kube-system controller ServiceAccounts. Arbitrary humans, arbitrary
# groups, and Group:system:masters are NEVER exemptible — a wildcard human
# cluster-admin cannot be signed back into validity. The single bootstrap
# cluster-admin -> system:masters binding is recognized only through the
# ATTESTOR-SIGNED provider attestation (masters_certificate_issuance:
# provider-held — outside cluster-admin reach on managed mk8s).
BOOTSTRAP_EXEMPTIBLE_SUBJECTS = frozenset(
    {
        "User:system:kube-controller-manager",
        "User:system:kube-scheduler",
        "User:system:apiserver",
        "Group:system:nodes",
        # NOTE deliberately absent: Group:system:serviceaccounts:kube-system —
        # a GROUP exemption would cover every present and future kube-system
        # ServiceAccount wholesale; controllers are exempted individually by
        # name (ServiceAccount:kube-system:<name>) instead.
    }
)
RBAC_SUBJECT_PATTERN = re.compile(
    r"^(User:[A-Za-z0-9@:/._-]{1,255}"
    r"|Group:[A-Za-z0-9@:/._-]{1,255}"
    r"|ServiceAccount:[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?:"
    r"[a-z0-9]([a-z0-9-]{0,251}[a-z0-9])?)$"
)
# The owner's COMPLETE identity-path closure list, machine-readable: any
# subject holding ANY of these permissions can reach the security identity
# (directly or by minting/stealing credentials) and voids the external
# boundary. `verify-iam-boundary` audits live RBAC against this table and the
# renderer refuses to render while a non-exempt subject holds one.
FORBIDDEN_IDENTITY_RULES: tuple[dict[str, "set[str] | str | bool | dict"], ...] = (
    # Mutating the admission configuration itself (architecturally exempt
    # from in-cluster admission, so ONLY IAM can protect it). Wildcard
    # resources: this covers every current AND future admission kind
    # (mutating admission policies included) rather than a four-name list.
    # Applying and recovering the committed policy objects is the SECURITY
    # identity's function — the one rule it is permitted, and the ONLY rule
    # any principal is permitted by role: allowances are per-function, never
    # a blanket identity exemption.
    {
        "apiGroups": {"admissionregistration.k8s.io"},
        "resources": {"*"},
        "verbs": {"create", "update", "patch", "delete", "deletecollection"},
        "why": "admission-configuration write/delete",
        "permitted_role": "security",
        # The allowance is GRANT-SHAPE-BOUND, not principal-blanket: the
        # security identity's granted rule may carry only these verbs and
        # resources, and its update/patch must be resourceName-scoped to the
        # protected objects — a delete-capable, wildcard, or broader grant
        # violates even for the permitted principal.
        "permitted_grant": {
            "verbs": {"get", "list", "watch", "create", "update", "patch"},
            "resources": {
                "validatingadmissionpolicies",
                "validatingadmissionpolicybindings",
            },
            "resource_names_for": {"update", "patch"},
            "resource_names": set(PROTECTED_POLICY_NAMES),
        },
    },
    # Impersonation of every identity dimension.
    {
        "apiGroups": {""},
        "resources": {"users", "groups", "serviceaccounts", "uids"},
        "verbs": {"impersonate"},
        "why": "identity impersonation",
    },
    {
        "apiGroups": {"authentication.k8s.io"},
        "resources": {"userextras", "uids"},
        "verbs": {"impersonate"},
        "why": "identity impersonation (extras/uid)",
    },
    # Minting ServiceAccount credentials.
    {
        "apiGroups": {""},
        "resources": {"serviceaccounts/token"},
        "verbs": {"create"},
        "why": "ServiceAccount token minting",
    },
    # Privilege delegation through RBAC itself — including MUTATING the
    # bindings/roles (writing RBAC is granting RBAC).
    {
        "apiGroups": {"rbac.authorization.k8s.io"},
        "resources": {"roles", "clusterroles"},
        "verbs": {"bind", "escalate"},
        "why": "RBAC bind/escalate delegation",
    },
    {
        "apiGroups": {"rbac.authorization.k8s.io"},
        "resources": {"roles", "clusterroles", "rolebindings", "clusterrolebindings"},
        "verbs": {"create", "update", "patch", "delete", "deletecollection"},
        "why": "RBAC mutation delegation",
    },
    # Stored-credential delegation: reading Secrets in the protected
    # namespaces can expose ServiceAccount credentials, and WRITING one of
    # type kubernetes.io/service-account-token MINTS a legacy long-lived
    # token for any ServiceAccount — both directions are identity paths.
    {
        "apiGroups": {""},
        "resources": {"secrets"},
        "verbs": {"get", "list", "watch", "create", "update", "patch"},
        "why": "stored-credential access/minting in protected namespaces",
        "namespaced_to_scope": True,
    },
    # Runtime credential theft: exec/attach/port-forward into a pod (or
    # injecting an ephemeral container) reaches its mounted credentials.
    {
        "apiGroups": {""},
        "resources": {
            "pods/exec",
            "pods/attach",
            "pods/ephemeralcontainers",
            "pods/portforward",
        },
        "verbs": {"create", "update", "patch"},
        "why": "runtime credential theft via exec/attach/ephemeral/portforward",
        "namespaced_to_scope": True,
    },
    # Proxy paths reach kubelets and pod endpoints BEHIND admission and
    # audit: nodes/proxy is kubelet API access (exec on every pod of the
    # node), services/pods proxy tunnels into workloads directly.
    {
        "apiGroups": {""},
        "resources": {"nodes/proxy", "services/proxy", "pods/proxy"},
        "verbs": {"get", "create", "update", "patch", "delete"},
        "why": "kubelet/workload proxy access bypassing admission",
    },
    # Node object mutation steers scheduling (labels/taints) onto attacker
    # nodes and can reach every pod scheduled there.
    {
        "apiGroups": {""},
        "resources": {"nodes", "nodes/status"},
        "verbs": {"update", "patch"},
        "why": "node mutation (scheduling steering)",
    },
    # Storage-layer pivots: a PersistentVolume can bind arbitrary host
    # paths, and StorageClass/CSIDriver mutation redirects future volumes.
    {
        "apiGroups": {""},
        "resources": {"persistentvolumes"},
        "verbs": {"create", "update", "patch", "delete"},
        "why": "PersistentVolume host-path/storage pivot",
    },
    {
        "apiGroups": {"storage.k8s.io"},
        "resources": {"storageclasses", "csidrivers"},
        "verbs": {"create", "update", "patch", "delete"},
        "why": "storage-provisioning mutation",
    },
    # Endpoint hijack: writing Endpoints/EndpointSlices in the protected
    # namespaces redirects Service traffic to attacker pods.
    {
        "apiGroups": {"", "discovery.k8s.io"},
        "resources": {"endpoints", "endpointslices"},
        "verbs": {"create", "update", "patch"},
        "why": "service-endpoint hijack in a protected namespace",
        "namespaced_to_scope": True,
        "permitted_role": "deploy",
        "permitted_grant": {
            "verbs": {"get", "list", "watch", "create", "update", "patch"},
            "resources": {"endpoints", "endpointslices"},
        },
    },
    # Identity minting through the certificates API: creating and approving
    # CSRs (or driving a signer) yields client certificates for any subject.
    {
        "apiGroups": {"certificates.k8s.io"},
        "resources": {
            "certificatesigningrequests",
            "certificatesigningrequests/approval",
            "signers",
        },
        "verbs": {"create", "update", "patch", "approve", "sign"},
        "why": "identity minting via CSR create/approve/sign",
    },
    # Workload/ServiceAccount writes in the SECURITY identity's namespaces:
    # creating a workload there mounts the security ServiceAccount, and
    # recreating the ServiceAccount substitutes the identity. NOBODY is
    # permitted here.
    {
        "apiGroups": {"", "apps", "batch"},
        "resources": {
            "pods",
            "deployments",
            "daemonsets",
            "statefulsets",
            "replicasets",
            "replicationcontrollers",
            "jobs",
            "cronjobs",
            "serviceaccounts",
        },
        "verbs": {"create", "update", "patch"},
        "why": "workload/ServiceAccount write in a security namespace",
        "security_namespaces_only": True,
    },
    # Workload creation in the SCOPE namespaces mounts the RELEASE
    # ServiceAccount (deploy-identity theft). Deploying releases is the
    # deploy identity's FUNCTION, so it alone is permitted — everyone else
    # (humans included) is a violation.
    {
        "apiGroups": {"", "apps", "batch"},
        "resources": {
            "pods",
            "deployments",
            "daemonsets",
            "statefulsets",
            "replicasets",
            "replicationcontrollers",
            "jobs",
            "cronjobs",
            "serviceaccounts",
        },
        "verbs": {"create", "update", "patch"},
        "why": "workload write in a protected namespace (deploy-identity theft)",
        "namespaced_to_scope": True,
        "permitted_role": "deploy",
        # The deploy allowance never covers ServiceAccount writes (identity
        # substitution) and never delete: a combined workload+SA grant or a
        # delete-capable grant violates even for the deploy principal.
        "permitted_grant": {
            "verbs": {"get", "list", "watch", "create", "update", "patch"},
            "resources": {
                "pods",
                "services",
                "configmaps",
                "deployments",
                "daemonsets",
                "statefulsets",
                "replicasets",
                "replicationcontrollers",
                "jobs",
                "cronjobs",
            },
        },
    },
)
# NOTE: external (provider) IAM paths — cloud-console access to the managed
# apiserver, node SSH, etcd — are OUTSIDE the Kubernetes RBAC surface this
# audit can see; they are the provider-held arm of the boundary and remain
# named owner-attestation items, never silently assumed closed.


def _parse_rfc3339(value: str, context: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ProvenanceError(f"{context} has an invalid timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise ProvenanceError(f"{context} timestamp {value!r} lacks a timezone")
    return parsed


def _validated_scope(value, context: str) -> dict:
    """Validate an exact admission scope object; refuse anything loose."""
    if not isinstance(value, dict) or set(value) != set(SCOPE_FIELDS):
        raise ProvenanceError(
            f"{context} must be an object with exactly these fields: "
            + ", ".join(SCOPE_FIELDS)
        )
    if not CLUSTER_PATTERN.match(str(value.get("cluster", ""))):
        raise ProvenanceError(f"{context} has an invalid cluster identity")
    namespaces = value.get("namespaces")
    if (
        not isinstance(namespaces, list)
        or not namespaces
        or len(set(namespaces)) != len(namespaces)
        or not all(
            isinstance(item, str) and NAMESPACE_PATTERN.match(item)
            for item in namespaces
        )
    ):
        raise ProvenanceError(
            f"{context} needs a non-empty list of unique valid namespaces"
        )
    prefixes = value.get("registry_prefixes")
    if (
        not isinstance(prefixes, list)
        or not prefixes
        or len(set(prefixes)) != len(prefixes)
        or not all(
            isinstance(item, str) and item and item.endswith("/")
            for item in prefixes
        )
    ):
        raise ProvenanceError(
            f"{context} needs a non-empty list of unique registry prefixes "
            "ending with '/'"
        )
    platform_prefix = value.get("platform_repository_prefix")
    if (
        not isinstance(platform_prefix, str)
        or not platform_prefix.endswith("/")
        or not any(platform_prefix.startswith(prefix) for prefix in prefixes)
    ):
        raise ProvenanceError(
            f"{context} platform_repository_prefix must end with '/' and lie "
            "under one of the allowed registry prefixes"
        )
    principals = value.get("deploy_principals")
    if (
        not isinstance(principals, list)
        or len(set(principals)) != len(principals)
        or not all(
            isinstance(item, str) and AUTOMATION_PRINCIPAL_PATTERN.match(item)
            for item in principals
        )
    ):
        raise ProvenanceError(
            f"{context} needs a list of unique AUTOMATION deploy principals "
            "(system:serviceaccount:<namespace>:<name>); the owner decision "
            "is an automation-only, short-lived release identity — human "
            "usernames never hold deploy authority"
        )
    helm_writers = value.get("helm_secret_writers")
    if (
        not isinstance(helm_writers, list)
        or len(set(helm_writers)) != len(helm_writers)
        or not all(
            isinstance(item, str) and AUTOMATION_PRINCIPAL_PATTERN.match(item)
            for item in helm_writers
        )
        or not set(helm_writers) <= set(principals if isinstance(principals, list) else [])
    ):
        raise ProvenanceError(
            f"{context} needs helm_secret_writers: the (possibly EMPTY) "
            "subset of deploy principals allowed to write helm.sh/release.v1 "
            "Secrets; under the HELM_DRIVER=sql contract the owner sets [] "
            "and the governance policy then denies every release-Secret "
            "write outright"
        )
    helm_storage = value.get("helm_storage")
    if (
        not isinstance(helm_storage, dict)
        or set(helm_storage) != {"driver", "connection_identity"}
        or helm_storage.get("driver") != "sql"
        or not isinstance(helm_storage.get("connection_identity"), str)
        or not helm_storage["connection_identity"].strip()
    ):
        raise ProvenanceError(
            f"{context} needs helm_storage {{driver: 'sql', "
            "connection_identity}}: the owner DECISION fixed Helm storage to "
            "the SQL driver — 'secret' is not a permitted driver — and the "
            "connection_identity (host:port/database?user=<user>) is the "
            "NON-SECRET backend identity the live DSN must match (the "
            "password never enters the scope)"
        )
    if value.get("helm_secret_writers"):
        raise ProvenanceError(
            f"{context} helm_secret_writers must be EMPTY under the fixed "
            "SQL contract: no principal writes helm.sh/release.v1 Secrets"
        )
    workload_accounts = value.get("workload_service_accounts")
    if (
        not isinstance(workload_accounts, list)
        or not workload_accounts
        or len(set(workload_accounts)) != len(workload_accounts)
        or not all(
            isinstance(item, str)
            and NAMESPACE_PATTERN.match(item.partition(":")[0])
            and NAMESPACE_PATTERN.match(item.partition(":")[2])
            for item in workload_accounts
        )
    ):
        raise ProvenanceError(
            f"{context} needs workload_service_accounts: the non-empty "
            "owner-enumerated <namespace>:<name> ServiceAccounts that "
            "workloads written by the automation identities may run as — "
            "the deploy identity can never schedule pods under an "
            "unenumerated ServiceAccount"
        )
    if any(
        str(item).partition(":")[2] == "default" for item in workload_accounts
    ):
        raise ProvenanceError(
            f"{context} workload_service_accounts may not enumerate a "
            "'default' ServiceAccount: the catch-all identity is never an "
            "owner-reviewed workload identity"
        )
    csi_driver = value.get("deploy_credential_csi_driver")
    if not isinstance(csi_driver, str) or not re.match(
        r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$", csi_driver
    ):
        raise ProvenanceError(
            f"{context} needs deploy_credential_csi_driver: the ONE "
            "owner-named CSI driver through which automation pods receive "
            "file-mounted credentials (e.g. secrets-store.csi.k8s.io); "
            "automation pods may mount no other credential path"
        )
    if not isinstance(value.get("deploy_credential_spc"), str) or not re.match(
        r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$",
        str(value.get("deploy_credential_spc")),
    ):
        raise ProvenanceError(
            f"{context} needs deploy_credential_spc: the exact "
            "SecretProviderClass name the credential CSI mount must "
            "reference — an unpinned provider class could serve foreign "
            "credential material"
        )
    workload_csi = value.get("workload_csi_drivers")
    if not isinstance(workload_csi, list) or not all(
        isinstance(item, str)
        and re.match(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$", item)
        for item in workload_csi
    ) or len(set(workload_csi)) != len(workload_csi):
        raise ProvenanceError(
            f"{context} needs workload_csi_drivers: the (possibly empty) "
            "owner-enumerated CSI drivers automation-written workloads may "
            "mount; arbitrary CSI drivers are refused in admission"
        )
    if not isinstance(value.get("run_root"), str) or not re.match(
        r"^/[A-Za-z0-9/._-]{1,511}$", str(value.get("run_root"))
    ):
        raise ProvenanceError(
            f"{context} needs run_root: the OWNER-PINNED absolute private "
            "run-root path; locks, consumption ledgers, and journals bind "
            "globally to it, so a caller-selected directory can never reset "
            "single-use state or escape serialization"
        )
    if not SHA256_PATTERN.match(str(value.get("iam_boundary_sha256", ""))):
        raise ProvenanceError(
            f"{context} needs iam_boundary_sha256: the exact SHA-256 of the "
            "committed iam-boundary.yaml whose objects the live cluster "
            "must equal"
        )
    if not PROVIDER_ENDPOINT_PATTERN.match(
        str(value.get("provider_endpoint", ""))
    ):
        raise ProvenanceError(
            f"{context} needs provider_endpoint: the exact https provider "
            "API endpoint every provider-CLI call is pinned to"
        )
    if not isinstance(value.get("provider_principal"), str) or not (
        PRINCIPAL_PATTERN.match(str(value.get("provider_principal")))
    ):
        raise ProvenanceError(
            f"{context} needs provider_principal: the exact read-only "
            "provider identity the CLI must authenticate as (verified via "
            "whoami before any enumeration is trusted)"
        )
    if not PROVIDER_PARENT_ID_PATTERN.match(
        str(value.get("provider_cluster_id", ""))
    ):
        raise ProvenanceError(
            f"{context} needs provider_cluster_id: the managed-cluster "
            "resource id the ancestry derivation starts from"
        )
    readonly_roles = value.get("provider_readonly_roles")
    if not isinstance(readonly_roles, list) or not all(
        isinstance(item, str)
        and item
        and not PROVIDER_ADMIN_ROLE_PATTERN.search(item)
        for item in readonly_roles
    ) or len(set(readonly_roles)) != len(readonly_roles):
        raise ProvenanceError(
            f"{context} needs provider_readonly_roles: the owner-enumerated "
            "harmless provider roles; an admin/editor/owner-class role can "
            "never be declared read-only, and any role OUTSIDE this list is "
            "treated as admin-class (fail closed)"
        )
    parent_ids = value.get("provider_parent_ids")
    if (
        not isinstance(parent_ids, list)
        or not parent_ids
        or len(set(parent_ids)) != len(parent_ids)
        or not all(
            isinstance(item, str) and PROVIDER_PARENT_ID_PATTERN.match(item)
            for item in parent_ids
        )
    ):
        raise ProvenanceError(
            f"{context} needs provider_parent_ids: the non-empty "
            "owner-enumerated provider resource ancestry (cluster, folder/"
            "project, cloud/tenant ids) whose LIVE access bindings the "
            "provider-boundary check enumerates through the pinned provider "
            "CLI — an omitted ancestry level hides admin grants"
        )
    if not isinstance(value.get("worm_bucket"), str) or not re.match(
        r"^[a-z0-9]([a-z0-9.-]{1,61}[a-z0-9])?$", str(value.get("worm_bucket"))
    ):
        raise ProvenanceError(
            f"{context} needs worm_bucket: the object-storage bucket whose "
            "LIVE object-lock/retention configuration the provider-boundary "
            "check reads through the pinned provider CLI"
        )
    security = value.get("security_principals")
    if (
        not isinstance(security, list)
        or not security
        or len(set(security)) != len(security)
        or not all(
            isinstance(item, str) and AUTOMATION_PRINCIPAL_PATTERN.match(item)
            for item in security
        )
    ):
        raise ProvenanceError(
            f"{context} needs a non-empty list of unique AUTOMATION security "
            "principals (system:serviceaccount:<namespace>:<name>); the "
            "external security-owned admission boundary is operated by a "
            "separate automation identity, never a human"
        )
    if set(security) & set(principals):
        raise ProvenanceError(
            f"{context} security principals must be DISJOINT from deploy "
            "principals: the identity that guards the provenance controls "
            "can never be the identity that deploys through them"
        )
    security_rows = {
        item[len("system:serviceaccount:"):]
        for item in security
        if item.startswith("system:serviceaccount:")
    }
    if security_rows & set(map(str, workload_accounts)):
        raise ProvenanceError(
            f"{context} workload_service_accounts may not enumerate a "
            "SECURITY principal: automation-written workloads must never "
            "run as the identity that guards the controls"
        )
    if not SHA256_PATTERN.match(str(value.get("verification_key_sha256", ""))):
        raise ProvenanceError(
            f"{context} needs the exact SHA-256 of the verification key"
        )
    if not SHA256_PATTERN.match(str(value.get("attestation_key_sha256", ""))):
        raise ProvenanceError(
            f"{context} needs attestation_key_sha256: the exact SHA-256 of "
            "the SEPARATE attestor public key that signs provider "
            "attestations; the provider-held boundary facts must never be "
            "self-attestable by the release pipeline key"
        )
    if str(value.get("attestation_key_sha256", "")) == str(
        value.get("verification_key_sha256", "")
    ):
        raise ProvenanceError(
            f"{context} attestation_key_sha256 must DIFFER from "
            "verification_key_sha256: an attestation signed by the release "
            "pipeline's own key is self-attestation, not external evidence"
        )
    if str(value.get("attestation_key_sha256", "")) != ATTESTATION_KEY_SHA256:
        raise ProvenanceError(
            f"{context} attestation_key_sha256 does not equal the "
            "SOURCE-PINNED attestor fingerprint (ATTESTATION_KEY_SHA256); "
            "the attestor is designated through code review, never delegated "
            "to whoever holds the release signing key"
            + (
                " — the shipped constant is EMPTY, so every attestation "
                "path fails closed until the owner commits the attestor "
                "fingerprint"
                if not ATTESTATION_KEY_SHA256
                else ""
            )
        )
    if not str(value.get("worm_store_uri", "")).strip() or not isinstance(
        value.get("worm_store_uri"), str
    ):
        raise ProvenanceError(
            f"{context} needs worm_store_uri: the exact off-host WORM store "
            "the anchored-heads snapshots live in; the attestation's "
            "worm_store must equal it, so the anchor location is owner "
            "authority, not a free-form attestation string"
        )
    if not SHA256_PATTERN.match(str(value.get("policy_sha256", ""))):
        raise ProvenanceError(
            f"{context} needs the exact SHA-256 of the committed admission "
            "policy manifest it authorizes"
        )
    exempt = value.get("iam_exempt_subjects")
    if (
        not isinstance(exempt, list)
        or len(set(exempt)) != len(exempt)
        or not all(
            isinstance(item, str)
            and (
                item in BOOTSTRAP_EXEMPTIBLE_SUBJECTS
                or item.startswith("ServiceAccount:kube-system:")
            )
            and RBAC_SUBJECT_PATTERN.match(item)
            for item in exempt
        )
    ):
        raise ProvenanceError(
            f"{context} iam_exempt_subjects may only name enumerated "
            "Kubernetes bootstrap identities or kube-system controller "
            "ServiceAccounts; arbitrary users/groups — and Group:system:"
            "masters in particular — are NEVER exemptible, so a human "
            "cluster-admin cannot be signed back into validity"
        )
    if not TOKEN_AUDIENCE_PATTERN.match(str(value.get("token_audience", ""))):
        raise ProvenanceError(
            f"{context} needs token_audience: the EXACT audience every "
            "automation-identity token projection must carry; any-nonempty "
            "audiences are not a contract"
        )
    tooling = value.get("tooling")
    if (
        not isinstance(tooling, dict)
        or set(tooling) != {"kubectl", "helm", "nebius"}
        or not all(
            isinstance(tooling[name], dict)
            and set(tooling[name]) == {"path", "sha256"}
            and isinstance(tooling[name].get("path"), str)
            and BINARY_PATH_PATTERN.match(tooling[name]["path"])
            and SHA256_PATTERN.match(str(tooling[name].get("sha256", "")))
            for name in ("kubectl", "helm", "nebius")
        )
    ):
        raise ProvenanceError(
            f"{context} needs owner-pinned tooling {{kubectl, helm, nebius}} "
            "each as {{path, sha256}}: the absolute binary path AND the "
            "exact digest of its bytes — a path alone can be repointed or "
            "overwritten; the binary CONTENT is the trust root"
        )
    authority = value.get("stage_binding_authority")
    if (
        not isinstance(authority, dict)
        or set(authority) != {"namespace", "workload"}
        or not NAMESPACE_PATTERN.match(str(authority.get("namespace", "")))
        or not WORKLOAD_REF_PATTERN.match(str(authority.get("workload", "")))
    ):
        raise ProvenanceError(
            f"{context} needs stage_binding_authority "
            "{namespace, workload}: the control-plane workload whose "
            "PostgreSQL state is the AUTHORITATIVE frozen-binding source"
        )
    return value


def _collapse_whitespace(value) -> str:
    return " ".join(str(value or "").split())


def _defaulted(value, default):
    # The API server persists defaults the committed YAML may omit
    # (e.g. matchPolicy: Equivalent, rule scope '*'); equality must compare
    # the EFFECTIVE configuration, not the spelling.
    return default if value in (None, "") else value


def _normalized_rule(rule: dict) -> dict:
    return {
        "apiGroups": list(rule.get("apiGroups") or []),
        "apiVersions": list(rule.get("apiVersions") or []),
        "operations": sorted(rule.get("operations") or []),
        "resources": list(rule.get("resources") or []),
        "resourceNames": sorted(rule.get("resourceNames") or []),
        "scope": _defaulted(rule.get("scope"), "*"),
    }


def _normalized_selector(selector) -> dict:
    selector = selector or {}
    return {
        "matchLabels": dict(selector.get("matchLabels") or {}),
        "matchExpressions": [
            {
                "key": expression.get("key"),
                "operator": expression.get("operator"),
                "values": sorted(expression.get("values") or []),
            }
            for expression in selector.get("matchExpressions") or []
        ],
    }


def _normalized_policy_spec(document: dict) -> dict:
    """Normalize EVERY behavior-bearing policy field, narrowing ones included.

    A live object that silently narrows enforcement — excludeResourceRules,
    an objectSelector, a namespaceSelector on matchConstraints, a changed
    matchPolicy, or injected matchConditions — must compare UNEQUAL, not be
    ignored.
    """
    spec = document.get("spec", {}) or {}
    constraints = spec.get("matchConstraints") or {}
    return {
        "failurePolicy": _defaulted(spec.get("failurePolicy"), "Fail"),
        "matchPolicy": _defaulted(constraints.get("matchPolicy"), "Equivalent"),
        "paramKind": {
            "apiVersion": (spec.get("paramKind") or {}).get("apiVersion"),
            "kind": (spec.get("paramKind") or {}).get("kind"),
        },
        "resourceRules": [
            _normalized_rule(rule)
            for rule in constraints.get("resourceRules") or []
        ],
        "excludeResourceRules": [
            _normalized_rule(rule)
            for rule in constraints.get("excludeResourceRules") or []
        ],
        "constraintNamespaceSelector": _normalized_selector(
            constraints.get("namespaceSelector")
        ),
        "constraintObjectSelector": _normalized_selector(
            constraints.get("objectSelector")
        ),
        "matchConditions": [
            {
                "name": condition.get("name"),
                "expression": _collapse_whitespace(condition.get("expression")),
            }
            for condition in spec.get("matchConditions") or []
        ],
        "variables": [
            {
                "name": variable.get("name"),
                "expression": _collapse_whitespace(variable.get("expression")),
            }
            for variable in spec.get("variables", [])
        ],
        "validations": [
            {
                "expression": _collapse_whitespace(validation.get("expression")),
                "reason": validation.get("reason"),
                "message": _collapse_whitespace(validation.get("message")),
            }
            for validation in spec.get("validations", [])
        ],
        "auditAnnotations": [
            {
                "key": annotation.get("key"),
                "valueExpression": _collapse_whitespace(
                    annotation.get("valueExpression")
                ),
            }
            for annotation in spec.get("auditAnnotations") or []
        ],
    }


def _normalized_binding_spec(document: dict) -> dict:
    """Normalize EVERY behavior-bearing binding field, narrowing ones included.

    The reproduced bypass compared a live binding carrying an exclude-all
    excludeResourceRules as EQUAL because only the namespaceSelector was
    normalized; every matchResources field now participates.
    """
    spec = document.get("spec", {}) or {}
    param_ref = spec.get("paramRef") or {}
    matches = spec.get("matchResources") or {}
    return {
        "policyName": spec.get("policyName"),
        "validationActions": sorted(spec.get("validationActions") or []),
        "paramRef": {
            "name": param_ref.get("name"),
            "namespace": param_ref.get("namespace"),
            "selector": _normalized_selector(param_ref.get("selector")),
            "parameterNotFoundAction": param_ref.get("parameterNotFoundAction"),
        },
        "matchPolicy": _defaulted(matches.get("matchPolicy"), "Equivalent"),
        "namespaceSelector": _normalized_selector(
            matches.get("namespaceSelector")
        ),
        "objectSelector": _normalized_selector(matches.get("objectSelector")),
        "resourceRules": [
            _normalized_rule(rule) for rule in matches.get("resourceRules") or []
        ],
        "excludeResourceRules": [
            _normalized_rule(rule)
            for rule in matches.get("excludeResourceRules") or []
        ],
    }


def _committed_all_documents(policy_path: Path) -> list[dict]:
    import yaml

    return [
        document
        for document in yaml.safe_load_all(
            _read_evidence_bytes(policy_path, private=False)
        )
        if document
    ]


def _committed_policy_documents(policy_path: Path) -> tuple[bytes, dict, dict]:
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - environment guard
        raise ProvenanceError(
            "PyYAML is required to prove the admission policy matches the "
            "owner scope; rendering fails closed"
        ) from error
    policy_bytes = _read_evidence_bytes(policy_path, private=False)
    try:
        documents = [
            document
            for document in yaml.safe_load_all(policy_bytes)
            if document
        ]
    except yaml.YAMLError as error:
        raise ProvenanceError(
            f"cannot parse the committed admission policy: {policy_path}"
        ) from error
    policy = next(
        (
            document
            for document in documents
            if document.get("kind") == "ValidatingAdmissionPolicy"
            and document.get("metadata", {}).get("name") == "fs2-image-provenance"
        ),
        None,
    )
    binding = next(
        (
            document
            for document in documents
            if document.get("kind") == "ValidatingAdmissionPolicyBinding"
            and document.get("spec", {}).get("policyName") == "fs2-image-provenance"
        ),
        None,
    )
    if policy is None or binding is None:
        raise ProvenanceError(
            f"the committed admission policy manifest {policy_path} lacks the "
            "fs2-image-provenance policy or binding; rendering fails closed"
        )
    return policy_bytes, policy, binding


def _assert_policy_matches_scope(
    owner_scope: dict,
    live_runner,
    policy_path: Path | None = None,
    binding_action_overrides: dict[str, tuple[list, str]] | None = None,
    skip_bindings: frozenset[str] | set[str] | None = None,
) -> None:
    """The signed scope, the committed policy, and the LIVE policy must agree.

    `binding_action_overrides` maps a protected binding name to the
    (validationActions, recovery-authorization sha256) an OWNER-SIGNED
    recovery legitimately applied: for exactly that binding the live actions
    must equal the authorized actions and the live object must carry the
    matching recovery annotation, while every OTHER field still equals the
    committed definition. Without this, a completed Audit/Warn break-glass
    could never pass its own post-check (live actions would differ from the
    committed Deny/Audit), making sanctioned recovery impossible to finish.

    Checked strictly, not by selector values alone:
    - the owner-signed scope pins the committed policy manifest by SHA-256;
    - the committed binding selects namespaces with operator `In` (a `NotIn`
      with identical values would invert the coverage), its values equal the
      scope's namespaces exactly, its validationActions include `Deny`
      (Audit-only enforcement is observation, not a boundary), and its
      paramRef fails closed with `parameterNotFoundAction: Deny` on the exact
      allow-list ConfigMap;
    - the committed policy fails closed, matches pods (incl. the
      pods/ephemeralcontainers subresource) and every workload controller,
      and carries all four validations;
    - the LIVE ValidatingAdmissionPolicy and binding, fetched through the
      authenticated API session, must equal the committed definitions on
      every enforced field — a missing, deleted, weakened, or Audit-only
      live object refuses rendering.
    """
    if policy_path is None:
        policy_path = Path(__file__).resolve().parent / "policy.yaml"
    overrides = binding_action_overrides or {}
    if not set(overrides) <= set(PROTECTED_POLICY_NAMES):
        raise ProvenanceError(
            "binding action overrides may only name protected bindings"
        )
    # `skip_bindings` exempts a protected binding from the LIVE equality and
    # annotation checks entirely — used ONLY for reconcile drift detection
    # when an owner-signed recovery document owns that exact binding, whose
    # state the fenced patch (never a bulk apply) will transition. Skipping
    # is never available to render paths: the caller decides, and postcheck
    # always uses strict overrides instead.
    skipped = frozenset(skip_bindings or ())
    if not skipped <= set(PROTECTED_POLICY_NAMES):
        raise ProvenanceError(
            "skip_bindings may only name protected bindings"
        )

    def expected_binding(name: str, committed: dict) -> dict:
        if name not in overrides:
            return committed
        actions, _ = overrides[name]
        adjusted = dict(committed)
        adjusted["validationActions"] = sorted(actions)
        return adjusted

    def assert_override_annotation(name: str, live_object: dict) -> None:
        if name not in overrides:
            return
        _, annotation_sha = overrides[name]
        live_annotations = (
            (live_object.get("metadata") or {}).get("annotations") or {}
        )
        if str(live_annotations.get(RECOVERY_ANNOTATION, "")) != str(
            annotation_sha
        ):
            raise ProvenanceError(
                f"the live binding {name} does not carry the {RECOVERY_ANNOTATION} "
                "annotation of the authorizing recovery document; weakened "
                "actions without the owner's annotation are drift, not "
                "recovery"
            )

    policy_bytes, policy, binding = _committed_policy_documents(policy_path)
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    if policy_sha256 != owner_scope["policy_sha256"]:
        raise ProvenanceError(
            f"the committed admission policy manifest hashes to "
            f"{policy_sha256}, but the owner-signed scope authorizes "
            f"{owner_scope['policy_sha256']}; align the scope with the "
            "reviewed policy revision"
        )
    committed_binding = _normalized_binding_spec(binding)
    expressions = committed_binding["namespaceSelector"]["matchExpressions"]
    if (
        len(expressions) != 1
        or expressions[0]["key"] != "kubernetes.io/metadata.name"
        or expressions[0]["operator"] != "In"
        or expressions[0]["values"] != sorted(owner_scope["namespaces"])
        or committed_binding["namespaceSelector"]["matchLabels"]
        or committed_binding["objectSelector"] != _normalized_selector(None)
        or committed_binding["resourceRules"]
        or committed_binding["excludeResourceRules"]
    ):
        raise ProvenanceError(
            "the committed admission policy binding must select exactly the "
            f"owner-approved namespaces {sorted(owner_scope['namespaces'])} "
            "with a single kubernetes.io/metadata.name In expression; the "
            "claimed coverage must equal the enforced coverage exactly"
        )
    if "Deny" not in committed_binding["validationActions"]:
        raise ProvenanceError(
            "the committed admission policy binding does not Deny; Audit-only "
            "enforcement is observation, not a security boundary — rendering "
            "fails closed"
        )
    if (
        committed_binding["paramRef"]["name"] != ALLOWLIST_NAME
        or committed_binding["paramRef"]["namespace"] != ALLOWLIST_NAMESPACE
        or committed_binding["paramRef"]["parameterNotFoundAction"] != "Deny"
    ):
        raise ProvenanceError(
            "the committed admission policy binding must reference the exact "
            f"allow-list ConfigMap {ALLOWLIST_NAMESPACE}/{ALLOWLIST_NAME} "
            "with parameterNotFoundAction: Deny"
        )
    committed_policy = _normalized_policy_spec(policy)
    if committed_policy["failurePolicy"] != "Fail":
        raise ProvenanceError(
            "the committed admission policy must set failurePolicy: Fail"
        )
    matched_resources = {
        resource
        for rule in committed_policy["resourceRules"]
        for resource in rule["resources"]
    }
    required_resources = {
        "pods",
        "pods/ephemeralcontainers",
        "deployments",
        "daemonsets",
        "statefulsets",
        "jobs",
        "cronjobs",
    }
    if not required_resources <= matched_resources:
        raise ProvenanceError(
            "the committed admission policy must match pods, the "
            "pods/ephemeralcontainers subresource, and every workload "
            f"controller; missing: {sorted(required_resources - matched_resources)}"
        )
    if len(committed_policy["validations"]) < 7:
        raise ProvenanceError(
            "the committed admission policy must carry the namespace, "
            "digest-pin, registry, platform-digest, automation-token, "
            "audience-exclusivity, and automation-writer workload "
            "validations"
        )
    # LIVE equality: the enforced objects in the cluster must equal the
    # committed, owner-pinned definitions. Absent objects fail closed.
    try:
        live_policy = json.loads(
            live_runner(
                [
                    "kubectl",
                    "get",
                    "validatingadmissionpolicy",
                    "fs2-image-provenance",
                    "-o",
                    "json",
                ]
            )
        )
        live_binding = json.loads(
            live_runner(
                [
                    "kubectl",
                    "get",
                    "validatingadmissionpolicybinding",
                    "fs2-image-provenance",
                    "-o",
                    "json",
                ]
            )
        )
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(
            "the LIVE fs2-image-provenance admission policy/binding cannot "
            "be read; rendering fails closed until the owner-pinned policy "
            "objects are applied and readable (apply the policy manifest "
            "first — it fails closed even before the ConfigMap exists)"
        ) from error
    if _normalized_policy_spec(live_policy) != committed_policy:
        raise ProvenanceError(
            "the LIVE fs2-image-provenance ValidatingAdmissionPolicy does "
            "not equal the committed, owner-pinned definition; a drifted or "
            "weakened live policy refuses rendering"
        )
    if "fs2-image-provenance" not in skipped:
        if _normalized_binding_spec(live_binding) != expected_binding(
            "fs2-image-provenance", committed_binding
        ):
            raise ProvenanceError(
                "the LIVE fs2-image-provenance binding does not equal the "
                "committed, owner-pinned definition (actions, paramRef, "
                "selectors, or resource rules drifted — e.g. Audit-only, "
                "NotIn, or an exclude-all narrowing); rendering fails closed"
            )
        assert_override_annotation("fs2-image-provenance", live_binding)
    # The security-owned guard must be live and identical too: rendering an
    # allow-list while the guard is absent or weakened would hand out a
    # release artifact whose protections do not actually exist.
    guard_policy = next(
        (
            document
            for document in _committed_all_documents(policy_path)
            if document.get("kind") == "ValidatingAdmissionPolicy"
            and document.get("metadata", {}).get("name") == "fs2-provenance-guard"
        ),
        None,
    )
    guard_binding = next(
        (
            document
            for document in _committed_all_documents(policy_path)
            if document.get("kind") == "ValidatingAdmissionPolicyBinding"
            and document.get("metadata", {}).get("name") == "fs2-provenance-guard"
        ),
        None,
    )
    if guard_policy is None or guard_binding is None:
        raise ProvenanceError(
            f"the committed manifest {policy_path} lacks the "
            "fs2-provenance-guard policy or binding; rendering fails closed"
        )
    try:
        live_guard_policy = json.loads(
            live_runner(
                [
                    "kubectl",
                    "get",
                    "validatingadmissionpolicy",
                    "fs2-provenance-guard",
                    "-o",
                    "json",
                ]
            )
        )
        live_guard_binding = json.loads(
            live_runner(
                [
                    "kubectl",
                    "get",
                    "validatingadmissionpolicybinding",
                    "fs2-provenance-guard",
                    "-o",
                    "json",
                ]
            )
        )
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(
            "the LIVE fs2-provenance-guard policy/binding cannot be read; "
            "the security-owned admission boundary must be applied before "
            "any allow-list renders — rendering fails closed"
        ) from error
    if _normalized_policy_spec(live_guard_policy) != _normalized_policy_spec(
        guard_policy
    ) or (
        "fs2-provenance-guard" not in skipped
        and _normalized_binding_spec(live_guard_binding)
        != expected_binding(
            "fs2-provenance-guard", _normalized_binding_spec(guard_binding)
        )
    ):
        raise ProvenanceError(
            "the LIVE fs2-provenance-guard does not equal the committed, "
            "owner-pinned definition; a weakened guard refuses rendering"
        )
    if "fs2-provenance-guard" not in skipped:
        assert_override_annotation("fs2-provenance-guard", live_guard_binding)
    helm_policy = next(
        (
            document
            for document in _committed_all_documents(policy_path)
            if document.get("kind") == "ValidatingAdmissionPolicy"
            and document.get("metadata", {}).get("name")
            == "fs2-helm-release-governance"
        ),
        None,
    )
    helm_binding = next(
        (
            document
            for document in _committed_all_documents(policy_path)
            if document.get("kind") == "ValidatingAdmissionPolicyBinding"
            and document.get("metadata", {}).get("name")
            == "fs2-helm-release-governance"
        ),
        None,
    )
    if helm_policy is None or helm_binding is None:
        raise ProvenanceError(
            f"the committed manifest {policy_path} lacks the "
            "fs2-helm-release-governance policy or binding; rendering fails "
            "closed"
        )
    try:
        live_helm_policy = json.loads(
            live_runner(
                [
                    "kubectl",
                    "get",
                    "validatingadmissionpolicy",
                    "fs2-helm-release-governance",
                    "-o",
                    "json",
                ]
            )
        )
        live_helm_binding = json.loads(
            live_runner(
                [
                    "kubectl",
                    "get",
                    "validatingadmissionpolicybinding",
                    "fs2-helm-release-governance",
                    "-o",
                    "json",
                ]
            )
        )
        live_guard_params = json.loads(
            live_runner(
                [
                    "kubectl",
                    "get",
                    "configmap",
                    GUARD_PARAMS_NAME,
                    "-n",
                    ALLOWLIST_NAMESPACE,
                    "-o",
                    "json",
                ]
            )
        )
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(
            "the LIVE fs2-helm-release-governance objects or the "
            f"{GUARD_PARAMS_NAME} ConfigMap cannot be read; rendering fails "
            "closed"
        ) from error
    if _normalized_policy_spec(live_helm_policy) != _normalized_policy_spec(
        helm_policy
    ) or (
        "fs2-helm-release-governance" not in skipped
        and _normalized_binding_spec(live_helm_binding)
        != expected_binding(
            "fs2-helm-release-governance",
            _normalized_binding_spec(helm_binding),
        )
    ):
        raise ProvenanceError(
            "the LIVE fs2-helm-release-governance does not equal the "
            "committed, owner-pinned definition; a weakened Helm-governance "
            "control refuses rendering"
        )
    if "fs2-helm-release-governance" not in skipped:
        assert_override_annotation(
            "fs2-helm-release-governance", live_helm_binding
        )
    # The guard-params ConfigMap is DERIVED STATE, never authority: its live
    # content must equal what the owner-signed scope renders.
    expected_params = render_guard_params(owner_scope["security_principals"])
    if (live_guard_params.get("data") or {}) != expected_params["data"]:
        raise ProvenanceError(
            f"the LIVE {GUARD_PARAMS_NAME} ConfigMap does not equal the "
            "owner-signed scope's security principals; in-cluster parameters "
            "are derived state and never authority — rendering fails closed"
        )


def _ledger_checkpoint_path(path: Path) -> Path:
    return path.parent / (path.name + ".head.json")


def _write_all(descriptor: int, payload: bytes) -> None:
    """os.write may return SHORT; loop until every byte is written."""
    written = 0
    while written < len(payload):
        written += os.write(descriptor, payload[written:])


def _ledger_segments(path: Path) -> list[Path]:
    """A ledger is the base file plus chained continuation segments.

    A crash can tear the tail of a segment mid-write; under the no-delete
    constraint the torn bytes are never removed, so the chain CONTINUES in
    `<name>.cont<N>` — every segment's records chain across the boundary by
    prev-hash, and readers ignore exactly the unterminated tail fragment.
    """
    segments = [path]
    index = 1
    while Path(f"{path}.cont{index}").exists():
        segments.append(Path(f"{path}.cont{index}"))
        index += 1
    return segments


def _ledger_scan(path: Path) -> dict:
    """Walk every segment verifying the hash chain end to end.

    Returns {records, lines (raw valid line bytes), count, head, torn_tail}.
    A TERMINATED malformed line anywhere is tampering and fails closed; an
    UNTERMINATED final fragment (no trailing newline — the signature of a
    crash mid-append, since appends write line+newline as one buffer) is
    tolerated as a torn tail: it is NOT a record, its bytes stay preserved,
    and later appends continue in the next segment.
    """
    records: list[dict] = []
    lines: list[bytes] = []
    previous = GENESIS_HASH
    torn_tail = False
    segments = [
        segment for segment in _ledger_segments(path) if segment.exists()
    ]
    for position, segment in enumerate(segments):
        if segment.is_symlink():
            raise ProvenanceError(f"ledger must not be a symlink: {segment}")
        if torn_tail:
            raise ProvenanceError(
                f"ledger segment {segment} follows a segment whose torn "
                "tail was never sealed by a continuation append; fails closed"
            )
        payload = _read_evidence_bytes(segment)
        pieces = payload.split(b"\n")
        terminated, fragment = pieces[:-1], pieces[-1]
        for line in terminated:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ProvenanceError(
                    f"ledger {segment} is malformed; fails closed"
                ) from error
            if record.get("prev") != previous:
                raise ProvenanceError(
                    f"ledger {segment} breaks its hash chain; fails closed"
                )
            previous = hashlib.sha256(line).hexdigest()
            records.append(record)
            lines.append(bytes(line))
        if fragment:
            if position == len(segments) - 1:
                torn_tail = True
            else:
                # A mid-chain segment may carry a sealed torn tail exactly
                # when a continuation follows it (the continuation's first
                # record chains to the last VALID line, proven above).
                torn_tail = False
    return {
        "records": records,
        "lines": lines,
        "count": len(records),
        "head": previous if records else GENESIS_HASH,
        "torn_tail": torn_tail,
        "segments": segments,
    }


def _read_chained_records(path: Path, adopt_legacy: bool = False) -> list[dict]:
    """Read a chained ledger VERIFYING every link and the head checkpoint.

    A JSONL file is mutable on disk; trusting it raw would let truncation
    silently un-consume an authorization (replay) or drop a journaled
    intent. Every record must chain to its predecessor by hash — across
    continuation segments too — and the companion head checkpoint (record
    count + terminal line hash, rewritten on every append) must match, so
    rewriting, splicing, and SUFFIX truncation are detected and fail closed.

    A TORN TAIL (unterminated final fragment — the crash-mid-append
    signature) is tolerated ONLY when the checkpoint accounts exactly for
    the valid records before it; an unaccounted torn tail is tampering. A
    NONEMPTY file with NO checkpoint is legacy content adopted ONLY when
    `adopt_legacy=True`, which callers set exclusively inside anchored-heads
    verification after the SIGNED anchor confirmed the exact content.
    """
    if not path.exists():
        if _ledger_checkpoint_path(path).exists():
            raise ProvenanceError(
                f"ledger {path} is missing but its checkpoint exists; a "
                "deleted ledger fails closed"
            )
        return []
    scan = _ledger_scan(path)
    checkpoint_path = _ledger_checkpoint_path(path)

    def write_checkpoint_forward() -> None:
        # ADDITIVE adoption/repair: the checkpoint only ever moves FORWARD to
        # match a fully chain-verified store; nothing is deleted or rewound.
        payload = json.dumps(
            {"count": scan["count"], "head": scan["head"]}, sort_keys=True
        ).encode("utf-8")
        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix="." + checkpoint_path.name + "-"
        )
        try:
            _write_all(descriptor, payload)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp_name, checkpoint_path)
        _fsync_dir(path.parent)

    if not checkpoint_path.exists():
        if scan["count"] or scan["torn_tail"]:
            if not adopt_legacy or scan["torn_tail"] or len(scan["segments"]) > 1:
                raise ProvenanceError(
                    f"ledger {path} carries records but no head checkpoint; "
                    "a locally verifiable-but-unsigned legacy chain is "
                    "adopted ONLY through anchored-heads verification "
                    "(single clean segment, exact anchored content) — "
                    "fails closed"
                )
            # ANCHORED LEGACY ADOPTION: the caller has already verified this
            # exact content against the signed off-host anchor.
            write_checkpoint_forward()
        return scan["records"]
    try:
        checkpoint = json.loads(_read_evidence_bytes(checkpoint_path))
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"ledger checkpoint is malformed: {checkpoint_path}"
        ) from error
    if not isinstance(checkpoint, dict):
        raise ProvenanceError(
            f"ledger checkpoint is malformed: {checkpoint_path}"
        )
    if scan["torn_tail"]:
        # The torn fragment is a crash artifact ONLY when the checkpoint
        # accounts exactly for every valid record before it.
        if (
            checkpoint.get("count") == scan["count"]
            and checkpoint.get("head") == scan["head"]
        ):
            return scan["records"]
        raise ProvenanceError(
            f"ledger {path} carries a torn tail the checkpoint does not "
            "account for; a tampered or truncated store fails closed"
        )
    if (
        checkpoint.get("count") == scan["count"]
        and checkpoint.get("head") == scan["head"]
    ):
        return scan["records"]
    # CRASH ROLL-FORWARD: an append fsyncs the JSONL before replacing the
    # checkpoint, so a crash between the two leaves the store exactly ONE
    # verified record ahead of the checkpoint — recognized and repaired
    # forward. Anything else is truncation or rewriting and fails closed.
    if (
        scan["count"] >= 1
        and checkpoint.get("count") == scan["count"] - 1
        and checkpoint.get("head")
        == (
            hashlib.sha256(scan["lines"][-2]).hexdigest()
            if scan["count"] >= 2
            else GENESIS_HASH
        )
    ):
        write_checkpoint_forward()
        return scan["records"]
    raise ProvenanceError(
        f"ledger {path} does not match its head checkpoint (count/head); "
        "truncated or rewritten history fails closed"
    )


def _append_chained_record(path: Path, record: dict) -> None:
    """Append one hash-chained record crash-safely and advance the checkpoint.

    The whole store is VERIFIED before every append; the record chains to
    the current head; the write loops until every byte (including the
    terminating newline) is on disk and fsync'd BEFORE the checkpoint
    advances. When the active segment carries a torn tail (crash artifact,
    never deleted), the append opens the next continuation segment so the
    chain grows past the preserved fragment.
    """
    existing = _read_chained_records(path)
    scan = _ledger_scan(path) if path.exists() else {
        "count": 0,
        "head": GENESIS_HASH,
        "torn_tail": False,
        "segments": [path],
    }
    if len(existing) != scan["count"]:
        raise ProvenanceError(
            f"ledger {path} changed between verification and append; fails "
            "closed"
        )
    target = scan["segments"][-1] if scan["segments"] else path
    if scan["torn_tail"]:
        target = Path(f"{path}.cont{len(scan['segments'])}")
    line = json.dumps({**record, "prev": scan["head"]}, sort_keys=True).encode(
        "utf-8"
    )
    descriptor = os.open(
        target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        _write_all(descriptor, line + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    checkpoint_path = _ledger_checkpoint_path(path)
    checkpoint_payload = json.dumps(
        {
            "count": scan["count"] + 1,
            "head": hashlib.sha256(line).hexdigest(),
        },
        sort_keys=True,
    ).encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix="." + checkpoint_path.name + "-"
    )
    try:
        _write_all(descriptor, checkpoint_payload)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp_name, checkpoint_path)
    _fsync_dir(path.parent)


def _consume_ledger_path(run_root: Path) -> Path:
    return run_root / "release-authorization-consumed.jsonl"


def _is_consumed(run_root: Path, document_sha256: str) -> bool:
    for record in _read_chained_records(_consume_ledger_path(run_root)):
        if record.get("sha256") == document_sha256:
            return True
        for entry in record.get("consumed") or []:
            if isinstance(entry, dict) and entry.get("sha256") == document_sha256:
                return True
    return False


def _record_consumed(run_root: Path, document_sha256: str, purpose: str) -> None:
    _record_consumed_batch(run_root, [(document_sha256, purpose)])


def _record_consumed_batch(
    run_root: Path, entries: Sequence[tuple[str, str]]
) -> None:
    """Consume several documents in ONE chained append — atomically.

    Consuming a rollout authorization and its recovery document in two
    separate appends leaves a crash window where the authorization is
    consumed but the recovery is not: --execute refuses (single-use) AND
    --resume refuses (recovery never consumed) — a wedge. One record, one
    fsync'd append, no window.
    """
    _append_chained_record(
        _consume_ledger_path(run_root),
        {
            "consumed": [
                {"sha256": sha256_hex, "purpose": purpose}
                for sha256_hex, purpose in entries
            ],
            "consumed_at": datetime.now(UTC).isoformat(),
        },
    )


def load_recovery_authorization(
    recovery_path: Path,
    public_key_path: str,
    verifier=None,
    run_root: Path | None = None,
) -> tuple[dict, str]:
    """Verify an OWNER-SIGNED break-glass recovery authorization.

    The reversible Audit/Warn toggle on a protected binding requires the
    guard's recovery annotation to carry the SHA-256 of this document; the
    security automation runs this verification BEFORE applying the toggle.
    The document is owner-signed (detached cosign signature over the exact
    bytes), names one protected binding and the exact validationActions to
    set, is bound to a tracking identifier, and is valid only inside a
    bounded time window — deletion is never a recovery action.
    """
    signature_path = recovery_path.parent / (recovery_path.name + ".sig")
    if not recovery_path.is_file() or recovery_path.is_symlink():
        raise ProvenanceError(
            f"missing recovery authorization: {recovery_path}"
        )
    if not signature_path.is_file() or signature_path.is_symlink():
        raise ProvenanceError(
            f"recovery authorization at {recovery_path} is UNSIGNED "
            f"({signature_path} is missing); break-glass fails closed"
        )
    payload = _read_evidence_bytes(recovery_path, private=False)
    signature = _read_evidence_bytes(signature_path, private=False)
    _verify_blob_bytes(
        public_key_path,
        payload,
        signature,
        verifier,
        f"recovery authorization {recovery_path}",
    )
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"recovery authorization is malformed: {recovery_path}"
        ) from error
    if not isinstance(document, dict) or document.get("schema") != RECOVERY_SCHEMA:
        raise ProvenanceError(
            f"{recovery_path} is not a {RECOVERY_SCHEMA} document"
        )
    if document.get("target") not in PROTECTED_POLICY_NAMES:
        raise ProvenanceError(
            f"{recovery_path} does not target a protected binding"
        )
    # State fences: the authorization binds one exact cluster and one exact
    # OBSERVED object state; applying it against anything else is refused by
    # the API server itself (uid + resourceVersion preconditions in the
    # patch), so a captured document cannot replay after the state moved.
    if not CLUSTER_PATTERN.match(str(document.get("cluster", ""))):
        raise ProvenanceError(
            f"{recovery_path} must pin the cluster (kube-system namespace UID)"
        )
    if not re.match(r"^[0-9a-f-]{16,64}$", str(document.get("target_uid", ""))):
        raise ProvenanceError(
            f"{recovery_path} must pin the target object's UID"
        )
    if not re.match(
        r"^[0-9]{1,20}$", str(document.get("target_resource_version", ""))
    ):
        raise ProvenanceError(
            f"{recovery_path} must pin the target object's resourceVersion"
        )
    prior = document.get("prior_actions")
    if not isinstance(prior, list) or sorted(prior) not in (
        ["Audit", "Deny"],
        ["Audit", "Warn"],
    ):
        raise ProvenanceError(
            f"{recovery_path} must record the exact prior validationActions "
            "(one sanctioned state) so the toggle is provably reversible"
        )
    actions = document.get("actions")
    if not isinstance(actions, list) or sorted(actions) not in (
        ["Audit", "Deny"],
        ["Audit", "Warn"],
    ):
        raise ProvenanceError(
            f"{recovery_path} must name exactly one sanctioned enforcement "
            "state: [Deny, Audit] (enforce/restore) or [Audit, Warn] "
            "(observation break-glass); arbitrary action subsets and "
            "deletion are never recovery"
        )
    if not DRAIN_REASON_PATTERN.match(str(document.get("reason", ""))):
        raise ProvenanceError(
            f"{recovery_path} needs a reason bound to a tracking identifier "
            "(incident:/change:/ticket:/task:)"
        )
    issued_at = _parse_rfc3339(
        str(document.get("issued_at", "")), f"{recovery_path} issued_at"
    )
    expires_at = _parse_rfc3339(
        str(document.get("expires_at", "")), f"{recovery_path} expires_at"
    )
    validity = (expires_at - issued_at).total_seconds()
    if not 0 < validity <= RECOVERY_MAX_VALIDITY_HOURS * 3600:
        raise ProvenanceError(
            f"{recovery_path} validity window must be positive and at most "
            f"{RECOVERY_MAX_VALIDITY_HOURS}h"
        )
    now = datetime.now(UTC)
    if (issued_at - now).total_seconds() > _CLOCK_SKEW_SECONDS:
        raise ProvenanceError(f"{recovery_path} is not yet valid")
    if now > expires_at:
        raise ProvenanceError(f"{recovery_path} has expired")
    document_sha256 = hashlib.sha256(payload).hexdigest()
    if run_root is not None and _is_consumed(run_root, document_sha256):
        raise ProvenanceError(
            f"{recovery_path} was already consumed; a recovery authorization "
            "is SINGLE-USE — the owner issues a new one for a new action"
        )
    return document, document_sha256


def load_provider_attestation(
    attestation_path: Path,
    public_key_path: str,
    verifier=None,
) -> dict:
    """Verify the ATTESTOR-SIGNED provider attestation — evidence, not comments.

    The provider-held arm of the boundary (system:masters certificate
    issuance, apiserver/static admission control, etcd/node access) cannot
    be observed through the Kubernetes API, so it enters the system as a
    SIGNED, cluster-pinned, time-bounded document. `public_key_path` here is
    the SEPARATE attestor key pinned by the owner-signed scope's
    attestation_key_sha256 — REQUIRED to differ from the release
    verification key, so the release pipeline can never attest its own
    boundary (non-self-attested by key separation; custody of the attestor
    private key outside the pipeline is completed by the owner at the
    rollout window). The document must also bind concrete provider evidence
    (the SHA-256 of the exported provider IAM policy plus a tracking
    reference) and embed the latest off-host anchored-heads snapshot
    covering EVERY required chain (plus the WORM store URI), which callers
    enforce against the local chains — anchor verification is never
    optional, manual, or unsigned, and an empty/omitted chain never
    verifies. The shipped repository carries NO attestation: rendering
    fails closed until the attestor issues and signs one.
    """
    signature_path = attestation_path.parent / (attestation_path.name + ".sig")
    if not attestation_path.is_file() or attestation_path.is_symlink():
        raise ProvenanceError(
            f"missing provider attestation: {attestation_path}; rendering "
            "fails closed until the attestor signs the provider-held facts"
        )
    if not signature_path.is_file() or signature_path.is_symlink():
        raise ProvenanceError(
            f"provider attestation at {attestation_path} is UNSIGNED; "
            "rendering fails closed"
        )
    payload = _read_evidence_bytes(attestation_path, private=False)
    signature = _read_evidence_bytes(signature_path, private=False)
    _verify_blob_bytes(
        public_key_path,
        payload,
        signature,
        verifier,
        f"provider attestation {attestation_path}",
    )
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"provider attestation is malformed: {attestation_path}"
        ) from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != PROVIDER_ATTESTATION_SCHEMA
    ):
        raise ProvenanceError(
            f"{attestation_path} is not a {PROVIDER_ATTESTATION_SCHEMA} "
            "document"
        )
    for fact in (
        "masters_certificate_issuance",
        "apiserver_control",
        "etcd_access",
    ):
        if str(document.get(fact, "")) != "provider-held":
            raise ProvenanceError(
                f"{attestation_path} does not attest {fact} as "
                "provider-held; rendering fails closed"
            )
    if not CLUSTER_PATTERN.match(str(document.get("cluster", ""))):
        raise ProvenanceError(
            f"{attestation_path} must pin the cluster (kube-system UID)"
        )
    if not str(document.get("worm_store", "")).strip():
        raise ProvenanceError(
            f"{attestation_path} must name the off-host WORM store"
        )
    evidence = document.get("evidence")
    admin_subjects = (
        evidence.get("provider_admin_subjects")
        if isinstance(evidence, dict)
        else None
    )
    if (
        not isinstance(evidence, dict)
        or not DRAIN_REASON_PATTERN.match(str(evidence.get("reference", "")))
        or not isinstance(admin_subjects, list)
        or not admin_subjects
        or len(set(admin_subjects)) != len(admin_subjects)
        or not all(
            isinstance(item, str) and PRINCIPAL_PATTERN.match(item)
            for item in admin_subjects
        )
    ):
        raise ProvenanceError(
            f"{attestation_path} must bind concrete provider evidence: "
            "evidence.reference (a tracking identifier) and "
            "evidence.provider_admin_subjects (the non-empty enumerated "
            "subjects allowed to hold provider admin-class roles, which the "
            "LIVE provider enumeration is compared against); bare "
            "provider-held strings are assertions, not evidence"
        )
    anchor_object = document.get("anchor_object")
    if (
        not isinstance(anchor_object, dict)
        or not str(anchor_object.get("key", "")).strip()
        or not str(anchor_object.get("version_id", "")).strip()
        or not SHA256_PATTERN.match(str(anchor_object.get("sha256", "")))
    ):
        raise ProvenanceError(
            f"{attestation_path} must pin the exact WORM anchor object "
            "(anchor_object.key + version_id + sha256); an anchor that "
            "cannot be fetched and byte-verified from the store is a claim, "
            "not a proof"
        )
    anchored = document.get("anchored_heads")
    if not isinstance(anchored, dict) or not isinstance(
        anchored.get("chains"), dict
    ):
        raise ProvenanceError(
            f"{attestation_path} must embed the anchored-heads snapshot"
        )
    chains = anchored["chains"]
    missing_chains = [name for name in REQUIRED_ANCHOR_CHAINS if name not in chains]
    if missing_chains:
        raise ProvenanceError(
            f"{attestation_path} anchored-heads snapshot omits required "
            f"chains {missing_chains}; a snapshot that does not enumerate "
            "every tamper-evident chain anchors nothing for it — issue the "
            "attestation from a full export-anchored-heads snapshot"
        )
    for name, state in chains.items():
        count = state.get("count") if isinstance(state, dict) else None
        head = state.get("head") if isinstance(state, dict) else None
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or not isinstance(head, str)
        ):
            raise ProvenanceError(
                f"{attestation_path} anchored chain {name!r} is malformed; "
                "each chain needs an integer count and a head digest"
            )
    issued_at = _parse_rfc3339(
        str(document.get("issued_at", "")), f"{attestation_path} issued_at"
    )
    expires_at = _parse_rfc3339(
        str(document.get("expires_at", "")), f"{attestation_path} expires_at"
    )
    validity = (expires_at - issued_at).total_seconds()
    if not 0 < validity <= 30 * 24 * 3600:
        raise ProvenanceError(
            f"{attestation_path} validity must be positive and at most 30 days"
        )
    now = datetime.now(UTC)
    if (issued_at - now).total_seconds() > _CLOCK_SKEW_SECONDS:
        raise ProvenanceError(f"{attestation_path} is not yet valid")
    if now > expires_at:
        raise ProvenanceError(f"{attestation_path} has expired")
    return document


def _provider_field(node, names: tuple[str, ...]):
    """Extract ONE unambiguous field by exact normalized key, nested.

    Substring markers are how a disabled lock with mode text slipped past a
    parser; this walks dictionaries comparing exact normalized key names and
    refuses AMBIGUITY (two different values for the same logical field).
    """
    wanted = {name.replace("_", "").lower() for name in names}
    found: list = []

    def walk(candidate):
        if isinstance(candidate, dict):
            for key, value in candidate.items():
                if str(key).replace("_", "").lower() in wanted:
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(candidate, list):
            for item in candidate:
                walk(item)

    walk(node)
    unique = {json.dumps(item, sort_keys=True) for item in found}
    if len(unique) > 1:
        raise ProvenanceError(
            f"provider answer carries AMBIGUOUS values for {names}; refusing"
        )
    return found[0] if found else None


def _provider_cli(owner_scope: dict, runner, *arguments: str) -> str:
    """Every provider-CLI call is endpoint-pinned and format-fixed."""
    return runner(
        [
            "nebius",
            *arguments,
            "--endpoint",
            str(owner_scope["provider_endpoint"]),
            "--format",
            "json",
        ]
    )


def _provider_access_bindings(
    owner_scope: dict, runner, parent_id: str
) -> list[tuple[str, str]]:
    """Fully paginated (subject, role) enumeration for one parent."""
    bindings: list[tuple[str, str]] = []
    page_token = ""
    for _ in range(100):
        arguments = [
            "iam",
            "access-binding",
            "list",
            "--parent-id",
            str(parent_id),
            "--page-size",
            "1000",
        ]
        if page_token:
            arguments += ["--page-token", page_token]
        listing = json.loads(_provider_cli(owner_scope, runner, *arguments))
        items = listing.get("items") if isinstance(listing, dict) else listing
        if not isinstance(items, list):
            raise ProvenanceError(
                f"the provider access-binding listing for {parent_id} has "
                "no recognizable items; the provider boundary is "
                "unverifiable — fails closed"
            )
        for item in items:
            if not isinstance(item, dict):
                raise ProvenanceError(
                    f"unrecognizable provider access binding under "
                    f"{parent_id}; fails closed"
                )
            role = str(
                item.get("role_id")
                or item.get("roleId")
                or item.get("role")
                or ""
            )
            subject_field = item.get("subject")
            if isinstance(subject_field, dict):
                subject = str(
                    subject_field.get("id") or subject_field.get("name") or ""
                )
            else:
                subject = str(subject_field or "")
            if not role or not subject:
                raise ProvenanceError(
                    f"a provider access binding under {parent_id} lacks a "
                    "readable role/subject; fails closed"
                )
            bindings.append((subject, role))
        token_value = (
            listing.get("next_page_token") or listing.get("nextPageToken")
            if isinstance(listing, dict)
            else None
        )
        if not token_value:
            return sorted(bindings)
        page_token = str(token_value)
    raise ProvenanceError(
        f"the provider access-binding listing for {parent_id} did not "
        "terminate within 100 pages; fails closed"
    )


def _derived_provider_ancestry(owner_scope: dict, runner) -> list[str]:
    """Derive cluster -> folder -> cloud LIVE; owner list must equal it.

    An owner-typed parent list alone could omit a level; the ancestry is
    therefore DERIVED from the managed cluster resource itself and the
    owner enumeration must equal the derived chain exactly.
    """
    cluster_id = str(owner_scope["provider_cluster_id"])
    cluster = json.loads(
        _provider_cli(owner_scope, runner, "mk8s", "cluster", "get", "--id", cluster_id)
    )
    folder_id = str(
        _provider_field(cluster, ("folder_id", "parent_id")) or ""
    )
    if not folder_id:
        raise ProvenanceError(
            "the managed cluster resource exposes no folder/parent id; the "
            "provider ancestry cannot be derived — fails closed"
        )
    folder = json.loads(
        _provider_cli(
            owner_scope, runner, "resource-manager", "folder", "get", "--id", folder_id
        )
    )
    cloud_id = str(
        _provider_field(folder, ("cloud_id", "parent_id")) or ""
    )
    if not cloud_id:
        raise ProvenanceError(
            "the folder resource exposes no cloud/parent id; the provider "
            "ancestry cannot be derived — fails closed"
        )
    derived = [cluster_id, folder_id, cloud_id]
    if sorted(derived) != sorted(map(str, owner_scope["provider_parent_ids"])):
        raise ProvenanceError(
            f"the DERIVED provider ancestry {derived} does not equal the "
            "owner-enumerated provider_parent_ids; an omitted or substituted "
            "ancestry level hides grants — fails closed"
        )
    return derived


def _assert_provider_principal(owner_scope: dict, runner) -> None:
    whoami = json.loads(_provider_cli(owner_scope, runner, "iam", "whoami"))
    identity = str(
        _provider_field(whoami, ("id", "login", "email", "account")) or ""
    )
    if identity != str(owner_scope["provider_principal"]):
        raise ProvenanceError(
            f"the provider CLI authenticates as {identity!r}, not the "
            "owner-pinned provider_principal; an unexpected credential's "
            "answers are never trusted — fails closed"
        )


def _assert_provider_boundary(
    owner_scope: dict, attestation: dict, runner
) -> None:
    """Enforce the provider arm against LIVE, provider-native answers.

    The authority is the provider's own API through the owner-pinned,
    digest-verified CLI at the owner-pinned endpoint, authenticated as the
    owner-pinned read-only principal:

    - ANCESTRY is DERIVED live (cluster -> folder -> cloud) and must equal
      the owner enumeration; every level's access bindings are fetched with
      full pagination and enumerated TWICE (a mid-enumeration mutation is a
      refusal, not a race window).
    - ROLE semantics are fail-closed: a binding's role must be in the
      owner-enumerated read-only role list, or its subject must be attested
      as a provider admin — an UNKNOWN role is admin-class by default, and
      an admin/editor/owner-class role can never be declared read-only.
    - WORM is proven by exact fields, then by the OBJECT: the bucket's
      configuration must show name equality, ancestry membership,
      versioning enabled, object-lock status exactly enabled and mode
      exactly COMPLIANCE (governance is bypassable and refused), retention
      >= the required days — via exact normalized keys, never substring
      markers — and the attested anchor OBJECT (exact key + version id) is
      DOWNLOADED, its bytes hash-verified against the attestation pin, and
      required to equal the embedded anchored-heads snapshot byte-for-value.
    """
    _assert_provider_principal(owner_scope, runner)
    ancestry = _derived_provider_ancestry(owner_scope, runner)
    allowed_admins = set(
        map(str, (attestation.get("evidence") or {}).get(
            "provider_admin_subjects"
        ) or [])
    )
    readonly_roles = set(map(str, owner_scope["provider_readonly_roles"]))
    try:
        passes = []
        for _ in range(2):
            snapshot = {
                parent_id: _provider_access_bindings(
                    owner_scope, runner, parent_id
                )
                for parent_id in ancestry
            }
            passes.append(snapshot)
        if passes[0] != passes[1]:
            raise ProvenanceError(
                "the provider IAM surface changed between enumeration "
                "passes; an unstable answer is never a boundary proof — "
                "fails closed"
            )
        rogue = sorted(
            {
                f"{subject} holds {role} on {parent_id}"
                for parent_id, bindings in passes[0].items()
                for subject, role in bindings
                if not (
                    role in readonly_roles
                    and not PROVIDER_ADMIN_ROLE_PATTERN.search(role)
                )
                and subject not in allowed_admins
            }
        )
        bucket = json.loads(
            _provider_cli(
                owner_scope,
                runner,
                "storage",
                "bucket",
                "get",
                "--name",
                str(owner_scope["worm_bucket"]),
            )
        )
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(
            "the live provider boundary cannot be enumerated through the "
            "owner-pinned provider CLI; the provider arm is unverifiable — "
            "fails closed"
        ) from error
    if rogue:
        raise ProvenanceError(
            "the LIVE provider IAM surface shows non-read-only access "
            "outside the attested subject enumeration: "
            + "; ".join(rogue[:10])
            + " — the provider boundary does not hold, fails closed"
        )
    _assert_worm_lock(bucket, owner_scope, ancestry)
    _assert_worm_anchor_object(owner_scope, attestation, runner)


def _assert_worm_lock(
    bucket: dict, owner_scope: dict, ancestry: list[str]
) -> None:
    """Exact-field WORM proof: name, ancestry, versioning, lock, retention."""
    bucket_name = str(owner_scope["worm_bucket"])
    live_name = str(_provider_field(bucket, ("name", "bucket_name")) or "")
    if live_name != bucket_name:
        raise ProvenanceError(
            f"the provider returned bucket {live_name!r}, not the pinned "
            f"{bucket_name!r}; fails closed"
        )
    bucket_parent = str(
        _provider_field(bucket, ("folder_id", "project_id")) or ""
    )
    if bucket_parent not in ancestry:
        raise ProvenanceError(
            f"bucket {bucket_name} belongs to {bucket_parent!r}, outside "
            "the derived provider ancestry; a look-alike bucket in a "
            "foreign project is never the WORM store — fails closed"
        )
    versioning = str(
        _provider_field(bucket, ("versioning", "versioning_status")) or ""
    ).lower()
    if versioning not in ("enabled", "versioning_enabled", "true"):
        raise ProvenanceError(
            f"bucket {bucket_name} versioning is {versioning!r}, not "
            "enabled; anchors could be overwritten in place — fails closed"
        )
    lock = _provider_field(
        bucket, ("object_lock", "object_lock_configuration", "lock_configuration")
    )
    if not isinstance(lock, dict):
        raise ProvenanceError(
            f"bucket {bucket_name} shows no object-lock configuration; a "
            "WORM URI without a live lock is a claim, not a proof — fails "
            "closed"
        )
    status = str(_provider_field(lock, ("status", "state")) or "").lower()
    if status not in ("enabled", "object_lock_status_enabled", "active"):
        raise ProvenanceError(
            f"bucket {bucket_name} object-lock status is {status!r}, not "
            "enabled; fails closed"
        )
    mode = str(_provider_field(lock, ("mode", "retention_mode")) or "").lower()
    if "compliance" not in mode.split("_")[-1:] and mode != "compliance":
        raise ProvenanceError(
            f"bucket {bucket_name} object-lock mode is {mode!r}; only "
            "COMPLIANCE mode is non-bypassable (governance can be lifted) — "
            "fails closed"
        )
    retention_value = _provider_field(
        lock, ("days", "retention_period_days", "period_days")
    )
    try:
        retention = int(str(retention_value))
    except (TypeError, ValueError):
        retention = -1
    if retention < WORM_MIN_RETENTION_DAYS:
        raise ProvenanceError(
            f"bucket {bucket_name} default retention "
            f"({retention_value!r}) is absent or below the required "
            f"{WORM_MIN_RETENTION_DAYS} days; fails closed"
        )


def _assert_worm_anchor_object(
    owner_scope: dict, attestation: dict, runner
) -> None:
    """The anchor is proven by the WORM OBJECT itself, not a config claim.

    The attestation pins the exact anchor object (key + version id + byte
    digest); that exact VERSION is downloaded from the WORM bucket, its
    bytes must hash to the pin, and its content must equal the attestation's
    embedded anchored-heads snapshot — so the enforced anchor is the one
    that actually sits immutable in the store.
    """
    anchor_object = attestation.get("anchor_object") or {}
    try:
        payload = _provider_cli(
            owner_scope,
            runner,
            "storage",
            "object",
            "download",
            "--bucket",
            str(owner_scope["worm_bucket"]),
            "--key",
            str(anchor_object.get("key", "")),
            "--version-id",
            str(anchor_object.get("version_id", "")),
        )
    except (subprocess.CalledProcessError, OSError) as error:
        raise ProvenanceError(
            "the attested WORM anchor object cannot be downloaded; the "
            "anchor is unverifiable — fails closed"
        ) from error
    payload_bytes = payload.encode("utf-8")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    if digest != str(anchor_object.get("sha256", "")):
        raise ProvenanceError(
            f"the downloaded WORM anchor object hashes to {digest}, not the "
            "attested pin; a substituted anchor object never verifies — "
            "fails closed"
        )
    try:
        stored = json.loads(payload_bytes)
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            "the WORM anchor object is not a parsable snapshot; fails closed"
        ) from error
    if stored != attestation.get("anchored_heads"):
        raise ProvenanceError(
            "the WORM anchor object does not equal the attestation's "
            "embedded anchored-heads snapshot; the enforced anchor must be "
            "the one immutably stored — fails closed"
        )


def _local_chain_element_hash(run_root: Path, name: str, position: int) -> str:
    """SHA-256 of the local chain's element at 1-based `position`.

    Used for PREFIX verification: when the local chain has grown past the
    anchored count, the anchored head must equal the hash of the local
    element AT the anchored position — a longer chain whose history was
    rewritten under the old count must never verify.
    """
    if name == "acceptance-heads":
        directory = _acceptance_heads_directory(run_root)
        heads = sorted(
            entry
            for entry in directory.iterdir()
            if entry.name.endswith(".json") and not entry.name.startswith(".")
        )
        if position > len(heads):
            raise ProvenanceError(
                f"anchored chain {name}: local store has no element "
                f"{position}"
            )
        return hashlib.sha256(
            _read_evidence_bytes(heads[position - 1], allow_hardlinks=True)
        ).hexdigest()
    ledger = {
        "consumed": _consume_ledger_path(run_root),
        "publication-journal": _publication_journal_path(run_root),
        "reconcile-journal": run_root / "release-reconcile-journal.jsonl",
    }.get(name)
    if ledger is None:
        raise ProvenanceError(f"unknown anchored chain {name!r}")
    lines = _ledger_scan(ledger)["lines"] if ledger.exists() else []
    if position > len(lines):
        raise ProvenanceError(
            f"anchored chain {name}: local ledger has no record {position}"
        )
    return hashlib.sha256(lines[position - 1]).hexdigest()


def _anchor_checkpoint_path(run_root: Path) -> Path:
    return run_root / "anchored-heads-checkpoint.json"


def _assert_anchor_monotonic(run_root: Path, anchored: dict) -> None:
    """Presented anchors may never regress behind the best anchor seen."""
    checkpoint_path = _anchor_checkpoint_path(run_root)
    if not checkpoint_path.exists():
        return
    try:
        recorded = json.loads(_read_evidence_bytes(checkpoint_path))
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"the anchor checkpoint {checkpoint_path} is malformed; fails "
            "closed"
        ) from error
    presented = anchored.get("chains") or {}
    for name, best in (recorded.get("chains") or {}).items():
        state = presented.get(name)
        presented_count = (
            int(state.get("count", -1)) if isinstance(state, dict) else -1
        )
        best_count = int(best.get("count", 0)) if isinstance(best, dict) else 0
        if presented_count < best_count:
            raise ProvenanceError(
                f"the presented anchor records count {presented_count} for "
                f"{name}, behind the best previously verified anchor "
                f"({best_count}); an older or zero anchor never replays — "
                "fails closed"
            )
        if presented_count == best_count and isinstance(state, dict) and str(
            state.get("head", "")
        ) != str(best.get("head", "")):
            raise ProvenanceError(
                f"the presented anchor for {name} carries a DIFFERENT head "
                "at the same count as the best previously verified anchor; "
                "forked anchor history never verifies — fails closed"
            )


def _advance_anchor_checkpoint(run_root: Path, anchored: dict) -> None:
    """Forward-only record of the best verified anchor (atomic replace)."""
    checkpoint_path = _anchor_checkpoint_path(run_root)
    payload = json.dumps(
        {"chains": anchored.get("chains") or {}}, sort_keys=True
    ).encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(
        dir=run_root, prefix="." + checkpoint_path.name + "-"
    )
    try:
        os.write(descriptor, payload)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp_name, checkpoint_path)
    _fsync_dir(run_root)


def _adopt_anchored_legacy(run_root: Path, anchored: dict) -> None:
    """Adopt checkpoint-less legacy ledgers ONLY under anchored authority.

    A pre-checkpoint ledger's hash chain proves internal consistency, not
    provenance — an attacker can fabricate a whole consistent history. The
    ONLY authority that can vouch for legacy content is the SIGNED
    attestation's off-host anchor: the anchored head must confirm the exact
    local content (at the anchored position), and a ZERO-count anchor can
    vouch for nothing, so legacy content under an empty anchor is refused
    outright. Confirmed content gets its checkpoint written forward
    (additive; nothing rewritten or deleted).
    """
    chains = anchored.get("chains") or {}
    for name, ledger in (
        ("consumed", _consume_ledger_path(run_root)),
        ("publication-journal", _publication_journal_path(run_root)),
        ("reconcile-journal", run_root / "release-reconcile-journal.jsonl"),
    ):
        if not ledger.exists() or _ledger_checkpoint_path(ledger).exists():
            continue
        lines = _read_evidence_bytes(ledger).splitlines()
        if not lines:
            continue
        previous = GENESIS_HASH
        line_hashes: list[str] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ProvenanceError(
                    f"legacy ledger {ledger} is malformed; fails closed"
                ) from error
            if record.get("prev") != previous:
                raise ProvenanceError(
                    f"legacy ledger {ledger} breaks its hash chain; fails "
                    "closed"
                )
            previous = hashlib.sha256(line).hexdigest()
            line_hashes.append(previous)
        raw_state = chains.get(name)
        state: dict = raw_state if isinstance(raw_state, dict) else {}
        anchored_count = int(state.get("count", 0))
        if anchored_count < 1:
            raise ProvenanceError(
                f"legacy ledger {ledger} predates its checkpoint and the "
                "anchor records count 0 for it; an unanchored legacy chain "
                "is never adopted — fails closed"
            )
        if anchored_count > len(lines):
            raise ProvenanceError(
                f"legacy ledger {ledger} is BEHIND its anchored count; "
                "truncated legacy content is never adopted — fails closed"
            )
        if anchored_count < len(lines):
            # EXACT-PREFIX-ONLY: adopting a longer file would launder the
            # unanchored suffix through the checkpoint. The owner must
            # export a newer anchor covering the new content first.
            raise ProvenanceError(
                f"legacy ledger {ledger} carries {len(lines)} records but "
                f"the anchor covers only {anchored_count}; an unanchored "
                "suffix is never adopted — fails closed"
            )
        if line_hashes[anchored_count - 1] != str(state.get("head", "")):
            raise ProvenanceError(
                f"legacy ledger {ledger} does not match the anchored head "
                "at the anchored position; fabricated legacy content is "
                "never adopted — fails closed"
            )
        _read_chained_records(ledger, adopt_legacy=True)


def _assert_anchored_heads(run_root: Path, anchored: dict) -> None:
    """Local chains must EXTEND the signed off-host anchor — never rewrite it.

    Three refusals per chain: the local chain is missing or shorter than the
    anchor (truncation/deletion); the counts are equal but the heads differ
    (in-place rewrite); or the local chain is LONGER but its element at the
    anchored position no longer hashes to the anchored head (history rewrite
    hidden behind growth — a bare count comparison would accept it, so the
    anchored head is verified as a strict PREFIX of the local chain). The
    snapshot must also cover every required chain: an omitted chain anchors
    nothing and never verifies. Checkpoint-less LEGACY ledgers are adopted
    here and only here — after the anchor confirms their EXACT content
    (equal length, never a prefix of a longer file) — and a zero-count
    anchor adopts nothing. Anchors are ANTI-REPLAY monotonic: a run-root
    checkpoint records the highest anchored counts ever verified, a
    presented snapshot must not fall behind it (an older or zero anchor can
    never replay once a newer one was seen — so a longer local chain can
    never ride past on a vacuous count-0 attestation), and the checkpoint
    advances after each successful verification.
    """
    _assert_anchor_monotonic(run_root, anchored)
    _adopt_anchored_legacy(run_root, anchored)
    current = _anchor_snapshot(run_root)
    problems: list[str] = []
    chains = anchored.get("chains") or {}
    for name in REQUIRED_ANCHOR_CHAINS:
        if name not in chains:
            problems.append(f"{name}: chain OMITTED from the anchor snapshot")
    for name, anchored_state in chains.items():
        live_state = (current.get("chains") or {}).get(name)
        if live_state is None:
            problems.append(f"{name}: chain MISSING locally")
            continue
        local_count = int(live_state["count"])
        anchored_count = int(anchored_state["count"])
        if local_count < anchored_count:
            problems.append(
                f"{name}: local count {local_count} is BEHIND "
                f"anchored {anchored_count} (truncation/deletion)"
            )
        elif local_count == anchored_count:
            if live_state["head"] != anchored_state["head"]:
                problems.append(f"{name}: head diverged from the anchor")
        elif anchored_count > 0:
            try:
                prefix_head = _local_chain_element_hash(
                    run_root, name, anchored_count
                )
            except (ProvenanceError, OSError) as error:
                problems.append(f"{name}: prefix unverifiable ({error})")
                continue
            if prefix_head != anchored_state["head"]:
                problems.append(
                    f"{name}: the anchored head is NOT a prefix of the local "
                    "chain (history was rewritten beneath new growth)"
                )
    if problems:
        raise ProvenanceError(
            "anchored-heads verification failed: " + "; ".join(problems)
        )
    _advance_anchor_checkpoint(run_root, anchored)


def load_rollout_authorization(
    authorization_path: Path,
    public_key_path: str,
    expected_cluster: str,
    expected_plan_sha256: str | None,
    run_root: Path,
    verifier=None,
    allow_consumed: bool = False,
) -> tuple[dict, str]:
    """Verify an OWNER-SIGNED, single-use, plan-bound rollout authorization.

    An environment flag is caller-set and therefore spoofable; execution
    authority must be a cryptographic signal. The document is owner-signed
    over its exact bytes, pins the cluster (kube-system namespace UID) and
    the SHA-256 of the EXACT canonical plan the owner approved, is bounded
    to at most 24h, and is consumed exactly once through the chained ledger.
    """
    signature_path = authorization_path.parent / (
        authorization_path.name + ".sig"
    )
    if not authorization_path.is_file() or authorization_path.is_symlink():
        raise ProvenanceError(
            f"missing rollout authorization: {authorization_path}"
        )
    if not signature_path.is_file() or signature_path.is_symlink():
        raise ProvenanceError(
            f"rollout authorization at {authorization_path} is UNSIGNED; "
            "execution fails closed"
        )
    payload = _read_evidence_bytes(authorization_path, private=False)
    signature = _read_evidence_bytes(signature_path, private=False)
    _verify_blob_bytes(
        public_key_path,
        payload,
        signature,
        verifier,
        f"rollout authorization {authorization_path}",
    )
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"rollout authorization is malformed: {authorization_path}"
        ) from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != ROLLOUT_AUTHORIZATION_SCHEMA
    ):
        raise ProvenanceError(
            f"{authorization_path} is not a {ROLLOUT_AUTHORIZATION_SCHEMA} "
            "document"
        )
    if str(document.get("cluster", "")) != expected_cluster:
        raise ProvenanceError(
            f"{authorization_path} authorizes cluster "
            f"{document.get('cluster')!r}, not the live cluster "
            f"{expected_cluster!r}"
        )
    plan = document.get("plan")
    if not isinstance(plan, list) or not all(
        isinstance(entry, dict)
        and isinstance(entry.get("argv"), list)
        and all(isinstance(part, str) for part in entry["argv"])
        and (
            entry.get("stdin_sha256") is None
            or SHA256_PATTERN.match(str(entry.get("stdin_sha256")))
        )
        for entry in plan
    ):
        raise ProvenanceError(
            f"{authorization_path} must EMBED the byte-bound plan (argv + "
            "stdin digests); executed commands come only from the signed "
            "document, never from a local journal"
        )
    embedded_sha = hashlib.sha256(
        json.dumps(plan, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if str(document.get("plan_sha256", "")) != embedded_sha:
        raise ProvenanceError(
            f"{authorization_path} plan_sha256 does not match its own "
            "embedded plan; the owner signs the EXACT actions"
        )
    if not AUTOMATION_PRINCIPAL_PATTERN.match(
        str(document.get("executor", ""))
    ):
        raise ProvenanceError(
            f"{authorization_path} must name the exact automation "
            "`executor` (system:serviceaccount:<ns>:<name>) authorized to "
            "run AND resume this plan; execution is bound to one "
            "owner-designated identity, never to whichever security "
            "principal shows up"
        )
    if expected_plan_sha256 is not None and embedded_sha != expected_plan_sha256:
        raise ProvenanceError(
            f"{authorization_path} authorizes plan {embedded_sha}, not the "
            f"freshly recomputed plan {expected_plan_sha256!r}; live state "
            "moved since the owner signed — obtain a fresh authorization"
        )
    if not DRAIN_REASON_PATTERN.match(str(document.get("reason", ""))):
        raise ProvenanceError(
            f"{authorization_path} needs a tracking-identifier reason"
        )
    issued_at = _parse_rfc3339(
        str(document.get("issued_at", "")), f"{authorization_path} issued_at"
    )
    expires_at = _parse_rfc3339(
        str(document.get("expires_at", "")), f"{authorization_path} expires_at"
    )
    validity = (expires_at - issued_at).total_seconds()
    if not 0 < validity <= 24 * 3600:
        raise ProvenanceError(
            f"{authorization_path} validity must be positive and at most 24h"
        )
    now = datetime.now(UTC)
    if (issued_at - now).total_seconds() > _CLOCK_SKEW_SECONDS:
        raise ProvenanceError(f"{authorization_path} is not yet valid")
    if now > expires_at:
        if not allow_consumed:
            raise ProvenanceError(f"{authorization_path} has expired")
        # RESUME COMPLETABILITY: expiry bounds FIRST USE. A consumed
        # authorization whose execution crashed near the deadline must still
        # be resumable — inside a bounded grace window, not forever.
        if (now - expires_at).total_seconds() > RESUME_GRACE_DAYS * 24 * 3600:
            raise ProvenanceError(
                f"{authorization_path} expired more than "
                f"{RESUME_GRACE_DAYS} days ago; the resume grace window is "
                "over — the owner issues a fresh authorization for the "
                "remaining state"
            )
    document_sha256 = hashlib.sha256(payload).hexdigest()
    consumed = _is_consumed(run_root, document_sha256)
    if consumed and not allow_consumed:
        raise ProvenanceError(
            f"{authorization_path} was already consumed; rollout "
            "authorizations are SINGLE-USE"
        )
    if allow_consumed and not consumed:
        raise ProvenanceError(
            f"{authorization_path} was never consumed; --resume only "
            "completes a post-consume crash — run --execute instead"
        )
    return document, document_sha256


def load_owner_scope(
    scope_path: Path, public_key_path: str, verifier=None
) -> dict:
    """Load the OWNER-SIGNED admission scope; unsigned or tampered fails closed.

    Authority is cryptographic, never positional: the scope bytes must carry
    a detached cosign signature (`<scope>.sig`) that verifies against the
    pinned release verification key over EXACTLY the bytes parsed. A Git ref
    is not authority — local tracking refs are writable by any local process
    (`git update-ref`), so no ref, branch, or commit is consulted. A dirty,
    locally-committed, or substituted scope simply fails signature
    verification; producing a new valid signature requires the owner-held
    private key, which never lives in the repository. The shipped scope is
    EMPTY and owner-SIGNED: the signature verifies, and its EMPTINESS is what
    fails closed — rendering stays impossible until the owner populates and
    re-signs the exact scope. The verification key itself is pinned by
    SHA-256 in reviewed source (RELEASE_KEY_SHA256), so a substituted
    co-located key never verifies anything.
    """
    signature_path = scope_path.parent / (scope_path.name + ".sig")
    if not scope_path.is_file() or scope_path.is_symlink():
        raise ProvenanceError(
            f"missing owner-approved release scope: {scope_path}; allow-list "
            "rendering fails closed until the owner commits the exact "
            "admission scope"
        )
    if not signature_path.is_file() or signature_path.is_symlink():
        raise ProvenanceError(
            f"owner release scope at {scope_path} is UNSIGNED "
            f"({signature_path} is missing); allow-list rendering fails "
            "closed until the owner signs the exact scope with the release key"
        )
    scope_bytes = _read_evidence_bytes(scope_path, private=False)
    signature_bytes = _read_evidence_bytes(signature_path, private=False)
    try:
        _verify_blob_bytes(
            public_key_path,
            scope_bytes,
            signature_bytes,
            verifier,
            f"owner release scope {scope_path}",
        )
    except ProvenanceError as error:
        raise ProvenanceError(
            f"owner release scope signature verification failed for "
            f"{scope_path}; a dirty, locally-committed, or substituted scope "
            "never authorizes rendering — restore the signed scope and route "
            "changes through owner review and re-signing"
        ) from error
    try:
        document = json.loads(scope_bytes)
    except json.JSONDecodeError as error:
        raise ProvenanceError(
            f"owner release scope is malformed and fails closed: {scope_path}"
        ) from error
    if not isinstance(document, dict) or document.get("schema") != SCOPE_SCHEMA:
        raise ProvenanceError(
            f"{scope_path} is not a {SCOPE_SCHEMA} document"
        )
    scope = document.get("scope")
    if scope is None:
        raise ProvenanceError(
            f"owner release scope at {scope_path} is EMPTY: allow-list "
            "rendering fails closed until the owner populates and reviews the "
            "exact admission scope"
        )
    return _validated_scope(scope, f"owner release scope {scope_path}")


def load_signed_inventory(
    inventory_path: Path,
    public_key_path: str,
    verifier=None,
    max_age_hours: float = INVENTORY_MAX_AGE_HOURS,
) -> tuple[dict, str]:
    """Load and verify the signed inventory; return it with its bytes' hash.

    The inventory is the acceptance-gate document enumerating every platform
    image reference from the live workloads, the Helm rollback window, and the
    frozen scientific-stage bindings. Each source carries its observation
    snapshot (observed_at plus the resource identities it was read from) and
    the document carries the cluster identity and capture time, which must be
    fresh — a stale or future-dated inventory is refused, bounding replay.
    The returned SHA-256 is computed over EXACTLY the verified/parsed bytes,
    so callers recording it can never hash different bytes than were checked.

    Drains can never remove an ACTIVE image: a drained reference must not
    appear in live_workloads and must come from a non-live source (rollback
    window or frozen bindings), with a reason bound to a tracking identifier.
    There is NO sibling-program carve-out: the owner decision (2026-09-16)
    is that MindEval passes the identical signed-source/SBOM/provenance/
    admission gates — its live digests must be receipted, signed, and
    inventoried like every other platform image. platform_images
    must equal the source union minus those audited non-live drains.
    """
    if (
        not isinstance(max_age_hours, (int, float))
        or isinstance(max_age_hours, bool)
        or not math.isfinite(max_age_hours)
        or not 0 < float(max_age_hours) <= INVENTORY_MAX_AGE_HOURS_LIMIT
    ):
        raise ProvenanceError(
            "max inventory age must be a finite number of hours in "
            f"(0, {INVENTORY_MAX_AGE_HOURS_LIMIT}]; got {max_age_hours!r}"
        )
    max_age_hours = float(max_age_hours)
    signature = inventory_path.parent / (inventory_path.name + ".sig")
    if (
        inventory_path.is_symlink()
        or signature.is_symlink()
        or not inventory_path.is_file()
        or not signature.is_file()
    ):
        raise ProvenanceError(
            f"a signed release inventory is required: {inventory_path} and "
            f"{signature} must both exist (and not be symlinks); assemble it "
            "from live workloads, the Helm rollback window, and frozen "
            "scientific bindings, then sign it with the release key"
        )
    inventory_bytes = _read_evidence_bytes(inventory_path)
    signature_bytes = _read_evidence_bytes(signature)
    try:
        _verify_blob_bytes(
            public_key_path,
            inventory_bytes,
            signature_bytes,
            verifier,
            f"inventory {inventory_path}",
        )
    except ProvenanceError as error:
        raise ProvenanceError(
            f"inventory signature verification failed for {inventory_path}"
        ) from error
    try:
        inventory = json.loads(inventory_bytes)
    except json.JSONDecodeError as error:
        raise ProvenanceError(f"unreadable inventory: {inventory_path}") from error
    if not isinstance(inventory, dict) or inventory.get("schema") != INVENTORY_SCHEMA:
        raise ProvenanceError(
            f"{inventory_path} is not a {INVENTORY_SCHEMA} document"
        )
    if not CLUSTER_PATTERN.match(str(inventory.get("cluster", ""))):
        raise ProvenanceError(
            f"{inventory_path} lacks an exact valid cluster identity"
        )
    collector = inventory.get("collector")
    if (
        not isinstance(collector, dict)
        or set(collector) != {"method", "identity"}
        or collector.get("method") != COLLECTOR_METHOD
        or not PRINCIPAL_PATTERN.match(str(collector.get("identity", "")))
    ):
        raise ProvenanceError(
            f"{inventory_path} needs a typed authoritative collector: "
            f"{{method: {COLLECTOR_METHOD}, identity: <authenticated user>}}"
        )
    generation = inventory.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ProvenanceError(
            f"{inventory_path} needs a positive integer generation so an "
            "older signed inventory can never replay over a newer one"
        )
    scope = _validated_scope(
        inventory.get("scope"), f"{inventory_path} scope"
    )
    if scope["cluster"] != inventory["cluster"]:
        raise ProvenanceError(
            f"{inventory_path} scope cluster {scope['cluster']!r} does not "
            f"equal the captured cluster {inventory['cluster']!r}"
        )
    now = datetime.now(UTC)
    captured_at = _parse_rfc3339(
        inventory.get("captured_at", ""), f"{inventory_path} captured_at"
    )
    age_seconds = (now - captured_at).total_seconds()
    if age_seconds < -_CLOCK_SKEW_SECONDS:
        raise ProvenanceError(f"{inventory_path} is dated in the future")
    if age_seconds > max_age_hours * 3600:
        raise ProvenanceError(
            f"{inventory_path} is stale: captured {captured_at.isoformat()}, "
            f"older than {max_age_hours}h; re-capture the inventory"
        )
    sources = inventory.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(INVENTORY_SOURCES):
        raise ProvenanceError(
            f"{inventory_path} must enumerate exactly these sources: "
            + ", ".join(INVENTORY_SOURCES)
        )
    union: set[str] = set()
    per_source: dict[str, set[str]] = {}
    for name in INVENTORY_SOURCES:
        source = sources.get(name) or {}
        refs = source.get("refs")
        if not isinstance(refs, list):
            raise ProvenanceError(f"{inventory_path} source {name} lacks a refs list")
        observed_at = _parse_rfc3339(
            source.get("observed_at", ""), f"{inventory_path} source {name}"
        )
        observed_age = (now - observed_at).total_seconds()
        if observed_age < -_CLOCK_SKEW_SECONDS:
            raise ProvenanceError(
                f"{inventory_path} source {name} observation is future-dated "
                f"beyond the {_CLOCK_SKEW_SECONDS}s clock-skew bound"
            )
        if observed_age > max_age_hours * 3600:
            raise ProvenanceError(
                f"{inventory_path} source {name} observation is stale"
            )
        resource_ids = source.get("resource_ids")
        if not isinstance(resource_ids, list) or (refs and not resource_ids):
            raise ProvenanceError(
                f"{inventory_path} source {name} lacks the resource identities "
                "its refs were observed on"
            )
        if (
            len(set(map(str, resource_ids))) != len(resource_ids)
            or not all(
                isinstance(item, str) and RESOURCE_ID_PATTERN.match(item)
                for item in resource_ids
            )
        ):
            raise ProvenanceError(
                f"{inventory_path} source {name} resource identities must be "
                "unique, non-empty, structured strings"
            )
        per_source[name] = {
            validate_digest_reference(str(reference)) for reference in refs
        }
        union |= per_source[name]
    live = per_source["live_workloads"]
    non_live = per_source["helm_rollback_window"] | per_source[
        "frozen_scientific_bindings"
    ]
    drained: set[str] = set()
    for removal in inventory.get("drained_removals") or []:
        if not isinstance(removal, dict):
            raise ProvenanceError(
                f"{inventory_path} drained removals must be objects"
            )
        image = validate_digest_reference(str(removal.get("image", "")))
        reason = str(removal.get("reason", ""))
        if not DRAIN_REASON_PATTERN.match(reason):
            raise ProvenanceError(
                f"{inventory_path} drain of {image} needs a reason bound to a "
                "tracking identifier (incident:/change:/ticket:/task:)"
            )
        if image in live:
            raise ProvenanceError(
                f"{inventory_path} drains {image}, which is STILL LIVE in "
                "live_workloads; an active image can never be drained — scope "
                "the admission policy instead, or receipt and sign the image"
            )
        if image not in non_live:
            raise ProvenanceError(
                f"{inventory_path} drains {image}, which no non-live source "
                "lists; there is nothing to drain"
            )
        drained.add(image)
    platform_images = inventory.get("platform_images")
    if not isinstance(platform_images, list) or not platform_images:
        raise ProvenanceError(f"{inventory_path} lists no platform images")
    listed = [validate_digest_reference(str(ref)) for ref in platform_images]
    if len(listed) != len(set(listed)):
        raise ProvenanceError(f"{inventory_path} lists duplicate platform images")
    expected = union - drained
    if set(listed) != expected:
        missing = sorted(expected - set(listed))
        extras = sorted(set(listed) - expected)
        raise ProvenanceError(
            f"{inventory_path} platform_images do not equal sources minus "
            f"drained removals; missing: {missing or 'none'}; extras: "
            f"{extras or 'none'}"
        )
    return inventory, hashlib.sha256(inventory_bytes).hexdigest()


PLATFORM_IMAGE_IN_TEXT = None  # compiled lazily against the scope prefix


def _platform_references_in_text(text_value: str, platform_prefix: str) -> set[str]:
    pattern = re.compile(
        re.escape(platform_prefix) + r"[A-Za-z0-9._/-]*@sha256:[0-9a-f]{64}"
    )
    return set(pattern.findall(text_value))


WORKLOAD_KINDS = (
    ("deployment", "deployments"),
    ("daemonset", "daemonsets"),
    ("statefulset", "statefulsets"),
    ("replicaset", "replicasets"),
    ("replicationcontroller", "replicationcontrollers"),
    ("job", "jobs"),
    ("cronjob", "cronjobs"),
)
HELM_PAGE_SIZE = 256

def _pod_template_images(spec: dict) -> Iterator[str]:
    for field in ("containers", "initContainers", "ephemeralContainers"):
        for container in spec.get(field) or []:
            yield str(container.get("image", ""))


def collect_authoritative_observation(scope: dict, runner=_run_capture) -> dict:
    """Authoritatively observe the cluster through the authenticated API.

    Returns the authenticated identity, the kubeconfig cluster identity, the
    live platform images (Pods AND workload controllers — a scaled-to-zero or
    crash-looping Deployment counts even with no Pod), the pod/controller
    resource identities they were observed on, and the Helm rollback-window
    images per release revision. Any failure — no kubectl/helm, no access, an
    unpinned live platform image, a foreign cluster — fails closed: the
    signed inventory is only ever ACCEPTED against this observation, never
    trusted on its own resource claims.
    """
    platform_prefix = scope["platform_repository_prefix"]
    try:
        whoami = json.loads(runner(["kubectl", "auth", "whoami", "-o", "json"]))
        identity = str(
            (whoami.get("status") or {}).get("userInfo", {}).get("username", "")
        )
        if not PRINCIPAL_PATTERN.match(identity):
            raise ProvenanceError(
                "authenticated live enumeration returned no usable identity; "
                "rendering fails closed"
            )
        # The kubeconfig cluster NAME is client-side mutable; the
        # kube-system namespace UID is assigned by the API server at cluster
        # creation and cannot be edited, so the scope pins THAT.
        cluster = runner(
            [
                "kubectl",
                "get",
                "namespace",
                "kube-system",
                "-o",
                "jsonpath={.metadata.uid}",
            ]
        ).strip()
        if cluster != scope["cluster"]:
            raise ProvenanceError(
                f"the authenticated session targets the cluster whose "
                f"kube-system namespace UID is {cluster!r}, but the "
                f"owner-approved scope pins {scope['cluster']!r}; rendering "
                "fails closed against a foreign cluster"
            )
        live_images: set[str] = set()
        live_resources: set[str] = set()

        def record(image: str, resource_id: str) -> None:
            if not image.startswith(platform_prefix):
                return
            try:
                live_images.add(validate_digest_reference(image))
            except ProvenanceError as error:
                raise ProvenanceError(
                    f"live platform image {image!r} at {resource_id} is not "
                    "digest-pinned; it can never be allow-listed — rendering "
                    "fails closed"
                ) from error
            live_resources.add(resource_id)

        for namespace in scope["namespaces"]:
            pods = json.loads(
                runner(["kubectl", "get", "pods", "-n", namespace, "-o", "json"])
            )
            for pod in pods.get("items") or []:
                name = str((pod.get("metadata") or {}).get("name", ""))
                for image in _pod_template_images(pod.get("spec") or {}):
                    record(image, f"pod/{namespace}/{name}")
            for singular, plural in WORKLOAD_KINDS:
                listing = json.loads(
                    runner(
                        ["kubectl", "get", plural, "-n", namespace, "-o", "json"]
                    )
                )
                for item in listing.get("items") or []:
                    name = str((item.get("metadata") or {}).get("name", ""))
                    spec = item.get("spec") or {}
                    if singular == "cronjob":
                        template_spec = (
                            (spec.get("jobTemplate") or {})
                            .get("spec", {})
                            .get("template", {})
                            .get("spec", {})
                        )
                    else:
                        template_spec = (spec.get("template") or {}).get(
                            "spec", {}
                        )
                    for image in _pod_template_images(template_spec or {}):
                        record(image, f"{singular}/{namespace}/{name}")

        helm_images: set[str] = set()
        helm_resources: set[str] = set()
        for namespace in scope["namespaces"]:
            releases: list[dict] = []
            offset = 0
            while True:
                # helm list caps at 256 and --max 0 does NOT mean unlimited:
                # paginate with --offset until a short page arrives, and use
                # --all so no status filter hides a release.
                page = json.loads(
                    runner(
                        [
                            "helm",
                            "list",
                            "-n",
                            namespace,
                            "--all",
                            "--max",
                            str(HELM_PAGE_SIZE),
                            "--offset",
                            str(offset),
                            "-o",
                            "json",
                        ]
                    )
                    or "[]"
                )
                releases.extend(page or [])
                if len(page or []) < HELM_PAGE_SIZE:
                    break
                offset += HELM_PAGE_SIZE
            for release in releases:
                name = str(release.get("name", ""))
                history = json.loads(
                    runner(
                        [
                            "helm",
                            "history",
                            name,
                            "-n",
                            namespace,
                            "--max",
                            "10000",
                            "-o",
                            "json",
                        ]
                    )
                    or "[]"
                )
                for entry in history or []:
                    revision = int(entry.get("revision", 0))
                    manifest = runner(
                        [
                            "helm",
                            "get",
                            "manifest",
                            name,
                            "-n",
                            namespace,
                            "--revision",
                            str(revision),
                        ]
                    )
                    found = _platform_references_in_text(manifest, platform_prefix)
                    if found:
                        helm_images |= found
                        helm_resources.add(f"helm/{namespace}/{name}/{revision}")
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError, ValueError) as error:
        raise ProvenanceError(
            "authenticated live enumeration is unavailable; the allow-list "
            "renders only against a live authoritative collection — "
            "rendering fails closed"
        ) from error
    return {
        "identity": identity,
        "cluster": cluster,
        "live_images": live_images,
        "live_resources": live_resources,
        "helm_images": helm_images,
        "helm_resources": helm_resources,
    }


STAGE_BINDING_DUMP_SCRIPT = (
    "import asyncio, hashlib, json, os\n"
    "import asyncpg\n"
    "async def main():\n"
    "    dsn = os.environ['DATABASE_URL'].replace("
    "'postgresql+asyncpg://', 'postgresql://', 1)\n"
    "    conn = await asyncpg.connect(dsn=dsn)\n"
    "    try:\n"
    "        # One REPEATABLE READ read-only snapshot: every query below\n"
    "        # observes the SAME database state, so identity facts and rows\n"
    "        # can never come from different moments.\n"
    "        txn = conn.transaction(isolation='repeatable_read',"
    " readonly=True)\n"
    "        await txn.start()\n"
    "        database = await conn.fetchval('SELECT current_database()')\n"
    "        role = await conn.fetchval('SELECT current_user')\n"
    "        search_path = await conn.fetchval('SHOW search_path')\n"
    "        current_schema = await conn.fetchval('SELECT current_schema()')\n"
    "        server_version = await conn.fetchval('SELECT version()')\n"
    "        server_version_num = await conn.fetchval("
    "'SHOW server_version_num')\n"
    "        system_identifier = await conn.fetchval(\n"
    "            'SELECT system_identifier::text FROM pg_control_system()'\n"
    "        )\n"
    "        expected_table_oid = await conn.fetchval(\n"
    "            \"SELECT 'fs2_scientific_batches'::regclass::oid::text\"\n"
    "        )\n"
    "        migration = await conn.fetchrow(\n"
    "            'SELECT version, sha256 FROM fs2_schema_migrations '\n"
    "            'ORDER BY applied_at DESC, version DESC LIMIT 1'\n"
    "        )\n"
    "        trigger = await conn.fetchrow(\n"
    "            \"SELECT tgenabled::text AS enabled, \"\n"
    "            \"tgrelid::regclass::text AS relation, \"\n"
    "            \"tgrelid::oid::text AS table_oid, \"\n"
    "            \"tgfoid::regproc::text AS function, \"\n"
    "            \"pg_get_triggerdef(oid) AS definition, \"\n"
    "            \"pg_get_functiondef(tgfoid) AS function_definition \"\n"
    "            \"FROM pg_trigger WHERE tgname = \"\n"
    "            \"'fs2_scientific_batch_state_immutable_trigger' \"\n"
    "            \"AND tgrelid = 'fs2_scientific_batches'::regclass\"\n"
    "        )\n"
    "        rows = await conn.fetch(\n"
    "            \"SELECT batch_id::text AS batch_id, revision, \"\n"
    "            \"(state->'adapter_execution'->'stage_bindings')::text \"\n"
    "            \"AS bindings FROM fs2_scientific_batches \"\n"
    "            \"WHERE jsonb_typeof(state->'adapter_execution'\"\n"
    "            \"->'stage_bindings') = 'array'\"\n"
    "        )\n"
    "        await txn.commit()\n"
    "    finally:\n"
    "        await conn.close()\n"
    "    out_rows = []\n"
    "    digests = []\n"
    "    for row in sorted(rows, key=lambda r: (r['batch_id'], r['revision'])):\n"
    "        digest = hashlib.sha256("
    "row['bindings'].encode('utf-8')).hexdigest()\n"
    "        digests.append([row['batch_id'], row['revision'], digest])\n"
    "        for binding in json.loads(row['bindings']):\n"
    "            out_rows.append([row['batch_id'], row['revision'],"
    " binding.get('image'), digest])\n"
    "    print(json.dumps({\n"
    "        'database': database,\n"
    "        'role': role,\n"
    "        'search_path': search_path,\n"
    "        'current_schema': current_schema,\n"
    "        'server_version_sha256': hashlib.sha256("
    "str(server_version).encode('utf-8')).hexdigest(),\n"
    "        'server_version_num': str(server_version_num),\n"
    "        'system_identifier': str(system_identifier),\n"
    "        'expected_table_oid': str(expected_table_oid),\n"
    "        'migration_version': migration['version'] if migration else None,\n"
    "        'migration_sha256': migration['sha256'] if migration else None,\n"
    "        'trigger_enabled': trigger['enabled'] if trigger else None,\n"
    "        'trigger_relation': trigger['relation'] if trigger else None,\n"
    "        'table_oid': trigger['table_oid'] if trigger else None,\n"
    "        'trigger_function': trigger['function'] if trigger else None,\n"
    "        'trigger_definition_sha256': hashlib.sha256("
    "trigger['definition'].encode('utf-8')).hexdigest() if trigger else None,\n"
    "        'trigger_function_sha256': hashlib.sha256("
    "trigger['function_definition'].encode('utf-8')).hexdigest()"
    " if trigger else None,\n"
    "        'rows_sha256': hashlib.sha256(json.dumps(digests,"
    " sort_keys=True).encode('utf-8')).hexdigest(),\n"
    "        'rows': out_rows,\n"
    "    }))\n"
    "asyncio.run(main())\n"
)

AUTHORITY_FIELDS = (
    "workload_uid",
    "workload_resource_version",
    "pod_name",
    "pod_uid",
    "pod_resource_version",
    "pod_controller",
    "container_name",
    "image",
    "database",
    "role",
    "search_path",
    "current_schema",
    "server_version_sha256",
    "server_version_num",
    "system_identifier",
    "migration_version",
    "migration_sha256",
    "table_oid",
    "trigger_relation",
    "trigger_function",
    "trigger_definition_sha256",
    "trigger_function_sha256",
    "trigger_enabled",
    "rows_sha256",
)


def _verify_frozen_bindings(
    scope: dict, source: dict, inventory_path: Path, runner
) -> None:
    """Frozen bindings verified against their TRUE authority: PostgreSQL.

    The scientific stage bindings are frozen in the control plane's database
    (fs2_scientific_batches.state->'adapter_execution'->'stage_bindings',
    protected by an immutability trigger) — ConfigMaps are at most a
    materialization, and scanning them was looking in the wrong place: a
    same-name ConfigMap replacement could erase history without the database
    changing at all. The collector therefore executes a READ-ONLY SELECT
    inside the owner-scope-pinned control-plane workload (its own asyncpg +
    DATABASE_URL; no credentials leave the pod) and requires the signed
    source's refs AND resource identities (batch/<uuid>/rev/<n>) to equal
    the database enumeration exactly. An unreachable database fails closed.

    The observation is fully attributable: all queries run in ONE
    REPEATABLE READ read-only snapshot; the selected pod is bound by
    controller ownership chain to the exact workload UID and by container to
    the workload's digest-pinned image; the pod (by name/UID) and the
    workload (UID AND resourceVersion) are re-fetched after the dump; and
    the server identity carries the PostgreSQL system identifier and version
    number, not just a version-string hash.
    """
    recorded = {
        validate_digest_reference(str(ref)) for ref in source.get("refs") or []
    }
    recorded_ids = {str(item) for item in source.get("resource_ids") or []}
    recorded_authority = source.get("authority")
    authority = scope["stage_binding_authority"]
    found: set[str] = set()
    found_ids: set[str] = set()
    try:
        kind, workload_name = str(authority["workload"]).split("/", 1)
        workload = json.loads(
            runner(
                [
                    "kubectl",
                    "get",
                    kind,
                    workload_name,
                    "-n",
                    str(authority["namespace"]),
                    "-o",
                    "json",
                ]
            )
        )
        workload_metadata = workload.get("metadata") or {}
        workload_uid = str(workload_metadata.get("uid", ""))
        workload_rv = str(workload_metadata.get("resourceVersion", ""))
        template_spec = (
            (workload.get("spec") or {}).get("template", {}).get("spec", {})
        )
        containers = template_spec.get("containers") or []
        workload_image = str(containers[0].get("image", "")) if containers else ""
        # The authority workload itself must run verified platform code: a
        # same-name replacement changes its UID, and its image must be a
        # digest-pinned platform reference (covered by the live set too).
        if not workload_image.startswith(scope["platform_repository_prefix"]):
            raise ProvenanceError(
                "the stage-binding authority workload does not run a "
                "platform image; rendering fails closed"
            )
        validate_digest_reference(workload_image)
        # Racing-exec closure: resolve ONE exact pod (name + UID) first, exec
        # into that pod — never the deployment alias, whose target can change
        # between resolution and exec — and re-check the pod's UID afterwards.
        selector = (
            (workload.get("spec") or {}).get("selector", {}).get("matchLabels")
            or {}
        )
        selector_text = ",".join(
            f"{key}={value}" for key, value in sorted(selector.items())
        )
        pods = json.loads(
            runner(
                [
                    "kubectl",
                    "get",
                    "pods",
                    "-n",
                    str(authority["namespace"]),
                    "-l",
                    selector_text,
                    "-o",
                    "json",
                ]
            )
        )
        running = sorted(
            (
                pod
                for pod in pods.get("items") or []
                if str((pod.get("status") or {}).get("phase", "")) == "Running"
            ),
            key=lambda pod: str((pod.get("metadata") or {}).get("name", "")),
        )
        if not running:
            raise ProvenanceError(
                "no Running pod backs the stage-binding authority workload; "
                "rendering fails closed"
            )
        selected = running[0]
        pod_metadata = selected.get("metadata") or {}
        pod_name = str(pod_metadata.get("name", ""))
        pod_uid = str(pod_metadata.get("uid", ""))
        pod_resource_version = str(pod_metadata.get("resourceVersion", ""))
        # CONTROLLER binding: label selection alone would accept a foreign
        # pod wearing copied labels. The selected pod's controller owner
        # chain must terminate at the EXACT authority workload UID
        # (Deployment -> its ReplicaSet -> the pod; StatefulSet -> the pod).
        controller_owner = next(
            (
                owner
                for owner in pod_metadata.get("ownerReferences") or []
                if owner.get("controller") is True
            ),
            None,
        )
        if controller_owner is None:
            raise ProvenanceError(
                "the selected authority pod has no controller owner; an "
                "unmanaged look-alike pod is never the authority — rendering "
                "fails closed"
            )
        owner_kind = str(controller_owner.get("kind", ""))
        owner_name = str(controller_owner.get("name", ""))
        owner_uid = str(controller_owner.get("uid", ""))
        if kind == "deployment":
            if owner_kind != "ReplicaSet":
                raise ProvenanceError(
                    "the authority pod is not controlled by a ReplicaSet of "
                    "the authority Deployment; rendering fails closed"
                )
            replica_set = json.loads(
                runner(
                    [
                        "kubectl",
                        "get",
                        "replicaset",
                        owner_name,
                        "-n",
                        str(authority["namespace"]),
                        "-o",
                        "json",
                    ]
                )
            )
            rs_metadata = replica_set.get("metadata") or {}
            if str(rs_metadata.get("uid", "")) != owner_uid:
                raise ProvenanceError(
                    "the authority pod's ReplicaSet owner UID does not match "
                    "the live ReplicaSet; rendering fails closed"
                )
            rs_controller = next(
                (
                    owner
                    for owner in rs_metadata.get("ownerReferences") or []
                    if owner.get("controller") is True
                ),
                None,
            )
            if (
                rs_controller is None
                or str(rs_controller.get("kind", "")) != "Deployment"
                or str(rs_controller.get("uid", "")) != workload_uid
            ):
                raise ProvenanceError(
                    "the authority pod's controller chain does not terminate "
                    "at the exact authority workload UID; a same-name or "
                    "look-alike controller never dumps the authority — "
                    "rendering fails closed"
                )
        elif str(controller_owner.get("uid", "")) != workload_uid or (
            owner_kind.lower() != kind
        ):
            raise ProvenanceError(
                "the authority pod's controller owner does not equal the "
                "exact authority workload UID; rendering fails closed"
            )
        pod_controller = f"{owner_kind}/{owner_name}/{owner_uid}"
        # CONTAINER binding: the pod must actually run the workload's exact
        # digest-pinned image; the dump execs into that container's pod.
        container_name = next(
            (
                str(container.get("name", ""))
                for container in (selected.get("spec") or {}).get(
                    "containers"
                )
                or []
                if str(container.get("image", "")) == workload_image
            ),
            None,
        )
        if not container_name:
            raise ProvenanceError(
                "the selected authority pod runs no container with the "
                "workload's exact digest-pinned image; rendering fails closed"
            )
        dump = json.loads(
            runner(
                [
                    "kubectl",
                    "exec",
                    "-n",
                    str(authority["namespace"]),
                    f"pod/{pod_name}",
                    # The EXACT verified container: without -c, kubectl uses
                    # the default-container annotation, so a sidecar could
                    # supply the dump while the platform container is bound.
                    "-c",
                    container_name,
                    "--",
                    "python",
                    "-c",
                    STAGE_BINDING_DUMP_SCRIPT,
                ]
            )
        )
        # Post-dump identity fences: the EXACT pod (by name) must still carry
        # the same UID, and the WORKLOAD must be untouched (UID and
        # resourceVersion) across the dump — a rollout or same-name
        # replacement racing the exec never renders.
        after_pod = json.loads(
            runner(
                [
                    "kubectl",
                    "get",
                    "pods",
                    pod_name,
                    "-n",
                    str(authority["namespace"]),
                    "-o",
                    "json",
                ]
            )
        )
        after_pod_metadata = after_pod.get("metadata") or {}
        if (
            str(after_pod_metadata.get("uid", "")) != pod_uid
            or str(after_pod_metadata.get("resourceVersion", ""))
            != pod_resource_version
        ):
            raise ProvenanceError(
                "the authority pod changed (UID or resourceVersion) during "
                "the dump; the observation is not attributable to one pod "
                "state — rendering fails closed"
            )
        after_workload = json.loads(
            runner(
                [
                    "kubectl",
                    "get",
                    kind,
                    workload_name,
                    "-n",
                    str(authority["namespace"]),
                    "-o",
                    "json",
                ]
            )
        )
        after_workload_metadata = after_workload.get("metadata") or {}
        if (
            str(after_workload_metadata.get("uid", "")) != workload_uid
            or str(after_workload_metadata.get("resourceVersion", ""))
            != workload_rv
        ):
            raise ProvenanceError(
                "the authority workload changed (UID or resourceVersion) "
                "during the dump; the observation is not attributable to one "
                "workload state — rendering fails closed"
            )
        if str(dump.get("trigger_relation") or "") != "fs2_scientific_batches":
            raise ProvenanceError(
                "the immutability trigger is not attached to the exact "
                "fs2_scientific_batches relation; a trigger on any other "
                "relation protects nothing — rendering fails closed"
            )
        if not str(dump.get("table_oid") or "") or str(
            dump.get("table_oid")
        ) != str(dump.get("expected_table_oid")):
            raise ProvenanceError(
                "the trigger's relation OID does not equal the live "
                "fs2_scientific_batches regclass OID from the same snapshot; "
                "rendering fails closed"
            )
        if str(dump.get("trigger_enabled") or "") not in ("O", "A"):
            raise ProvenanceError(
                "the database immutability trigger "
                "(fs2_scientific_batch_state_immutable_trigger) is absent or "
                "disabled; the frozen authority is unprotected — rendering "
                "fails closed"
            )
        live_authority = {
            "workload_uid": workload_uid,
            "workload_resource_version": workload_rv,
            "pod_name": pod_name,
            "pod_uid": pod_uid,
            "pod_resource_version": pod_resource_version,
            "pod_controller": pod_controller,
            "container_name": container_name,
            "image": workload_image,
            "database": str(dump.get("database", "")),
            "role": str(dump.get("role", "")),
            "search_path": str(dump.get("search_path", "")),
            "current_schema": str(dump.get("current_schema", "")),
            "server_version_sha256": str(dump.get("server_version_sha256", "")),
            "server_version_num": str(dump.get("server_version_num", "")),
            "system_identifier": str(dump.get("system_identifier", "")),
            "migration_version": str(dump.get("migration_version", "")),
            "migration_sha256": str(dump.get("migration_sha256", "")),
            "table_oid": str(dump.get("table_oid", "")),
            "trigger_relation": str(dump.get("trigger_relation", "")),
            "trigger_function": str(dump.get("trigger_function", "")),
            "trigger_definition_sha256": str(
                dump.get("trigger_definition_sha256", "")
            ),
            "trigger_function_sha256": str(
                dump.get("trigger_function_sha256", "")
            ),
            "trigger_enabled": str(dump.get("trigger_enabled", "")),
            "rows_sha256": str(dump.get("rows_sha256", "")),
        }
        if recorded_authority != live_authority:
            raise ProvenanceError(
                f"{inventory_path} frozen_scientific_bindings authority does "
                "not equal the live workload/pod/database identity "
                "(workload UID/RV, pod name/UID/RV/controller-chain/"
                "container, database/role/search_path/current_schema/server "
                "version+num/system identifier, migration, trigger relation/"
                "function/DEFINITION/enabled, aggregate rows digest); a "
                "same-name authority replacement or schema drift never "
                "renders"
            )
        for batch_id, revision, image, row_digest in dump.get("rows") or []:
            image_text = str(image or "")
            if not image_text.startswith(scope["platform_repository_prefix"]):
                continue
            try:
                found.add(validate_digest_reference(image_text))
            except ProvenanceError as error:
                raise ProvenanceError(
                    f"frozen stage binding batch/{batch_id}/rev/{revision} "
                    f"carries a non-digest-pinned image {image_text!r}; it "
                    "can never be allow-listed — rendering fails closed"
                ) from error
            found_ids.add(f"batch/{batch_id}/rev/{revision}/{row_digest}")
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError, ValueError) as error:
        raise ProvenanceError(
            f"{inventory_path} frozen_scientific_bindings cannot be verified "
            "against the authoritative control-plane database; rendering "
            "fails closed"
        ) from error
    if found != recorded:
        raise ProvenanceError(
            f"{inventory_path} frozen_scientific_bindings refs do not equal "
            "the authoritative database enumeration; omitted: "
            f"{sorted(found - recorded) or 'none'}; recorded but not in the "
            f"database: {sorted(recorded - found) or 'none'}"
        )
    if found_ids != recorded_ids:
        raise ProvenanceError(
            f"{inventory_path} frozen_scientific_bindings resource identities "
            "do not equal the authoritative database enumeration; forged or "
            "self-asserted identities never render"
        )


ACCEPTANCE_HEAD_SCHEMA = "fs2-serve.nebius.ai/inventory-acceptance/v1"
GENESIS_HASH = "0" * 64


def _security_namespaces(scope: dict) -> set[str]:
    return {
        principal.split(":", 3)[2]
        for principal in scope["security_principals"]
        if principal.startswith("system:serviceaccount:")
    }


def _rbac_rule_is_forbidden(
    rule: dict,
    binding_namespace: str | None,
    scope: dict,
    protected_namespaces: frozenset[str] | None = None,
) -> dict | None:
    """Return the reason when an RBAC rule grants a forbidden identity path.

    `binding_namespace` is None for cluster-wide grants. Rules marked
    `namespaced_to_scope` apply to cluster-wide grants and to grants inside
    the scope namespaces (the only namespaced bindings enumerated); rules
    marked `security_namespaces_only` apply to cluster-wide grants and to
    grants inside the security identity's namespaces.
    """
    groups = {str(g) for g in rule.get("apiGroups") or []}
    resources = {str(r) for r in rule.get("resources") or []}
    verbs = {str(v) for v in rule.get("verbs") or []}
    for forbidden in FORBIDDEN_IDENTITY_RULES:
        forbidden_groups = set(map(str, forbidden["apiGroups"]))  # type: ignore[arg-type]
        forbidden_resources = set(map(str, forbidden["resources"]))  # type: ignore[arg-type]
        forbidden_verbs = set(map(str, forbidden["verbs"]))  # type: ignore[arg-type]
        if forbidden.get("security_namespaces_only") and (
            binding_namespace is not None
            and binding_namespace not in _security_namespaces(scope)
        ):
            continue
        if forbidden.get("namespaced_to_scope") and (
            binding_namespace is not None
            and binding_namespace
            not in (
                protected_namespaces
                if protected_namespaces is not None
                else frozenset(scope["namespaces"])
                | frozenset(_security_namespaces(scope))
            )
        ):
            # Namespace-scoped rules protect the scope/security namespaces
            # PLUS every PRIVILEGED namespace (home of a ServiceAccount that
            # holds forbidden/cluster authority) the caller derived — a
            # foreign-namespace secrets/exec/workload grant there is a
            # transitive identity path; unrelated app namespaces stay out.
            continue
        if not (groups & forbidden_groups or "*" in groups):
            continue
        if not any(
            _granted_resource_matches(granted, banned)
            for granted in resources
            for banned in forbidden_resources
        ):
            continue
        if not (verbs & forbidden_verbs or "*" in verbs):
            continue
        return forbidden
    return None


def _granted_resource_matches(granted: str, forbidden: str) -> bool:
    """Wildcard-aware RBAC resource matching, subresources included.

    RBAC grants may spell `pods`, `pods/exec`, `pods/*`, `*/token`, or `*`;
    the forbidden table spells parents and exact subresources. A grant
    covers a forbidden entry when every segment matches (with `*` matching
    anything on either side). A plain-parent grant does NOT cover a
    subresource (RBAC semantics), but `pods/*` covers both `pods` and every
    `pods/<sub>` — the shape the previous set-arithmetic matcher missed.
    """

    def segment_match(left: str, right: str) -> bool:
        return left == "*" or right == "*" or left == right

    granted_parent, granted_slash, granted_sub = granted.partition("/")
    forbidden_parent, forbidden_slash, forbidden_sub = forbidden.partition("/")
    if not segment_match(granted_parent, forbidden_parent):
        return False
    if forbidden_slash:
        # Forbidden names a subresource: a grant covers it via an exact or
        # wildcard subresource, or the full `*` grant.
        if granted_slash:
            return segment_match(granted_sub, forbidden_sub)
        return granted == "*"
    # Forbidden names a parent resource: parent-level grants cover it, and
    # so does a wildcard-subresource grant like `pods/*` (fail-closed
    # reading: some API servers honor it for the parent too).
    if granted_slash:
        return granted_sub == "*"
    return True


def _binding_subjects(binding: dict) -> list[str]:
    subjects = []
    for subject in binding.get("subjects") or []:
        kind = str(subject.get("kind", ""))
        name = str(subject.get("name", ""))
        if kind == "ServiceAccount":
            namespace = str(subject.get("namespace", ""))
            subjects.append(f"ServiceAccount:{namespace}:{name}")
        else:
            subjects.append(f"{kind}:{name}")
    return subjects


def _assert_iam_boundary(owner_scope: dict, live_runner, attestation: dict | None = None) -> None:
    """ENFORCE the external identity boundary at render time, read-only.

    The external provider/IAM control is not prose: this audit walks every
    live (Cluster)RoleBinding through the authenticated API — across the
    scope namespaces AND the security identity's namespaces — resolves each
    role's rules, and REFUSES to render while any subject outside the
    owner-signed exemption list (enumerated bootstrap identities and
    kube-system controller ServiceAccounts only) plus the security
    principals holds a forbidden identity path. The single bootstrap
    cluster-admin -> Group:system:masters binding is tolerated ONLY under
    the ATTESTOR-SIGNED provider attestation. Until the
    owner executes the IAM closure at the authorized rollout window,
    rendering fails closed; afterwards, any regression re-opens the refusal.

    Allowances are FUNCTION-SCOPED, never identity-blanket: the deploy
    identity is permitted only the workload-write rule in the scope
    namespaces (its function), and the security identity is permitted only
    admission-configuration writes (its reconciler function). A security
    principal holding, say, impersonation or token minting is a violation
    like any other subject — no principal set is exempt from the table as a
    whole.
    """
    allowed = set(owner_scope["iam_exempt_subjects"])
    violations: list[str] = []
    try:
        cluster_roles = {
            str(item.get("metadata", {}).get("name", "")): item
            for item in json.loads(
                live_runner(["kubectl", "get", "clusterroles", "-o", "json"])
            ).get("items")
            or []
        }
        cluster_bindings = json.loads(
            live_runner(["kubectl", "get", "clusterrolebindings", "-o", "json"])
        ).get("items") or []
        # EVERY namespace is audited, not only the scope/security set: a
        # RoleBinding in a foreign namespace can still grant an unflagged
        # identity path (token minting, CSR, RBAC mutation, impersonation)
        # whose effect escapes that namespace transitively — e.g. minting a
        # token for a foreign-namespace ServiceAccount that itself holds
        # cluster-wide grants.
        all_roles: dict[tuple[str, str], dict] = {}
        for item in (
            json.loads(
                live_runner(["kubectl", "get", "roles", "-A", "-o", "json"])
            ).get("items")
            or []
        ):
            metadata = item.get("metadata", {}) or {}
            all_roles[
                (str(metadata.get("namespace", "")), str(metadata.get("name", "")))
            ] = item
        namespaced_bindings: list[tuple[str, dict]] = []
        for binding in (
            json.loads(
                live_runner(
                    ["kubectl", "get", "rolebindings", "-A", "-o", "json"]
                )
            ).get("items")
            or []
        ):
            namespace = str(
                (binding.get("metadata") or {}).get("namespace", "")
            )
            namespaced_bindings.append((namespace, binding))
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(
            "the live RBAC surface cannot be audited through the "
            "authenticated API; the identity boundary is unverifiable — "
            "rendering fails closed"
        ) from error

    def principal_subjects(principals: Sequence[str]) -> set[str]:
        subjects: set[str] = set()
        for principal in principals:
            subjects.add(f"User:{principal}")
            if principal.startswith("system:serviceaccount:"):
                subjects.add(
                    "ServiceAccount:"
                    + principal[len("system:serviceaccount:"):]
                )
        return subjects

    role_subjects = {
        "deploy": principal_subjects(owner_scope["deploy_principals"]),
        "security": principal_subjects(owner_scope["security_principals"]),
    }

    def is_attested_bootstrap_masters(binding: dict) -> bool:
        # The one recognized bootstrap binding — and ONLY under the owner's
        # SIGNED provider attestation that system:masters certificate
        # issuance is provider-held. Nothing else touching system:masters is
        # ever tolerated, and the group itself is not exemptible.
        if attestation is None or (
            str(attestation.get("masters_certificate_issuance", ""))
            != "provider-held"
        ):
            return False
        role_ref = binding.get("roleRef") or {}
        return (
            str(binding.get("metadata", {}).get("name", "")) == "cluster-admin"
            and str(role_ref.get("kind", "")) == "ClusterRole"
            and str(role_ref.get("name", "")) == "cluster-admin"
            and _binding_subjects(binding) == ["Group:system:masters"]
        )

    def resolve_rules(
        binding: dict, namespace: str | None
    ) -> list[dict]:
        role_ref = binding.get("roleRef") or {}
        if namespace is None or str(role_ref.get("kind", "")) == "ClusterRole":
            role = cluster_roles.get(str(role_ref.get("name", "")), {})
        else:
            role = all_roles.get((namespace, str(role_ref.get("name", ""))), {})
        return role.get("rules") or []

    # PHASE 1 — derive the PRIVILEGED namespaces: the home namespace of any
    # ServiceAccount subject holding a forbidden identity path (checked
    # namespace-unfiltered). Stealing such an SA through its home
    # namespace's secrets/exec/workload writes is a transitive identity
    # path, so those namespaces join the protected set in phase 2.
    privileged_namespaces = set(owner_scope["namespaces"]) | (
        _security_namespaces(owner_scope)
    )
    all_bindings: list[tuple[str | None, dict]] = [
        (None, binding) for binding in cluster_bindings
    ] + [(namespace, binding) for namespace, binding in namespaced_bindings]
    for scan_namespace, binding in all_bindings:
        if scan_namespace is None and is_attested_bootstrap_masters(binding):
            continue
        rules = resolve_rules(binding, scan_namespace)
        if any(
            _rbac_rule_is_forbidden(rule, None, owner_scope)
            for rule in rules
        ):
            for subject in _binding_subjects(binding):
                if subject in allowed:
                    continue
                if subject.startswith("ServiceAccount:"):
                    privileged_namespaces.add(subject.split(":", 2)[1])
    protected_namespaces = frozenset(privileged_namespaces)

    def grant_within(rule: dict, permitted_grant: dict | None) -> bool:
        # A permitted_role allowance is GRANT-SHAPE-BOUND: the granted rule
        # must stay inside the function's exact verbs/resources, and where
        # the shape demands resourceNames, they must be present and inside
        # the allowed set. A wildcard or broader grant never qualifies.
        if not isinstance(permitted_grant, dict):
            return False
        granted_verbs = {str(v) for v in rule.get("verbs") or []}
        granted_resources = {str(r) for r in rule.get("resources") or []}
        if not granted_verbs or not granted_resources:
            return False
        if not granted_verbs <= set(permitted_grant["verbs"]):
            return False
        if not granted_resources <= set(permitted_grant["resources"]):
            return False
        names_for = set(permitted_grant.get("resource_names_for") or ())
        if granted_verbs & names_for:
            granted_names = {str(n) for n in rule.get("resourceNames") or []}
            if not granted_names or not granted_names <= set(
                permitted_grant.get("resource_names") or ()
            ):
                return False
        return True

    def check(
        binding: dict, rules: list[dict], binding_namespace: str | None
    ) -> None:
        if binding_namespace is None and is_attested_bootstrap_masters(binding):
            return
        for rule in rules or []:
            forbidden = _rbac_rule_is_forbidden(
                rule, binding_namespace, owner_scope, protected_namespaces
            )
            if forbidden is None:
                continue
            for subject in _binding_subjects(binding):
                if subject in allowed:
                    continue
                permitted_role = forbidden.get("permitted_role")
                if (
                    permitted_role is not None
                    and subject
                    in role_subjects.get(str(permitted_role), set())
                    and grant_within(rule, forbidden.get("permitted_grant"))  # type: ignore[arg-type]
                ):
                    # This exact rule, in this exact bounded shape, is that
                    # identity's own FUNCTION — never an exemption from any
                    # other forbidden verb or from a broader grant.
                    continue
                binding_name = str(
                    binding.get("metadata", {}).get("name", "?")
                )
                violations.append(
                    f"{subject} holds '{forbidden['why']}' via {binding_name}"
                )

    for check_namespace, binding in all_bindings:
        check(binding, resolve_rules(binding, check_namespace), check_namespace)
    if violations:
        raise ProvenanceError(
            "the external identity boundary does not hold: "
            + "; ".join(sorted(set(violations))[:10])
            + " — rendering fails closed until the owner-executed IAM "
            "closure removes these grants (or the owner signs an explicit "
            "exemption into the scope)"
        )


def _normalized_rbac_rules(rules) -> list[dict]:
    return sorted(
        (
            {
                "apiGroups": sorted(map(str, rule.get("apiGroups") or [])),
                "resources": sorted(map(str, rule.get("resources") or [])),
                "verbs": sorted(map(str, rule.get("verbs") or [])),
                "resourceNames": sorted(
                    map(str, rule.get("resourceNames") or [])
                ),
            }
            for rule in rules or []
        ),
        key=lambda rule: json.dumps(rule, sort_keys=True),
    )


def _assert_iam_manifest_matches(
    owner_scope: dict, live_runner, boundary_path: Path | None = None
) -> None:
    """The LIVE identity boundary must equal the committed iam-boundary.yaml.

    The policy manifest earned live equality rounds ago; the boundary
    manifest gets the same treatment: the owner-signed scope pins its bytes
    (iam_boundary_sha256), and every defined object — namespace,
    ServiceAccounts (automountServiceAccountToken: false), (Cluster)Role
    rules, and (Cluster)RoleBinding roleRef/subjects — must exist live and
    equal the committed definition. Absent or drifted objects fail closed.
    """
    if boundary_path is None:
        boundary_path = Path(__file__).resolve().parent / "iam-boundary.yaml"
    import yaml

    payload = _read_evidence_bytes(boundary_path, private=False)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != owner_scope["iam_boundary_sha256"]:
        raise ProvenanceError(
            f"the committed identity boundary manifest hashes to {digest}, "
            f"but the owner-signed scope pins "
            f"{owner_scope['iam_boundary_sha256']}; align the scope with the "
            "reviewed boundary revision"
        )
    kind_commands = {
        "Namespace": "namespace",
        "ServiceAccount": "serviceaccount",
        "ClusterRole": "clusterrole",
        "Role": "role",
        "ClusterRoleBinding": "clusterrolebinding",
        "RoleBinding": "rolebinding",
    }
    try:
        for document in yaml.safe_load_all(payload):
            if not document:
                continue
            kind = str(document.get("kind", ""))
            if kind not in kind_commands:
                raise ProvenanceError(
                    f"unexpected kind {kind!r} in {boundary_path}; the "
                    "boundary manifest carries only identity objects"
                )
            metadata = document.get("metadata") or {}
            name = str(metadata.get("name", ""))
            namespace = str(metadata.get("namespace", "") or "")
            command = ["kubectl", "get", kind_commands[kind], name]
            if namespace:
                command += ["-n", namespace]
            command += ["-o", "json"]
            live = json.loads(live_runner(command))
            if kind == "ServiceAccount":
                if live.get("automountServiceAccountToken") is not False:
                    raise ProvenanceError(
                        f"live ServiceAccount {namespace}/{name} does not "
                        "set automountServiceAccountToken: false as the "
                        "committed boundary requires; fails closed"
                    )
            elif kind in ("ClusterRole", "Role"):
                if _normalized_rbac_rules(
                    live.get("rules")
                ) != _normalized_rbac_rules(document.get("rules")):
                    raise ProvenanceError(
                        f"live {kind} {name} rules do not equal the "
                        "committed boundary definition; a broadened or "
                        "weakened identity role fails closed"
                    )
            elif kind in ("ClusterRoleBinding", "RoleBinding"):
                live_ref = live.get("roleRef") or {}
                committed_ref = document.get("roleRef") or {}
                normalize_subject = lambda subject: {  # noqa: E731
                    "kind": str(subject.get("kind", "")),
                    "name": str(subject.get("name", "")),
                    "namespace": str(subject.get("namespace", "") or ""),
                }
                if (
                    str(live_ref.get("kind", ""))
                    != str(committed_ref.get("kind", ""))
                    or str(live_ref.get("name", ""))
                    != str(committed_ref.get("name", ""))
                    or sorted(
                        (
                            normalize_subject(subject)
                            for subject in live.get("subjects") or []
                        ),
                        key=lambda item: json.dumps(item, sort_keys=True),
                    )
                    != sorted(
                        (
                            normalize_subject(subject)
                            for subject in document.get("subjects") or []
                        ),
                        key=lambda item: json.dumps(item, sort_keys=True),
                    )
                ):
                    raise ProvenanceError(
                        f"live {kind} {name} does not equal the committed "
                        "boundary binding (roleRef/subjects drifted); fails "
                        "closed"
                    )
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(
            "the LIVE identity boundary objects cannot be read or do not "
            "exist; the committed iam-boundary.yaml must be applied and "
            "equal before verification proceeds — fails closed"
        ) from error


def _assert_identity_hygiene(owner_scope: dict, live_runner) -> None:
    """Prove BOTH automation identities are live, non-human, short-lived.

    For every security AND deploy principal ServiceAccount: it must exist;
    `automountServiceAccountToken` must be explicitly false on the
    ServiceAccount AND on every pod running as it (no implicit long-lived
    mounts); no legacy kubernetes.io/service-account-token Secret may be
    bound to it; and every pod running AS it must carry AT LEAST ONE
    serviceAccountToken projection whose audience equals the scope's exact
    token_audience with expirationSeconds of at most 3600 — the REQUIRED
    projection — and no other token or credential path: no other-audience
    projections, no Secret volumes, and no env/envFrom Secret references.
    Projected tokens are the only sanctioned issuance because the kubelet's
    TokenRequest for a projection is BOUND to that exact Pod object
    (boundObjectRef = the Pod, enforced by the API server), while direct
    `serviceaccounts/token` minting is forbidden for every subject by the
    IAM table — so "short-lived, audience-bound, pod-bound" is verified
    against the cluster, not asserted. Failures refuse rendering.
    """
    principals = list(owner_scope["security_principals"]) + list(
        owner_scope["deploy_principals"]
    )
    try:
        for principal in principals:
            if not principal.startswith("system:serviceaccount:"):
                raise ProvenanceError(
                    f"automation principal {principal!r} is not a "
                    "ServiceAccount identity"
                )
            _, _, namespace, name = principal.split(":", 3)
            account = json.loads(
                live_runner(
                    [
                        "kubectl",
                        "get",
                        "serviceaccount",
                        name,
                        "-n",
                        namespace,
                        "-o",
                        "json",
                    ]
                )
            )
            if account.get("automountServiceAccountToken") is not False:
                raise ProvenanceError(
                    f"automation principal {principal!r} does not set "
                    "automountServiceAccountToken: false; implicit "
                    "long-lived token mounts are refused"
                )
            secrets = json.loads(
                live_runner(
                    ["kubectl", "get", "secrets", "-n", namespace, "-o", "json"]
                )
            )
            for secret in secrets.get("items") or []:
                if (
                    str(secret.get("type", ""))
                    == "kubernetes.io/service-account-token"
                    and str(
                        (secret.get("metadata") or {})
                        .get("annotations", {})
                        .get("kubernetes.io/service-account.name", "")
                    )
                    == name
                ):
                    raise ProvenanceError(
                        f"automation principal {principal!r} has a "
                        "LONG-LIVED token Secret "
                        f"{secret.get('metadata', {}).get('name')!r}; the "
                        "identity must be TokenRequest-only — rendering "
                        "fails closed"
                    )
            pods = json.loads(
                live_runner(
                    ["kubectl", "get", "pods", "-n", namespace, "-o", "json"]
                )
            )
            for pod in pods.get("items") or []:
                spec = pod.get("spec") or {}
                if str(spec.get("serviceAccountName", "")) != name:
                    continue
                pod_name = str((pod.get("metadata") or {}).get("name", ""))
                if spec.get("automountServiceAccountToken") is not False:
                    raise ProvenanceError(
                        f"pod {namespace}/{pod_name} runs as {principal!r} "
                        "without automountServiceAccountToken: false at the "
                        "POD level; the identity's pods must disable "
                        "automount explicitly so the required projection is "
                        "the only token path"
                    )
                qualifying_projections = 0
                for volume in spec.get("volumes") or []:
                    if volume.get("secret") is not None:
                        raise ProvenanceError(
                            f"pod {namespace}/{pod_name} runs as "
                            f"{principal!r} and mounts a Secret volume; the "
                            "automation identity's pods must carry no "
                            "stored-credential mounts — rendering fails "
                            "closed"
                        )
                    for source in (volume.get("projected") or {}).get(
                        "sources"
                    ) or []:
                        token = source.get("serviceAccountToken")
                        if token is None:
                            continue
                        expiration = token.get("expirationSeconds")
                        audience = str(token.get("audience", "") or "")
                        if (
                            not isinstance(expiration, int)
                            or isinstance(expiration, bool)
                            or expiration > 3600
                            or audience != owner_scope["token_audience"]
                        ):
                            raise ProvenanceError(
                                f"pod {namespace}/{pod_name} mounts a token "
                                f"for {principal!r} that is not bound to the "
                                "scope's EXACT token_audience with <=3600s "
                                "expiry; pod-bound short-lived audience-"
                                "bound projections are the only permitted "
                                "form"
                            )
                        qualifying_projections += 1
                if qualifying_projections < 1:
                    raise ProvenanceError(
                        f"pod {namespace}/{pod_name} runs as {principal!r} "
                        "without the REQUIRED serviceAccountToken projection "
                        "(exact audience, <=3600s); an identity pod with no "
                        "sanctioned token path is out of contract — "
                        "rendering fails closed"
                    )
                container_groups = (
                    spec.get("containers") or [],
                    spec.get("initContainers") or [],
                    spec.get("ephemeralContainers") or [],
                )
                for containers in container_groups:
                    for container in containers:
                        env_secret = any(
                            ((item.get("valueFrom") or {}).get("secretKeyRef"))
                            is not None
                            for item in container.get("env") or []
                        )
                        env_from_secret = any(
                            item.get("secretRef") is not None
                            for item in container.get("envFrom") or []
                        )
                        if env_secret or env_from_secret:
                            raise ProvenanceError(
                                f"pod {namespace}/{pod_name} runs as "
                                f"{principal!r} and injects Secret values "
                                "through env/envFrom; the automation "
                                "identity's pods must carry no stored-"
                                "credential paths — rendering fails closed"
                            )
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(
            "the automation identities cannot be verified through the "
            "authenticated API; rendering fails closed"
        ) from error


def _acceptance_heads_directory(run_root: Path) -> Path:
    return run_root / "release-inventory-heads"


def _anchor_snapshot(run_root: Path) -> dict:
    """Canonical snapshot of every local chain head for off-host anchoring.

    EVERY required chain is always enumerated — a not-yet-bootstrapped chain
    exports count 0 explicitly rather than being omitted, so an attestation
    built from this export always satisfies the required-chain coverage and
    an omitted chain in any OTHER snapshot is distinguishable from an empty
    one.
    """
    chains: dict[str, dict] = {}
    heads_directory = _acceptance_heads_directory(run_root)
    heads: list[str] = []
    if heads_directory.is_dir():
        heads = sorted(
            entry.name
            for entry in heads_directory.iterdir()
            if entry.name.endswith(".json") and not entry.name.startswith(".")
        )
    terminal = ""
    if heads:
        terminal = hashlib.sha256(
            _read_evidence_bytes(
                heads_directory / heads[-1], allow_hardlinks=True
            )
        ).hexdigest()
    chains["acceptance-heads"] = {"count": len(heads), "head": terminal}
    for name, ledger in (
        ("consumed", _consume_ledger_path(run_root)),
        ("publication-journal", _publication_journal_path(run_root)),
        ("reconcile-journal", run_root / "release-reconcile-journal.jsonl"),
    ):
        records = _read_chained_records(ledger)
        terminal = GENESIS_HASH
        if records and ledger.exists():
            terminal = _ledger_scan(ledger)["head"]
        chains[name] = {"count": len(records), "head": terminal}
    return {
        "schema": "fs2-serve.nebius.ai/anchored-heads/v1",
        "exported_at": datetime.now(UTC).isoformat(),
        "chains": chains,
    }


def _verified_acceptance_chain(
    run_root: Path, public_key_path: str, verifier
) -> list[dict]:
    """Verify the SIGNED, hash-chained, no-replace inventory acceptance heads.

    Every accepted render appends one head record — sequence, inventory
    generation, capture time, inventory sha256, and the sha256 of the
    PREVIOUS head's exact bytes — cosign-signed with the release key and
    published via link(2) under a serialized content-addressed name. On
    every render the whole chain is re-verified: signatures over the exact
    bytes, filenames matching content, sequences dense from 1, prev-hash
    linkage from the genesis hash, and strictly increasing generations. A
    tampered, unsigned, reordered, or gap-ridden store fails closed. The
    chain is local anti-replay state: WORM/off-host anchoring of the newest
    head (so whole-store deletion is also detectable) is the same owner
    infrastructure item as for the gate history.
    """
    directory = _acceptance_heads_directory(run_root)
    if directory.is_symlink():
        raise ProvenanceError(
            f"inventory acceptance store is a symlink: {directory}"
        )
    if not directory.is_dir():
        return []
    heads = sorted(
        entry
        for entry in directory.iterdir()
        if entry.name.endswith(".json") and not entry.name.startswith(".")
    )
    records: list[dict] = []
    previous_hash = GENESIS_HASH
    previous_generation = 0
    for index, head in enumerate(heads, start=1):
        signature_path = directory / (head.name + ".sig")
        if not signature_path.is_file():
            raise ProvenanceError(
                f"inventory acceptance head {head} is UNSIGNED; the "
                "acceptance chain fails closed"
            )
        payload = _read_evidence_bytes(head, allow_hardlinks=True)
        signature = _read_evidence_bytes(signature_path, allow_hardlinks=True)
        _verify_blob_bytes(
            public_key_path,
            payload,
            signature,
            verifier,
            f"inventory acceptance head {head}",
        )
        try:
            record = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ProvenanceError(
                f"inventory acceptance head {head} is malformed; the "
                "acceptance chain fails closed"
            ) from error
        sequence = record.get("sequence") if isinstance(record, dict) else None
        generation = record.get("generation") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or record.get("schema") != ACCEPTANCE_HEAD_SCHEMA
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or not SHA256_PATTERN.match(str(record.get("sha256", "")))
            or not SHA256_PATTERN.match(str(record.get("prev", "")))
        ):
            raise ProvenanceError(
                f"inventory acceptance head {head} is malformed; the "
                "acceptance chain fails closed"
            )
        _parse_rfc3339(
            str(record.get("captured_at", "")), f"{head} captured_at"
        )
        record_hash = hashlib.sha256(payload).hexdigest()
        expected_name = f"{sequence:012d}-{record_hash[:12]}.json"
        if head.name != expected_name:
            raise ProvenanceError(
                f"inventory acceptance head {head} does not match its "
                f"content address ({expected_name}); the chain fails closed"
            )
        if sequence != index:
            raise ProvenanceError(
                f"inventory acceptance chain has a gap or reordering at "
                f"sequence {index} (found {sequence}); a truncated or "
                "spliced chain fails closed"
            )
        if record["prev"] != previous_hash:
            raise ProvenanceError(
                f"inventory acceptance head {head} breaks the hash chain; "
                "the chain fails closed"
            )
        if generation <= previous_generation:
            raise ProvenanceError(
                f"inventory acceptance head {head} does not increase the "
                "generation; the chain fails closed"
            )
        previous_hash = record_hash
        previous_generation = generation
        records.append({**record, "_payload_hash": record_hash})
    return records


def _enforce_inventory_monotonicity(
    chain: list[dict], inventory: dict, inventory_sha256: str, inventory_path: Path
) -> bool:
    """Return True when this exact inventory is already the accepted head."""
    if not chain:
        return False
    head = chain[-1]
    generation = inventory["generation"]
    if generation > head["generation"]:
        newer = _parse_rfc3339(
            str(inventory.get("captured_at", "")), f"{inventory_path} captured_at"
        )
        accepted = _parse_rfc3339(
            str(head.get("captured_at", "")), "accepted head captured_at"
        )
        if newer < accepted:
            raise ProvenanceError(
                f"{inventory_path} generation {generation} was captured "
                "BEFORE the last accepted inventory; a rewound capture never "
                "advances the acceptance chain"
            )
        return False
    if generation == head["generation"] and inventory_sha256 == head["sha256"]:
        return True
    raise ProvenanceError(
        f"{inventory_path} replays generation {generation}; the signed "
        f"acceptance chain is at generation {head['generation']} "
        f"(sha256 {head['sha256'][:12]}…) — a previously valid signed "
        "inventory can never be replayed over a newer one"
    )


@contextmanager
def _exclusive_lock(run_root: Path, name: str) -> Iterator[None]:
    """Serialize a critical section per run root via an exclusive flock."""
    import fcntl

    lock_path = run_root / name
    if lock_path.is_symlink():
        raise ProvenanceError(f"lock must not be a symlink: {lock_path}")
    with lock_path.open("a+", encoding="utf-8") as lock:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProvenanceError(
                f"another process owns {name} at {run_root}; concurrent "
                "execution is refused"
            ) from error
        yield


@contextmanager
def _acceptance_chain_lock(run_root: Path) -> Iterator[None]:
    """Serialize acceptance-chain verify+append cycles across processes.

    Without the lock, two concurrent renders could both compute the same next
    sequence and link two DIFFERENT records at that sequence, permanently
    poisoning the chain (which can never be repaired by deletion under the
    no-delete constraint). The lock makes verify+append atomic per run root.
    """
    import fcntl

    lock_path = run_root / "release-inventory-heads.lock"
    if lock_path.is_symlink():
        raise ProvenanceError(
            f"acceptance chain lock must not be a symlink: {lock_path}"
        )
    with lock_path.open("a+", encoding="utf-8") as lock:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProvenanceError(
                f"another render owns the inventory acceptance chain at "
                f"{run_root}; concurrent acceptance is refused"
            ) from error
        yield


def _link_no_replace_or_adopt(staged: Path, final: Path, payload: bytes) -> None:
    """link(2) publication that ADOPTS a byte-identical survivor.

    A crash-and-retry (or a concurrent identical append) leaves the same
    bytes at the final path; anything else at that path fails closed.
    """
    try:
        os.link(staged, final)
    except FileExistsError:
        if final.is_symlink() or not final.is_file() or (
            _read_evidence_bytes(final, allow_hardlinks=True) != payload
        ):
            raise ProvenanceError(
                f"conflicting file already exists at {final}; the acceptance "
                "chain never replaces existing content"
            ) from None


def _append_acceptance_head(
    run_root: Path,
    chain: list[dict],
    inventory: dict,
    inventory_sha256: str,
    key_path: str,
    public_key_path: str,
    verifier,
    capture,
) -> None:
    """Append a SIGNED head, no-replace, linked into the serialized chain."""
    directory = _acceptance_heads_directory(run_root)
    directory.mkdir(mode=0o700, exist_ok=True)
    previous_hash = chain[-1]["_payload_hash"] if chain else GENESIS_HASH
    record = {
        "schema": ACCEPTANCE_HEAD_SCHEMA,
        "sequence": len(chain) + 1,
        "generation": inventory["generation"],
        "captured_at": inventory["captured_at"],
        "sha256": inventory_sha256,
        "prev": previous_hash,
    }
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    record_hash = hashlib.sha256(payload).hexdigest()
    final = directory / f"{record['sequence']:012d}-{record_hash[:12]}.json"
    final_signature = directory / (final.name + ".sig")
    with tempfile.TemporaryDirectory(dir=directory) as staging:
        staged = Path(staging) / "head.json"
        staged_signature = Path(staging) / "head.json.sig"
        staged.write_bytes(payload)
        staged.chmod(0o600)
        # Crash recovery must come BEFORE re-signing: real ECDSA signatures
        # are randomized, so a retry can never reproduce the orphan
        # signature's bytes. If a signature already sits at the final path,
        # it is VERIFIED over this deterministic head payload and ADOPTED;
        # only a signature that fails verification is a conflict.
        adopted_signature: bytes | None = None
        if final_signature.exists() or final_signature.is_symlink():
            if final_signature.is_symlink() or not final_signature.is_file():
                raise ProvenanceError(
                    f"conflicting file already exists at {final_signature}; "
                    "the acceptance chain never replaces existing content"
                )
            orphan = _read_evidence_bytes(final_signature, allow_hardlinks=True)
            try:
                _verify_blob_bytes(
                    public_key_path,
                    payload,
                    orphan,
                    verifier,
                    f"orphaned acceptance-head signature {final_signature.name}",
                )
            except ProvenanceError as error:
                raise ProvenanceError(
                    f"an orphaned signature at {final_signature} does not "
                    "verify over the deterministic head payload; the "
                    "acceptance chain never replaces existing content"
                ) from error
            adopted_signature = orphan
        if adopted_signature is None:
            capture(
                [
                    "cosign",
                    "sign-blob",
                    "--key",
                    key_path,
                    "--use-signing-config=false",
                    "--tlog-upload=false",
                    "--yes",
                    "--output-file",
                    str(staged_signature),
                    str(staged),
                ]
            )
            staged_signature.chmod(0o600)
            signature_bytes = _read_evidence_bytes(staged_signature)
            _verify_blob_bytes(
                public_key_path,
                payload,
                signature_bytes,
                verifier,
                f"new inventory acceptance head {final.name}",
            )
            _fsync_file(staged_signature)
        _fsync_file(staged)
        # Signature FIRST: a bare .sig is inert to chain verification, but a
        # bare .json would poison the chain unrecoverably (deletion is
        # forbidden). A crash between the two links leaves a recoverable
        # state: the retry verifies and adopts the orphan signature above.
        if adopted_signature is None:
            _link_no_replace_or_adopt(
                staged_signature, final_signature, signature_bytes
            )
        _link_no_replace_or_adopt(staged, final, payload)
        _fsync_dir(directory)


def verified_allowlist(
    public_key_path: str,
    references: Sequence[str],
    registry_prefixes: Sequence[str],
    platform_repository_prefix: str,
    receipts_root: Path,
    inventory_path: Path,
    scope_path: Path,
    attestation_path: Path | None = None,
    attestation_key_path: str | None = None,
    deploy_principals: Sequence[str] = (),
    key_path: str | None = None,
    verifier=None,
    max_age_hours: float = INVENTORY_MAX_AGE_HOURS,
    capture=_run_capture,
    live_runner=None,
) -> dict:
    """Render the allow-list only from the signed inventory + owner scope.

    The reference set comes from the verified inventory — never from an
    arbitrary operator-chosen subset. If explicit --image references are also
    given they must equal the inventory set exactly (extras and missing are
    both refused). Every inventory digest must then carry a bound release
    receipt — fully re-proven against the registry and retained SBOM
    evidence — and a valid cosign signature, so the ConfigMap can never
    drift ahead of the release evidence or silently drop coverage.

    Admission scope is never caller-chosen: the committed, owner-reviewed
    release-scope document is the single authority for cluster, namespaces,
    registry prefixes, platform repository prefix, deploy principals, and
    the verification-key identity. The SIGNED inventory must carry exactly
    that scope (a signer cannot substitute their own coverage), the CLI
    arguments must equal it (a mistyped prefix cannot bypass platform-digest
    gating), the pinned key hash must equal its recorded key identity, and
    the ConfigMap renders FROM the owner scope values.

    The verification key is read exactly once and pinned: every signature
    check and the recorded key annotation refer to that single identity. The
    recorded inventory annotation is the hash of the bytes that were verified
    and parsed — never a re-read of the mutable pathname.
    """
    with (
        _PinnedPublicKey(public_key_path) as pinned,
        # One render per run root: chain verify+append is atomic under an
        # exclusive lock, so concurrent renders can never fork the sequence.
        _acceptance_chain_lock(receipts_root),
    ):
        owner_scope = load_owner_scope(Path(scope_path), pinned.path, verifier)
        if Path(receipts_root).resolve() != Path(
            owner_scope["run_root"]
        ).resolve():
            raise ProvenanceError(
                f"--run-root {receipts_root} does not resolve to the "
                "owner-pinned scope run_root; single-use ledgers, journals, "
                "and locks bind globally to ONE root — a caller-selected "
                "directory never resets or forks them"
            )
        if attestation_path is None:
            raise ProvenanceError(
                "allow-list rendering requires --attestation: the "
                "provider-held boundary facts and the off-host anchored-heads "
                "snapshot enter only as an ATTESTOR-SIGNED document"
            )
        if attestation_key_path is None:
            raise ProvenanceError(
                "allow-list rendering requires --attestation-key: the "
                "attestation verifies only against the SEPARATE attestor key "
                "pinned by the owner-signed scope, never the release "
                "pipeline's own key"
            )
        if ATTESTATION_KEY_PROVENANCE != "owner-originated":
            raise ProvenanceError(
                "the committed attestor key is a BOOTSTRAP PLACEHOLDER "
                "(ATTESTATION_KEY_PROVENANCE is not 'owner-originated'): a "
                "trust anchor generated inside the remediation/release path "
                "is self-bootstrapped and never trusted — rendering fails "
                "closed until the OWNER originates the attestor key and "
                "replaces the fingerprint and provenance through review"
            )
        if not ATTESTATION_KEY_SHA256:
            raise ProvenanceError(
                "no attestor key is designated in reviewed source "
                "(ATTESTATION_KEY_SHA256 is empty); the attestor is never "
                "delegated to the release key — rendering fails closed until "
                "the owner commits the attestor fingerprint through review"
            )
        with _PinnedPublicKey(
            attestation_key_path, ATTESTATION_KEY_SHA256
        ) as attestor:
            if attestor.sha256 == pinned.sha256:
                raise ProvenanceError(
                    "the attestor key equals the release verification key; "
                    "a self-attested provider boundary never renders"
                )
            attestation = load_provider_attestation(
                Path(attestation_path), attestor.path, verifier
            )
        if str(attestation.get("worm_store", "")) != owner_scope["worm_store_uri"]:
            raise ProvenanceError(
                "the attestation names a different WORM store than the "
                "owner-signed scope's worm_store_uri; the anchor location is "
                "owner authority — rendering fails closed"
            )
        _assert_anchored_heads(receipts_root, attestation["anchored_heads"])
        if live_runner is None:
            live_runner = _pinned_live_runner(owner_scope)
        # The provider arm is enforced against LIVE provider-native answers
        # (owner-pinned provider CLI), never a caller-supplied export file.
        _assert_provider_boundary(owner_scope, attestation, live_runner)
        if pinned.sha256 != owner_scope["verification_key_sha256"]:
            raise ProvenanceError(
                "verification key does not match the owner-approved scope: "
                f"pinned key sha256 {pinned.sha256} != scope key "
                f"{owner_scope['verification_key_sha256']}"
            )
        inventory, inventory_sha256 = load_signed_inventory(
            inventory_path, pinned.path, verifier, max_age_hours
        )
        if not key_path:
            raise ProvenanceError(
                "allow-list rendering requires --key: every accepted "
                "inventory appends a SIGNED head to the acceptance chain"
            )
        acceptance_chain = _verified_acceptance_chain(
            receipts_root, pinned.path, verifier
        )
        already_accepted = _enforce_inventory_monotonicity(
            acceptance_chain, inventory, inventory_sha256, inventory_path
        )
        if inventory["scope"] != owner_scope:
            raise ProvenanceError(
                f"{inventory_path} scope does not equal the owner-approved "
                "release scope exactly; a signed inventory for a different "
                "scope never renders an allow-list"
            )
        if sorted(registry_prefixes) != sorted(owner_scope["registry_prefixes"]):
            raise ProvenanceError(
                "--registry-prefix arguments must equal the owner-approved "
                f"scope exactly: {sorted(owner_scope['registry_prefixes'])}"
            )
        if platform_repository_prefix != owner_scope["platform_repository_prefix"]:
            raise ProvenanceError(
                "--platform-repository-prefix must equal the owner-approved "
                f"scope exactly: {owner_scope['platform_repository_prefix']!r}"
            )
        if sorted(deploy_principals) != sorted(owner_scope["deploy_principals"]):
            raise ProvenanceError(
                "--deploy-principal arguments must equal the owner-approved "
                f"scope exactly: {sorted(owner_scope['deploy_principals'])}"
            )
        # Authoritative render-time collection: every signed source must
        # equal exactly what the authenticated API session observes NOW —
        # Pods AND workload controllers (a scaled-to-zero or crash-looping
        # Deployment counts), the Helm rollback window per release revision,
        # and the frozen bindings re-fetched from the exact resources they
        # claim. Signed resource identities are compared, not trusted.
        observation = collect_authoritative_observation(owner_scope, live_runner)
        if str(attestation.get("cluster")) != observation["cluster"]:
            raise ProvenanceError(
                "the provider attestation pins a different cluster than the "
                "live kube-system UID; rendering fails closed"
            )
        sources = inventory["sources"]
        recorded_live = {
            validate_digest_reference(str(ref))
            for ref in sources["live_workloads"]["refs"]
        }
        if recorded_live != observation["live_images"]:
            omitted = sorted(observation["live_images"] - recorded_live)
            phantom = sorted(recorded_live - observation["live_images"])
            raise ProvenanceError(
                f"{inventory_path} live_workloads does not equal the "
                "authenticated live enumeration; omitted live images: "
                f"{omitted or 'none'}; recorded-but-not-live: "
                f"{phantom or 'none'} — re-capture the inventory"
            )
        recorded_live_ids = {
            str(item) for item in sources["live_workloads"]["resource_ids"]
        }
        if recorded_live_ids != observation["live_resources"]:
            raise ProvenanceError(
                f"{inventory_path} live_workloads resource identities do not "
                "equal the authenticated observation; forged or stale "
                "resource identities never render"
            )
        recorded_helm = {
            validate_digest_reference(str(ref))
            for ref in sources["helm_rollback_window"]["refs"]
        }
        if recorded_helm != observation["helm_images"]:
            raise ProvenanceError(
                f"{inventory_path} helm_rollback_window does not equal the "
                "authenticated Helm history enumeration; omitted: "
                f"{sorted(observation['helm_images'] - recorded_helm) or 'none'}; "
                "recorded-but-not-in-history: "
                f"{sorted(recorded_helm - observation['helm_images']) or 'none'}"
            )
        recorded_helm_ids = {
            str(item) for item in sources["helm_rollback_window"]["resource_ids"]
        }
        if recorded_helm_ids != observation["helm_resources"]:
            raise ProvenanceError(
                f"{inventory_path} helm_rollback_window resource identities "
                "do not equal the authenticated Helm history observation"
            )
        _verify_frozen_bindings(
            owner_scope,
            sources["frozen_scientific_bindings"],
            inventory_path,
            live_runner,
        )
        if inventory["collector"]["identity"] != observation["identity"]:
            raise ProvenanceError(
                f"{inventory_path} collector identity "
                f"{inventory['collector']['identity']!r} does not equal the "
                f"authenticated identity {observation['identity']!r}; a "
                "foreign or self-asserted collection never renders"
            )
        collector_identity = observation["identity"]
        live_resources = observation["live_resources"]
        inventory_references = sorted(
            validate_digest_reference(str(ref))
            for ref in inventory["platform_images"]
        )
        if references:
            given = {validate_digest_reference(ref) for ref in references}
            if given != set(inventory_references):
                missing = sorted(set(inventory_references) - given)
                extras = sorted(given - set(inventory_references))
                raise ProvenanceError(
                    "--image references must equal the signed inventory "
                    f"exactly; missing: {missing or 'none'}; extras: "
                    f"{extras or 'none'}"
                )
        run_verifier = verifier or (
            lambda command: subprocess.run(list(command), check=True)
        )
        digests = []
        for reference in inventory_references:
            if not reference.startswith(
                tuple(owner_scope["registry_prefixes"])
            ):
                raise ProvenanceError(
                    f"inventory reference {reference} lies outside the "
                    "owner-approved registry prefixes; refusing to render"
                )
            load_bound_receipt(
                receipts_root, reference, pinned.path, verifier, capture
            )
            try:
                run_verifier(cosign_verify_command(pinned.path, reference))
            except subprocess.CalledProcessError as error:
                raise ProvenanceError(
                    f"signature verification failed for {reference}; sign it "
                    "before allow-listing"
                ) from error
            digests.append(reference.rsplit("@", 1)[1])
        _assert_policy_matches_scope(owner_scope, live_runner)
        _assert_iam_manifest_matches(owner_scope, live_runner)
        _assert_iam_boundary(owner_scope, live_runner, attestation)
        _assert_identity_hygiene(owner_scope, live_runner)
        automation_accounts = sorted(
            principal[len("system:serviceaccount:"):]
            for principal in (
                list(owner_scope["deploy_principals"])
                + list(owner_scope["security_principals"])
            )
            if principal.startswith("system:serviceaccount:")
        )
        manifest = render_allowlist(
            owner_scope["registry_prefixes"],
            owner_scope["platform_repository_prefix"],
            digests,
            owner_scope["helm_secret_writers"],
            owner_scope["namespaces"],
            owner_scope["token_audience"],
            automation_accounts,
            owner_scope["workload_service_accounts"],
            owner_scope["deploy_credential_csi_driver"],
            owner_scope["deploy_credential_spc"],
            owner_scope["workload_csi_drivers"],
        )
        annotations = manifest["metadata"].setdefault("annotations", {})
        annotations["security.fs2.nebius.ai/verified-with-key-sha256"] = (
            pinned.sha256
        )
        annotations["security.fs2.nebius.ai/inventory-sha256"] = inventory_sha256
        annotations["security.fs2.nebius.ai/scope-cluster"] = owner_scope[
            "cluster"
        ]
        annotations["security.fs2.nebius.ai/scope-namespaces"] = ",".join(
            owner_scope["namespaces"]
        )
        annotations["security.fs2.nebius.ai/inventory-generation"] = str(
            inventory["generation"]
        )
        annotations["security.fs2.nebius.ai/collector-identity"] = (
            collector_identity
        )
        annotations["security.fs2.nebius.ai/live-resources"] = str(
            len(live_resources)
        )
        # TOCTOU shrink: the boundary and policy state are re-audited at the
        # END of the render too, so a mutation racing the earlier checks
        # cannot ride out with a freshly rendered artifact.
        _assert_policy_matches_scope(owner_scope, live_runner)
        _assert_iam_manifest_matches(owner_scope, live_runner)
        _assert_iam_boundary(owner_scope, live_runner, attestation)
        if not already_accepted:
            _append_acceptance_head(
                receipts_root,
                acceptance_chain,
                inventory,
                inventory_sha256,
                key_path,
                pinned.path,
                verifier,
                capture,
            )
    return manifest


def cosign_sign_command(key_path: str, reference: str) -> list[str]:
    """Key-based signing without publishing to a public transparency log.

    The signing config and new bundle format are disabled: the first fetches
    public sigstore services, and the regional registry rejects the bundle
    media type, so signatures use the classic per-digest `.sig` tag format.
    """
    return [
        "cosign",
        "sign",
        "--key",
        key_path,
        "--use-signing-config=false",
        "--new-bundle-format=false",
        "--tlog-upload=false",
        "--yes",
        validate_digest_reference(reference),
    ]


def cosign_verify_command(public_key_path: str, reference: str) -> list[str]:
    return [
        "cosign",
        "verify",
        "--key",
        public_key_path,
        "--private-infrastructure=true",
        validate_digest_reference(reference),
    ]


def run_commands(commands: Sequence[Sequence[str]]) -> None:
    for command in commands:
        print("+ " + " ".join(command), file=sys.stderr)
        subprocess.run(list(command), check=True)


def _reconcile_persist_input(run_root: Path, payload: bytes) -> str:
    """Content-address a plan input under run_root/plan-inputs, no-replace."""
    inputs_store = run_root / "plan-inputs"
    digest = hashlib.sha256(payload).hexdigest()
    inputs_store.mkdir(mode=0o700, exist_ok=True)
    final = inputs_store / digest
    if not final.exists():
        descriptor, temp_name = tempfile.mkstemp(
            dir=inputs_store, prefix=".input-"
        )
        try:
            os.write(descriptor, payload)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _link_no_replace_or_adopt(Path(temp_name), final, payload)
    return digest


def _reconcile_load_input(run_root: Path, digest: str) -> bytes:
    payload = _read_evidence_bytes(
        run_root / "plan-inputs" / digest, allow_hardlinks=True
    )
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ProvenanceError(
            f"stored plan input {digest} no longer matches its "
            "content address; execution fails closed"
        )
    return payload


def _policy_bytes_excluding_binding(
    policy_bytes: bytes, binding_name: str
) -> bytes:
    """The committed manifest MINUS one protected binding, deterministically.

    When an owner-signed recovery document owns a binding, that binding is
    transitioned ONLY by the UID/resourceVersion-fenced patch. A bulk
    `kubectl apply` of the full manifest would rewrite the target too —
    changing its actions and bumping its resourceVersion so the fenced patch
    could never apply (and resume could never re-fence) — so the apply that
    repairs OTHER drifted objects must exclude the recovery target entirely.
    """
    import yaml

    documents = [
        document for document in yaml.safe_load_all(policy_bytes) if document
    ]
    kept = [
        document
        for document in documents
        if not (
            document.get("kind") == "ValidatingAdmissionPolicyBinding"
            and str(document.get("metadata", {}).get("name", ""))
            == binding_name
        )
    ]
    if len(kept) == len(documents):
        raise ProvenanceError(
            f"the committed manifest carries no binding named "
            f"{binding_name!r} to exclude; refusing to plan"
        )
    return yaml.safe_dump_all(kept, sort_keys=True).encode("utf-8")


def reconcile_boundary(
    public_key_path: str,
    scope_path: Path,
    run_root: Path,
    attestation_path: Path | None = None,
    attestation_key_path: str | None = None,
    recovery_path: Path | None = None,
    authorization_path: Path | None = None,
    execute: bool = False,
    resume: bool = False,
    verifier=None,
    live_runner=None,
    rollout_window_authorized: bool | None = None,
    policy_path: Path | None = None,
    output=print,
) -> int:
    """The security-owned reconciler: plan, execute, or resume — testable.

    Extracted from the CLI so the FULL execute/resume orchestration —
    fenced recovery sequencing, crash-resume, executor lineage — is provable
    with injected runners/verifiers in CI, not just described. The CLI wires
    argparse straight into this function.
    """
    if policy_path is None:
        policy_path = Path(__file__).resolve().parent / "policy.yaml"
    journal = run_root / "release-reconcile-journal.jsonl"
    with _PinnedPublicKey(public_key_path) as pinned:
        owner_scope = load_owner_scope(Path(scope_path), pinned.path, verifier)
        if Path(run_root).resolve() != Path(owner_scope["run_root"]).resolve():
            raise ProvenanceError(
                f"--run-root {run_root} does not resolve to the owner-pinned "
                "scope run_root; single-use ledgers, journals, and locks "
                "bind globally to ONE root — a caller-selected directory "
                "never resets or forks them"
            )
        attestation = None
        if (execute or resume) and attestation_path is None:
            raise ProvenanceError(
                "reconcile-boundary --execute/--resume requires "
                "--attestation: mutating the admission boundary without "
                "the enforced anchored-heads snapshot and provider facts "
                "is refused"
            )
        if attestation_path is not None:
            if attestation_key_path is None:
                raise ProvenanceError(
                    "--attestation requires --attestation-key: the "
                    "attestation verifies only against the SEPARATE "
                    "attestor key pinned in reviewed source"
                )
            if ATTESTATION_KEY_PROVENANCE != "owner-originated":
                raise ProvenanceError(
                    "the committed attestor key is a BOOTSTRAP PLACEHOLDER "
                    "(ATTESTATION_KEY_PROVENANCE is not 'owner-originated'); "
                    "a self-bootstrapped trust anchor never authorizes the "
                    "reconciler — the OWNER originates the attestor key and "
                    "replaces the fingerprint and provenance through review"
                )
            with _PinnedPublicKey(
                attestation_key_path, ATTESTATION_KEY_SHA256
            ) as attestor:
                if attestor.sha256 == pinned.sha256:
                    raise ProvenanceError(
                        "the attestor key equals the release verification "
                        "key; a self-attested provider boundary never "
                        "authorizes the reconciler"
                    )
                attestation = load_provider_attestation(
                    attestation_path, attestor.path, verifier
                )
            if (
                str(attestation.get("worm_store", ""))
                != owner_scope["worm_store_uri"]
            ):
                raise ProvenanceError(
                    "the attestation names a different WORM store than the "
                    "owner-signed scope's worm_store_uri; fails closed"
                )
        runner = live_runner if live_runner is not None else _pinned_live_runner(owner_scope)

        def authenticate_caller() -> str:
            whoami = json.loads(
                runner(["kubectl", "auth", "whoami", "-o", "json"])
            )
            caller = str(
                (whoami.get("status") or {})
                .get("userInfo", {})
                .get("username", "")
            )
            if caller not in owner_scope["security_principals"]:
                raise ProvenanceError(
                    f"the authenticated caller {caller!r} is not a scope "
                    "security principal; only the security automation "
                    "identity executes the reconciler"
                )
            return caller

        def live_cluster_uid() -> str:
            return runner(
                [
                    "kubectl",
                    "get",
                    "namespace",
                    "kube-system",
                    "-o",
                    "jsonpath={.metadata.uid}",
                ]
            ).strip()

        def enforce_attestation_binding() -> None:
            # Re-run the anchor, cluster, and PROVIDER-BOUNDARY enforcement
            # at the moment that matters: UNDER the exclusive lock for
            # execute/resume (a pre-lock check alone could be raced).
            if attestation is None:
                return
            _assert_anchored_heads(run_root, attestation["anchored_heads"])
            if str(attestation.get("cluster")) != live_cluster_uid():
                raise ProvenanceError(
                    "the provider attestation pins a different cluster than "
                    "the live kube-system UID; an attestation for another "
                    "cluster never authorizes anything here"
                )
            _assert_provider_boundary(owner_scope, attestation, runner)

        enforce_attestation_binding()

        def compute_plan(
            resuming: bool = False,
        ) -> tuple[list[dict], dict | None, str | None]:
            plan: list[dict] = []
            recovery_document = None
            recovery_sha = None
            skip: frozenset[str] = frozenset()
            if recovery_path is not None:
                recovery_document, recovery_sha = load_recovery_authorization(
                    recovery_path,
                    pinned.path,
                    verifier,
                    run_root=None if resuming else run_root,
                )
                target = str(recovery_document["target"])
                # The recovery target is owned EXCLUSIVELY by the fenced
                # patch below: drift detection and any repair apply must
                # leave it untouched, or the apply would bump its
                # resourceVersion and wedge the fence (and resume).
                skip = frozenset({target})
                if str(recovery_document.get("cluster")) != live_cluster_uid():
                    raise ProvenanceError(
                        "the recovery authorization pins a different "
                        "cluster than the LIVE kube-system UID; a "
                        "document for another cluster never authorizes "
                        "here"
                    )
                live_target = json.loads(
                    runner(
                        [
                            "kubectl",
                            "get",
                            "validatingadmissionpolicybinding",
                            target,
                            "-o",
                            "json",
                        ]
                    )
                )
                live_metadata = live_target.get("metadata") or {}
                live_actions = sorted(
                    live_target.get("spec", {}).get("validationActions") or []
                )
                already_applied = (
                    str(live_metadata.get("uid", ""))
                    == recovery_document["target_uid"]
                    and live_actions == sorted(recovery_document["actions"])
                    and str(
                        (live_metadata.get("annotations") or {}).get(
                            RECOVERY_ANNOTATION, ""
                        )
                    )
                    == recovery_sha
                )
                fences_hold = (
                    str(live_metadata.get("uid", ""))
                    == recovery_document["target_uid"]
                    and str(live_metadata.get("resourceVersion", ""))
                    == recovery_document["target_resource_version"]
                    and live_actions
                    == sorted(recovery_document["prior_actions"])
                )
                if not fences_hold and not (resuming and already_applied):
                    # On resume the ONE other sanctioned live state is the
                    # toggle this exact document already applied (actions +
                    # annotation match); anything else means the state moved
                    # and the owner must re-issue.
                    raise ProvenanceError(
                        "the live target does not match the recovery "
                        "authorization's pinned UID/resourceVersion/"
                        "prior state; the owner must issue a fresh "
                        "authorization against the CURRENT state"
                    )
                patch = json.dumps(
                    {
                        "metadata": {
                            "uid": recovery_document["target_uid"],
                            "resourceVersion": recovery_document[
                                "target_resource_version"
                            ],
                            "annotations": {RECOVERY_ANNOTATION: recovery_sha},
                        },
                        "spec": {
                            "validationActions": recovery_document["actions"]
                        },
                    }
                )
                # The fenced patch comes FIRST: it is pinned to the exact
                # resourceVersion the owner observed, and nothing before it
                # may touch the target.
                plan.append(
                    {
                        "argv": [
                            "kubectl",
                            "patch",
                            "validatingadmissionpolicybinding",
                            target,
                            "--type=merge",
                            "-p",
                            patch,
                        ],
                        "stdin_sha256": None,
                        "verify": {
                            "kind": "binding-actions",
                            "target": target,
                            "actions": list(recovery_document["actions"]),
                            "annotation": recovery_sha,
                        },
                    }
                )
            try:
                _assert_policy_matches_scope(
                    owner_scope, runner, skip_bindings=skip or None
                )
            except ProvenanceError as drift:
                print(f"# policy drift detected: {drift}", file=sys.stderr)
                policy_bytes = _read_evidence_bytes(policy_path, private=False)
                policy_sha = hashlib.sha256(policy_bytes).hexdigest()
                if policy_sha != owner_scope["policy_sha256"]:
                    raise ProvenanceError(
                        "committed policy bytes do not match the "
                        "owner-signed scope pin; refusing to plan an "
                        "apply"
                    ) from drift
                apply_bytes = policy_bytes
                if skip:
                    apply_bytes = _policy_bytes_excluding_binding(
                        policy_bytes, next(iter(skip))
                    )
                stdin_sha = _reconcile_persist_input(run_root, apply_bytes)
                plan.append(
                    {
                        "argv": ["kubectl", "apply", "-f", "-"],
                        "stdin_sha256": stdin_sha,
                        "verify": {
                            "kind": "policy-equality",
                            "skip": sorted(skip),
                        },
                    }
                )
            return plan, recovery_document, recovery_sha

        def entry_satisfied(entry: dict) -> bool:
            verify = entry.get("verify")
            if not verify:
                return False
            if verify.get("kind") == "policy-equality":
                try:
                    _assert_policy_matches_scope(
                        owner_scope,
                        runner,
                        skip_bindings=frozenset(verify.get("skip") or ())
                        or None,
                    )
                except ProvenanceError:
                    return False
                return True
            if verify.get("kind") != "binding-actions":
                return False
            live = json.loads(
                runner(
                    [
                        "kubectl",
                        "get",
                        "validatingadmissionpolicybinding",
                        str(verify["target"]),
                        "-o",
                        "json",
                    ]
                )
            )
            if sorted(
                live.get("spec", {}).get("validationActions") or []
            ) != sorted(verify["actions"]):
                return False
            expected_annotation = verify.get("annotation")
            if expected_annotation is None:
                return True
            # Matching actions alone never satisfy the entry: only the
            # state THIS signed document applied (its annotation) counts;
            # a same-actions state from any other patch is unaccounted
            # drift and must be re-patched under the pinned fences.
            live_annotations = (
                (live.get("metadata") or {}).get("annotations") or {}
            )
            return str(
                live_annotations.get(RECOVERY_ANNOTATION, "")
            ) == str(expected_annotation)

        def run_plan(plan: list[dict]) -> None:
            for entry in plan:
                if entry_satisfied(entry):
                    # Idempotent completion: a crash after a successful
                    # step never reruns it against a stale fence.
                    continue
                input_text = None
                if entry.get("stdin_sha256"):
                    input_text = _reconcile_load_input(
                        run_root, str(entry["stdin_sha256"])
                    ).decode("utf-8")
                runner(list(entry["argv"]), input_text)

        def postcheck(
            recovery_document: dict | None, recovery_sha: str | None
        ) -> None:
            # After a legitimate Audit/Warn recovery the live target
            # DIFFERS from the committed Deny/Audit by design; the
            # override compares exactly the authorized actions (plus the
            # authorizing annotation) for the target and full committed
            # equality for everything else — without it, sanctioned
            # break-glass could never complete its own post-check.
            overrides = None
            if recovery_document is not None and recovery_sha is not None:
                overrides = {
                    str(recovery_document["target"]): (
                        list(recovery_document["actions"]),
                        recovery_sha,
                    )
                }
            _assert_policy_matches_scope(
                owner_scope, runner, binding_action_overrides=overrides
            )
            if recovery_document is not None:
                applied = json.loads(
                    runner(
                        [
                            "kubectl",
                            "get",
                            "validatingadmissionpolicybinding",
                            recovery_document["target"],
                            "-o",
                            "json",
                        ]
                    )
                )
                if sorted(
                    applied.get("spec", {}).get("validationActions") or []
                ) != sorted(recovery_document["actions"]):
                    raise ProvenanceError(
                        "post-check failed: the applied validationActions "
                        "do not equal the authorized recovery actions"
                    )

        def enforce_executor(authorization: dict, caller: str) -> None:
            executor = str(authorization.get("executor", ""))
            if executor not in owner_scope["security_principals"]:
                raise ProvenanceError(
                    "the authorization's executor is not a scope security "
                    "principal; execution fails closed"
                )
            if caller != executor:
                raise ProvenanceError(
                    f"the authenticated caller {caller!r} is not the "
                    f"authorization's named executor {executor!r}; execution "
                    "and resume are bound to ONE owner-designated identity, "
                    "never to whichever security principal shows up"
                )

        if resume:
            if authorization_path is None:
                raise ProvenanceError(
                    "--resume requires the ORIGINAL --authorization: "
                    "executed commands come only from the re-verified "
                    "signed document, never from a local journal"
                )
            with _exclusive_lock(run_root, "release-reconcile.lock"):
                caller = authenticate_caller()
                enforce_attestation_binding()
                authorization, authorization_sha = load_rollout_authorization(
                    authorization_path,
                    pinned.path,
                    live_cluster_uid(),
                    None,
                    run_root,
                    verifier,
                    allow_consumed=True,
                )
                enforce_executor(authorization, caller)
                records = _read_chained_records(journal)
                completes = {
                    record.get("authorization_sha256")
                    for record in records
                    if record.get("phase") == "complete"
                }
                if authorization_sha in completes:
                    output("PLAN: nothing to resume — already complete")
                    return 0
                intent = next(
                    (
                        record
                        for record in reversed(records)
                        if record.get("phase") == "intent"
                        and record.get("authorization_sha256")
                        == authorization_sha
                    ),
                    None,
                )
                if intent is None:
                    raise ProvenanceError(
                        "no journaled intent exists for this "
                        "authorization; --resume only completes a "
                        "post-consume crash"
                    )
                # The journal is accounting, never authority: every field
                # of the intent must re-verify against the SIGNED
                # documents and the live cluster before anything runs.
                if str(intent.get("plan_sha256", "")) != str(
                    authorization.get("plan_sha256", "")
                ):
                    raise ProvenanceError(
                        "the journaled intent's plan digest does not "
                        "equal the signed authorization's embedded plan; "
                        "a spliced or foreign intent never resumes"
                    )
                if str(intent.get("caller", "")) != str(
                    authorization.get("executor", "")
                ):
                    raise ProvenanceError(
                        "the journaled intent was not recorded by the "
                        "authorization's named executor; a foreign intent "
                        "never resumes"
                    )
                signed_plan = list(authorization["plan"])
                plan_expects_recovery = any(
                    (entry.get("verify") or {}).get("kind")
                    == "binding-actions"
                    for entry in signed_plan
                )
                recovery_document = None
                recovery_sha = None
                if plan_expects_recovery:
                    if recovery_path is None:
                        raise ProvenanceError(
                            "--resume of a plan containing a recovery "
                            "action requires the ORIGINAL --recovery "
                            "document"
                        )
                    recovery_document, recovery_sha = (
                        load_recovery_authorization(
                            recovery_path, pinned.path, verifier
                        )
                    )
                    if str(intent.get("recovery_sha256", "")) != recovery_sha:
                        raise ProvenanceError(
                            "the presented recovery document does not "
                            "equal the one journaled at intent time; a "
                            "substituted recovery never resumes"
                        )
                    if not _is_consumed(run_root, recovery_sha):
                        raise ProvenanceError(
                            "the recovery document was never consumed; "
                            "--resume only completes a post-consume "
                            "crash — run --execute instead"
                        )
                    if not any(
                        (entry.get("verify") or {}).get("annotation")
                        == recovery_sha
                        for entry in signed_plan
                    ):
                        raise ProvenanceError(
                            "the signed plan's recovery entry is not "
                            "bound to the presented recovery document"
                        )
                    if str(
                        recovery_document.get("cluster")
                    ) != live_cluster_uid():
                        raise ProvenanceError(
                            "the recovery authorization pins a different "
                            "cluster than the LIVE kube-system UID"
                        )
                elif recovery_path is not None:
                    raise ProvenanceError(
                        "--recovery was presented but the signed plan "
                        "contains no recovery action; refusing an "
                        "unaccounted document"
                    )
                _assert_iam_manifest_matches(owner_scope, runner)
                _assert_iam_boundary(owner_scope, runner, attestation)
                # Recompute the plan from LIVE state under the lock. ONLY
                # the recomputed (still-outstanding) entries run, and every
                # one of them must be an entry the owner signed — a
                # recomputed entry outside the signed plan means live state
                # diverged since signing, and nothing runs. Signed entries
                # absent from the recomputation are exactly the ones live
                # state already satisfies.
                recomputed_plan, _, _ = compute_plan(resuming=True)
                signed_entries = {
                    json.dumps(entry, sort_keys=True)
                    for entry in signed_plan
                }
                unsigned_entries = [
                    entry
                    for entry in recomputed_plan
                    if json.dumps(entry, sort_keys=True)
                    not in signed_entries
                ]
                if unsigned_entries:
                    raise ProvenanceError(
                        "the live state requires actions the owner never "
                        "signed; --resume executes ONLY the signed plan — "
                        "obtain a fresh authorization for the new state"
                    )
                run_plan(recomputed_plan)
                postcheck(recovery_document, recovery_sha)
                _append_chained_record(
                    journal,
                    {
                        "phase": "complete",
                        "authorization_sha256": authorization_sha,
                        "resumed": True,
                        "caller": caller,
                        "at": datetime.now(UTC).isoformat(),
                    },
                )
            return 0

        if not execute:
            plan, _, _ = compute_plan()
            violations = None
            try:
                _assert_iam_manifest_matches(owner_scope, runner)
                _assert_iam_boundary(owner_scope, runner, attestation)
            except ProvenanceError as violation:
                violations = str(violation)
                print(
                    f"# identity-path violation: {violations}",
                    file=sys.stderr,
                )
            plan_sha256 = hashlib.sha256(
                json.dumps(plan, sort_keys=True).encode("utf-8")
            ).hexdigest()
            for entry in plan:
                output("PLAN: " + " ".join(entry["argv"]))
            output("PLAN-DOCUMENT: " + json.dumps(plan, sort_keys=True))
            output(f"PLAN-SHA256: {plan_sha256}")
            if not plan and not violations:
                output("PLAN: nothing to reconcile — live state matches")
            return 0

        # EXECUTE: everything observed and re-verified UNDER the lock —
        # anchored heads, attestation cluster pin, plan recomputation,
        # recovery fences, IAM re-audit — so nothing signed refers to
        # pre-lock state.
        window_open = (
            rollout_window_authorized
            if rollout_window_authorized is not None
            else os.environ.get("FS2_ROLLOUT_AUTHORIZED") == "1"
        )
        if not window_open:
            raise ProvenanceError(
                "reconcile-boundary --execute requires the rollout "
                "window (FS2_ROLLOUT_AUTHORIZED=1) in addition to the "
                "owner-signed authorization"
            )
        if authorization_path is None:
            raise ProvenanceError(
                "reconcile-boundary --execute requires --authorization: "
                "execution authority is an OWNER-SIGNED, plan-bound, "
                "single-use document, never an environment flag"
            )
        with _exclusive_lock(run_root, "release-reconcile.lock"):
            caller = authenticate_caller()
            enforce_attestation_binding()
            plan, recovery_document, recovery_sha = compute_plan()
            plan_sha256 = hashlib.sha256(
                json.dumps(plan, sort_keys=True).encode("utf-8")
            ).hexdigest()
            _assert_iam_manifest_matches(owner_scope, runner)
            _assert_iam_boundary(owner_scope, runner, attestation)
            authorization, authorization_sha = load_rollout_authorization(
                authorization_path,
                pinned.path,
                live_cluster_uid(),
                plan_sha256,
                run_root,
                verifier,
            )
            enforce_executor(authorization, caller)
            _append_chained_record(
                journal,
                {
                    "phase": "intent",
                    "plan_sha256": plan_sha256,
                    "authorization_sha256": authorization_sha,
                    "recovery_sha256": recovery_sha,
                    "caller": caller,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
            consumed_entries = [(authorization_sha, "rollout-authorization")]
            if recovery_sha is not None:
                consumed_entries.append((recovery_sha, "recovery"))
            # ONE atomic append: no crash window can leave the authorization
            # consumed while its recovery document is not (which would wedge
            # both --execute and --resume).
            _record_consumed_batch(run_root, consumed_entries)
            run_plan(plan)
            postcheck(recovery_document, recovery_sha)
            _append_chained_record(
                journal,
                {
                    "phase": "complete",
                    "authorization_sha256": authorization_sha,
                    "caller": caller,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    render = subcommands.add_parser(
        "render-allowlist",
        help=(
            "cosign-verify platform digest references, then render the "
            "admission allow-list ConfigMap as JSON"
        ),
    )
    render.add_argument(
        "--public-key",
        required=True,
        help="cosign public key used to verify every digest before rendering",
    )
    render.add_argument(
        "--key",
        required=True,
        help=(
            "cosign private key; every ACCEPTED inventory appends a signed "
            "head to the run-root acceptance chain (replay protection)"
        ),
    )
    render.add_argument(
        "--registry-prefix",
        action="append",
        default=[],
        help="allowed image reference prefix, must end with '/'; repeatable",
    )
    render.add_argument(
        "--platform-repository-prefix",
        required=True,
        help="repository prefix whose images require an allow-listed digest",
    )
    render.add_argument(
        "--image",
        action="append",
        default=[],
        help="signed platform image (<registry>/<repo>@sha256:<hex>); repeatable",
    )
    render.add_argument(
        "--run-root",
        required=True,
        type=Path,
        help="private run root holding release-receipts/ for every image",
    )
    render.add_argument(
        "--deploy-principal",
        action="append",
        default=[],
        help="Kubernetes username allowed to write Helm release Secrets; repeatable",
    )
    render.add_argument(
        "--inventory",
        required=True,
        type=Path,
        help=(
            "signed release-inventory JSON (with .sig) enumerating live "
            "workloads, the Helm rollback window, frozen scientific bindings, "
            "and audited non-live drained removals; the allow-list renders "
            "only from it"
        ),
    )
    render.add_argument(
        "--max-inventory-age-hours",
        type=float,
        default=INVENTORY_MAX_AGE_HOURS,
        help=(
            "refuse inventories captured longer ago than this (finite, "
            f"0 < hours <= {INVENTORY_MAX_AGE_HOURS_LIMIT}; nan/inf refused)"
        ),
    )
    render.add_argument(
        "--attestation",
        required=True,
        type=Path,
        help=(
            "ATTESTOR-SIGNED provider attestation (masters/apiserver/etcd "
            "provider-held facts, provider IAM export evidence, and the "
            "off-host anchored-heads snapshot); rendering fails closed "
            "without it"
        ),
    )
    render.add_argument(
        "--attestation-key",
        required=True,
        help=(
            "SEPARATE attestor public key (pinned by the SOURCE constant "
            "ATTESTATION_KEY_SHA256, which the owner-signed scope must "
            "equal); the release pipeline key can never attest its own "
            "boundary, and a release-key holder cannot rotate the attestor"
        ),
    )
    render.add_argument(
        "--scope",
        required=True,
        type=Path,
        help=(
            "committed owner-approved release-scope JSON; rendering fails "
            "closed when it is absent or empty, and the signed inventory, "
            "CLI arguments, and key identity must all equal it exactly"
        ),
    )

    receipt = subcommands.add_parser(
        "receipt",
        help=(
            "bind an image digest to its source commit, durable anchor bundle "
            "and SBOM evidence; signing and allow-listing require this receipt"
        ),
    )
    receipt.add_argument("--image", required=True, help="<registry>/<repo>@sha256:<hex>")
    receipt.add_argument("--run-root", required=True, type=Path)
    receipt.add_argument(
        "--repository",
        required=True,
        type=Path,
        help="Git repository used to prove the image commit is anchored",
    )
    receipt.add_argument(
        "--anchor-tag", required=True, help="release/* or deploy/* anchor tag"
    )
    receipt.add_argument(
        "--sbom",
        type=Path,
        help="SPDX document for images without a BuildKit attestation manifest",
    )
    receipt.add_argument(
        "--key",
        required=True,
        help="cosign private key; the receipt itself is signed so it is tamper-evident",
    )
    receipt.add_argument(
        "--public-key",
        required=True,
        help="cosign public key; the receipt signature is verified before publication",
    )

    sign = subcommands.add_parser(
        "sign",
        help=(
            "cosign-sign digest references with the release key; every "
            "reference must already have a bound release receipt"
        ),
    )
    sign.add_argument("--key", required=True, help="cosign private key path")
    sign.add_argument(
        "--public-key",
        required=True,
        help="cosign public key used to verify each release receipt signature",
    )
    sign.add_argument(
        "--run-root",
        required=True,
        type=Path,
        help="private run root holding release-receipts/ for every reference",
    )
    sign.add_argument("reference", nargs="+", help="<registry>/<repo>@sha256:<hex>")

    guard = subcommands.add_parser(
        "render-guard-params",
        help=(
            "render the SECURITY-owned guard parameter ConfigMap from the "
            "owner-signed scope (applied by the security automation, never "
            "the release identity)"
        ),
    )
    guard.add_argument("--public-key", required=True)
    guard.add_argument("--scope", required=True, type=Path)

    reconcile = subcommands.add_parser(
        "reconcile-boundary",
        help=(
            "security-owned reconciler: audit the live policy objects, "
            "guard parameters, and IAM identity paths against the committed "
            "manifest + owner-signed scope, and emit the exact restore "
            "commands (PLAN ONLY by default; --execute is refused without "
            "FS2_ROLLOUT_AUTHORIZED=1 at the separately authorized window)"
        ),
    )
    reconcile.add_argument("--public-key", required=True)
    reconcile.add_argument("--scope", required=True, type=Path)
    reconcile.add_argument(
        "--run-root",
        required=True,
        type=Path,
        help="private run root holding the chained journals and ledgers",
    )
    reconcile.add_argument(
        "--attestation",
        type=Path,
        help=(
            "attestor-signed provider attestation; REQUIRED for --execute "
            "and --resume (its anchored-heads snapshot is enforced against "
            "the local chains and its cluster pin against the live cluster), "
            "and required to recognize the attested bootstrap masters "
            "binding during the IAM re-audit"
        ),
    )
    reconcile.add_argument(
        "--attestation-key",
        help=(
            "SEPARATE attestor public key (pinned by the SOURCE constant "
            "ATTESTATION_KEY_SHA256); required with --attestation"
        ),
    )
    reconcile.add_argument(
        "--recovery",
        type=Path,
        help=(
            "owner-signed recovery authorization (v2: cluster/UID/RV/prior "
            "fenced, single-use); when given, emit the annotated, "
            "RV-preconditioned validationActions patch it authorizes"
        ),
    )
    reconcile.add_argument(
        "--authorization",
        type=Path,
        help=(
            "owner-signed rollout authorization pinning the exact plan "
            "SHA-256 and cluster; REQUIRED for --execute (an environment "
            "flag alone is caller-set and never authorizes execution)"
        ),
    )
    reconcile.add_argument(
        "--execute",
        action="store_true",
        help=(
            "apply the plan (requires --authorization; the caller's "
            "authenticated identity must be a scope security principal; "
            "refused while any identity-path violation exists)"
        ),
    )
    reconcile.add_argument(
        "--resume",
        action="store_true",
        help=(
            "complete a journaled intent whose authorization was already "
            "consumed but whose execution crashed; no new authorization is "
            "required or accepted"
        ),
    )

    export_heads = subcommands.add_parser(
        "export-anchored-heads",
        help=(
            "print the canonical anchor snapshot (acceptance-chain head + "
            "every ledger/journal checkpoint) for the owner's off-host WORM "
            "store; whole-store deletion becomes detectable via "
            "verify-anchored-heads"
        ),
    )
    export_heads.add_argument("--run-root", required=True, type=Path)

    verify_heads = subcommands.add_parser(
        "verify-anchored-heads",
        help=(
            "fail closed when the local chains have gone BACKWARDS relative "
            "to an off-host anchored snapshot (deletion/truncation detection)"
        ),
    )
    verify_heads.add_argument("--run-root", required=True, type=Path)
    verify_heads.add_argument("--anchored", required=True, type=Path)

    recovery = subcommands.add_parser(
        "verify-recovery",
        help=(
            "verify an owner-signed break-glass recovery authorization and "
            "print the annotation value the guard requires"
        ),
    )
    recovery.add_argument("--public-key", required=True)
    recovery.add_argument("--recovery", required=True, type=Path)

    verify = subcommands.add_parser(
        "verify", help="cosign-verify digest references with the public key"
    )
    verify.add_argument("--public-key", required=True, help="cosign public key path")
    verify.add_argument("reference", nargs="+", help="<registry>/<repo>@sha256:<hex>")

    args = parser.parse_args(argv)
    if args.command == "render-allowlist":
        manifest = verified_allowlist(
            args.public_key,
            args.image,
            args.registry_prefix,
            args.platform_repository_prefix,
            args.run_root,
            args.inventory,
            args.scope,
            args.attestation,
            args.attestation_key,
            args.deploy_principal,
            key_path=args.key,
            max_age_hours=args.max_inventory_age_hours,
        )
        print(json.dumps(manifest, indent=2, sort_keys=True))
    elif args.command == "receipt":
        with _PinnedPublicKey(args.public_key) as pinned:
            written = create_release_receipt(
                args.image,
                args.run_root,
                args.repository,
                args.anchor_tag,
                args.key,
                pinned.path,
                args.sbom,
            )
        print(json.dumps(written, indent=2, sort_keys=True))
    elif args.command == "sign":
        with _PinnedPublicKey(args.public_key) as pinned:
            for reference in args.reference:
                load_bound_receipt(args.run_root, reference, pinned.path)
            run_commands(
                [cosign_sign_command(args.key, ref) for ref in args.reference]
            )
    elif args.command == "render-guard-params":
        with _PinnedPublicKey(args.public_key) as pinned:
            owner_scope = load_owner_scope(args.scope, pinned.path)
        print(
            json.dumps(
                render_guard_params(owner_scope["security_principals"]),
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "reconcile-boundary":
        return reconcile_boundary(
            args.public_key,
            args.scope,
            args.run_root,
            attestation_path=args.attestation,
            attestation_key_path=args.attestation_key,
            recovery_path=args.recovery,
            authorization_path=args.authorization,
            execute=args.execute,
            resume=args.resume,
        )
    elif args.command == "export-anchored-heads":
        print(json.dumps(_anchor_snapshot(args.run_root), indent=2, sort_keys=True))
    elif args.command == "verify-anchored-heads":
        anchored = json.loads(
            _read_evidence_bytes(args.anchored, private=False)
        )
        # Same enforcement as the render/execute paths: required-chain
        # coverage, count monotonicity, head equality, and PREFIX continuity
        # when the local chain has grown past the anchor.
        _assert_anchored_heads(args.run_root, anchored)
        print("anchored-heads verification OK")
    elif args.command == "verify-recovery":
        with _PinnedPublicKey(args.public_key) as pinned:
            document, annotation = load_recovery_authorization(
                args.recovery, pinned.path
            )
        print(
            json.dumps(
                {"recovery": document, "annotation": annotation},
                indent=2,
                sort_keys=True,
            )
        )
    elif args.command == "verify":
        run_commands(
            [cosign_verify_command(args.public_key, ref) for ref in args.reference]
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProvenanceError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error
