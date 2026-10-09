# Cosmos3-Nano customer workflow acceptance

Current release verdict: **not ready**. The suite exists, but the LeRobot App
is still a candidate and no unchanged-release public cohorts have run. Earlier
text-to-video evidence does not qualify video-to-video or LeRobot augmentation.

This directory is the release-blocking acceptance path for Timothy's exact
requests:

- HTTPS MP4 input to a validated, materially changed MP4 output;
- client-local MP4 upload to the same typed video-to-video workflow;
- a tiny LeRobot v3 dataset plus a lighting configuration to a dataset that is
  reopened and iterated with `lerobot==0.6.1`.

`run_acceptance.py` treats `cosmos3-nano` and
`cosmos3-lerobot-augmentation` as independent Apps and uses the public HTTPS
gateway, typed MCP tools, artifact
data plane, robotics-tenant policy and admin observability surfaces. It runs
two cohorts, submits an actual concurrent pair, checks idempotent replay,
cancellation, a pre-admission invalid local path, queue observation, hot and
scale-from-zero states, output checksums, decoded MP4 frames, LeRobot
frame/action/timestamp alignment and per-run admin correlation.

Before submission, the runner joins the attested deployment receipt to the
live admin configuration digest, Cosmos model/runtime-image identity, and
LeRobot source/model/runtime-image identity and qualified execution join. A
stale receipt or a candidate/null image therefore cannot produce evidence.

The runner does not create or silently broaden a customer key. Give it a
short-lived, Cosmos-only canary key with `max_concurrency >= 2`, tenant
`robotics`, and exactly these App grants:

- `cosmos3-nano`
- `cosmos3-lerobot-augmentation`

Its exact scope set must be `catalog.read`, `mcp.invoke`, `inference.invoke`,
`operations.read`, `operations.result`, `operations.cancel` and
`artifacts.write`. The release identity binds the complete effective policy,
including concurrency, request/GPU budgets, rate limit, expiry and active state;
changing any of them invalidates the receipt.

The key JSON and admin access JSON must be owner-only mode `0600`. Output must
be a new private directory outside the repository. The runner never prints or
retains a credential.

## Prepare fixtures

```bash
python3 acceptance/cosmos3-customer-20260915/build_mp4_fixture.py \
  --output /private/cosmos-acceptance/input.mp4 \
  --receipt /private/cosmos-acceptance/input-mp4.json

uv run --project models/general-media/lerobot-augmentation/runtime \
  python models/general-media/lerobot-augmentation/fixtures/build_tiny_v3.py \
  --output /private/cosmos-acceptance/tiny-v3
```

Publish the exact generated MP4 at a stable HTTPS URL reachable from both the
customer client and model worker. Do not substitute an unverified third-party
fixture. The LeRobot fixture builder creates `tiny-v3.tar.zst` beside the
dataset directory.

Generate a release identity only after observing the exact deployed source,
digest-pinned images, configuration, model revision, client build, canary key
policy and public tool list. `deployment-receipt.example.json` documents the
required identity. Use the exact immutable revision reported by both deployed
Cosmos Apps, without adding a repository-name prefix. `build_manifest.py`
converts that identity into the complete Cosmos capability manifest.

## Run

Run from the `k8s-inference` directory. The LeRobot runtime lock owns the pinned
`lerobot==0.6.1` reader and PyAV dependency; add the local control-plane package
to supply its pinned MCP/HTTP client. The operator host must also provide
`ffmpeg` and `ffprobe`, used to build and independently decode the MP4 fixture.

```bash
uv run --project models/general-media/lerobot-augmentation/runtime \
  --with-editable components/control-plane \
  python \
  acceptance/cosmos3-customer-20260915/run_acceptance.py \
  --release-identity /private/cosmos-acceptance/release-identity.json \
  --deployment-receipt /private/cosmos-acceptance/deployment.json \
  --key-file /private/cosmos-acceptance/canary-key.json \
  --admin-access /private/cosmos-acceptance/admin-access.json \
  --mp4-file /private/cosmos-acceptance/input.mp4 \
  --mp4-url https://fixtures.example/cosmos/input.mp4 \
  --lerobot-source /private/cosmos-acceptance/tiny-v3 \
  --lerobot-bundle /private/cosmos-acceptance/tiny-v3.tar.zst \
  --lerobot-request models/general-media/lerobot-augmentation/fixtures/scientific-run-request.json \
  --librechat-receipt /private/cosmos-acceptance/librechat.json \
  --output /private/cosmos-acceptance/cohort-result \
  --execute
```

Raw MCP-SDK execution is useful diagnosis, but without a matching
`--librechat-receipt` the capability evidence records `client_path=mcp-sdk` and
the gate returns `partial`. A LibreChat receipt must name the same operation
IDs with their exact scenario IDs (including the cancellation), exact public
endpoint and exact client build digest; missing, extra, duplicate, or
misattributed operations fail. This prevents a direct server test from being
mislabeled as the customer integration.

The receipt, per-App manifests, per-scenario evidence, verdict index and human
`VERDICT.md` are written only when the relevant stages exist. The broad
`cosmos3-nano` App verdict may remain `not-ready` after Timothy's two requested
cross-App capabilities qualify because its other advertised Cosmos modes need
their own evidence; that is intentional. Forward- and inverse-dynamics remain
inventoried but unadvertised after the H100 runtime failures, and no LeRobot
action regeneration is claimed: the augmentation App preserves source actions.
