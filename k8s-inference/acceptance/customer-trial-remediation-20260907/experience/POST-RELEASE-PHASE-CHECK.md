# Bounded phase-duration regression check

Executed on `5f5061b28ee71a59432492a1bdf6106428a85367`: the first attempt was
blank before sign-in; a separately authorized second attempt passed. Both attempts and
the intervening unauthenticated diagnostic are preserved in
[phase-followup-20260907.json](phase-followup-20260907.json). The original blank-page
cause remains unknown. For later releases, run only after the release owner confirms
the exact deployed commit.
This reuses the real-browser preflight and existing operator sign-in; it submits no model
requests, changes no policies, and does not save credentials or browser storage state.

From `k8s-inference`, substitute the release owner's full 40-character commit and a new
private output directory (never overwrite earlier evidence):

```sh
node acceptance/customer-trial-remediation-20260907/experience/admin_preflight.cjs \
  /home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/final-stack-output.json \
  /home/tux/.local/state/k8s-inference-dual-acceptance/h100/releases/trial-customer-remediation-20260907/experience/phase-followup-r01 \
  --phase-regression --source-commit=FULL_DEPLOYED_COMMIT
```

The single retained Protenix operation is `18efdb3c-fa99-42a7-9a39-876d22a2021b`.
The check requires all of the following:

- API restore duration **3.946846 seconds**, source `lifecycle-signal-boundaries`,
  evidence `estimated` (application-observed timestamps, not downgraded to ingestion timing
  or overstated as device instrumentation).
- Browser restore card **3.95s estimated**, with the same source. The API's unrounded
  value remains in the receipt; the UI's two-decimal formatting is intentional.
- Separate `Cluster context checked` and `Run data observed` timestamps, plus explicit
  wall-time-union/non-additive phase wording.
- Completed immutable result remains available and fresh; occupied GPU accounting
  reconciles without changing the accepted ledger.
- A real browser download of the structure has the expected SHA-256 and byte length.
- No unexpected browser/server error; browser closes on success or failure.

The supplied commit is provenance from the separate Terraform/release verification,
not an independent image-identity assertion by this helper. Raw screenshots and API
receipts stay private. Failure receipts are preserved; do not silently retry a failed
acceptance attempt. This is a targeted gate, not a substitute for the later full cohort.

Offline helper checks (no browser or network):

```sh
node --test acceptance/customer-trial-remediation-20260907/experience/test_admin_preflight.cjs
```
