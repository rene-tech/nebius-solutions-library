# Live admin artifact-reference browser check

Helm 227's actual admin console passed the six-record browser check: four
complete large-file transfers and two interrupted transfers belonging only to
the qualification owner. Chromium 149 exercised the normal deployed UI and
unmodified server responses; no application code or deployment was changed.

All displayed sizes, artifact IDs, expected/observed SHA-256 values and
complete/verified labels matched the exact server detail. Every copy action
produced **307 bytes of reference JSON**, not a 321 MB or 466 MB artifact.
There were no artifact-content requests and no raw artifact-body display.
The [receipt](receipt.json) links seven cropped metadata-only screenshots and
the complete sanitized browser observations.

Complete transfers say “Stream complete” and “Verified size and SHA-256”.
Interrupted transfers show **25,165,824 / 465,681,353 bytes**, “Stream incomplete”
and “Not verified — incomplete or mismatched stream”. These delivered counts
describe the ASGI send boundary, not bytes acknowledged by the remote reader.

Two nonblocking UX limitations remain explicit. These artifact debug records
have no operation/model attribution, and the UI has no owner filter or direct
exchange route. The release owner approved one-microsecond timestamp windows;
each list returned exactly its known owned UUID. Also, complete rows retain a
standalone “Disconnected” label, while correctly stating “succeeded”, “Complete”
and verified size/hash. No success was mislabeled as failed.

The live JavaScript SHA-256 is
`d77a71bc7b9835b3b32f4a281e95a6a5f963349267180b3d9d65c931757e6a5f`,
identical to the tested production build. Exact backend/admin image and source
identities are in the receipt. An initial private browser-guard parser error is
retained as a harness failure; the unchanged deployed UI passed after correction.

The temporary browser was closed, its operator session deleted with HTTP 204,
and its private cookie file removed. No credentials, headers or scientific
payloads appear in the screenshots. This qualifies the bounded operator view
and copy behavior, not general browser UX or whole-platform readiness.
