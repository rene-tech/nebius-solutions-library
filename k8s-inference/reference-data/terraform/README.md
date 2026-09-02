# Standalone reference-data Terraform module

This opt-in module is intentionally not wired into the shared root deployment.
It lets the integration owner bind or create a versioned private Nebius Object
Storage bucket, create a dedicated Kueue CPU lane, enforce private-MSA egress,
and optionally expose the shared-filesystem readiness API/Prometheus metrics.

The caller must provide the existing cluster's Kubernetes provider and the
same Nebius project/region contract. The module fails when the object endpoint
region differs from the cluster. It never creates GPU capacity or assumes local
NVMe. The shared host path must already be mounted on CPU and eligible GPU nodes
and pre-created writable by uid/gid 1000; this module does not run a privileged
permission-changing workload.

`create_object_bucket=false` is the safe integration default. The named bucket
must be private and versioned by its external owner. When true, the module
creates a versioned task-owned bucket but deliberately creates no broad editor
membership, access key, or Kubernetes Secret. Supply a narrowly scoped existing
Secret only when rendering staging/preprocessing Jobs.

Private preprocessing has default-deny egress. Set exact
`object_storage_egress_cidrs` for the private S3 endpoint/proxy. Public source
staging and public MSA egress are separate explicit opt-ins; enabling one does
not enable the other. When the optional status service is enabled, ingress is
limited to `status_ingress_namespaces` (by default `fs2-observability` and
`fs2-system`); it has no public ingress.
