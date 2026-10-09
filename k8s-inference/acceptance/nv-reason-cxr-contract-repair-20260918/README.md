# NV-Reason-CXR opt-in structured research output — September 18

This is a **revised caller request contract**, not a model/runtime repair or
clinical qualification. Original `general-r1` requests and failures stay intact.
No precision, weights, context, generation budget, snapshot, resources, shared
schema or deployment is changed.

## Retained failure and minimal candidate

The original 20 public NIH-mirror research image requests used
`response_format={"type":"json_object"}`. Six generated valid JSON but labels
outside the exact requested vocabulary: leading spaces, capitalization changes,
`Pleural_Thinking`, or findings from a different label set. The seventh, row22,
ended with `finish_reason=length`, 4,096 completion tokens and an unterminated
23,316-character response. These remain failures; no whitespace/case/label
normalization, JSON salvage, answer substitution or evaluator relaxation occurs.

The candidate changes **only `response_format`** in the inference arguments:

- Exact enum spellings for the same 14 abnormality labels, or `No Finding` alone.
- Required `findings` and `limitations`, with no additional properties.
- At most 14 abnormality entries; limitations contain 1–512 characters.

The original image bytes, image detail, prompts, temperature=0,
max_completion_tokens=4096 and scientific evaluator remain unchanged. Reference
labels never enter the schema or model inputs. The new case-ID suffix and new
cohort identity prevent overwriting or replaying an original submission.

`json_object` constrains syntax, not a domain-specific enum or brevity. The
[pinned vLLM 0.28 structured-output documentation](https://docs.vllm.ai/en/v0.28.0/features/structured_outputs/)
supports caller-supplied `response_format.json_schema`. The
[pinned backend implementation](https://github.com/vllm-project/vllm/blob/v0.28.0/vllm/v1/structured_output/backend_xgrammar.py)
compiles JSON-object mode using an unrestricted object schema. This recipe avoids
`uniqueItems`, which that backend lists as unsupported; duplicates are not a
claimed property of the candidate. A bounded schema is not a guarantee that
every completion finishes within its budget; truncation remains a failed test.

The exact [NVIDIA model card at revision 056bd038](https://huggingface.co/nvidia/NV-Reason-CXR-3B/blob/056bd0383b35226554da9dc5866e095df174ae19/README.md)
describes a Qwen2.5-VL-based research model with XML-like reasoning and answer
sections. This recipe deliberately requests structured label output instead;
it does not install the Qwen3 reasoning parser, remove generated reasoning, or
claim equivalence to the model's default reasoning workflow.

## Discovery and skill guidance

The existing `get_model_schema` openai-chat contract already advertises
`response_format` with `type=json_schema`, named schema, `schema`, and `strict`;
the recipe passes the actual source-projected schema unchanged. The public
discovery response is retained privately. No backend API schema change is needed.
The shared scientific-gateway skill correctly requires current discovery but
does not provide a CXR-specific bounded-output recipe.

Suggested opt-in client guidance: when the research workflow requires an exact
label vocabulary, supply that vocabulary in `response_format.json_schema`, not
only in prose, and validate the complete result. Use the returned named tool and
artifact-input schema. Do not force this response format for free-text reasoning,
change a caller's schema silently, or interpret schema validity as correct image
interpretation.

## Reproduce without model changes

```sh
python request_contract.py --manifest PRIVATE_ORIGINAL_CASES_JSON \
  --output PRIVATE_NEW_MANIFEST_JSON
pytest -q k8s-inference/acceptance/nv-reason-cxr-contract-repair-20260918
```

`request_contract.py` only prepares a separate 20-case manifest. The existing
public campaign runner handles ordinary-key discovery, immutable image upload,
typed MCP, durable polling, result/artifact retrieval and the original evaluator.
Run it only after the release owner allocates a campaign identity. Keep two
repetitions separate, and validate the returned schema in addition to the
unchanged evaluator. No new answer is inferred from an old failed response.

Local validation: 32 tests and Ruff pass. The request helper and recipe are
frozen in commit `feb22181a1892cb7bcdba16a798888d099a614a8`.
Prepared manifest SHA256 is
`7d1111392841229450e711f58c3ebb1a8b94aa1aaa1771fd1360dacd8c84e5bc`.
Original evidence is under the protected `general-r1` campaign; revised evidence
is under `cxr-json-schema-r1` and `cxr-json-schema-r2`.
The original failed operation IDs are:

- `df993893-ae40-4a73-a0fc-c7042f49ef70` (row15)
- `50d35aea-2d19-4801-867b-d0666d248343` (row19)
- `ee5aa644-6a6c-4d39-a5a2-5a50914c9675` (row2)
- `6431310d-ee50-470a-a9b1-bbf1edaf0602` (row21)
- `53399445-5fb5-49ec-b6ce-f6d51457d0a8` (row22, truncated)
- `f4a53332-d543-44d9-b53c-19c3baf9917b` (row31)
- `375263f7-8cdb-454e-ad6e-3de5c467ef70` (row6)

## Observed public request-contract results

The ordinary campaign identity `scientist-08` completed both sequential public
typed-MCP repetitions: **40/40 succeeded on attempt 1**, 40/40 passed the original
evaluator, and 40/40 independently passed the exact bounded schema with
`finish_reason=stop`. There were 20 distinct images, not 40 independent samples.
The actual submitted image artifact hashes/sizes, prompts, temperature and
completion budget were compared with the originals; only `response_format`
changed in model inputs. Original request/result/evaluation hashes were rechecked
and remain unchanged.

| Revised request cohort | Complete schema-valid outputs | Completion tokens | Exact weak-label matches |
| --- | --- | --- | --- |
| r1 | 20/20 | 17–120 | 7/20 |
| r2 | 20/20 | 21–120 | 7/20 |

The CXR ModelDeployment specification remained identical (generation 10), with
model revision `056bd0383b35226554da9dc5866e095df174ae19`, BF16, context 8192,
and runtime image
`sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635`.
The control-plane deployment changed during the wider campaign, so this does
**not** satisfy the frozen whole-release clean-cohort gate. No cold-start,
scale-out, concurrency, LibreChat or default free-text guarantee is inferred.
The allocated key was returned to the release owner after all work terminated;
no key, model or cluster setting was changed by this lane.

Private receipt:
`/home/tux/secure-handoff/scientific-qualification-20260918/cxr-contract-repair/qualification-receipt.json`,
SHA256 `5a707a69ccdde44a9bf4a3805cb0900db11c47884b4f6e1aff6a34c939e48d47`.
It retains operation identities, runtime attribution, per-case hashes,
first/last times, schema checks and descriptive weak-label measurements.
29/40 operation receipts lack a Pod identity; those attribution fields remain
unknown, not zero GPU usage or proof of which replica served the request.
The original evidence-index SHA256 is
`9cb70d68bd4f33cb98be6809fc1fd8bbbf853db2fd0014c2cbb1d82639d0d061`.

NIH report-mined labels are weak labels, not adjudicated expert reference
reports; the mirror JPEGs are not diagnostic DICOM. Formatting success cannot
establish clinical accuracy, clinical safety, paper reproduction, training-data
independence, or performance on untested images. Incorrect image interpretation
may persist even with a valid schema and must remain visible in the descriptive
weak-label measurements.
