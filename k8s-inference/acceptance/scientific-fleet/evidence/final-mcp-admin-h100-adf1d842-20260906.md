# Final deployed MCP and H100 Configuration acceptance

Passed against deployed source `adf1d8423e9b75afc5ba208baf53e479e7793922`
on 2026-09-06. The [structured receipt](final-mcp-admin-h100-adf1d842-20260906.json)
records exact image identities, timings, artifact digests and private evidence
hashes. No credentials, private resource locations or browser session material
are included in this public report.

## Scientific MCP usability

The real, TLS-verified MCP `2026-07-28` transport advertised and registered
`submit_esmfold2_fast`. The caller invoked that **named model tool**, with the
unchanged canonical request and original idempotency key from the successful
ESMFold2-Fast seed-107 case in the
[live access/policy acceptance](customer-access-policy-h100-8bb53aab-20260906.md).
The immutable `8bb53aab` fixture and retained uploaded input pointer reconstructed
the exact request; no new input or model workload was submitted.

The MCP alias returned the same operation with `reused=true` in **0.527423 s**.
Both `get_scientific_status` and `get_scientific_result` were called through MCP.
The operation remained succeeded, its two original attempt identities remained
unchanged, and the unchanged production acceptance validator checked the full
result's model/runtime identity, input binding and semantic-validation receipt.
The customer HTTP artifact-download route then returned the output manifest
(695 bytes) and a scientific JSON result artifact (602 bytes), with matching
lengths, SHA-256 digests and artifact-hash response headers.

This is a transport, authorization, idempotency and result-delivery proof,
**not a new inference or cold-start benchmark**. No new GPU job was created.
Existing real H100 execution and optimization measurements remain in the
linked model acceptance reports; this replay does not replace them.

An independent final-release discovery check also verified all ten scientific
profiles' advertised aliases in `tools/list`, and Qwen/Cosmos general model
discovery. Tool discovery remained private with zero cache TTL. The academic
and general credentials retained their different licensed-model visibility.

## H100 Configuration browser

The existing authenticated Chromium session loaded `/admin/configuration`
from the deployed admin image. The visible Qwen and Cosmos cards showed:

- `nvidia-h100-sxm5-80gb` capacity and no unsupported-accelerator warnings;
- Qwen minimum/maximum replicas of 1/2 and Cosmos 0/2;
- revision 38, desired/effective state aligned, and no local draft changes.

The browser check made no configuration mutations. The exact existing H100
runtime qualification is now reflected in Configuration; it does not imply
GPU-snapshot or new fast-start-tier qualification. Screenshot and structured
browser evidence are retained privately with hashes in the public receipt.

The release manager's independent authenticated checks returned HTTP 200 for
overview, models, capacity, configuration, scientific policies and scientific
discovery. Overview/models/capacity/configuration warning lists were empty,
including no `unsupported_accelerator_placement` warning. Terraform no-op and
final access-output closure are recorded separately by the release manager.
