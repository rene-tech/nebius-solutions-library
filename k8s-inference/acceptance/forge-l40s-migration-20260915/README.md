# Forge L40S capacity migration acceptance

Date: 2026-09-15 UTC

## Intent

Retire the GPU capacity directly attached to the four Forge scheduler clusters
and recreate the Forge EU L40S pool in the Terraform-managed Scientific AI
cluster. This is a capacity migration, not a provider-level transfer of an
existing Kubernetes node group.

## Source retirement result

The following Forge node groups were configured with a zero-node autoscaling
floor and ceiling and verified at zero provider nodes:

| Forge cluster | GPU | Node-group ID |
| --- | --- | --- |
| `forge-eu` | H100 | `mk8snodegroup-e00k99ka2zbhjmaj29` |
| `forge-eu` | H200 | `mk8snodegroup-e00fk0d63vd1mr1879` |
| `forge-eu` | L40S | `mk8snodegroup-e00n35e2ax9e167tcw` |
| `forge-eu2` | H200 | `mk8snodegroup-e02jts3j0nsh1wv7em` |
| `forge-us` | H200 | `mk8snodegroup-u00bfgerr2wwfj2ec1` |
| `forge-us` | RTX6000 | `mk8snodegroup-u00ezk5jswt9kgbksy` |
| `forge-us` | B200 | `mk8snodegroup-u00bcnc82tkp2cbd0s` |
| `forge-uk` | B300 | `mk8snodegroup-e03nym0v2qvj350add` |

The Forge control cluster, CPU utility capacity, storage, registries, database,
and adjacent ARCHV/NIMS experimental clusters were not scaled down.

## Scientific AI target

| Field | Accepted value |
| --- | --- |
| Project | `project-e00rene` |
| Cluster | `k8s-inference-h100` / `mk8scluster-e00j5z9te7x5dd9g6a` |
| Region | `eu-north1` |
| Terraform pool | `l40s-1x` |
| Node-group ID | `mk8snodegroup-e00dkjxp9gf3gd7yhf` |
| Platform / preset | `gpu-l40s-a` / `1gpu-16vcpu-64gb` |
| Capacity | regular, minimum 8, maximum 16 |
| Driver / OS | managed `cuda13.0` / `ubuntu24.04` |
| Shared filesystems | model cache and reference data enabled |
| Local NVMe | unavailable / disabled |
| Accelerator class | `nvidia-l40s-48gb` |

Provider and Kubernetes verification both reported 8 Ready nodes and 8
allocatable GPUs. Live Kubernetes measurement on all eight nodes reported
`15900m` allocatable CPU and a minimum of `58488 MiB` allocatable memory per
node; that measured value, its node-group identity, capture time, and source
payload digest are recorded in the deployment `terraform.tfvars` for Kueue
core-resource admission.

## Placement boundary

The new pool is advertised as a distinct accelerator flavor. Existing Apps
and scientific execution stages remain explicitly eligible for their existing
H100 pools; this migration does not reinterpret H100 snapshots, compiled
caches, or acceptance evidence as L40S-compatible. A model may be assigned to
`l40s-1x` only after an exact image/model/GPU qualification and any required
cache or snapshot artifact has been produced for the L40S ABI.

## Validation

- The single-file configuration passed root and all three stage validations.
- Nebius compatibility preflight passed for Kubernetes 1.35,
  `gpu-l40s-a`, `1gpu-16vcpu-64gb`, Ubuntu 24.04, and managed CUDA 13.0.
- The reviewed infrastructure plan created only the L40S node group and
  updated generated accelerator contracts; it replaced or deleted no cloud
  infrastructure.
- Terraform created the node group and propagated the new resource flavor,
  capacity totals, model-controller envelope, and admin capacity contract.
- Post-apply checks cover provider node count, Kubernetes Ready/GPU totals,
  L40S labels and taint, Kueue resource flavor, platform rollout health, and a
  final no-change plan.

