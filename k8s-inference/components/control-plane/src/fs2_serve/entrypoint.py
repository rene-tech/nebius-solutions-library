"""Select a process role before importing the gateway and its dependencies."""

from __future__ import annotations

import sys

SCIENTIFIC_COMPANION_COMMANDS = (
    "scientific-materialize",
    "scientific-materialize-many",
    "scientific-collect",
    "scientific-prepare-workspace",
    "scientific-verify-runtime-artifacts",
)


def main() -> None:
    # Every scientific stage starts several short-lived init containers. Those
    # processes need artifact helpers, but never the API, database, MCP, admin,
    # autoscaler or tracing services imported by the long-lived gateway.
    if len(sys.argv) > 1 and sys.argv[1] in SCIENTIFIC_COMPANION_COMMANDS:
        from .scientific_companion_cli import main as companion_main

        companion_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "gpu-allocation-observer":
        # One small process per GPU node must not load API/MCP/controller
        # schemas merely to publish kubelet allocation annotations.
        from .gpu_allocation_observer_cli import main as observer_main

        observer_main()
    else:
        from .cli import main as gateway_main

        gateway_main()


if __name__ == "__main__":
    main()
