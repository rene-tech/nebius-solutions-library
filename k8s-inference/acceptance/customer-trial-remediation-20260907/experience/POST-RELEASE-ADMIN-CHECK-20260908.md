# Prepared admin checks — 2026-09-08

Prepared only. Do not run until root confirms the exact deployed release. Planned source: `bc264980f3fedc33c8fdc6d59c93096d8db9ccaf`; root must update this provenance if the release changes. All standalone checks finish before r03, and no model settings change during r03/r04.

## Existing scientific result

Reuse `admin_preflight.cjs --phase-regression` against the retained Protenix operation `18efdb3c-fa99-42a7-9a39-876d22a2021b`. Require API restore duration 3.946846 seconds, UI 3.95 seconds with the actual boundary source, distinct observation labels, fresh completed artifacts, reconciled GPU accounting and the exact-byte browser download. Save to a new private `admin-phase-r03-preflight` directory, preserving every older failed and passed attempt. See [phase-check instructions](POST-RELEASE-PHASE-CHECK.md).

## Reversible Cosmos setting

The prepared `startup_retention_preflight.cjs` uses the real model-deployment form and existing preview/apply endpoints. It does not invoke inference, drain a model, change replica settings or modify any other model.

- Read and privately preserve the exact current Cosmos spec and revision.
- Require observed **Cold**, desired/ready/available replicas all zero, hot floor zero, the setting omitted/null, and no matching Kubernetes Pod. Otherwise stop before mutation and report to root.
- Start a namespace/model-scoped Pod watch from the initial resourceVersion. Credentials remain in memory; only Pod metadata events are retained.
- Set **Maximum startup retention (seconds)** to 1800 through the actual form, preview and apply. Verify the API spec differs in that field only.
- Immediately restore the original omitted value through **Use default startup retention**, preview and apply in `finally`. Preserve exact original spec equality, not merely equivalent floors.
- Wait for the restored revision to be observed Cold. Require no Pod event or final Pod, and no unexpected browser read failure. Stop the owned watch/browser. If a failure prevents restoration, retain the exact original private spec and report the failure; do not overwrite an unrelated concurrent change.

From `k8s-inference`, only after root's GO:

```sh
node acceptance/customer-trial-remediation-20260907/experience/startup_retention_preflight.cjs \
  /home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/final-stack-output.json \
  /home/tux/.local/state/k8s-inference-dual-acceptance/h100/releases/trial-customer-remediation-20260907/experience/startup-retention-r03-preflight \
  bc264980f3fedc33c8fdc6d59c93096d8db9ccaf \
  /home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig \
  k8s-inference-h100 --execute
```

The two pure helper tests validate the cold-only scope, exact owned-field change and refusal to overwrite another operator; they execute no browser or network calls.

## Access evidence reused, not rerun

Root explicitly approved reusing the [September6 real-browser scoped API-key lifecycle proof](../../scientific-fleet/evidence/customer-access-policy-h100-8bb53aab-20260906.json), because access/key code is unchanged in this repair. At 22:37–22:38 UTC the synthetic viewer key was created (201), discovered the allowed academic scientific models (200), was denied an out-of-scope model (403), was revoked (subsequent401), and its principal was disabled. This is dated retained evidence, not a new release-specific key test. No new identity will be created by these preflights.

## Coordinated cohorts

Once standalone checks pass and Cosmos is restored, signal READY to root. During r03/r04 use `browser_session.cjs` with a fresh private directory for each cohort: navigate once to the newly running RF/Protenix operation, record automatic UI queries, call `verify-publication` after valid results arrive, download/hash the actual artifact, and verify polling stops after publication. Retain any unsampled race window explicitly. Observe batch/shard/pending progress without settings changes; close the browser at the coordinated end. See [publication acceptance details](PUBLICATION-REPAIR-20260908.md).
