#!/usr/bin/env python3
"""Keep an internal-only inference edge reachable through loopback port-forwards."""

from __future__ import annotations

import argparse
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
    configure_local_ports,
    port_forward_command,
    wait_for_port,
)


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
    args = parse_args()
    if not args.context or any(character.isspace() for character in args.context):
        raise ValueError("context must be a non-empty Kubernetes context name")
    if not 5 <= args.ready_timeout_seconds <= 300:
        raise ValueError("ready-timeout-seconds must be from 5 through 300")
    configure_local_ports(
        args.control_plane_local_port,
        args.admin_console_local_port,
        args.operator_proxy_port,
    )
    kubeconfig = args.kubeconfig.resolve(strict=True)
    checked_private_file(kubeconfig, "kubeconfig")
    expected_origin = f"http://localhost:{args.operator_proxy_port}"
    expected_urls = {
        "mcp_endpoint_url": f"{expected_origin}/mcp",
        "admin_web_interface_url": f"{expected_origin}/admin/",
    }
    actual_urls = {
        "mcp_endpoint_url": args.mcp_endpoint_url,
        "admin_web_interface_url": args.admin_web_interface_url,
    }
    if actual_urls != expected_urls or any(
        urlsplit(url).hostname != "localhost" for url in actual_urls.values()
    ):
        raise ValueError("endpoint URLs do not match the configured loopback proxy")

    processes: list[subprocess.Popen[bytes]] = []
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    previous_handlers: dict[int, signal.Handlers] = {}

    def interrupt(_signal_number: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, interrupt)
        for service, port in (
            (CONTROL_SERVICE, args.control_plane_local_port),
            (ADMIN_SERVICE, args.admin_console_local_port),
        ):
            processes.append(
                subprocess.Popen(  # noqa: S603
                    port_forward_command(
                        kubeconfig,
                        args.context,
                        service,
                        port,
                        kubectl=args.kubectl,
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=None,
                    env={**os.environ, "KUBECONFIG": str(kubeconfig)},
                )
            )
        deadline = time.monotonic() + args.ready_timeout_seconds
        wait_for_port(processes, args.control_plane_local_port, deadline)
        wait_for_port(processes, args.admin_console_local_port, deadline)
        server = ThreadingHTTPServer(
            (BIND_ADDRESS, args.operator_proxy_port), SameOriginProxy
        )
        print(
            json.dumps(
                {
                    "status": "serving",
                    **actual_urls,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        while True:
            exited = [process for process in processes if process.poll() is not None]
            if exited:
                raise RuntimeError("kubectl port-forward exited while proxy was serving")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            if server_thread is not None:
                server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        terminate(processes)
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


if __name__ == "__main__":
    main()
