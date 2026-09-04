#!/usr/bin/env python3
"""One filesystem-group ownership contract for fs2 pod templates.

When a pod sets ``fsGroup`` the kubelet takes ownership of each of its volumes.
Under the Kubernetes default ``fsGroupChangePolicy: Always`` that is a recursive
walk rewriting owner and mode on every inode before the first container starts.
It lands between "pod scheduled" and "container started", where no container log
shows it, and it scales with the size of the volume rather than the model.  On
the shared 128 GiB scientific cache a recorded H100 campaign measured 153-305 s
of it per Job, against 1-2 s once the volume root already matched.

``OnRootMismatch`` reduces the kubelet's check to the volume root, but only a
volume whose tree some producer already owns may use it, and "owns" is a much
narrower claim than it first appears.  The kubelet compares the root's group
against the consuming pod's ``fsGroup``.  If the producer wrote the tree under a
different group the root cannot match, so the walk runs anyway and the policy
buys nothing.  Worse, once any pod's walk stamps the root with the consumer's
group, a later pod skips while the producer keeps adding inodes under its own
group, and those files silently become unreadable.

:class:`FilesystemGroupProducer` and :class:`FilesystemGroupConsumer` make that
pairing explicit, and :func:`assert_authority` refuses it unless the producer and
the consumer agree exactly on the group, on the mounted volume root, and on the
producer owning every inode below it.  A read-only consumer is not exempt:
``fsGroup`` ownership is applied per volume, not per mount.

**No caller in this repository satisfies that contract today**, which is the
point of shipping the check first.  The acquisition Job writes as gid 10001
while the serving pod mounts with ``fsGroup`` 1000; the localization Job pins no
``runAsUser`` or ``runAsGroup`` at all, so its writer identity is whatever its
image happens to use.  Rollout is therefore deferred until those identities are
aligned deliberately, and this module changes no rendered manifest.

Everything here is GPU-, region- and model-agnostic, and it never touches
images, argv, resources or placement.  Licensed academic trees are governed
elsewhere and must keep using ``supplementalGroups`` with no ``fsGroup`` at all,
because the kubelet's ownership pass would rewrite their delivered modes to
group-writable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .loader import CatalogError


# The kubelet compares only the volume root under this policy.  Any other value,
# including the Kubernetes default of an absent policy, reintroduces the
# recursive ownership walk this module exists to remove.
FS_GROUP_CHANGE_POLICY = "OnRootMismatch"
RECURSIVE_FS_GROUP_CHANGE_POLICY = "Always"

# pkg/volume/volume_linux.go requires the volume root to carry the pod fsGroup,
# the setgid bit, and at least owner/group rwx before it will skip the walk.
ROOT_MODE_REQUIRED_BY_KUBELET = 0o2770


class VolumeOwnershipError(CatalogError):
    """A pod template would reintroduce the recursive volume ownership walk."""


def _pod_security_context(pod_spec: Mapping[str, Any]) -> Mapping[str, Any]:
    security = pod_spec.get("securityContext")
    return security if isinstance(security, Mapping) else {}


def filesystem_group_security_context(
    *,
    fs_group: int,
    run_as_user: int | None = None,
    run_as_group: int | None = None,
    run_as_non_root: bool = True,
    seccomp_profile: str | None = "RuntimeDefault",
    supplemental_groups_policy: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the only pod securityContext shape allowed to carry an fsGroup."""

    if not isinstance(fs_group, int) or isinstance(fs_group, bool) or fs_group <= 0:
        raise VolumeOwnershipError("pod fsGroup must be a positive non-root gid")
    security: dict[str, Any] = {"runAsNonRoot": run_as_non_root}
    if run_as_user is not None:
        security["runAsUser"] = run_as_user
    if run_as_group is not None:
        security["runAsGroup"] = run_as_group
    security["fsGroup"] = fs_group
    security["fsGroupChangePolicy"] = FS_GROUP_CHANGE_POLICY
    if supplemental_groups_policy is not None:
        security["supplementalGroupsPolicy"] = supplemental_groups_policy
    if seccomp_profile is not None:
        security["seccompProfile"] = {"type": seccomp_profile}
    if extra:
        for key, value in extra.items():
            if key in {"fsGroup", "fsGroupChangePolicy"}:
                raise VolumeOwnershipError(
                    "filesystem group ownership fields are owned by this contract"
                )
            security[key] = value
    return security


def apply_filesystem_group(
    pod_spec: dict[str, Any],
    *,
    fs_group: int,
    run_as_user: int | None = None,
    run_as_group: int | None = None,
    run_as_non_root: bool = True,
    seccomp_profile: str | None = "RuntimeDefault",
    supplemental_groups_policy: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace a pod spec's securityContext with the contracted ownership shape."""

    pod_spec["securityContext"] = filesystem_group_security_context(
        fs_group=fs_group,
        run_as_user=run_as_user,
        run_as_group=run_as_group,
        run_as_non_root=run_as_non_root,
        seccomp_profile=seccomp_profile,
        supplemental_groups_policy=supplemental_groups_policy,
        extra=extra,
    )
    return pod_spec


def _claim(volume: Mapping[str, Any]) -> Mapping[str, Any] | None:
    claim = volume.get("persistentVolumeClaim")
    return claim if isinstance(claim, Mapping) else None


def persistent_mounts(pod_spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every container mount of this pod that is backed by a claim.

    Init containers are included: the kubelet has already taken ownership of the
    volume by the time the first of them runs, so they are part of the same cost.
    """

    volumes = pod_spec.get("volumes")
    claims: dict[str, Mapping[str, Any]] = {}
    if isinstance(volumes, Sequence):
        for volume in volumes:
            if not isinstance(volume, Mapping):
                continue
            claim = _claim(volume)
            name = volume.get("name")
            if claim is not None and isinstance(name, str):
                claims[name] = claim
    found: list[dict[str, Any]] = []
    for key in ("initContainers", "containers"):
        containers = pod_spec.get(key)
        if not isinstance(containers, Sequence):
            continue
        for container in containers:
            if not isinstance(container, Mapping):
                continue
            for mount in container.get("volumeMounts") or ():
                if not isinstance(mount, Mapping):
                    continue
                name = mount.get("name")
                if not isinstance(name, str) or name not in claims:
                    continue
                found.append(
                    {
                        "container": container.get("name"),
                        "volume": name,
                        "claim_name": claims[name].get("claimName"),
                        "claim_read_only": bool(claims[name].get("readOnly", False)),
                        "mount_path": mount.get("mountPath"),
                        "sub_path": mount.get("subPath"),
                        "read_only": bool(mount.get("readOnly", False)),
                    }
                )
    return found


@dataclass(frozen=True)
class FilesystemGroupProducer:
    """Who wrote the tree, and under exactly which identity.

    ``gid`` is the group every inode of the tree carries.  ``root`` is the
    mounted volume root the writer used, without a ``subPath``.  ``whole_tree``
    records whether this producer owns every inode below that root or only part
    of it; a partial producer can never be an authority, because the kubelet's
    skip is all-or-nothing.
    """

    name: str
    gid: int
    root: str
    whole_tree: bool

    def __post_init__(self) -> None:
        if not isinstance(self.gid, int) or isinstance(self.gid, bool) or self.gid <= 0:
            raise VolumeOwnershipError(f"producer {self.name!r} needs a positive non-root gid")
        if not self.root.startswith("/") or self.root == "/":
            raise VolumeOwnershipError(f"producer {self.name!r} needs an absolute volume root")


@dataclass(frozen=True)
class FilesystemGroupConsumer:
    """Who reads or writes the tree afterwards, under which fsGroup and root."""

    name: str
    fs_group: int
    root: str
    read_only: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.fs_group, int)
            or isinstance(self.fs_group, bool)
            or self.fs_group <= 0
        ):
            raise VolumeOwnershipError(f"consumer {self.name!r} needs a positive non-root gid")
        if not self.root.startswith("/") or self.root == "/":
            raise VolumeOwnershipError(f"consumer {self.name!r} needs an absolute volume root")


def authority_violations(
    producer: FilesystemGroupProducer, consumer: FilesystemGroupConsumer
) -> list[str]:
    """Why this producer may not authorise this consumer to skip the walk.

    The kubelet compares the volume root's group against the consuming pod's
    ``fsGroup``.  If they differ, the root mismatches and the walk runs anyway,
    so the policy buys nothing.  Worse, once any pod's walk stamps the root with
    the consumer's group, a later pod skips while the producer keeps adding
    inodes under its own group, and those files become unreadable.  Exact
    agreement on group, on the mounted root, and on the producer owning the
    whole tree is therefore the only safe basis for the fast path.

    A read-only consumer is not exempt.  ``fsGroup`` ownership is applied per
    volume, not per mount, and the consumer still reads the tree through that
    group.
    """

    violations: list[str] = []
    if producer.gid != consumer.fs_group:
        violations.append(
            f"producer {producer.name!r} writes gid {producer.gid} but consumer "
            f"{consumer.name!r} mounts with fsGroup {consumer.fs_group}; the root "
            "cannot match both"
        )
    if producer.root != consumer.root:
        violations.append(
            f"producer {producer.name!r} owns root {producer.root!r} but consumer "
            f"{consumer.name!r} mounts {consumer.root!r}; the kubelet only compares "
            "the mounted volume root"
        )
    if not producer.whole_tree:
        violations.append(
            f"producer {producer.name!r} does not own every inode below its root, so a "
            "skipped walk would leave part of the tree under another group"
        )
    return violations


def assert_authority(
    producer: FilesystemGroupProducer, consumer: FilesystemGroupConsumer
) -> None:
    """Fail closed unless this producer really authorises this consumer."""

    violations = authority_violations(producer, consumer)
    if violations:
        raise VolumeOwnershipError(
            f"{consumer.name!r} may not use {FS_GROUP_CHANGE_POLICY}: "
            + "; ".join(violations)
        )


def effective_writer_identity(
    pod_spec: Mapping[str, Any], container_name: str | None
) -> tuple[int | None, int | None]:
    """The uid/gid a container actually writes as, container overriding pod."""

    pod = _pod_security_context(pod_spec)
    container_security: Mapping[str, Any] = {}
    for key in ("initContainers", "containers"):
        for container in pod_spec.get(key) or ():
            if isinstance(container, Mapping) and container.get("name") == container_name:
                found = container.get("securityContext")
                if isinstance(found, Mapping):
                    container_security = found
    run_as_user = container_security.get("runAsUser", pod.get("runAsUser"))
    run_as_group = container_security.get("runAsGroup", pod.get("runAsGroup"))
    return run_as_user, run_as_group


def validate_pod_volume_ownership(
    pod_spec: Mapping[str, Any],
    *,
    owned_roots: Sequence[str] = (),
    read_only_mount_paths: Sequence[str] = (),
) -> list[str]:
    """Return every way this pod template violates the ownership contract.

    ``owned_roots`` are the persistent mounts whose tree a producer verified by
    :func:`assert_authority` owns under this pod's exact ``fsGroup``.  A
    persistent mount outside that set is rejected: ``OnRootMismatch`` would
    otherwise be free to trust a root that no agreed producer ever owned.

    Read-only mounts are not exempt.  ``fsGroup`` ownership is applied to the
    volume, not to the mount, and a read-only consumer still reads the tree
    through that group, so it needs the same producer agreement a writer does.

    A root must be a whole mounted volume root, because that is the only path the
    kubelet compares.  A descendant of one, or a mount reached through a
    ``subPath``, proves nothing about the root that will actually be checked.
    """

    violations: list[str] = []
    security = _pod_security_context(pod_spec)
    fs_group = security.get("fsGroup")
    policy = security.get("fsGroupChangePolicy")

    if fs_group is None:
        if policy is not None:
            violations.append(
                "fsGroupChangePolicy is set without an fsGroup, so the kubelet ignores it"
            )
    else:
        if not isinstance(fs_group, int) or isinstance(fs_group, bool) or fs_group <= 0:
            violations.append("fsGroup must be a positive non-root gid")
        if policy != FS_GROUP_CHANGE_POLICY:
            violations.append(
                "fsGroup is set without "
                f"fsGroupChangePolicy={FS_GROUP_CHANGE_POLICY}, so every cold start "
                "pays a recursive volume ownership walk"
            )

    mounts = persistent_mounts(pod_spec)
    writable_claims = {
        mount["volume"] for mount in mounts if not mount["read_only"]
    }
    for mount in mounts:
        path = mount["mount_path"]
        if mount["claim_read_only"] and mount["volume"] in writable_claims:
            violations.append(
                f"volume {mount['volume']!r} marks the whole claim readOnly while it "
                "also carries a writable mount; express read-only intent on the mount"
            )
        if isinstance(path, str) and path in read_only_mount_paths and not mount["read_only"]:
            violations.append(
                f"immutable artifact mount {path!r} must set readOnly on the mount"
            )
        if not mount["read_only"]:
            # A writer with no pinned identity cannot be anyone's authority: the
            # group its tree ends up owned by is whatever the image happens to
            # use, so no consumer can ever match it deliberately.
            run_as_user, run_as_group = effective_writer_identity(
                pod_spec, mount["container"]
            )
            missing = [
                field
                for field, value in (("runAsUser", run_as_user), ("runAsGroup", run_as_group))
                if value is None
            ]
            if missing:
                violations.append(
                    f"writable persistent mount {path!r} has no frozen writer identity: "
                    f"the pod pins no {' or '.join(missing)}, so the group its tree ends "
                    "up owned by is undefined"
                )
        if fs_group is None:
            continue
        if mount["sub_path"]:
            violations.append(
                f"persistent mount {path!r} uses a subPath, so the pod never "
                "sees the volume root the kubelet compares and no authority here can "
                "vouch for it"
            )
            continue
        # An owned root must be the mounted volume root itself.  A descendant
        # would leave the real root mismatched, so it proves nothing.
        if not isinstance(path, str) or path not in set(owned_roots):
            violations.append(
                f"persistent mount {path!r} is not a declared adopted volume "
                "root, so OnRootMismatch could skip an unadopted tree"
            )
    return violations


def assert_pod_volume_ownership(
    pod_spec: Mapping[str, Any],
    *,
    subject: str,
    owned_roots: Sequence[str] = (),
    read_only_mount_paths: Sequence[str] = (),
) -> None:
    """Fail closed on any pod template that would reintroduce the walk."""

    violations = validate_pod_volume_ownership(
        pod_spec,
        owned_roots=owned_roots,
        read_only_mount_paths=read_only_mount_paths,
    )
    if violations:
        raise VolumeOwnershipError(f"{subject}: " + "; ".join(violations))


__all__ = [
    "FS_GROUP_CHANGE_POLICY",
    "FilesystemGroupConsumer",
    "FilesystemGroupProducer",
    "ROOT_MODE_REQUIRED_BY_KUBELET",
    "assert_authority",
    "authority_violations",
    "RECURSIVE_FS_GROUP_CHANGE_POLICY",
    "VolumeOwnershipError",
    "apply_filesystem_group",
    "assert_pod_volume_ownership",
    "effective_writer_identity",
    "filesystem_group_security_context",
    "persistent_mounts",
    "validate_pod_volume_ownership",
]
