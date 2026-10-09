# Large-trajectory transport release acceptance

The deployed Helm 227 API fixes the full-response debug-buffer allocation that
OOM-killed the previous Pods during large scientific artifact downloads. The
new bounded stream retains the original tenant-owned artifact plus byte-count
and digest metadata; ordinary request/error bodies are still captured. Pod
memory limits and cloud quotas are unchanged.

The [sanitized actual receipt](receipt.json) records two authorized cohorts:

- Four complete concurrent transfers: 1,573,747,708 bytes independently verified.
- Two interrupted transfers: incomplete and unverified, not falsely successful.
- Six corresponding small debug-body artifact references, with complete hashes
  for successful responses and explicit partial-stream accounting.
- 42/42 successful readiness/operation polls, maximum latency 0.672 seconds.
- Unchanged identities for all three API Pods and zero restart deltas; each
  retained its 2 GiB memory limit.

These are real HTTP/download checks, separate from GPU simulation throughput
and the [final-client recovery of eight complete operations](../final-client-recovery-227/README.md).
The actual source-bound scientific runs remain attributed to their original
Helm 226 deployment. No new simulation, key, limit or cloud resource was created
by this streaming check.

Read the [helper and detailed report](../../comparison/analysis/release_checks/README.md)
for exact artifact/image identities, timestamps and memory samples. Server
`delivered_bytes` is an ASGI send boundary, not a remote acknowledgement.
`disconnected=true` can also occur after a complete verified stream. Success
therefore uses completeness, size and digest together, not that flag alone.
Memory values are sampled observations, not a claimed continuous peak.

Original predeployment OOM states are retained in
[predeployment-oom.json](predeployment-oom.json). The failed initial correlation
receipts remain at the explicitly documented private-server evidence paths;
they were repaired by ingress-safe correlation, not discarded or relabeled.

`receipt.json` SHA256:
`0c7d86f90095a26722f04f38a54547e20146fb4314b757ed57980d2f76144b56`.
