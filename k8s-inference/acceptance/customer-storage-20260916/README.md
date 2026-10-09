# Customer storage — final live acceptance

## Outcome: passed on 2026-09-18

Customer S3 storage is reconciled and live-qualified in the Scientific AI H100
cluster after the project IAM policy quota was resolved. Every eligible active
user has a ready 5,000,000,000-byte bucket and a stable per-user S3 identity.
Shared-tenant and private-user modes, direct S3 access, the authenticated public
API, the admin UI, disable/re-enable, and provider byte-limit rejection all
passed against the live service.

This acceptance did not change model deployments, GPU/node capacity, LibreChat,
BioIR, customer quotas, cloud limits, or Stockholm's disabled storage policy.
No customer key was rotated or revoked, and no customer data was deleted.

## Final inventory

| Platform tenant | Principal | Bucket | Mode/result |
| --- | --- | --- | --- |
| fs2-h100 | fs2-h100-operator-handover | `fs2-data-bucket-c298e54c85186c77820b93ee` | Ready, 5 GB; preserved legacy name |
| kopra | kopra | `fs2-kopra-b559903873831e20` | Ready, 5 GB shared tenant |
| rene | rene | `fs2-data-bucket-081a89fb4b12bf2194ad340d` | Ready, 5 GB; preserved legacy name and existing data |
| rene-tech | dual-acceptance-h100-operator | `fs2-rene-tech-0b22504be9db9b25` | Ready, 5 GB shared tenant |
| robotics | timmothy | `fs2-robotics-9bbacec25c21ce81` | Ready, 5 GB shared tenant |
| tenant-academic | terraform-academic-scientific-client | `fs2-tenant-academic-079968c4be6437f7` | Ready, 5 GB shared tenant |
| tenant-academic | terraform-bootstrap-client | `fs2-tenant-academic-079968c4be6437f7` | Ready; same bucket, distinct S3 identity |
| tenant-e00f3wdfzwfjgbcyfv | terraform-bootstrap-client | `fs2-tenant-e00f3wdfzwfjgbcyfv-6879e07b58342034` | Ready, 5 GB shared tenant |
| stockholm | team-01 through team-20 | None | Intentionally disabled and excluded |

New buckets use the readable `fs2-<tenant>-<optional-user>-<id>` convention.
Nebius bucket names are immutable. The two legacy buckets were deliberately not
replaced or migrated: preserving their identities and objects is safer than a
cosmetic rename, and Rene's bucket contains retained medical demo assets.

## Live evidence

The committed `verify.py` suite ran from
`2026-09-18T09:10:47.700980Z` to `2026-09-18T09:14:28.346078Z` and passed all
seven checks:

1. All eligible active users ready at 5 GB; Stockholm excluded with no bucket.
2. Cross-tenant list access denied in both directions.
3. Two users shared one tenant bucket with distinct keys; a 17 MiB multipart
   upload/download passed SHA-256 verification.
4. Alice and Bob received different private-user buckets; cross-user reads and
   writes were denied.
5. Two inference keys for one user resolved to the same S3 identity through
   `/v1/storage` and `/v1/storage/credentials`; inference auth could not call an
   admin storage route.
6. Disabling fixture users deactivated direct S3 access.
7. Re-enabling Alice preserved the bucket, access key ID, and secret; S3
   read/write returned; the fixture was disabled again afterward.

The committed `verify_quota.py` suite ran from
`2026-09-18T09:10:47.857841Z` to `2026-09-18T09:12:25.294270Z`. A disposable
1 MiB bucket rejected a 2 MiB write with HTTP 400 `BucketMaxSizeExceeded`.
Nebius documents that rapid writes can temporarily overshoot `max_size_bytes`,
so the check uses paced bounded writes and requires that exact provider error.
It never lowers a customer's allowance.

Live browser acceptance passed on the Admin → Users → user → Data storage panel:
the operator could reveal populated S3 connection details, hide them again, and
the page produced no browser-console errors. No secret value was captured in the
evidence. The admin endpoint returned HTTP 200 with TLS verification enabled.

Post-test cleanup was verified:

- Alice, Bob, and the quota-overflow fixture are disabled with zero active
  inference keys.
- All task-owned objects were deleted; empty fixture buckets are retained so the
  tests can safely adopt and reuse them.
- All 20 Stockholm teams still report disabled storage and no bucket.

## Live deployment

- Project: `project-e00rene`
- Region: `eu-north1`
- Cluster: `mk8scluster-e00j5z9te7x5dd9g6a`
- Namespace: `fs2-system`
- Working context: `fs2-remediation-sandbox2`
- Gateway/controller image:
  `fs2-serve-control-plane@sha256:ab2f802727b29b60a301870771610ec5b9851e2d92f837ee422c52b7fd78f214`
- Admin image:
  `fs2-serve-admin-console@sha256:6428b3500d2dd6784c0ff2308335b3d2962f725434cc5e7ea8fd5098dbf21659`

The old storage-only kubeconfig in the secure handoff references a deactivated
public key and is intentionally not used. Live verification used the existing
`sandbox2` cluster handoff; no cloud key or role was changed.

The provisioner Terraform state remains private at
`/home/tux/secure-handoff/scientific-ai-customer-storage-20260916/provisioner`.
It contains private key material and must never be committed or disclosed. If
the canonical workloads Terraform is enabled for this retained cluster, first
move/adopt those six provisioner resources and import the existing Kubernetes
Secret; do not create a second provisioner identity.

## Re-run

Read each script's mutation scope before execution. Both use only named fixture
users, revoke temporary inference keys, delete task-owned objects, and leave the
fixtures disabled.

```bash
python3 acceptance/customer-storage-20260916/verify.py \
  --kubeconfig /path/to/current/kubeconfig \
  --context current-context \
  --origin https://admin-origin

python3 acceptance/customer-storage-20260916/verify_quota.py \
  --kubeconfig /path/to/current/kubeconfig \
  --context current-context \
  --origin https://admin-origin
```

`verify_existing.py` remains the smaller non-provisioning smoke test for an
already-ready user. It is not a substitute for either final suite above.
