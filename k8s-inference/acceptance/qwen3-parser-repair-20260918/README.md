# Qwen3-8B parser-only candidate — September 18

This is **isolated H100 direct-runtime qualification**, not a production
selection or public HTTP/MCP/customer-ready verdict. The full additional
semantic matrix is not clean: four thinking/JSON-object cases fail below.

## Proven failure and bounded repair

The public `general-r1` cohort has 60 Qwen requests: all 20 forced-function calls
failed upstream HTTP 400; all 20 plain chat requests failed strict JSON parsing;
all 20 `response_format=json_object` requests passed. Retained upstream exchange
`d4df1825-9074-41c6-b3df-13e0135b5c69` for operation
`cde7e5b3-780d-4587-afe3-f74baee1e664` says the named function requires a tool
parser. Plain chat returned an empty `<think>` block followed by the answer.
No input or token budget change is required to repair those parser failures.

The candidate retains Qwen revision
`b968826d9c46dd6066d109eabc6255188de91218` and exact vLLM 0.28.0 image
`sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635`.
It adds only:

```text
--enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3
```

The [official Qwen deployment guide](https://qwen.readthedocs.io/en/latest/deployment/vllm.html)
documents these Qwen3 parsers. The pinned
[vLLM 0.28.0 reasoning contract](https://docs.vllm.ai/en/v0.28.0/features/reasoning_outputs/)
supports Qwen3 reasoning with tool/structured output; the
[tool-calling contract](https://docs.vllm.ai/en/v0.28.0/features/tool_calling/)
distinguishes named, automatic, required and disabled tool choices.
Actual candidate responses use the `reasoning` field; the checker preserves it,
does not strip tags from `content`, and validates visible answers/tool arguments
against independently prepared reference values.

Context stays 32,768, BF16/TP1 and memory utilization stay unchanged; no user
completion budget, thinking default, quota, resources or weights change.

## Snapshot selection is separate from the Off performance target

The stored `qwen3-8b.legacy-v1` bundle is plain vLLM. Its live owner spec had
`fastStart.level=Off` but `cache.snapshotPreference=Prefer` plus the historical
qwen-r12 snapshot reference. The controller therefore injected a restore
wrapper. Logs show failed CRIU attempts and fallback; this does **not** establish
how much of the previously observed approximately 297 seconds was restore work.

The proposal adds the parser template and explicitly sets snapshot preference
to `Never`, removing that old reference. The existing image, weights, cache tier,
replica settings and old immutable template/receipt remain unchanged. The new
process must not inherit qualification for a snapshot of a parser-less process.
An offline actual-controller render accepted the new spec and proved the
generated runtime has parsers and no checkpoint wrapper or snapshot volumes.

## Measured isolated results

The one task-owned Pod ran on an already-existing preemptible H100 80GB HBM3,
driver 580.159.04, GPU `GPU-d90ed78c-e42d-2698-6fcc-23628551186c`. Its shared
weight PVC was read-only and compilation used its private ephemeral volume.
Pod UID `87e36c99-ccde-48d8-b862-dab80e929104` was created 19:59:06 UTC,
application started 19:59:07, and Ready was observed 20:01:06: **120 seconds**
creation-to-Ready. vLLM reported 6.45 seconds reading weights and 7.815 seconds
model loading. This is not a new-node, download, snapshot or public activation
measurement. No WARN/ERROR lines were observed in retained runtime logs.

Two repetitions between 20:02:26 and 20:03:42 ran:

| Scope | Outcome |
| --- | --- |
| Original 60 requests, unchanged, independently prepared references | 120/120 pass |
| Extra named/auto/required/none, JSON object/schema, nonstream and SSE | 24/24 pass |
| Extra arithmetic `/think` with JSON-object output, nonstream and SSE | 0/4 semantic pass |

The four failures contain separately correct reasoning but an empty visible
JSON object. They are retained as failures, not converted into success because
HTTP was 200 or JSON syntax was valid. Five additional same-budget diagnostic
requests showed plain thinking and strict required-property JSON schema return
the correct arithmetic answer; thinking automatic/forced tools also returned
correct structured arguments. Explicitly disabling thinking for that arithmetic
question returned a wrong numeric answer. No budget/default/model adjustment
was made to conceal these results. This repair fixes the measured parser
configuration defects; it does not guarantee task correctness or general
thinking-plus-JSON-object quality.

## Reproducibility and handoff

`parser_candidate.py` transforms a retained **plain** bundle, refuses an already
wrapped/parser-modified baseline, computes the established template digest, and
can render an isolated Pod without applying it. `qualify.py` replays all 60
Qwen cases from the campaign manifest plus 14 independent cases twice. It exits
nonzero if any semantic case fails, including the retained four failures.

```sh
python qualify.py --base-url http://127.0.0.1:PORT \
  --cases PRIVATE_GENERAL_CASES_JSON --output PRIVATE_NEW_DIRECTORY
pytest -q k8s-inference/acceptance/qwen3-parser-repair-20260918
```

Private evidence:
`/home/tux/secure-handoff/qwen-parser-qualification-20260918`.
The manifest is `qualification-general-v1/cases.json` under the protected
LibreChat handoff, byte SHA256
`623f7f8e471f331453d08ce0a5220a90bf540b732907cad9a0acadd82b916e53`;
the campaign's canonical JSON SHA256 is
`7f02224e79d5cf0f32c4078d0d5e6697f0f2bfb1b79d90a0780dfce52420892b`.
All 60 replay bodies were also compared to the actual retained public requests:
only gateway wait/idempotency controls are removed and the served model ID added.

- Matrix summary SHA256: `0932f4c475b929fe7647de04f51e9517b7976a65076cd54b324c1d5975454a78`.
- Qualification receipt: `e21cfdeb55ee35b873ce55c51fff193eaa6c57939dd28bf80fd8b89a92f050b3`.
- Offline render receipt: `567f5d98c6549ee09381bfa041b2ed69a1c08531c74c7706ffe9f25c20f6a2b2`.
- Cleanup receipt: `446dcd4c4e811b1e590d047d42da33aa10e6a6660dcdcd6758a5d7a0dd8ade6b`.

The Pod was deleted and absence verified; its port-forward exited. No shared
resource or customer record changed. Template candidate
`qwen3-8b.parsers-20260918`, digest
`sha256:6d628e0bcebea09053bdf8ed4cd863b120210baa761b7095e8e7a6452bf8e2a5`,
must be added to the **fresh** complete release contract, including the admin
bootstrap configuration baseline, without dropping other model promotions or
five speech models. The main release owner handles publication, drain/owner
proposal and exact public-client retest. Pre-155 offline captures are regression
inputs, not a deployable successor release.
