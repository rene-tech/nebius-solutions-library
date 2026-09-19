# NVIDIA Physical AI video augmentation — integration candidate

Status: implemented locally; **not activated, not GPU end-to-end qualified, and not ready for customer use**. The candidate profile is deliberately unrouted. Local tests and live caption-provider checks are not Cosmos generation evidence.

## Workflow

Scientific AI chat upload → immutable MP4 artifacts → NVIDIA PAIDF caption and prompt generation → full-sequence Cosmos edge transfer → NVIDIA motion/weather checks → authenticated before/after playback → human recipe approval → frozen bucket-prefix batch → separate outputs and per-clip report.

The upstream pipeline is [NVIDIA/paidf-augmentation](https://github.com/NVIDIA/paidf-augmentation/tree/bc5719362492a1e3b40bd7d33b43c46dd89efad5), pinned at `bc5719362492a1e3b40bd7d33b43c46dd89efad5`. Its actual pipeline runner, configuration parser, prompt synthesis, retry loop, hallucination/motion evaluator and attribute verifier are used. This is not a replacement implementation labelled as the blueprint.

Platform-specific adaptations are in `runtime/fs2_video/`:

- Five sampled frames, bounded to 768 pixels, are sent to the configured caption VLM; the resulting caption and requested weather drive PAIDF's LLM prompt generator.
- The generation adapter delegates to the platform's pinned `cosmos3-nano` App, using the **complete recorded video as edge control**. It does not use first-frame continuation. The existing tenant-attributed, attempt-scoped child transport is reused.
- The input must retain its dimensions, frame count and integer FPS. Audio is optionally copied from the source with FFmpeg; it is not regenerated.
- PAIDF's motion check examines all frames; its weather verifier samples five frames. Both checks must pass for an accepted clip. Rejected clips remain reviewable and are never silently called successful.
- The immutable recipe includes full defaults, NVIDIA/Cosmos/runtime revisions, adapter source hash, provider model IDs and endpoint, evaluator settings and prompt-template version. Changing these invalidates a prior approval.

The generation backend is **Cosmos3-Nano transfer**, not Cosmos Transfer 2.5. No claim of equivalence, physically valid simulation, label preservation, or training suitability is made. Human review is still required. OSMO is not installed: the existing Scientific AI scheduler owns durable operations, CPU coordination, GPU child jobs and artifacts.

## Input and batch limits

| Property | Implemented bound |
| --- | --- |
| Container | MP4, one video stream |
| Geometry | 640×480 or 1280×720 |
| Frames | 16–400, all decoded during preflight |
| Frame rate | Constant integer 1–30 FPS |
| Audio | At most one AAC, MP3 or ALAC stream; preserve or drop |
| Size | 128 MiB per input/output clip; 2 GiB total input/output video budget |
| Batch | At most 64 clips, processed serially; no silent truncation |
| Weather | Overcast/cloudy, clear, rain |
| Workspace | The workbench's authorized mounted bucket only |

No implicit resizing, trimming, variable-frame-rate conversion, long-video chunk/stitch, arbitrary bucket credentials, or arbitrary remote URLs are accepted. A larger dataset must be partitioned explicitly. The output budget conservatively reserves a full clip allowance before starting another item. Source audio offset/synchronization across unusual MP4 edit lists needs live qualification beyond the tested constant-rate AAC fixture.

## API and runtime

App ID: `physical-ai-video-augmentation`. Operation: `augment-videos`. Typed MCP tool: `submit_physical_ai_video_augmentation`.

The normal scientific manifest contains one `source-video/v1`, `video/mp4`, uncompressed entry per `parameters.items` member. The manifest name equals `video-NNNN`; content hashes must match. See `catalog/runtime/schema/video-augmentation-request.schema.json` for the closed parameter schema. Multi-video requests require an approved recipe hash, and the runtime verifies that hash against its exact configuration.

The workbench's approve/confirm buttons are human workflow gates, **not a new cryptographic authorization system**. Platform keys/grants remain the access boundary. Direct API clients are responsible for their review process.

Required operator configuration for the CPU stage:

| Setting | Purpose |
| --- | --- |
| `PAIDF_PROVIDER_URL` | HTTPS OpenAI-compatible provider base, e.g. `https://api.tokenfactory.nebius.com/v1` |
| `PAIDF_VLM_MODEL` | Tested provider model: `MiniMaxAI/MiniMax-M3` |
| `PAIDF_LLM_MODEL` | Tested provider model: `Qwen/Qwen3-235B-A22B-Instruct-2507` |
| Secret `fs2-video-augmentation-provider`, key `api-key` | Injected as `PAIDF_PROVIDER_API_KEY`; never a user recipe field |
| Platform child capability/internal URL | Injected by the control plane for the exact allowlisted parent/stage contract |

Caption frames and verification frames go to the operator-configured provider. That provider must be approved for the customer's data. Calls incur additional provider usage. Token counts are retained in per-clip reports, but **provider charges are not yet integrated into the tenant billing ledger**. Cosmos child usage retains existing platform attribution.

The CPU coordinator requests 2 vCPU/16 GiB RAM, with 4 vCPU/24 GiB limits and 32 GiB ephemeral workspace. GPU generation uses the existing Cosmos App and its exact qualified capacity configuration; this candidate has not established new GPU compatibility or throughput. A batch can outlast its stage deadline and must not be priced from fixture timings.

Build from `k8s-inference`:

```sh
docker build -f models/general-media/video-augmentation/runtime/Containerfile \
  -t fs2-video-augmentation:candidate .
```

The image pins the Python base, PAIDF commit and upstream `uv.lock`; it contains FFmpeg and the existing delegated Cosmos transport. The worker runs as UID/GID 10001. `build_contracts.py` regenerates the two schema copies and the **unrouted** profile projection. It does not deploy or publish anything.

## Verification retained

`tests/test_runtime.py` decodes/remuxes real MP4 fixtures and exercises the actual PAIDF runner with explicitly fake providers/generation. The control-plane tests check manifest identities, the real Cosmos API/adapter contract, scoped delegation, publication and complete alignment evidence. Workbench tests cover frozen inventories, approval, immutable upload replay, reconnects and cancellation races. Fixture runs are labelled and are not model-quality evidence.

`acceptance/caption-provider.json` and `acceptance/caption-verification-provider.json` retain actual live Token Factory caption/prompt and weather-verification probes. The unchanged sunny NVIDIA sample was rejected as overcast and accepted as clear. These are provider capability probes, predate the final adapter source identity, and **do not show a transformed video**.

See `acceptance/local-validation.json` for exact local builds and test scope. No image has been pushed, no GPU preview or approved GPU batch has run, and no public release qualification is attached.

## Safe activation and remaining acceptance

The shared Stockholm backend advanced after this branch was created. **Do not deploy this branch's old platform base over the current release.** Integrate the additive changes into the current release first, resolving overlapping execution/delegation changes without removing others' work.

1. Publish immutable worker and integrated control-plane/workbench images. Record registry digests, not local Docker image IDs as registry receipts.
2. Complete onboarding: additive workload profile and execution-map row for `augment-videos/main`, `paidf-video-v1`; exact image/execution identity; provider environment/Secret; appropriate CPU service account and artifact workspace. Keep existing qualification identities unchanged. Add public-site/catalog metadata before any customer publication.
3. Create an isolated ordinary-user canary and key with only required grants for this App and `cosmos3-nano`; use a dedicated workbench and test bucket/prefix. Do not reuse another release owner's canary or run two bucket workers for one owner.
4. Run a real short driving clip through **the chat client**: upload, ask for cloudy weather, inspect every output frame, confirm geometry/FPS/frame count/audio, and review weather/motion scores. Then approve the exact recipe and run two separate videos through bucket-prefix planning and explicit batch confirmation.
5. Test failed/rejected clips, source changes after snapshot, cold/warm activation, cancellation during generation, reconnect/restart, artifact expiry/re-download, recipe version changes, wrong-tenant access and byte budgets on the real bucket mount. Retain operation IDs, input/output hashes, usage, timings and visual judgments without keys or signed URLs.
6. Run two unchanged clean customer-client cohorts before release. Do not inherit another model's GPU, scheduler or workbench qualification.

Until those steps pass, keep `route_exposed=false`, MCP `invocable=false`, and the state `candidate-unqualified`.
