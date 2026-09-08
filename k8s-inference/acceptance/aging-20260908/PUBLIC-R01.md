# Public aging Apps r01: blocked before inference

Runtime release `faef58e16ecc266d1c42ee708161dafde748f0a9`, deployed by Terraform
source `aecbe75ae1caa73f5d6f959432f7ab7ca3f44953`. The root verified both native
ModelDeployment resources were Ready before starting this campaign.

The public admin campaign ran on 2026-09-08 from 14:06:15.161064 UTC to
14:06:17.003303 UTC. Both canonical Apps were listed successfully, but their
Settings returned HTTP 200 with `serving: null`, all worker/settings capabilities
false, and `unsupported_reason: "No managed ModelDeployment is registered for
this app."`

| Model | App ID | Logical operations |
| --- | --- | ---: |
| Clinical PhenoAge | `38aad847-9010-51d0-bed7-bcb404bc755d` | 0 |
| AltumAge | `197fa990-6f65-539d-a1d8-240877ce861b` | 0 |

No PATCH, inference request, model key creation, Kubernetes write, node or limit
change occurred. The admin session was closed. No test clients remain. Neither
model failed inference; inference was never reached. This is a real Apps
integration failure, not a clean public qualification or an image failure.

Read-only code diagnosis found that initial catalog registration can seed an
App before its ModelDeployment exists. `seed_defaults()` then skips any route
already present, retaining the unbound `deployment_name: null` forever. That
explains how Kubernetes can be Ready while Apps cannot manage the same model.

Private original HTTP/settings/outcome receipts remain under
`releases/aging-20260908/public-apps-r01`. The initial harness surfaced a
`TypeError` when dereferencing this missing serving revision; its precondition
now names the same recorded condition `managed_app_settings_unavailable`.
The original r01 receipts are unchanged. No retry has been made; a corrected
release and explicitly authorized fresh cohort are required.

The corrective Apps change attaches the exact canonical ModelDeployment when it
becomes available after registration, on both reseeding and normal Apps reads.
The database attachment is atomic and idempotent across API replicas. It keeps
the existing App UUID, display/access settings and history, advances its revision
once, and never rebinds an existing deployment or a different source/namespace.
There is no manual production database repair or new permission requirement.

Before release, 37 focused Apps/API/scientific/observability tests and 11 actual
PostgreSQL tests passed, including concurrent attachment, post-startup API
bootstrap and metadata preservation. The corrected public harness passed its
eight offline checks. Strict source typing, lint and unchanged scientific recipe
identity checks passed. This is implementation evidence, not a replacement for
the failed r01 result or the pending fresh live acceptance.
