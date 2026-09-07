# Customer-trial remediation cohorts

Run two consecutive complete, clean cohorts after the integrated deployment.
Keep all failed cohorts, exact operation IDs and unsuccessful exits. This is a
bounded platform-acceptance exercise using accepted synthetic inputs, not a
scientifically meaningful cancer-immunotherapy experiment or a capacity SLA.

`run_remediation_trial.py` delegates to the original unchanged public client and
14-operation runner. Before contacting the cluster, it verifies that the model
counts, scenario order, fixture bytes, parameters, input digests, priorities,
four-client maximum and underlying runner bytes match the original campaign.
Only new correlation IDs, timestamps and source identity differ. It adds no
retries, direct Kubernetes execution or administrative mutations.

The sequence is RFdiffusion → Protenix v2 → mosaic, followed by eleven mixed
operations with at most four clients. Overall there are two operations per
primary model and one per secondary profile, including ESMFold2 and Fast
separately. The second RFdiffusion request remains the accepted four-shard bulk
batch. Ordinary HTTP/MCP requests and independent cluster/UI observation run in
parallel under separate workers.

Prepare offline first, without reading credentials or making any requests:

```bash
python3 run_remediation_trial.py \
  --access-bundle /private/path/final-stack-output.json \
  --output /private/path/new-r01 --cohort 1 --prepare-only
```

After the manager confirms the exact deployment and observers are ready, omit
`--prepare-only`. After the first cohort's terminal state, cleanup and recovery
are confirmed, run cohort 2 with a new output directory. Never run both cohorts
concurrently; consecutive clean runs are required. The baseline took about
27 minutes, so allow roughly 25–40 minutes per cohort plus recovery; normal
queueing and different warm-cache states can change elapsed time.

Export each raw run with the original
`../../customer-trial-20260907/workload/export_results.py` into a fresh
`results-rNN` directory here. Private HTTP traces remain outside the repository;
the exporter publishes redacted receipts and their hashes. It separates local
client-slot waiting from accepted server delay, public polling and downloads.
Snapshot use requires actual runtime evidence from the observer, not just an
enabled capability or an image name.

Success requires all 14 operations, valid semantic results, all 14 exact
idempotent replays, expected shard completion and resource release with no manual
intervention. Legitimate priority preemption and transparent stage retries must
remain visible. An unexpected failure or customer-facing outage means the cohort
is not clean, even if later cases pass. Root owns the final cross-lane verdict.

Placement's no-fitting-pool rejection currently surfaces a generic public
scheduling-contract validation message. This is outside the bounded repaired
case: the tested models have fitting reserved capacity, and pending scheduler
diagnostics distinguish actual resource infeasibility from elastic-node startup.
Do not describe that generic validation as a detailed sizing recommendation.
