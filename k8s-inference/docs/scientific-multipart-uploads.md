# Large scientific inputs

Deployed 2026-10-06. A real 5,873,612,847-byte H5AD upload passed public API
finalization, including resuming 13 already-transferred parts after a client
interruption and independent full SHA-256 verification.
The final-release retest also transferred **14,326,834,896 bytes**: 232.85 s
multipart transfer plus 113.54 s independent finalization. This is measured
transport evidence, not a promise that every input of the configured 25 GiB
maximum has been qualified.

Reserve an upload with the existing `POST /v1/scientific-artifacts/uploads`,
including model ID, whole-file SHA-256, exact byte length, media type and optional
compression. Reuse the same idempotency key on retries. Small-file PUT remains
compatible. The response adds `multipart_path` for large inputs.

POST JSON to that path, always including `operation_id`:

1. `{"action":"start"}` creates or finds the unfinished upload of this exact
   immutable intent. Retain `multipart_upload_id` and `part_size_bytes` (64 MiB).
2. `{"action":"list","multipart_upload_id":"..."}` reports stored part
   numbers, sizes and ETags after interruption. It does not infer whole-file success.
3. `{"action":"parts","multipart_upload_id":"...","part_numbers":[1,2]}`
   signs at most 128 requested parts. PUT each part's bytes directly to its URL;
   do not send the platform API key to object storage. The supplied client
   streams four concurrent parts in 1 MiB chunks; it does not hold four complete
   64 MiB parts in memory. Refresh part handles if
   their 15-minute transfer lifetime elapses; the user's API key does not expire.
4. `{"action":"complete","multipart_upload_id":"..."}` lists the provider's
   parts and verifies contiguous numbering and expected total length before
   completion. It is **not** artifact finalization.
5. Call the existing `POST .../{upload_id}:finalize` with `operation_id`.
   The platform streams the completed object, independently checks its full
   SHA-256, size, type and compression, and returns the immutable artifact ref.

The chart applies `httpRoute.artifactTransferTimeout` (default **900s**) only
to `/v1/scientific-artifacts/uploads` and `/v1/artifacts`. Ordinary model/MCP
requests keep their 40s bound and speech retains its separate stream timeout.
The original 40s rule truncated the first 14.33 GB verification response;
uploaded data survived, and finalization was recovered without retransmission.
The fresh-upload retest above passed after the scoped route correction. Public
website/API routing checks passed before and after it. No quota, API-key lifetime,
Gateway hostname or unrelated route was changed.

The Python client persists successful byte-transfer completion before waiting
for finalization. After a lost response it finalizes the existing upload; it
also reuses an already verified upload when starting from a new local run
directory. Reuse receipts/idempotency keys; do not overwrite a write-once key.

To abandon the transfer, POST `action: abort` with its multipart ID, then cancel
the input-upload operation using the normal operation API. Failed transfers
retain parts for resumption; retries do not require another bucket or user.
S3 holds durable part state, while PostgreSQL retains the tenant-owned artifact
intent. Provider upload IDs never authorize another tenant's storage key.

The `qualify_api.py` single-cell client exercises this path on actual large
`.h5ad` inputs. Never place the whole H5AD file or its base64 representation in
an MCP argument. API/MCP submissions carry the finalized artifact manifest.

This is standard S3 multipart behavior using the existing boto3 dependency:
[initiation](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/create_multipart_upload.html),
[completion](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/complete_multipart_upload.html).
The implementation keeps current inline-body limits unchanged.
