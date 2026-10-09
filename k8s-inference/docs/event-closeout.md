# Event handoff and closeout

An HTTP 200, healthy Pod or successful SDK example does not qualify the
customer's installed LibreChat and skills. Follow `CUSTOMER_RELEASE_POLICY.md`:
use the exact release, normal participant policy and actual supported client;
follow every accepted run to a checked result, including downloads, mixed-model
concurrency and batch waiting. Keep failures and waiting times in the report.
One isolated LibreChat per user remains the supported architecture; a shared
tenant bucket does not imply a shared client instance.

## Before and during an event

Record the advertised capability matrix, exact images/configuration/client,
participant grants and concurrency, fixtures/validators and the event window.
Use a disposable participant-equivalent identity. Do not reuse an old event's
credentials or an administrator's broader key. Agree key expiry with the event
owner; the existing expiry setting is sufficient, no new global limit is added.

Run two unchanged-release cohorts through the actual client. A partial pass is
labelled with its model/client/workload coverage, never simply "working".
Check batch admission, queue progress, reconnect/resume, cancellation and output
access while other work continues. Capacity waiting is reported separately from
software failure, but remains part of the participant experience.

Use terminal model outcomes for success, not MCP transport status. Separate
discovery, transport/session GETs and result polling from inference requests.
Check that alert delivery has a real receiver and a delivered controlled test;
loaded Prometheus rules alone do not prove anyone will be notified.

Do not bill admission reservation counters as GPU consumption. Distinguish queue,
startup/restore/load, active, resident-idle/cooldown and unknown time. Shared
serving attribution requires the actual responding runtime, not a guess from
the available Pods. Preserve quality/coverage and do not invent historical data.

## Export before retirement or retention expiry

Capture the event report while operation and terminal facts still exist. Keep a
protected archive of IDs, timings, attempts, request/operation correlation,
artifact hashes, errors and usage quality, plus release and participant policy.
Keep credentials and captured payloads out of source control. Store payload-free
lessons and checksums in Git. A log archive cannot reconstruct missing terminal
GPU measurements simply because it contains HTTP exchanges.

The Stockholm reference exporter is
`acceptance/stockholm-closeout-20260920/archive.py`. It is deliberately scoped to
Stockholm, uses a repeatable-read snapshot and exports only columns readable by
the existing role. It never broadens database privileges. Its verified manifest
and previous acceptance receipts must exist before destructive cleanup.

## Delete a bucketless event tenant

`DELETE /admin/api/v1/tenants/{tenant_id}` requires an admin session and JSON:

```json
{
  "archive_sha256": "<64-character SHA-256 of the verified archive manifest>",
  "expected_users": 20,
  "expected_keys": 22
}
```

The operator verifies the archive; the server records its checksum, not its
contents. Counts must match current configured users and retained keys. The
transaction refuses active operations, buckets/storage credentials, tenant-owned
deployments or operator accounts: their separate offboarding is not implemented
by this endpoint. Do not use it as a general cloud-resource deletion tool.

It deletes inference-account rows and storage policy, revokes all retained keys,
and writes a retirement marker. Historical token records remain as foreign-key
anchors for runs, usage and debugging, but cannot authenticate and are excluded
from active key/user discovery. Historical operations cannot recreate accounts.
Issuing keys or configuring users under the retired tenant is rejected. Repeating
the same archived retirement is idempotent; a different archive is a conflict.

This is active-identity removal, not erasure of all personal or customer data.
Historical evidence follows its existing retention. No automatic undo endpoint
is provided; restoration requires an explicit operator decision and the private
archive, and old credentials must not be re-enabled as an accidental side effect.

Verify empty user/key APIs, rejected old authentication, unchanged retained run
IDs and unaffected other tenants. Check normal maintenance, public readiness,
admin availability and a normal-user operation on the exact deployed release.
Archive the closeout receipt and disposition remaining work explicitly.

Schema 0034 advances the exact PostgreSQL migration manifest. An image that only
knows schema 0033 is not a valid automatic rollback; recover with a compatible
forward fix. Do not erase the migration ledger or history to force a rollback.
