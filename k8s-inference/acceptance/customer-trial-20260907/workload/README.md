# Trial-customer scientific campaign

This is a bounded platform test, not a scientifically meaningful cancer-immunotherapy experiment. The model-owned accepted synthetic fixture bytes and semantic oracles are reused unchanged. The one multi-shard override is the previously accepted `rfdiffusion-four-designs` scenario from `acceptance/scientific-fleet/scenarios/customer-readiness.json`.

The campaign runs 14 public scientific operations over nine model/profile IDs: two each of Proteina-Complexa, BoltzGen, Mosaic, BindCraft and RFdiffusion; one each of ESMFold2, ESMFold2-Fast, Protenix v2 and AlphaFold3. RFdiffusion → Protenix → Mosaic is sequential. The remaining eleven operations run with at most four concurrent public clients. The bulk RF request has four inference shards. No model startup policy, hot floor, queue quota, capacity ceiling or infrastructure is changed.

Each operation uses the existing public scenario client, including exact idempotent submission replay, terminal semantic validation, and hash-verified download of its result manifest and one bounded scientific result. Unique `client_context` labels identify the trial and phase without changing the scientific input. A thin wrapper records HTTP statuses, latencies and operation/stage transitions; it adds no automatic retry or Kubernetes workaround. Failures remain failures.

Run with the private Terraform output bundle outside the repository:

```bash
python3 run_customer_trial.py \
  --access-bundle /private/path/final-stack-output.json \
  --output /private/path/unique-trial-run \
  --run-id trial-customer-20260907-r01
```

`--prepare-only` validates fixtures and prints the non-secret campaign plan without contacting the cluster. Live output includes receipts, one HTTP/state trace per case, per-case customer summaries, and the final aggregate. The bearer credential is loaded only into memory; no request headers or request bodies are traced.

Interpret end-to-end times as this customer's observed latency under concurrent shared-cluster work. They include public polling granularity and output retrieval and are not GPU-kernel time or a fully cold-node benchmark. Per-model sample counts are only one or two: do not infer a production SLA or stable percentiles from them. Actual snapshot usage requires separate live runtime proof; an available option alone is insufficient.
