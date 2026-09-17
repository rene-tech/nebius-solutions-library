# Expanded Porto clinician qualification — final cohort

17 September 2026. Source `e6407347f` plus the follow-up error-telemetry retention
patch committed with this evidence. Global public Token Factory endpoint:
`https://api.tokenfactory.nebius.com/v1`; no regional routing, no private Sword
endpoint. `catalog.json` is the contemporaneous provider catalog response.

Command: `uv run python scripts/qualify_expanded.py --output
evidence/20260917-porto-expanded-r3` from the gateway directory. Five concurrent
conversations maximum. Each clinician completed the same first 20 public MindEval
profiles with 10 rounds and one five-axis judgment per conversation. Fixed
patient Qwen3-30B-A3B-Instruct-2507, fixed pilot judge Gemma-3-27B; exact prompts,
provenance, transcripts, judgments and usage are retained per run.

| Clinician | Completed | Successful calls including patient/judge | Extra rejected generations | Successful-call tokens | Rejected-generation tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| GLM-5.2 | 20/20 | 420 | 0 | 1,705,099 | 0 |
| Nemotron Super 120B-A12B | 20/20 | 420 | 1 | 2,261,070 | 11,121 |

The rejected generation was the **Qwen patient**, not the Super clinician:
`finish_reason=length`, completion budget 4096, request
`chatcmpl-61c221bd-23c0-41bb-95cc-4857f97e5151`. One identical-payload retry
succeeded. Its 7,025 prompt + 4,096 completion tokens remain separately recorded
in `telemetry.invalid_completions`; they are not hidden in successful-call usage.
No token limit was raised, and no hidden reasoning was substituted for an answer.

Super uses its documented `force_nonempty_content=true` chat-template option;
thinking remains enabled. This addresses the reasoning-only failures retained
in `../20260917-porto-expanded-r1`. Intermediate Super-only cohort
`../20260917-porto-expanded-super-r2` completed 19/20, with the remaining truncated
patient response motivating the bounded retry. Earlier failures remain evidence.

Conversation wall times: GLM 48.502–184.660 seconds, Super 80.148–204.014 seconds.
These include patient, clinician and judge calls, queueing and retries; they
are **not** model latency, tokens/second, cold-start or GPU performance benchmarks.

Gateway unit tests: **71 passed** after the error-telemetry follow-up. The
follow-up ensures rejected-generation usage survives a subsequent HTTP 400 or
retry exhaustion as well as successful recovery.

This is provider contract/workflow qualification, not clinical validation,
judge validation of these extra families, or deployed LibreChat acceptance.
Production still advertises six clinicians until coordinated rollout. Expanded
public acceptance must use `--expected-clinicians 8`; full integrated ten-team
and twenty-profile release gates remain open. Private Sword weights are absent.
