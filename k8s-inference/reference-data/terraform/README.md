# Integrated reference-data Terraform module

This module is called by `stages/workloads/reference_data.tf`. The root facade
passes it the infrastructure-owned versioned private Nebius Object Storage
bucket, dedicated mounted filesystem, tainted CPU-pool contract and MysteryBox
access-key reference. It owns the isolated `fs2-reference-data` namespace and
must never target the shared `fs2-data` database namespace. It creates a
dedicated Kueue CPU lane, enforces private-MSA egress, optionally exposes
readiness metrics and can submit the official AlphaFold3 staging Job.

The caller provides the existing cluster's Kubernetes and Nebius providers and
the same region contract. The module fails when the object endpoint region
differs from the cluster. It never creates GPU capacity or assumes local NVMe.
Infrastructure mounts and prepares the dedicated host path on the reference
CPU pool and eligible GPU nodes as uid/gid 1000.

The module never creates cloud storage or broad IAM membership. It consumes the
least-privilege access-key identity from infrastructure, fetches its secret
ephemerally from MysteryBox and uses the Kubernetes provider's write-only
Secret field so credential material is not persisted in Terraform state.

Private preprocessing has default-deny egress. Set exact
`object_storage_egress_cidrs` for the private S3 endpoint/proxy. Public source
staging and public MSA egress are separate explicit opt-ins; enabling one does
not enable the other. When the optional status service is enabled, ingress is
limited to `status_ingress_namespaces` (by default `fs2-observability` and
`fs2-system`); it has no public ingress.
