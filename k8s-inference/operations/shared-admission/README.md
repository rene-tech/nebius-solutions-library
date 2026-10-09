# Shared admission: Lynx and WhiteLab

Owner decision, October 6, 2026: plan for eight concurrent Lynx simulations
and four WhiteLab single-cell jobs, allowing WhiteLab to burst to eight when
compatible resources are available. These are best-effort planning targets,
not dedicated nodes or a hard availability SLA. Capacity loss can reduce them.

Lynx already has one shared non-expiring key with `max_concurrency: 8`.
Do not rotate it, test with it, or issue a second independent eight-slot key.
WhiteLab is not yet onboarded. The prepared route uses tenant ID `whitelab`;
onboarding must explicitly create the intended user/key, with concurrency eight,
and qualify its actual access. This change does not fabricate a customer account.
Key concurrency counts accepted active **operations**, including queued work;
it is neither a GPU reservation nor a count of MPI ranks/umbrella windows.

## Placement and fairness

- Qualified MD uses L40S first, then regular H100, then preemptible H100.
  Each independent simulation normally requests one GPU. Multi-GPU work keeps
  its separately qualified placement and full resource requirements.
- scVI/scANVI remains H100-only. The core shape fits single-GPU nodes; the
  256 GiB atlas shape needs a full H100 host's RAM, but allocates just one GPU.
  On the current two full hosts, their approximately 277 GiB allocatable
  ephemeral storage admits only two 128 GiB scratch reservations per host:
  **four simultaneous atlas jobs**, despite having sixteen GPUs there. Standard
  128 GiB RAM jobs qualified at eight on the single-GPU H100 nodes. Do not infer
  job capacity from GPU count alone or shrink the scratch request without an
  independently verified storage design. Eight atlas submissions can wait for
  those four execution slots; immediate eight-atlas execution is not qualified.
- Existing Kueue LocalQueues separate Lynx MD and WhiteLab single-cell work.
  Relative weights 2:1 express the eight-to-four planning balance when both
  have compatible pending work. An idle lane does not strand GPUs.
- Usage ordering counts GPU allocation, not raw RAM bytes. CPU, RAM and RDMA
  remain fully quota-budgeted and constrained by per-node fit; zero fair-share
  weight does **not** remove their admission limits or usage accounting.
- A relative weight is not a concurrency cap or a four-GPU reservation. Kueue
  historical fairness orders pending work; it does not immediately evict a
  same-priority fourteen-day simulation. Do not claim otherwise. With severe
  capacity loss, work waits. A future guaranteed floor needs a separately
  agreed reservation/reclaim policy and checkpoint-aware eviction tests.
- Existing serving/academic/CPU queues remain unchanged. H200 quota already
  present in the live queue is preserved, but is not added to model eligibility.

## Reproducible configuration

Merge the `deployment.scheduling` fields from
[`../../examples/scheduling-lynx-whitelab.tfvars.json`](../../examples/scheduling-lynx-whitelab.tfvars.json)
into the real `terraform.tfvars`. This is an overlay example, not a complete
cluster configuration; Terraform replaces whole map variables, so do not pass
it as a second `-var-file` and expect a deep merge. Replace pool IDs and tenant
IDs for another deployment. The existing renderer creates these LocalQueues.
Only include the RDMA weight when managed RDMA is actually budgeted.

Normal fresh deployments use the foundation's Kueue resource weights and the
workloads stage's scheduling contract. Existing production has operator changes
newer than its saved Helm values and workload-stage inputs. Do not replay a full
stale apply to update two lanes. `plan.py` reads the current authoritative
objects and emits only:

1. the two new LocalQueues;
2. an order-only ClusterQueue patch, preserving every quota and extra flavor;
3. explicit weights in the existing Kueue controller configuration;
4. a new immutable scheduling ConfigMap and an API-only binding patch;
5. exact test-and-replace rollback patches.

It performs **no cluster writes**. Review the emitted patches and server-side
dry-runs, persist the same operator tfvars, and apply the scoped rollout. Restart
the Kueue controller after its config change. Roll the API after changing the
immutable contract binding. No model worker image, node group, GPU limit,
existing key, namespace binding, or running scientific Job changes.

```sh
python operations/shared-admission/plan.py \
  --context <verified-context> --kueue-release <installed-release> \
  --settings examples/scheduling-lynx-whitelab.tfvars.json \
  --output /private/new-plan-directory
pytest -q operations/shared-admission/test_plan.py
terraform -chdir=modules/kueue-scheduling test
```

Before deployment, test exact route resolution and GPU/RAM fit with the captured
contract. For live qualification use existing system identities, never customer
keys. Direct Kueue job admission proves scheduler behavior, not end-to-end
eight-operation API/customer readiness. Record those claims separately.

`qualify_capacity.py --single-cell-shape routine --single-cell-concurrency 8`
tests the eight-plus-eight resource shape. The atlas variant uses
`--single-cell-shape atlas --single-cell-concurrency 4` for eight-plus-four.
Each test creates one additional queued job per lane and checks that both drain
after release, then removes its exact owned test resources. It temporarily uses
independent system-owned queues to exercise GPU allocation without customer
usage or key changes; this is not a production fairness-ratio benchmark. Check
free resources before running it alongside customers.

See [the October 6 qualification](../../acceptance/shared-admission-20261006/README.md)
for live evidence, failed hypotheses and the separate pre-existing readiness
defect that remains open.

Rollback: restore the previous API contract, controller configuration and flavor
order with the saved patches; restart/verify Kueue. Drain newly created lanes
before removing them. Do not delete evidence, existing queues, or running jobs.

Upstream behavior is pinned to [Kueue 0.17.8 admission fair sharing](https://raw.githubusercontent.com/kubernetes-sigs/kueue/v0.17.8/site/content/en/docs/concepts/admission_fair_sharing.md).
