# Public aging Apps r03: real HTTP replay failure

Source `1f4015bcc45064fd0b272bff872956f3df6a42f6`, control-plane image
`sha256:dd403b999f4de5c562aec2eae140e35f4155f7521361bb373fe2a792533cee02`.
The client ran from 2026-09-08 14:31:17.752189 UTC to 14:31:29.610923 UTC.

The corrected normal-tenant scoped keys discovered exactly their respective
native model over both HTTP and MCP. Both Apps were observed Cold with no
containers twice before submission. Existing min0/max1 settings already
matched, so this cohort did not make redundant configuration writes.

| Model | Accepted operation | Accepted UTC | Initial HTTP |
| --- | --- | --- | ---: |
| Clinical PhenoAge | `fe10fdcf-1eaf-49dc-a1b3-4fa3d3297a2f` | 14:31:27.301519 | 202 |
| AltumAge | `aab0c233-029f-4a6c-8cac-0a7835fbeaa0` | 14:31:28.095593 | 202 |

Both immediate exact HTTP replays failed with non-JSON responses. The root
correlated API logs with an HTTP 500: replay lifecycle registration used the
new request's trace identity instead of the durable operation's original
trace identity, conflicting with its existing telemetry subject. This is a real
backend idempotent-replay defect, not a scientific model or tenant failure.

The original trace helper recorded its JSON decode exception before HTTP
metadata, so the original replay status/body is missing from those client
receipts; server logs provide the independent status and cause. The helper now
records status, content type, size and SHA-256 before parsing, plus bounded
non-JSON text except for potentially sensitive key disclosures. Twelve offline
checks passed, including both malformed-response regression cases.

No hidden retry or second fixture was submitted. The approved `finally` cleanup
revoked both task keys and verified 401 denial. Existing token-revocation behavior
also cancelled their unfinished operations: PhenoAge at 14:31:28.031191 and
AltumAge at 14:31:29.044026. These elapsed intervals are cancellation times,
**not** cold-start or inference benchmarks. Cold-start/inference measurements
remain unavailable; neither result is semantically qualified by this cohort.

Original receipts remain in `releases/aging-20260908/public-apps-r03`.
Separate read-only recovery uses the existing admin operation projection because
another inference key cannot read the revoked key's operation. Its initial
authorization-hidden 404 is retained in `public-apps-r03-recovery`; the bounded
admin fallback is in `public-apps-r03-recovery-admin`. Recovery does not submit,
cancel, reactivate keys, change grants, or rewrite the failed original cohort.
Natural worker cleanup is recorded separately after its observation completes.

The read-only recovery finished at 14:37:08.807854 UTC with both original
operations still cancelled and both Apps observed Cold with zero containers in
two consecutive samples. The admin session was closed and the recovery client
exited 0; exact owned process checks found no remaining r03/recovery client.
PhenoAge's reusable worker had been observed Ready during cleanup; that is not
evidence of a successful prediction. No manual resource deletion was needed.
