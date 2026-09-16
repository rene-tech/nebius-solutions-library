#!/usr/bin/env python3
"""Post-render the pinned Nebius chart for a distinct retained CSI driver."""

from __future__ import annotations

import sys


SOCKET_SOURCE = "/var/lib/kubelet/plugins/csi-mounted-fs-path"
SOCKET_TARGET = "/var/lib/kubelet/plugins/fs2-reference-data-retained"
STORAGE_CLASS_SOURCE = (
    "kind: StorageClass\n"
    "metadata:\n"
    "  name: fs2-reference-data-retained-sc\n"
    "provisioner: reference-data.mounted-fs-path.csi.nebius.ai\n"
    "volumeBindingMode: WaitForFirstConsumer\n"
)
STORAGE_CLASS_TARGET = STORAGE_CLASS_SOURCE.replace(
    "volumeBindingMode: WaitForFirstConsumer\n",
    "reclaimPolicy: Retain\nvolumeBindingMode: WaitForFirstConsumer\n",
)


def main() -> int:
    rendered = sys.stdin.read()
    if rendered.count(SOCKET_SOURCE) != 2:
        raise SystemExit("pinned CSI chart socket layout changed")
    if rendered.count(STORAGE_CLASS_SOURCE) != 1:
        raise SystemExit("pinned CSI chart StorageClass layout changed")
    rendered = rendered.replace(SOCKET_SOURCE, SOCKET_TARGET)
    rendered = rendered.replace(STORAGE_CLASS_SOURCE, STORAGE_CLASS_TARGET)
    if rendered.count("reclaimPolicy: Retain") != 1:
        raise SystemExit("retained reclaim policy was not rendered exactly once")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
