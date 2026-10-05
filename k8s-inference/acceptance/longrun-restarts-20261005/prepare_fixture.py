"""Download the finite public-benchmark TPR from an earlier internal QA receipt.

Keeps customer data and credentials out of this acceptance repository. The
original saved checkpoint must identify an exact finite 60,000-step study;
the SHA-256/length validator is the public client's production implementation.
"""

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path

import httpx2


async def run(args):
    values = dict(
        line.split("=", 1)
        for line in args.qa_env.read_text().splitlines()
        if "=" in line
    )
    key = values["SCIENTIFIC_MODELS_API_KEY"]
    if not key.startswith("fs2_pat_56130b22ae09"):
        raise ValueError("Only existing system/qa is permitted")
    checkpoint = json.loads(args.checkpoint.read_text())
    native = [item for item in checkpoint["state"]["commands"] if "mdrun" in item["command"]]
    if not native or native[-1]["checkpoint_step"] != 60000:
        raise ValueError("Use a completed internal 60,000-step native receipt")
    tpr = next(item for item in checkpoint["files"] if item["path"] == "benchmark.tpr")
    spec = importlib.util.spec_from_file_location(
        "scientific_acceptance", args.client_root / "scripts/scientific-batch-acceptance.py"
    )
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    async with httpx2.AsyncClient(
        base_url=args.origin,
        headers={"authorization": "Bearer " + key, "origin": args.origin},
        timeout=120,
        trust_env=False,
    ) as http:
        receipt = await helper.download(http, tpr["artifact"], args.output)
    print(json.dumps({"fixture": str(args.output), "sha256": tpr["sha256"], "download": receipt}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qa-env", "checkpoint", "client-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
