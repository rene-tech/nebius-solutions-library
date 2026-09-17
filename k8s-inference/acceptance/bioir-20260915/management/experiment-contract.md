# BioNeMo Inference Runtime evaluation — 2026-09-15

## User request and authority

Benchmark all BioNeMo models hosted on Scientific AI against the CURRENT deployed implementation; determine BIR applicability, actual speedups, GPU-snapshot and feature implications; produce a complete per-model recommendation report. Use currently unused H100 or L40S capacity. Create separate task-deck tasks, start and supervise workers. Evaluation only: no production promotion.

This current user authorization explicitly permits using currently unused existing Scientific AI GPU nodes, superseding the old epic prohibition on reusing any pre-existing resource. It does NOT authorize changing customer deployments, scaling customer models down, provisioning new GPUs, changing quotas/limits, changing drivers, resetting GPUs, or modifying security policy. Do not do those things. Ask if capacity/access truly blocks progress.

## Scope frozen from live catalog and website attribution

Required eleven BioNeMo ecosystem models:
boltz2, openfold2, openfold3 (Preview2), diffdock, evo2-40b, genmol, molmim, msa-search-pdb70, proteinmpnn, rfdiffusion, proteina-complexa.

Also evaluate protenix-v2 explicitly, because BIR supports its module and it was discussed with the user. Review scope/indirect applicability of openfold3-openbind, boltzgen, mosaic, bindcraft, esmfold2, esmfold2-fast, alphafold3; list these separately, not as silently added BioNeMo-branded products. NVIDIA branding alone does not imply BIR eligibility. Cosmos, medical imaging, Qwen, aging clocks, SDXL are outside this acceleration study.

Every required model needs a current-runtime baseline and a sourced applicability decision. An unsupported BIR path is N/A, never 1x, 0s, or silently omitted. If reasonable module reuse could help, document exact seam and test a bounded prototype where practical; do not embark on unbounded architecture rewrites.

## Repositories and starting material

Product source: /home/tux/nebius-solutions-library-inference; origin https://github.com/rene-tech/nebius-solutions-library.git
Observed HEAD 83bcb2d6c7f4dc112e414e00596e0d6b03e22712. Root checkout has unrelated active Cosmos/UI edits: preserve all.
Evaluation worktree: /home/tux/worktrees/fs2-bioir-evaluation-20260915 on branch fs2/bioir-evaluation-20260915. ONE shared evaluation branch; no worker-created branches. Distinct directories per lane. Manager alone commits/pushes.
Evaluation code and evidence: k8s-inference/acceptance/bioir-20260915/{boltz2,openfold,coverage,protenix,snapshot,report}/.
Deck shared contract (this file): /home/tux/dashboard/data/epics/nim-fast-start-platform/docs/bioir-evaluation-20260915.md.
Cached private launch bundle: /home/tux/trtbionemo-private-ea_1.4.0.
Separate launch service: /home/tux/trtbionemo-serverless (read-only reuse). That demo is not the live Scientific AI runtime. Do not stop/change it or publish private release contents.
Use current public pinned BIR release/source when possible; record exact wheel hash/version and source commit. EA 1.4.0 bundle contains bionemo_ir 0.5.0+cu132; do not confuse bundle version with wheel version or equate EA behavior with public docs.

## Sources to verify, not universal performance promises

https://docs.nvidia.com/bionemo/inference-runtime/overview/
https://docs.nvidia.com/bionemo/inference-runtime/references/support-matrix/
https://docs.nvidia.com/bionemo/inference-runtime/references/benchmark/
https://docs.nvidia.com/bionemo/inference-runtime/install/
https://github.com/NVIDIA-BioNeMo/BioNeMo-Inference-Runtime

BIR is a PyTorch library, not a NIM-container toggle or a new serving platform. Current docs: cp312, CUDA13.2 environment, driver580+. H100 and L40S qualified. Protenix and Boltz2 affinity have modules but no build_processor pipeline. CUDA Graphs are NOT CUDA process snapshots.

## Capacity and coordination

Kubeconfig: /home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig
Project project-e00rene, eu-north1.
Inventory at 2026-09-15 21:12 UTC: 5 single-H100 and 8 single-L40S nodes have no GPU-requesting active pods. RECHECK just before using; also check GPU processes/utilization, node readiness, pending customer jobs, CPU/RAM and storage headroom. Never treat 0% utilization on an allocated GPU as free.

Protect the two live eight-H100 nodes and all customer pods; no host-wide changes:
- computeinstance-e00m0hsph76ajt9sdb
- computeinstance-e00p3acr87k9k4mckj

Lane allocations (only these nodes until manager reassigns):
- boltz2: H100 computeinstance-e00fkt1bsa4ec657sn; L40S computeinstance-e00ax3mgt7y3a0asa6
- openfold: H100 computeinstance-e00j20a9hkb508cn4a; L40S computeinstance-e00dczh75qcnbj0bx8
- coverage: H100 computeinstance-e00r9tdjfszjs3angk; L40S computeinstance-e00krzha55t0sg3t56
- protenix: H100 computeinstance-e00xjaw5jqexvvnpat; L40S computeinstance-e00r165maajnbg7wrr
- snapshot: H100 computeinstance-e00y0jttwekyghrznp; L40S computeinstance-e00sa78kng1kwhej6q

Root manager may temporarily use the snapshot H100 for Protenix candidate bring-up while the conventional Protenix matrix runs on xjaw. No speedup is calculated across those nodes: final paired timings must use the same physical GPU. Release this temporary prototype before snapshot work needs that node; no worker may independently claim it.

Use at most ONE GPU per active lane initially; scale to its second assigned GPU only when first work is making progress and remaining customer headroom is healthy. CPU MSA baseline needs no GPU. Evo2 current runtime requests 2 GPUs. Manager exception at21:20UTC: a task-owned exact2GPU Evo2 clone may request the two free GPUs on computeinstance-e00m0hsph76ajt9sdb through Kubernetes. Inventory showed6/8 allocated, two GPUs0MiB, CPU requests34%,RAM17%, no pendingGPUcustomerpods. Recheck immediately before; never pickCUDAindices, move existingpods, reset GPUs or snapshot on that shared node. Record shared-host contention, baseline monitoring and cleanup. If capacity disappears askmanager, don't substitute a1GPU altered model.

Task-owned namespaces fs2-bioir-boltz2, fs2-bioir-openfold, fs2-bioir-coverage, fs2-bioir-protenix, fs2-bioir-snapshot. Label all resources with evaluation=fs2-bioir-20260915 and lane. Use Kubernetes scheduler/node affinity plus real GPU requests/limits; no manual CUDA device assignment on a shared host. Do not copy whole production secrets or private customer request logs into evidence. Access required existing artifacts read-only through approved mounts/credentials; no auth bypass. No production routing/API keys/catalog changes. Manager exception: when an exact baseline needs a namespace-scoped existingmodelPVC, task-owned uniquelylabelled benchmarkpods may live in fs2-models and mountthatPVC readOnly:true; verify no productionService selector matches, no productionownerrefs, and never changePV/PVC ornamespace. Delete onlyexactownedpod objects.

## Benchmark contract (shared)

1. Baseline = clone exact currently deployed digest/entrypoint/config, API contract and model/checkpoint revision onto allocated unused GPU. Record divergence from repo HEAD. Same GPU for A/B; report H100 and L40S separately.
2. Separate three variants where applicable: deployed current; persistent upstream worker with equivalent features (isolates process/weight-reload overhead); BIR persistent worker. Do not attribute all residency improvement to BIR.
3. Pin shapes/inputs, input digests, MSA/templates, steps, recycles, seeds, samples, dtype/precision and checkpoint. No reduced sampling, no silent single-sequence replacement, no substituting OpenFold for AlphaFold under same label.
4. Use non-sensitive public fixtures across short/medium/large inputs and supported monomer/complex/ligand/RNA/DNA features. Start smoke for debugging, then at least 3 measured repetitions per representative case and a mixed-input/batch workload. n/p50/p95 meaningful only with sufficient n; report individual timings/dispersion for small samples, don't invent p99 confidence.
5. Measure wall clock from accepted request through validated artifact; also parse/preprocess/MSA, loading, first shape compile/graph capture, GPU forward, postprocess/artifact write, queue. Cold image, cached image/cold process, warm persistent, snapshot restore are separate cohorts. Never flush shared caches.
6. Count ALL attempts/failures/OOMs/timeouts/retries. Report successful requests per allocated GPU-hour, total GPU allocation seconds / valid requests, GPU occupied-but-idle loading/queue/cooldown time. Pricing: GPU-seconds per request primary; monetary cost only with sourced explicit rate assumptions, not beta/free advertised prices.
7. Real scientific output validation + paired lDDT/DockQ/task metrics as appropriate; same seeds don't imply bit-identical diffusion. Record sample-level differences and uncertainty; no clinical/biological efficacy claims. Structural output-only smoke does not establish scientific equivalence.
8. Feature parity: native HTTP/MCP schemas at adapter boundary, errors, requested model/seeds/sampling, templates/MSA, affinity if advertised, output formats/confidence fields, cancellation/idempotency and batch paths where supported. No production public traffic for heavy benchmarks; preview/private routes.
9. Snapshot/caching: capture/restore task-owned persistent workers only. Recreate pod/process, restore on compatible GPU+software, then two DISTINCT real requests including new shape; check graph replay/recapture, correctness, timing, repeated cycle stability, failure fallback and GPU-memory cleanup. Same-process suspend/resume != fresh-pod restore; CUDA Graph != snapshot; host RAM cache != snapshot. No host driver/security changes or interference with other PIDs.
10. Keep raw JSONL/CSV, sanitized logs, commands, manifests, resolved digests, runtime environment, dataset checksums, clock boundaries, failures, resource disposition. Write result.json + report.md per lane with measured / unsupported / unverified labels, and links.
11. Do not promote anything. Clean up ONLY task-owned disposable resources after retaining evidence; never delete cached shared weights, production resources or the user's existing nodes.
12. Final manager review once substantive tests finish, not repeated ceremonial reviews. Consolidated report must cover every scoped model with recommendation: adopt after gate / conditional prototype / no BIR fit / blocked-unmeasured. Every incomplete claim explicit.

## Management / communications

Parent task fs2-bioir-manager-r20260915.
Workers update their local Task Deck card with agent ID, actual running job/pod, evidence path and concrete blocker. Cards are filesystem-backed; local edits are supported. HTTP dashboard API currently requires authentication; do not bypass its authentication or retrieve other credentials to launch. Use the already authorized collaboration workers in this session, and record that these are collaboration sessions, not deck-managed tmux sessions.
No Slack progress spam. Only parent sends final completion or a genuine input-required block; do not claim completion while tests are missing.
