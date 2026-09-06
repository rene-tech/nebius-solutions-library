#!/usr/bin/env python3
"""Compare exact upstream IGSO3 tables under different CPU thread counts.

Run inside the pinned RFdiffusion image. This CPU-only microbenchmark does not
load model weights or alter another process. Its timings are not end-to-end
inference or GPU utilization measurements.
"""

from __future__ import annotations

import argparse
import json
import time


def main() -> None:
    import numpy as np
    import torch
    from rfdiffusion.igso3 import calculate_igso3

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threads", nargs="+", type=int, default=[1, 4, 16, 64])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--num-sigma", type=int, default=8)
    args = parser.parse_args()
    if min(*args.threads, args.repetitions, args.num_sigma) < 1:
        parser.error("thread counts, repetitions and sigma count must be positive")

    parameters = dict(num_sigma=args.num_sigma, num_omega=1000, min_sigma=0.02, max_sigma=1.5, L=2000)
    print(json.dumps({
        "schema": "fs2-serve.nebius.ai/rfdiffusion-igso3-threads/v1",
        "torch": torch.__version__, "default_threads": torch.get_num_threads(),
        "parameters": parameters,
    }), flush=True)
    reference = None
    for threads in args.threads:
        torch.set_num_threads(threads)
        for repetition in range(args.repetitions):
            started = time.monotonic()
            result = calculate_igso3(**parameters)
            seconds = time.monotonic() - started
            if reference is None:
                reference = result
            for name in reference:
                np.testing.assert_array_equal(result[name], reference[name])
            print(json.dumps({
                "threads": threads, "repetition": repetition,
                "seconds": seconds, "exact_array_match": True,
            }), flush=True)


if __name__ == "__main__":
    main()
