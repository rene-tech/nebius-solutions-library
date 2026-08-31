#!/usr/bin/env python3
"""Run the optimized Evo2 backend as a normal service, not a CRIU donor."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from typing import Sequence

from evo2_deep.runtime import (
    Evo2Backend,
    GenerationRequest,
    RuntimeFailure,
    model_path_from_environment,
)
from evo2_deep.server import Evo2HTTPServer, LoadingBackend


def emit_startup_phase(name: str) -> None:
    print(
        json.dumps({"event": "fs2-startup-phase", "name": name}, sort_keys=True),
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    try:
        # Keep the SM103-optimized HCS/HCM/HCL kernels. The upstream server's
        # assert_snapshot_safe() check is deliberately absent: this retained
        # HTTP process is never a CRIU snapshot donor, and libcufile is valid
        # for ordinary model loading and inference.
        def load_and_warm_backend() -> Evo2Backend:
            emit_startup_phase("weight-load-start")
            loaded = Evo2Backend(model_path_from_environment(), use_kernels=True)
            emit_startup_phase("weight-load-end")
            # Readiness must cover first-use Triton/PTXAS compilation as well as
            # weight residency. Two generated tokens exercise prefill and the
            # cached decode path without replacing the canonical acceptance run.
            emit_startup_phase("engine-build-or-compile-start")
            loaded.generate(
                GenerationRequest(
                    sequence="ATCGATCGATCG",
                    num_tokens=2,
                    temperature=0.7,
                    top_k=1,
                    top_p=0.0,
                    random_seed=2407000,
                )
            )
            emit_startup_phase("engine-build-or-compile-end")
            return loaded

        backend = LoadingBackend(load_and_warm_backend)
        server = Evo2HTTPServer((args.host, args.port), backend)
    except (OSError, RuntimeFailure) as exc:
        print(f"fs2-evo2-server: FAIL: {exc}", file=sys.stderr)
        return 2

    loader = threading.Thread(
        target=backend.load, name="evo2-model-loader", daemon=False
    )
    loader.start()
    print("fs2-evo2-server: LISTENING status=loading", flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        loader.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
