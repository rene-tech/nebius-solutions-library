# JobSet Kubernetes 1.35 qualification

## Decision

FS2 uses JobSet v0.12.0 with Kueue v0.17.8 on Kubernetes 1.35. This is an
explicit FS2 qualification, not a claim that the JobSet project publishes a
1.35 E2E lane for v0.12.0.

The immutable installation identities are:

- JobSet release: v0.12.0, tag commit
  f22e565ac3cc3265c7c45ef67ecffdc689af5d77.
- JobSet chart:
  oci://registry.k8s.io/jobset/charts/jobset@sha256:02808a890a0b0e03a1d3bf5959e2f562b3b47c15e446bbba358c1d24e1f81b24.
- JobSet chart archive SHA-256:
  bd3503757561d93aa14f35fccab76ca417d17e14984aed9f69c9ab068d40980a.
- JobSet controller:
  registry.k8s.io/jobset/jobset:v0.12.0@sha256:e75536f1135b7bb2f19f8c3b620782fbdd9091d73398e3a272f9a5fed322980d.
- Kueue release: v0.17.8, tag commit
  818686e072da52af3564ba21f37f826fe05190af.
- Kueue controller:
  registry.k8s.io/kueue/kueue:v0.17.8@sha256:cecba825d0b0feab9bed2835efe2eb8d825512f1616c8762ab80c53f2ea6afe6.

## Primary upstream evidence

- The [JobSet v0.12.0 release](https://github.com/kubernetes-sigs/jobset/releases/tag/v0.12.0)
  is the current stable release and records its Kubernetes dependency updates,
  including 0.35.1 and 0.36.0.
- The [JobSet v0.12.0 README](https://github.com/kubernetes-sigs/jobset/blob/v0.12.0/README.md)
  publishes E2E lanes for Kubernetes 1.32, 1.33, and 1.34. Those remain
  upstream_tested_kubernetes_minors in the Terraform contract.
- The [Kueue v0.17.8 README](https://github.com/kubernetes-sigs/kueue/blob/v0.17.8/README.md)
  publishes E2E lanes for Kubernetes 1.33, 1.34, and 1.35.
- Kueue's [JobSet integration guide](https://kueue.sigs.k8s.io/docs/tasks/run/jobsets/)
  defines the queue label and JobSet resource accounting used by both canaries.
- Kueue v0.17.8 compiles its JobSet integration against
  sigs.k8s.io/jobset v0.11.1. Both releases serve
  jobset.x-k8s.io/v1alpha2, but that source dependency alone is not runtime
  evidence for the v0.12.0 controller; the two canaries below provide it.

## Qualification matrix

| Environment | Kubernetes | Test | Result |
| --- | --- | --- | --- |
| Local Kind | v1.35.0, image digest sha256:4613778f3cfcd10e615029370f5786704559103cf27bef934597ba562b269661 | Install the exact JobSet chart/CRD/controller, then the production Kueue release and configuration; admit and complete a two-replica CPU JobSet; delete it and prove its Workload is garbage-collected. | PASS 2026-09-03: Workload `jobset-fs2-gang-probe-1fdaf`, two succeeded child Jobs, zero residual Workloads; the Kind cluster was deleted. |
| Managed H100 cluster | v1.35.6 | Terraform-owned install; controller and CRD readiness; Kueue admission and completion of a two-replica CPU JobSet; deletion of a running two-replica cleanup JobSet; zero residual objects. | PASS 2026-09-03: completion and cleanup Workloads admitted at creation; all four CPU Pods ran on the system node; see evidence/kubernetes-1.35-h100.json. |

The live canaries request no GPU and run only on the system CPU node. They use
the existing LocalQueue without editing any LocalQueue, ClusterQueue,
ResourceFlavor, quota, node group, or workload controller. The canary records
canonical ClusterQueue and ResourceFlavor spec hashes before and after and
fails if either changes.

The release verifier reads the CRD, chart metadata, default values, and
controller template directly from the already hash-verified chart archive.
This avoids a second chart resolution and makes the exact bytes checked during
Terraform apply independent of local Helm CLI output behavior.

## Deployment and rollback

The live foundation plan must be reviewed as a closed allowlist. The intended
delta is the existing jobset-system namespace plus these five module-owned
managed addresses:

- module.jobset_controller[0].terraform_data.contract
- module.jobset_controller[0].terraform_data.artifacts_verified[0]
- module.jobset_controller[0].terraform_data.crd_upgrade[0]
- module.jobset_controller[0].helm_release.jobset[0]
- module.jobset_controller[0].terraform_data.api_ready[0]

The module also materializes its read-only external chart data from the
digest-pinned OCI reference. The previous live state has no JobSet release,
controller, CRD, namespace, or JobSet objects.

A rollback first disables new submissions and verifies that no JobSets exist,
then removes the Helm release and module state through the same foundation
state. The CRD installer deliberately has no destroy provisioner because
deleting a CRD can cascade user objects. Restoring the exact prior absence
therefore requires a separately reviewed CRD deletion only after the cluster
reports zero JobSets. No automatic rollback deletes the CRD.

Run the local qualification with:

    terraform -chdir=modules/jobset-controller test
    modules/jobset-controller/scripts/kind-jobset-kueue-integration.sh
    pytest -q tests/test_jobset_kubernetes_135_qualification.py

Run the live canaries only after the Terraform apply and controller readiness
checks. Supply the exact kubeconfig, context, controller names, server version,
namespace, queue, cluster ID, and a task-owned run ID through the environment;
the script accepts no implicit kubectl context.
