# Public aging Apps r02: test-client tenant mismatch

The 2026-09-08 campaign ran from 14:26:51.681351 UTC to 14:27:05.996411 UTC
on source `1f4015bcc45064fd0b272bff872956f3df6a42f6`, control-plane image
`sha256:dd403b999f4de5c562aec2eae140e35f4155f7521361bb373fe2a792533cee02`.
The original harness release label was the image prefix `dd403b`; a separate
provenance receipt supplies its full identity without changing original evidence.

The late-bootstrap binding correction worked: both original App UUIDs had
working managed settings. Each was saved with min0/max1, keeping the original
300-second idle and cooldown values and all existing placement/runtime settings.
Both showed zero containers before discovery. Their public summary was still
`Desired`, so this alone was not evidence of a fully reconciled Cold state.

The harness incorrectly derived the temporary key owner from the separate
`tenant-academic` scientific access token. These native serving Apps belong to
`tenant-e00f3wdfzwfjgbcyfv`, with Tenant visibility. Accordingly, both scoped
`GET /v1/models` requests returned HTTP 200 with an empty list. The registry's
existing exact-tenant filter explains this result independently of readiness.
No authentication, tenant policy, route authority or qualification flag was
changed to bypass that filter.

No inference operation was submitted. Both temporary keys were revoked, their
subsequent requests returned 401, and the client exited 1 with no retries. The
new Apps remain min0/max1 as authorized. Original receipts remain private under
`releases/aging-20260908/public-apps-r02`; r01 remains a distinct backend failure.

For a fresh authorized cohort, the harness now uses the existing normal inference
owner, verifies the exact serving tenant before any mutation, and requires two
observed Cold-plus-zero samples. It records settings-to-Cold convergence
separately from request cold-start timing and avoids unnecessary writes when the
requested min0/max1 settings already match. Ten focused offline checks passed,
including the tenant-mismatch no-mutation and Cold-observation regressions.
This does not turn r02 into a successful qualification.
