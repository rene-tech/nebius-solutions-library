# Controller-consumable fixtures

Every document here was generated, not hand-written. The reference-data
documents come from the reference-data producer's own code path, and the stage
receipts come from this runtime's own composers, so a controller test that binds
to these fixtures is binding to real behaviour.

Regenerate the command contract with `python3 build.py contract` and these
fixtures with `python3 fixtures/generate.py`. The generator uses the
reference-data producer from this repository; `FS2_AF3_PRODUCER_MODULE`
overrides that path when testing against another checkout. The tests fail if a
fixture drifts from the implementation.

| File | Produced by | Use |
| --- | --- | --- |
| `reference-terminal-receipt.json` | `reference_data.py` `_terminal_receipt` | feed a consumer a published bundle without waiting for a real publication |
| `reference-published-manifest.json` | the manifest the receipt binds | its canonical SHA-256 is the receipt's `content.manifest_sha256` |
| `preprocess-reference-data.json` | this runtime's transform | the `reference_data` block of a preprocess request |
| `data-stage-receipt.json` | this runtime, `mode: data` | assert the CPU-stage envelope and argv |
| `inference-stage-receipt.json` | this runtime, `mode: inference` | assert the GPU-stage argv and parameter identity |
| `failure-receipt.json` | this runtime's fail-closed path | assert the `FAIL` envelope and exit code 2 |

The tree behind the reference fixtures is a small synthetic three-file tree, not
the real 630 GB bundle, so the digests are fixture digests. They are real
digests of that real tree, computed by the producer; they are not the digests of
the published AlphaFold 3 bundle, which does not exist yet.

`failure-receipt.json` is the envelope a controller must treat as terminal: a
`FAIL` status with exit code 2 means a binding or identity requirement was not
met, and retrying without fixing the binding will fail identically.
