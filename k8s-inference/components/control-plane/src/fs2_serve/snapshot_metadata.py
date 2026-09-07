"""Reconstruct captured mutable files without changing a shared snapshot."""

from typing import Any

from pydantic import Field

from .models import StrictModel


class SnapshotWorkerLog(StrictModel):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)
    mode: int = Field(ge=0, le=0o777)
    uid: int = Field(ge=0)
    gid: int = Field(ge=0)


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
        f'if cp -a /snapshot-bundle/cache "{target}/cache" && '
        f'cp -a /snapshot-bundle/worker.log "{target}/worker.log"; then '
        f'if printf "%s  %s\\n" "{checksum}" "{target}/worker.log" | sha256sum -c -; then '
        f'chmod "{mode}" "{target}/worker.log" && chown "{uid}:{gid}" "{target}/worker.log"; '
        f'else mv "{target}/worker.log" "{target}/worker.log.invalid"; fi; fi; true; }}'
    )
    command.extend([shadow, metadata.sha256, format(metadata.mode, "04o"), str(metadata.uid), str(metadata.gid)])
    runtime["command"][runtime["command"].index("--source-directory") + 1] = shadow
