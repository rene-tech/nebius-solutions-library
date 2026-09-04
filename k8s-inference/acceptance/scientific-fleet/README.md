# Scientific fleet public acceptance runner

`run_acceptance.py` qualifies one model-owned canary through the same public
HTTP and bearer-token boundary available to a customer. It does not read
Kubernetes, object storage, Terraform state, or registry credentials and it
does not change a model route.

The runner reads an activation fragment's `model_id` and `public_fixtures`.
Paths are relative to the `k8s-inference` directory and must stay inside it:

```json
{
  "public_fixtures": {
    "request": "models/example/activation/public-request.json",
    "supporting_inputs": [
      {
        "role": "request-input-manifest",
        "path": "models/example/activation/input-manifest.json",
        "encoding": "canonical-json-newline"
      },
      {
        "role": "manifest-artifact",
        "name": "target_sequence",
        "path": "models/example/activation/target.fasta",
        "encoding": "raw"
      }
    ]
  }
}
```

For a direct input, declare exactly one `request-input-manifest` whose bytes
match the request's `input_manifest` digest and omit `manifest-artifact`
entries. For a scientific manifest, that declaration is the canonical
manifest template and each manifest entry has one `manifest-artifact` input.
`name` is recommended and binds to the manifest entry name; a declaration
without `name` is accepted only when its digest identifies exactly one entry.
The runner uploads every exact artifact, replaces template pointers with the
returned immutable public pointers, canonicalizes and uploads the resulting
manifest, then replaces the request pointer before submission.

Use a token that can upload and invoke the selected model and read its
operation result. The token is accepted only through an environment variable:

```bash
export FS2_INFERENCE_TOKEN='...'
python3 acceptance/scientific-fleet/run_acceptance.py \
  --endpoint https://inference.example \
  --activation-fragment models/example/activation/fragment.json \
  --run-id qualification-20260904-01 \
  --receipt /secure/run/qualification-20260904-01.json
```

The output is mode `0600` canonical JSON. It contains only the endpoint host,
model and operation identities, API-provided timestamps, cold-start/runtime
attribution, queue decisions and admissions, execution identity, attempt
identity, and artifact digests. Bearer tokens, principal and tenant identity,
cookies, presigned handles, signed URLs, and object-store locations are never
copied from responses. An existing receipt is not overwritten unless
`--overwrite` is explicit.

The command returns nonzero and writes no receipt when an upload identity does
not match its bytes, the route rejects admission, the operation misses its
deadline or terminates unsuccessfully, the terminal result is inconsistent,
or semantic validation did not pass.

Run the offline fake-HTTP acceptance suite with:

```bash
acceptance/scientific-fleet/run_checks.sh
```
