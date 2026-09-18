# Public-edge boundary enrollment handoff

The checked-in trust registries are deliberately empty development gates. They
must not be promoted. A production image is built only through the
`production-runtime` target, whose build-time gate requires nonempty,
independently supplied current/next public trust registries and their exact
SHA-256 digests.

The enrollment compiler is an offline tool. It is not a runtime authority and
does not compare its own executable digest with the accepted snapshot-authority
digest. It authenticates the signed acceptance generation, then reads the
separately supplied target `public-edge-snapshot-authority` executable as one
sealed regular file and compares those exact bytes with
`snapshot_authority_executable_sha256` from that acceptance. This avoids
conflating compiler and runtime identities.

## Reproducible tool output

Use an owner-approved Go builder image pinned by digest and a new output path:

```text
components/public-edge-boundary/scripts/build-enrollment-tools.sh \
  /absolute/new/enrollment-tools \
  registry.example/go-builder@sha256:<64-lowercase-hex>
```

The output contains the compiler, assembler, the exact target
snapshot-authority runtime binary, and a digest manifest. The script refuses to
reuse an output directory and never removes retained output. The owner must
independently bind the target runtime digest and production image digest into
the signed acceptance and image provenance before compilation.

## Compile, sign, and assemble

1. Prepare canonical enrollment input
   `fs2-serve.nebius.ai/public-edge-boundary-enrollment-input/v1`. Every source
   is an absolute protected regular file; no private key bytes are placed in
   the signed custody payload.
2. Run the compiler with exactly:

   ```text
   public-edge-enrollment-compiler \
     ACCEPTANCE_TRUST.json \
     ACCEPTANCE_ENVELOPE.json \
     /absolute/enrollment-tools/public-edge-snapshot-authority \
     ENROLLMENT_INPUT.json \
     /absolute/existing-protected-output
   ```

   The compiler authenticates the acceptance, validates the target runtime
   digest and snapshot-authority config, and publishes only no-replace,
   content-addressed source artifacts, five canonical custody payloads, and one
   `enrollment-handoff-<sha256>.json`.
3. An external owner signer with the
   `platform-security-public-edge-boundary-custody-enrollment` role signs each
   exact custody payload under
   `fs2-serve.nebius.ai/public-edge-custody-enrollment-envelope/v1`. It returns
   these exact filenames in a protected directory:

   ```text
   collector-custody-envelope.json
   native-authority-custody-envelope.json
   settlement-custodian-custody-envelope.json
   snapshot-authority-custody-envelope.json
   webhook-custody-envelope.json
   ```

4. Run the assembler:

   ```text
   public-edge-enrollment-assembler \
     ACCEPTANCE_TRUST.json \
     /absolute/compiler-output/enrollment-handoff-<sha256>.json \
     /absolute/signed-envelopes \
     /absolute/existing-protected-assembly-output
   ```

   It re-authenticates every signature, requires byte equality with the exact
   compiler payload, verifies that every projected Secret key maps to one
   retained source artifact, and publishes a no-replace
   `custody-enrollment-values-<sha256>.json`. Passing this file as a Helm values
   layer causes the chart to materialize the five immutable custody ConfigMaps;
   the handoff's per-component `projected_inputs` is the exact key-to-artifact
   contract for the separately custodied immutable Kubernetes Secrets named by
   `inputSecrets`.

The assembler does not create or transmit Kubernetes Secrets and neither tool
holds an owner signing key. Cluster application, external signing, Secret
creation, and production image promotion remain separate owner-controlled
operations.
