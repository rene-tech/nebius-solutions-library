# Actual-agent benchmark join — 3 October 2026

Fixed cutoff: **18:05:31.665361 UTC**. All 24 MPINAT inputs now have actual-agent
native/report evidence: **18 new R5 operations plus six earlier native
completions**, not 24 native runs on R5. Fourteen later recovery/report
associations remain attached to the original six operations and add no GPU
charges. This is the single final augmentation of
[the earlier REST/raw-MCP/native delta](BENCHMARK_DELTA.md), not a rewritten
history or a blanket customer-readiness verdict.

The joined ledger contains **89 operations/attempts**: the unchanged 58 baseline
rows, 24 completed actual-agent operations, six historical failed scientific
operations and one expected-negative test. It retains 679 checkpoint/command
records, 208 mdrun records and 85 observed Pod allocations. The 24 completions
have 72 successful logical repeats; no native simulation was replayed during
collection. The four historical forced-reset successes remain excluded from
the corrected comparison set, as before.

## Native throughput and dated allocation-share cost

Each completed agent operation used **one L40S** on the observed AMD
`4gpu-128vcpu-768gb` VM and three 10,000-step starts from its unchanged finite
TPR. Rates are median (minimum–maximum), with no forced counter reset. These
are repeated timing trials, not independent equilibrium samples. RIB uses
4 fs steps (0.12 ns summed over three repeats); the other inputs use 2 fs
(0.06 ns). Input, recipe, commands and full runtime identities are retained.

`M particle-steps/GPU-s` is derived from the native rate, timestep, particle
count and observed GPU allocation; it is **not GPU utilization**. GPU-s and USD
below instead use the production scheduler-occupancy clock. They include
reserved time outside native integration.

| Case | Native cohort | Native ns/day, n=3 | M particle-steps/GPU-s | Occupied GPU-s | Allocation-share USD |
| --- | --- | ---: | ---: | ---: | ---: |
| benchbfc | R5 | 125.106 (125.104–126.537) | 31.821 | 110 | 0.06980 |
| benchbfi | R5 | 42.756 (42.753–42.867) | 10.875 | 184 | 0.11676 |
| benchbnc | R5 | 148.019 (145.447–175.353) | 37.649 | 102 | 0.06472 |
| benchbni | R5 | 129.059 (128.381–136.258) | 32.826 | 103 | 0.06536 |
| benchbtc | R5 | 129.962 (129.962–132.719) | 33.056 | 108 | 0.06853 |
| benchbti | R5 | 114.989 (113.660–115.110) | 29.248 | 111 | 0.07044 |
| benchmem | Original R4 | 86.163 (84.035–87.009) | 40.759 | 116 | 0.07361 |
| benchpep | R5 | 2.075 (2.026–2.099) | 150.047 | 2993 | 1.89922 |
| benchpep-h | R5 | 2.891 (2.888–2.940) | 209.054 | 2234 | 1.41760 |
| benchrib | R5 | 17.941 (17.422–18.169) | 105.789 | 810 | 0.51399 |
| benchsfc | R5 | 380.962 (375.028–385.385) | 7.414 | 71 | 0.04505 |
| benchsfi | Original | 63.680 (63.307–64.365) | 1.239 | 137 | 0.08693 |
| benchsnc | Original | 958.697 (906.862–966.255) | 18.658 | 59 | 0.03744 |
| benchsni | Original | 694.224 (685.568–705.943) | 13.511 | 58 | 0.03680 |
| benchstc | Original | 413.682 (412.552–432.068) | 8.051 | 65 | 0.04125 |
| benchsti | Original | 359.152 (355.934–379.647) | 6.990 | 68 | 0.04315 |
| cmet-eq | R5 | 96.195 (95.786–100.002) | 37.460 | 132 | 0.08376 |
| cmet-ti | R5 | 73.570 (72.144–79.251) | 28.649 | 145 | 0.09201 |
| hif2a-eq | R5 | 151.496 (147.312–153.039) | 31.164 | 104 | 0.06599 |
| hif2a-ti | R5 | 130.012 (126.661–131.922) | 26.744 | 110 | 0.06980 |
| ligand-cmet-eq | R5 | 246.305 (232.391–250.646) | 9.184 | 76 | 0.04823 |
| ligand-cmet-ti | R5 | 221.468 (211.326–224.821) | 8.258 | 73 | 0.04632 |
| shp2-eq | R5 | 72.679 (72.021–73.724) | 45.141 | 146 | 0.09265 |
| shp2-ti | R5 | 58.150 (56.154–60.773) | 36.117 | 169 | 0.10724 |

The [2026-10-03 eu-north1 public-price scenario](public_cost_references.json)
apportions the observed $9.1376/hour VM, including CPU/RAM, at **$2.2844 per
allocated GPU-hour**. This is not an invoice or customer charge; it excludes
storage, network, taxes, discounts, control plane, agent tokens and unallocated
VM time. No current-price or future-cost guarantee is implied.

| Cohort | Production occupied GPU-s | Allocation-share USD | Clock quality |
| --- | ---: | ---: | --- |
| 18 new R5 native completions | 7,781 | 4.93748 | application-observed |
| Six original native completions | 503 | 0.31918 | application-observed |
| Six historical failed scientific operations | 135.954834 | 0.08627 | estimated |
| Separate expected-negative test | 24.847098 | 0.01577 | estimated |

The 24 completions total **8,284 GPU-s and $5.25666**, delivering
834,830,250,000 particle-steps and 1.50 ns summed across distinct systems/repeats,
not a single trajectory. Failed-operation cost is retained failure/retry
overhead, **not measured duplicated integration**: durable useful work and lost
steps remain unknown for those failures. The original reviewed REST retry map
is unchanged; case-name matches do not merge intentional repeats, MPI controls
or different runtime protocols into retries.

All 31 selected production rollups are reconciled. The 24 successful clocks
lie inside their independent Pod-observation bounds. All retain
`trace_context_missing`; 19 of the 31 retain `phase_classification_incomplete`.
The expected-negative estimated clock is 0.323857 GPU-s above the offline upper
bound; both observations remain visible. It is not silently repaired or used as
an exact billing measurement. Generic top-level reserved-GPU zeros are not cost.

## Client, native runtime and scientific comparisons stay separate

The original SFI/SNC/SNI/STC/STI client was `e96a6650…`, with observed worker
`14ffdae0…`. Original MEM used R4 client `6d8b2038…` and worker `dc5d908c…`.
The 18 new runs used R5 client
`b948ecca1d7f8d47ede578bbe7733b13714a4625269977014e77f602049ab927`,
seeded `moonshotai/Kimi-K3`, high reasoning, and worker
`dc5d908c64503c4c4cdc3bede10987d49a9acdde5ef2f4f1d0e61f0628739f93`.
Later R1/R3/R4/R5 report recovery does not replace any original native identity.
The failed R2 report recovery is retained too.

These are `agent-skill-MCP` operations. Raw REST and raw MCP remain separate
earlier cohorts; no chat overhead or client speedup can be inferred from their
rate differences. Baseline MPI `c6c353e…`/auto and CUDA-aware `5884569e…`/fixed
GPU-PME controls retain their original protocol groups and are not reclassified
by this actual-agent join.

The [primary MPINAT tables](https://www.mpinat.mpg.de/632182/bench.pdf) use a
GTX 1080/E3-1240v6, GROMACS 2018 and CUDA 8. Timing policy and complete protocol
equivalence are not established. The earlier RIB input-version warning and
absence of public MEM/PEP numerical baselines remain. **Every public matched
speedup stays null.** Short timing trials establish no scientific convergence,
whole-node 8/16-GPU result, shared-service capacity or complete customer release.

## Phase evidence and actual-agent friction

The following are observed reserved-GPU scopes, **not an additive timeline**.
Native Wall t is nested inside mdrun process wall, not pure integration.

| Agent case | Init-container GPU-s | Native Wall t GPU-s | Analysis GPU-s | Post-worker collector GPU-s |
| --- | ---: | ---: | ---: | ---: |
| SFC | 5 | 13.628 | 0.09683 | 4 |
| PEP | 6 | 2,509.193 | 0.09687 | 19.156–28.947 |
| PEP-h | 6 | 1,783.914 | 0.09677 | 19.097–28.859 |

Exact native initialization, pure integration, checkpoint duration and total
export remain **null**. Production `active_compute` includes broader worker
activity; zero classified checkpoint time does not mean zero checkpoint work.
Single-GPU cases do not acquire fabricated MPI-staging measurements. Chat
planning/wait/delivery phase timers and agent token cost remain null.

R5 has sixteen saved-study paths and two valid direct batch/MCP paths. The
initial saved-study chats acknowledged admission, not completion. For example,
PEP's initial reply took 42.211 seconds; its durable study elapsed 6,331.017
seconds, while native scheduler occupancy was 2,993 GPU-s. PEP-h's corresponding
values were 39.193 seconds, 3,116.474 seconds and 2,234 GPU-s. The client serialized
whole studies through analysis/publication; this is a client scheduling
limitation, not a measured native-platform capacity limit. Do not derive an
exact waiting/export duration by subtracting these differently scoped clocks.

CMET-TI delivered directly in 219.924 chat seconds, without a saved study.
SHP2-TI recovered the **same** completed operation and delivered after exits
75, 2 and 1 (259.370 chat seconds); failed batch-07 remains failed customer-path
history. BTI retains GET-status 503 and successful GET-only recovery, without
native resubmission. Both paths remain non-clean; absent clean flags elsewhere
remain unknown. Native success does not erase advisory errors, old empty
reports, tool-budget/upload failures, or the HIF2A/SHP2 observer false negatives.
SHP2-EQ's corrected proof permits omitted optional compression while checking
the actual gzip binding; its earlier failure file remains retained.

[Actual Lynx requests versus assumed controls](LYNX_WORKLOADS.md) and the
[exact client qualification](AGENT_QUALIFICATION.md) remain authoritative for
that distinction. MPINAT, alanine and GPU-scaling fixtures are representative
tests, not Lynx-supplied production systems. The R5 advisory/client failure is
separate from these native MD successes; no R4 result is relabelled as R5.

## Fixed evidence and validation

Private snapshot:
`/home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/agent-accounting-final-r1`.
The additive lifecycle sidecar was captured at **18:05:43.947711 UTC** using a
bounded read-only query for the 31 exact internal-QA IDs. Original historical
status gaps remain recorded; a fresh exact-ID GET now supplies the missing
older failed ligand status without synthesizing its old receipt or native work.

- `report.json`: `624108cf89c4b4f4f8a7d22afb3b58962288a2cc97fa4c53b9c52cef970233d4`
- `durable-lifecycle-sidecar.json`: `3a2a99a1610a7a4f60a6ed3a3a97ae8cdb86c7152225d3018081b8251da63180`
- `agent-join.json`: `3bfa95c3c23ff9fbcb99fdbbc05de79ae9081233e16a50cdbd9c5b10d5afc0ea`
- `legacy-agent-join.json`: `9eb25fbee68f95c26704883807953bd9436957b8f2b03e285684c9b5c8e1e17a`
- `agent-summary.json`: `38af59f38f856906a2be41873c8ddc316224826672e72c5668ee241452f9ae54`

Independent validation rehashed **1,027** retained metadata/native-log files;
594 binary/artifact references were deliberately not copied. SQLite integrity
and foreign keys passed; all 58 prior operation values and all prior report
hashes are unchanged. Full selected-input/download verification belongs to the
retained per-case proofs, not an invented rehash of uncopied TPRs. No trajectories,
simulations, policy/customer changes or historical ledger writes were made.

The adapter-only descendant-directory fix is commit `f91e03e49`; **74 focused
tests and Ruff passed**. Its failed partial local projection is retained as
collector history, not a native failure. Private `EVIDENCE.md` records the
single collection's commands, fixed cutoff, validation and exact source hashes.
