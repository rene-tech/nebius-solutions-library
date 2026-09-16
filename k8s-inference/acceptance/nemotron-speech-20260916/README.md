# Nemotron Speech: initial direct-runtime evidence

2026-09-16. **Diagnostic milestone only; neither App is customer-ready or in the
public catalog.** Both Jobs completed successfully. No public gateway, ordinary
tenant-key, long-recording, multilingual quality, concurrency, scaling or GPU
snapshot acceptance is implied by these results.

## Exact candidate and environment

- Repository: `rene-tech/nebius-solutions-library`, isolated detached worktree
  `/home/tux/worktrees/fs2-nemotron-speech-20260916`, based on storage-compatible
  `1cb5e6839`. No new branch or main-checkout overwrite.
- GPU-tested source: `05e9d97ad0f81b2e795cc353dcb2546a963dbe24`.
- Image: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-speech-runtime@sha256:70b31f37ec611241cab10e7b91afa20031fc8f7f503fee1f02e74bf33940a405`.
- Linux/amd64 manifest: `sha256:2f78debdc1976a82e43caae854a03dcb138b29d1d57b17a06dae1ea03bdad228`.
- NVIDIA NeMo: `3b08b2acacc13ec1268e53653346266202b2335f`.
- English checkpoint: `ebe59e5a817142986528bbbee5dba8db7b38ed50`.
- Multilingual checkpoint: `ea30d66debe3740a08b573244286791d423d6b3e`.
- Existing Scientific AI cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project
  `project-e00rene`, region `eu-north1`, namespace `fs2-models`.
- One existing preemptible H100 80 GB per Job; driver 580.159.04,
  PyTorch 2.8.0+cu128, CUDA 12.8. No new node or quota changes.
- Float32, greedy_batch, 560 ms chunks, one admitted stream, language-tag
  stripping, CUDA graphs disabled. No FP16/FP8, batching or snapshot claim.
- Synthetic English eSpeak-ng fixture, 14.69075 seconds, mono 16 kHz PCM16.
  SHA-256: `02bfe969674facbd59d5aef773a86cc9fbe030d12418b1657ae92d4ac678b4d4`.
  One warmup followed by three file-speed and three real-time-paced repetitions
  per model. All measurements and warmup traces are retained, not just successes.

The later Job renderer (`b9d7f3124`) and lifecycle runner (`2f8a828f0`) have CPU
test coverage but are **not part of this image's real GPU verification**. The
probe directly exercises the NeMo adapter/framer/events; it opens no public port.

## Results

Means below use three warm repetitions per mode. Min/max, every frame time,
transcript and individual repetition are retained in JSON; three samples do not
justify tail-percentile or sustained-capacity claims.

| Measurement | English | Multilingual |
| --- | ---: | ---: |
| Warm file-speed processing of 14.69 s audio | 0.727 s | 0.801 s |
| Warm file real-time factor (lower is faster) | 0.0495 | 0.0545 |
| First partial, real-time-paced input | 1.173 s | 1.176 s |
| Finalization after final audio, real-time-paced | 26.1 ms | 30.0 ms |
| Peak allocated GPU memory, warm | 2,688,301,568 B | 5,332,897,280 B |
| Peak reserved GPU memory, warm | 5,307,891,712 B | 5,523,898,368 B |
| Successful measured diagnostic repetitions | 6/6 | 6/6 |

Both produced nonempty partials before EOS and retained the final word
“telescope.” The English transcript matched the synthetic text ignoring casing
and punctuation. Multilingual consistently substituted “out” for “that” in
“checking that every”; this is recorded, not described as perfect accuracy.
The required-word diagnostic is not a WER benchmark or qualification of the
other 31 locales. English finalized multiple segments; multilingual finalized
this utterance at EOS. The current framer holds one frame for reliable EOS,
which contributes to live latency and remains an optimization target.

### Cold phases: one observation each, not repeated startup qualification

| Phase | English | Multilingual |
| --- | ---: | ---: |
| Image pull, including kubelet waiting | 119.716 s | 119.871 s |
| Runtime imports | 6.712 s | 7.653 s |
| Checkpoint acquisition to empty cache | 29.032 s | 31.117 s |
| Model load/build after acquisition | 7.422 s | 56.891 s |
| First warmup recording | 14.773 s | 17.196 s |
| First GPU step within warmup | 14.069 s | 16.411 s |
| Job creation to runtime-loaded event | 171.302 s | 220.223 s |
| Job creation to warmup-completed event | 186.143 s | 237.486 s |

Image size reported by kubelet: 5,127,248,114 bytes. The image is already in the
same-region registry, but fresh node-local pulls still dominate this observation.
Runtime-loaded is **not** warmed readiness. Node provisioning was not exercised.
The multilingual load/build gap needs profiling; this record does not attribute
it to larger weights or promise a snapshot improvement without measurement.

## Evidence and reproducibility

- `en-pod.log`, `multi-pod.log`: complete timestamped stdout/stderr, including
  vendor initialization and all structured events.
- `en-events.jsonl`, `multi-events.jsonl`: extracted structured probe records.
- `summary.json`: phase observations and mean/min/max warm measurements.
- `en-environment.json`, `multi-environment.json`: read-only GPU-performance
  skill environment collector run inside the actual Pods.
- `kubernetes-resources.json`, `kubernetes-events.json`: complete task-owned
  Job/Pod specifications, digests, node identities, exit status and pull events.
- `cpu-tests.txt`: subsequent lifecycle/contract/unit test run; distinct from
  the GPU-tested image source above.

From `components/speech-runtime`, render each diagnostic using the recorded
digest, then review and server-dry-run the manifest before applying it. Select
the correct explicit Kubernetes context; do not rely on a current-context default.

```bash
.venv/bin/python -m fs2_speech.probe_job \
  --name fs2-speech-probe-en-REPLACE \
  --namespace fs2-models \
  --model nemotron-speech-en-0.6b \
  --gpu-class nvidia-h100-sxm5-80gb \
  --image cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-speech-runtime@sha256:70b31f37ec611241cab10e7b91afa20031fc8f7f503fee1f02e74bf33940a405
```

For multilingual, change the Job name and model to
`nemotron-speech-multilingual-0.6b`. The renderer defaults to preemptibles,
one GPU, three repetitions, a 30-minute active deadline and no automatic retry.
Use lowercase names; replace `REPLACE` with a lowercase run identifier.

## Resource disposition and next work

Jobs `fs2-speech-probe-en-20260916a` and
`fs2-speech-probe-multi-20260916a` completed at 08:27:31 and 08:28:24 UTC.
Both released their GPUs on completion. Full evidence was saved before cleanup.
Both exact task-owned Jobs were deleted at approximately 08:36 UTC, removing their
temporary downloaded-weight caches. Logs remain in this directory, and public
checkpoints can be fetched again. No customer resources were deleted.
No public speech deployment or listener exists. Production Helm revision 123,
customer storage, existing model deployments, capacity and limits were not
changed by this diagnostic. The pushed development image is retained for replay.

Next: authenticated public live/file integration using existing identity,
operations and artifact services; genuine bounded long-file processing; complete
native option/metadata coverage; then measured admission/concurrency/scaling,
caches/snapshots and the two unchanged-release customer-path acceptance cohorts.
The Task Deck definition of done remains open.
