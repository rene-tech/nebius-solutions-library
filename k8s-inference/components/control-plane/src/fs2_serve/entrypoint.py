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
MODEL_BOOTSTRAP_RETENTION_COMMAND = "verify-model-bootstrap-retention"


def main() -> None:
    # Scientific helpers and the retained-history verifier are short-lived,
    # narrowly scoped processes. They must not import the API, database, MCP,
    # admin, autoscaler, or tracing services used by the long-lived gateway.
    if len(sys.argv) > 1 and sys.argv[1] == MODEL_BOOTSTRAP_RETENTION_COMMAND:
        from .model_bootstrap_retention import main as retention_main

        retention_main()
    elif len(sys.argv) > 1 and sys.argv[1] in SCIENTIFIC_COMPANION_COMMANDS:
        from .scientific_companion_cli import main as companion_main

        companion_main()
    else:
        from .cli import main as gateway_main

        gateway_main()


if __name__ == "__main__":
    main()
