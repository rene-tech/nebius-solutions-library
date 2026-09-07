# Independent customer-trial observation

These acceptance helpers observe the existing H100 platform without invoking
models or changing deployment, queue, scaling, startup, alert, or capacity policy.
They create only a temporary authenticated admin session and local evidence files.
Run the workload and interactive-client lanes separately.

Use the control-plane Python environment (`httpx` is required) and pass the
private Terraform access bundle, explicit kubeconfig/context, and a private output
directory to `sample_cluster.py`. It samples every 25 seconds until SIGTERM or
SIGINT, saves each completed sample immediately, and signs out on exit. Do not
commit its raw output: response metadata includes tenant and credential identities.

`capture_context.py` retains scientific policies, inventory, and observability
before/after the trial. Compare policy `desired` and `inherited` fields, not running
counts or timestamps. `capture_storage.py` reads historical or current disk bytes
and node identities, allowing capacity percentages to be interpreted correctly.
`capture_job_evidence.py` reads logs and events for an exact existing scientific
Job; completed Jobs may already be removed, in which case the installed Loki
history is the source of runtime evidence.

`summarize_observation.py --input PRIVATE/samples.jsonl --output REPORT.json
--campaign-dir PRIVATE/workload/r01` produces an allowlisted summary. The campaign
receipt IDs distinguish the 14 scientific requests from input preparation, result
retrieval, and background requests. Raw payloads, API-key prefixes, signed artifact
URLs, tenant IDs, and login credentials are excluded from the summary.

Interpretation:

- A Pod absent at the next sample is not automatically declared succeeded;
  public terminal receipts and lifecycle records establish completion.
- Pre-existing failed test Pods are excluded from new incidents. New-node CNI/CSI
  initialization is separate from a previously-ready node becoming unhealthy.
- Lifecycle GPU seconds describe scheduler/device ownership and application
  phases. `active_compute` is **not** measured time executing GPU kernels.
- The DCGM busy-fraction integral is a 25-second, left-held sampling estimate.
  Short requests and bursts can be missed; it is not billable GPU time.
- A resident allocated GPU sampled at ≤1% utilization may be loading, waiting,
  cooling down, or unused. Do not infer the cause without lifecycle phase evidence.
- Missing series and failed reads are not replaced with zero values.

Offline checks:

```bash
python3 -m unittest discover -s acceptance/customer-trial-20260907/observer -p 'test_*.py'
ruff check acceptance/customer-trial-20260907/observer
```

The completed measured verdict is in [report.md](report.md), with the allowlisted
operation/attempt/Pod and accounting evidence in [observation.json](observation.json).
