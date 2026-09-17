#!/usr/bin/env python3
"""Keep an internal-only inference edge reachable through loopback port-forwards."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from internal_edge_acceptance import (
    ADMIN_SERVICE,
    BIND_ADDRESS,
    CONTROL_SERVICE,
    DEFAULT_ADMIN_PORT,
    DEFAULT_CONTROL_PORT,
    DEFAULT_PROXY_PORT,
    SameOriginProxy,
    checked_private_file,
    configure_debug_scope,
    configure_local_ports,
    port_forward_command,
    wait_for_port,
)


def inherited_sealed_kubeconfig(path: Path) -> tuple[Path, int]:
    """Validate the inherited memfd itself without resolving its proc path."""

    match = __import__("re").fullmatch(r"/proc/self/fd/([0-9]+)", str(path))
    if match is None:
        raise ValueError("kubeconfig must be one inherited proc descriptor")
    descriptor = int(match.group(1))
    if descriptor < 3:
        raise ValueError("kubeconfig cannot name a standard descriptor")
    details = os.fstat(descriptor)
    required_seals = (
        fcntl.F_SEAL_SEAL
        | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_GROW
        | fcntl.F_SEAL_WRITE
    )
    if (
        not __import__("stat").S_ISREG(details.st_mode)
        or __import__("stat").S_IMODE(details.st_mode) != 0o600
        or details.st_size < 1
        or details.st_size > 4 * 1024 * 1024
        or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != required_seals
    ):
        raise ValueError("kubeconfig descriptor is not one bounded sealed mode-0600 memfd")
    return path, descriptor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument(
        "--control-plane-local-port", type=int, default=DEFAULT_CONTROL_PORT
    )
    parser.add_argument(
        "--admin-console-local-port", type=int, default=DEFAULT_ADMIN_PORT
    )
    parser.add_argument("--operator-proxy-port", type=int, default=DEFAULT_PROXY_PORT)
    parser.add_argument("--mcp-endpoint-url", required=True)
    parser.add_argument("--admin-web-interface-url", required=True)
    parser.add_argument("--debug-tenant-id")
    parser.add_argument("--debug-model-id")
    parser.add_argument("--debug-app-id")
    parser.add_argument("--ready-timeout-seconds", type=int, default=60)
    return parser.parse_args()


def terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> None:
    raise RuntimeError(
        "retired: operator-owned raw port-forward listeners are forbidden; "
        "inference-stack proxy/debug-proxy must lease the root broker's sole "
        "scope-enforcing listener"
    )


if __name__ == "__main__":
    main()
