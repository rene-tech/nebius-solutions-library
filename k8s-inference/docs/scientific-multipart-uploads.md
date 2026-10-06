# Large scientific inputs (candidate, not deployed yet)

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
   do not send the platform API key to object storage. Four concurrent parts
   use at most 256 MiB in the supplied Python client. Refresh part handles if
   their 15-minute transfer lifetime elapses; the user's API key does not expire.
4. `{"action":"complete","multipart_upload_id":"..."}` lists the provider's
   parts and verifies contiguous numbering and expected total length before
   completion. It is **not** artifact finalization.
5. Call the existing `POST .../{upload_id}:finalize` with `operation_id`.
   The platform streams the completed object, independently checks its full
   SHA-256, size, type and compression, and returns the immutable artifact ref.

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
The candidate keeps current inline-body limits unchanged.
