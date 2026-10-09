#!/usr/bin/env python3
"""One two-variant blur debug plus existing negative probes, not final cohorts.

Uses exactly the ordinary-key client's preparation, uploads, MCP submission,
reader validation, replays and stop-on-failure behavior. Run only after the
operator's explicit corrected-release GO, into a new private output directory.
"""

import run_acceptance as driver


async def execute(args, state, token):
    state["scope"] = "targeted_two_variant_blur_and_negative_probes"
    state["final_cohorts_verified"] = False
    driver.client.save(args.output / "acceptance.json", state, token)
    successful = await driver.run_directory(args, state, token, 1, "blur", "mcp")
    await driver.probes(args, state, token, successful)
    state.update(
        outcome="targeted_blur_probes_passed",
        completed_at=driver.client.now(),
        validated_datasets=sum(entry["validated_datasets"] for entry in state["runs"]),
    )


if __name__ == "__main__":
    raise SystemExit(driver.main(executor=execute))
