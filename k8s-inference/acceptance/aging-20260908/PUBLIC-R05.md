# Public aging Apps r05: workload acceptance passed

The bounded public workload campaign passed on 2026-09-08, from
15:06:39.576270 UTC to 15:18:11.007712 UTC. The client exited 0, with no hidden
retries, manual recovery, extra logical submissions or remaining test clients.

Runtime/Terraform source: `c188ddaada3d433ecc6792a0fdadbd24b705408e`.
CP image: `sha256:25c6b54cd803d4647f8909ed4f398d5326db8bcbf5cb696a354d167891f12c6c`.
UI source: `d00976744bd7cefb93896cac3cac8585ed8d6b0b`, image:
`sha256:d474e818258ca0e78a3ec6765d3a035f7ded616a5f94d89ad4252aa23aa4988a`.

## Verified public behavior

Two independent clients used normal-tenant keys scoped to exactly one model
each. Both HTTP and MCP discovery returned only the permitted model. Each App
had two consecutive observed Cold-plus-zero-container samples before demand.
Each first original synthetic fixture used HTTP; the second used MCP.

All four operations succeeded on attempt 1. Exact HTTP/MCP replays retained the
original operation IDs; HTTP and MCP result retrieval agreed. PhenoAge's two
different predictions matched the exact formula reference, and AltumAge's two
different predictions matched the retained CPU/CUDA parity reference within
0.001 years. Inputs and immutable model images were unchanged.

| Model/request | Operation | Accepted → terminal | Raw API startup field | Full client verification |
| --- | --- | ---: | ---: | ---: |
| PhenoAge cold HTTP | `42a96c54-3592-4a44-a0ce-1d442466f4a6` | 8.994396 s | 8.611769 s | 14.125396 s |
| PhenoAge warm MCP | `d3a66635-e5a1-4569-8d2e-af500104a11d` | 0.500330 s | 0.341889 s | 4.710000 s |
| AltumAge cold HTTP | `0e20df81-c296-4a4c-8012-bd2e5771691d` | 343.755471 s | 343.318074 s | 348.707353 s |
| AltumAge warm MCP | `f3375439-b07c-4ebd-849c-3e1d1de92ba8` | 0.541401 s | 0.203456 s | 6.969313 s |

Initial durable HTTP admission returned in 0.381438 seconds for PhenoAge and
1.275153 seconds for AltumAge. The full client clock additionally includes
polling, replays, result retrieval and verification. The raw API field is named
`cold_start_seconds`; on a warm request it is readiness-confirmation latency,
not another model load. Missing timing/runtime values remain null, not zero.

## Observed startup boundary

PhenoAge used an existing CPU node with its image cached, Pod
`c4fcbd56-1f4c-495a-ae8f-07e000ee59f1`. This is a worker-cold measurement,
not a new CPU-node provisioning benchmark.

AltumAge's original placement policy caused the existing preemptible group to
scale from 0 to 1. Pod `5f07f70a-c49a-41b2-ae40-9f0c9cd5738b` was created at
15:06:52; autoscaling triggered at 15:07:23. The new node was created at
15:10:02 and Ready at 15:10:21; Pod scheduling followed at 15:10:42. The
96.335-second image pull began at 15:10:43 and finished at 15:12:19. Container
start was 15:12:23; Pod Ready was 15:12:31. API readiness confirmation occurred
at 15:12:33.034088 and the first prediction completed at 15:12:33.471485.

That cold latency includes real new-node provisioning and a fresh image pull;
it is not weight loading alone. Both requests used the same H100 80GB, GPU
`GPU-50d4fdc8-4221-dfa1-9099-314fc61a6f1d`, node UID
`805e9188-8a90-4380-9f96-f191153865c3`, driver 580.159.04 and exact AltumAge
image `ca6352f7…`. Neither runtime restarted. No node or limit was manually
changed, and no GPU snapshot claim is made.

## Final cleanup and scope

Each App's Runs/Usage counted exactly two logical successes despite replays.
The original min0/max1, pools, startup policy and 300-second idle/cooldown values
were retained. Both returned naturally to observed Cold with zero containers
in consecutive samples: PhenoAge confirmed at 15:12:28.848557 and AltumAge at
15:18:09.616458. Both temporary keys were revoked and their explicit denial
checks returned 401; the admin session closed and the owned client exited.

The public workload trace contains no unexpected HTTP/MCP errors. Separately,
the browser observer retained an admin-context 503 at 15:11:23, recovering
1.103 seconds later. That remains an admin availability finding owned by the
release manager; this report does not claim every UI request was error-free.

The evidence supports these exact native routes, HTTP/MCP semantic behavior,
measured cold starts and single-replica 0→1→0 elasticity. It does not qualify
multi-replica scale-out, other GPUs, GPU snapshots, or new scientific inputs.
Private original receipts: `releases/aging-20260908/public-apps-r05` and
`public-apps-r05-runtime-witness/{phenoage,altumage}.json`. Prior failed cohorts
[r01](PUBLIC-R01.md), [r02](PUBLIC-R02.md), [r03](PUBLIC-R03.md) and
[r04](PUBLIC-R04.md) remain intact.
