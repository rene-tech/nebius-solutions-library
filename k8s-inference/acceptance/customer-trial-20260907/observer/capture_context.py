#!/usr/bin/env python3
"""Save private, read-only trial context without printing credentials or bodies."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    access = json.loads(args.credential_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    result = {"captured_at": datetime.now(UTC).isoformat(), "responses": {}}
    with httpx.Client(
        base_url=origin, headers={"origin": origin}, timeout=30
    ) as client:
        client.post(
            "/admin/api/v1/session",
            headers={
                "authorization": "Bearer "
                + access["credentials"]["admin_bootstrap_token"]
            },
        ).raise_for_status()
        try:
            for name in (
                "scientific-model-policies",
                "model-inventory",
                "observability",
            ):
                reply = client.get("/admin/api/v1/" + name)
                result["responses"][name] = {
                    "status": reply.status_code,
                    "data": reply.json(),
                }
        finally:
            client.delete("/admin/api/v1/session")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(
        json.dumps(
            {
                "captured_at": result["captured_at"],
                "statuses": {
                    key: value["status"] for key, value in result["responses"].items()
                },
            }
        )
    )


if __name__ == "__main__":
    main()
