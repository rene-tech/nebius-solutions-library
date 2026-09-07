"""Reconstruct captured mutable files without changing a shared snapshot."""

import shlex
from typing import Any

from pydantic import Field

from .models import StrictModel


class SnapshotWorkerLog(StrictModel):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)
    mode: int = Field(ge=0, le=0o777)
    uid: int = Field(ge=0)
    gid: int = Field(ge=0)


def snapshot_copy_command(source: str, destination: str, entries: tuple[str, ...] = (".",)) -> str:
    """Copy ordinary captured metadata without querying unsupported shared-FS ACLs.

    Source/destination are renderer-owned shell expressions (including positional
    arguments), not user strings. A private archive preserves bytes, links, numeric
    ownership, modes and nanosecond timestamps. It never copies GPU pages unless
    explicitly requested, and neither changes nor recursively chmods the source.
    """
    selected = " ".join(shlex.quote(entry) for entry in entries)
    return (
        '{ fs2_copy_archive=$(mktemp /tmp/fs2-snapshot-copy.XXXXXX) && '
        f'tar --format=pax --no-acls --no-xattrs --numeric-owner -C "{source}" '
        f'-cf "$fs2_copy_archive" {selected} && '
        f'tar --no-acls --no-xattrs --numeric-owner --same-owner --same-permissions -C "{destination}" '
        '-xf "$fs2_copy_archive"; fs2_copy_status=$?; '
        'rm -f "$fs2_copy_archive"; (exit "$fs2_copy_status"); }'
    )


def snapshot_cache_copy_command() -> str:
    return (
        'for part in runtime-cache tmp; do if [ -d "/snapshot-bundle/$part" ]; then '
        + snapshot_copy_command("/snapshot-bundle/$part", "$1/$part")
        + ' || exit $?; fi; done'
    )


def preserve_shared_snapshot_metadata(spec: dict[str, Any]) -> None:
    """Keep group access but avoid kubelet recursively rewriting captured modes."""
    security = spec.setdefault("securityContext", {})
    group = security.pop("fsGroup", None)
    security.pop("fsGroupChangePolicy", None)
    if group is not None:
        security["supplementalGroups"] = sorted({*security.get("supplementalGroups", []), group})


def prepare_worker_log_shadow(
    initializer: dict[str, Any], runtime: dict[str, Any], directory: str, metadata: SnapshotWorkerLog | None
) -> None:
    """Normalize only a verified per-Pod copy, using captured manifest metadata.

    Frozen supervisors copy cache/log from this shadow directory. Large CRIU
    images and serving backing files remain read-only links into the bundle.
    Missing/changed bytes leave preparation unavailable so the existing runtime
    Prefer/Require fallback decides the outcome, not a failed init container.
    """
    if metadata is None:
        return
    shadow = directory + "/restore-source"
    command = initializer["command"]
    offset = len(command) - 4  # Shell arguments follow sh, -c, script, argv[0].
    target, checksum, mode, uid, gid = ("${" + str(offset + index) + "}" for index in range(1, 6))
    command[2] += (
        f' && {{ mkdir -p "{target}"; '
        f'ln -s /snapshot-bundle/images "{target}/images"; '
        f'ln -s /snapshot-bundle/filesystem "{target}/filesystem"; '
        f'if {snapshot_copy_command("/snapshot-bundle", target, ("cache", "worker.log"))}; then '
        f'if printf "%s  %s\\n" "{checksum}" "{target}/worker.log" | sha256sum -c -; then '
        f'chmod "{mode}" "{target}/worker.log" && chown "{uid}:{gid}" "{target}/worker.log"; '
        f'else mv "{target}/worker.log" "{target}/worker.log.invalid"; fi; fi; true; }}'
    )
    command.extend([shadow, metadata.sha256, format(metadata.mode, "04o"), str(metadata.uid), str(metadata.gid)])
    runtime["command"][runtime["command"].index("--source-directory") + 1] = shadow
