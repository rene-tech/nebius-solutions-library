# Release178 public attribution failure and template repair

Four ordinary scientist06 requests completed with verified artifacts, but all
four public operation runtime identities were null. This is a failed attribution
gate, not a successful customer release. The remaining eight requests were
paused before admission.

The exact published wrapper returned one correct Pod/operation/attempt header
set on each response, across two Ready H100 Pods. Both actual image IDs and
desired images were the qualified OCI index `0c717984…`; an index-versus-child
manifest mismatch was disproven. The actual Pod templates lacked
`fs2.nebius/model-revision`. The strict Kubernetes verifier therefore rejected
otherwise valid metadata. Replaying the captured records offline rejects both
original Pods and accepts both when **only** that missing annotation is added
with the existing owner's exact artifact revision.

The isolated wrapper test did not exercise the generated Pod template through
the gateway verifier. The old helper test compared against an incomplete
expected template, so it repeated the omission. `test_revision.py` now renders
the retained release178 bundle through the real renderer and then calls the
strict endpoint verifier for both hot and burst workloads. Original output is
rejected, corrected output accepted, and a substituted revision rejected.

The repair derives the revision from `owner.spec.artifact.revision`, checks it
against the selected exact runtime record, and creates a new immutable template
and envelope. The original template remains registered. It changes neither the
model binary, weights, image, limits, replica policy, cache policy, four snapshot
bundles, Q64 scientific execution map, profiles, sibling qualifiers nor routes.
It does not weaken gateway checks or backfill old null-attribution calls.

```sh
uv run --frozen python ../../acceptance/open-runtime-attribution-20260919/prepare_revision.py \
  --baseline /protected/backend-baseline178 \
  --owners /protected/current-modeldeployments.json \
  --admin-owner /protected/current-diffdock-owner.json \
  --output /protected/new-revision-proposal
```

Only two Helm references change: the renderer bundle and infrastructure
envelope. The emitted four-map package keeps the other two maps byte-identical.
The manager owns deployment and owner mutation; new public requests on the
corrected template are still required. Existing inference artifacts and failed
attribution verdicts are retained under cohort
`diffdock-http-identity-public-r178-a`. The separate initial local launcher path
error admitted no requests and was corrected without replacing any key.
