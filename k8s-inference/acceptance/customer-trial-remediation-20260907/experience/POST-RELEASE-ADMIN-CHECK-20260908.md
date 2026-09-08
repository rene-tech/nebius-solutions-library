# Prepared admin checks — 2026-09-08

Root confirmed deployed source `bc264980f3fedc33c8fdc6d59c93096d8db9ccaf`; see actual attempts below. Any further attempt still requires root coordination and a new output directory. All standalone checks finish before r03, and no model settings change during r03/r04.

## Attempts retained

- Retained-Protenix preflight passed 07:35:44.484–07:35:47.890 UTC; browser closed 07:35:47.949. Restore API3.946846s/UI3.95s,9 fresh artifacts and reconciled33GPU-s accounting passed. The real32,796-byte download matched SHA-256 `55d5f96024aafba3d0be88fd02fabf272f5fee7209b453fd0a7bf8e941c37e24`. No failed browser requests/application exceptions; initial unauthenticated session401 was expected. Private report SHA-256 `abe0c5a46eb339ff6f6242b57b224b3c30a2c4111e615bd03825176da14eba35`.
- First Cosmos setting attempt stopped **before any preview/apply or configuration mutation** at07:36:55.748 UTC. The original revision11 was Cold with every replica count0 and no Pod, but the helper used the unsupported `kubectl get --resource-version` flag. Browser closed07:36:57.277; no owned process remained. Failure receipt SHA-256 `f585ba19fb0b59a58b0b5b3f66c42c0d909d7e09f45feeb57ec51f69772afe99`. This is a retained harness failure, not a passing setting test. The corrected helper uses the supported read-only `--raw` API watch with the exact list resourceVersion query; three pure tests pass. Navigation-cancelled reads are explicitly recorded separately, only during deliberate navigation. No network failures are silently discarded. Subsequent attempts were separately authorized by root.
- The second authorized attempt also stopped before preview/apply because the generic empty kubectl List omitted resourceVersion. It changed no setting; browser closed07:39:38.137. Receipt SHA-256 `1826de5434f5b2bd551f6c280c3bbb36493223511c13f39904a1158312b686fd`. The initial list was corrected to the raw PodList; a separate read-only list/watch compatibility check passed before the next attempt.
- The third authorized attempt **passed**. The real form set1800 seconds in revision12 (readback07:41:05.016), then restored the exact original unset configuration in revision13 (07:41:07.376). Revision13 was observed Cold at07:41:14.621 with all eight replica counters zero. The scoped resourceVersion watch recorded zero Pod events; final Pod list was empty. Browser closed07:41:15.672 and no owned process remained. No failed requests or application exceptions occurred. Receipt SHA-256 `077dc171237c26a27954e606b6c662920846949a65af79c25db67d16f826f680`.

The [public consolidated preflight receipt](admin-preflight-20260908.json) preserves both harness failures and the final successful functional checks. Cosmos is restored; no further settings changes are planned during the cohorts.

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

The three pure helper tests validate the cold-only scope, exact owned-field change, refusal to overwrite another operator, and the scoped resourceVersion watch; they execute no browser or network calls.

## Access evidence reused, not rerun

Root explicitly approved reusing the [September6 real-browser scoped API-key lifecycle proof](../../scientific-fleet/evidence/customer-access-policy-h100-8bb53aab-20260906.json), because access/key code is unchanged in this repair. At 22:37–22:38 UTC the synthetic viewer key was created (201), discovered the allowed academic scientific models (200), was denied an out-of-scope model (403), was revoked (subsequent401), and its principal was disabled. This is dated retained evidence, not a new release-specific key test. No new identity will be created by these preflights.

## Coordinated cohorts

Once standalone checks pass and Cosmos is restored, signal READY to root. During r03/r04 use `browser_session.cjs` with a fresh private directory for each cohort: navigate once to the newly running RF/Protenix operation, record automatic UI queries, call `verify-publication` after valid results arrive, download/hash the actual artifact, and verify polling stops after publication. Retain any unsampled race window explicitly. Observe batch/shard/pending progress without settings changes; close the browser at the coordinated end. See [publication acceptance details](PUBLICATION-REPAIR-20260908.md).
