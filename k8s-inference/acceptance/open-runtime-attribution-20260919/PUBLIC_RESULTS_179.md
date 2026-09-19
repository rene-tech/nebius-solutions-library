# DiffDock public attribution and paired repeatability

Release 179 passed this bounded customer-path gate on 19 September 2026. The exact
wrapper image is `sha256:0c717984c438bb3cac1a139297a06dc39c5fe7fc6ab387c130c0c464a48ba4f9`;
weights and upstream revision `85c49b60d3e0b0182a59ee43a34a6d7036981284` are unchanged.
This qualifies DiffDock on the observed H100 deployment, not every old image
using the common server and not the whole platform.

## What actually ran

Two unchanged ordinary typed-MCP cohorts used the frozen six experimental
complexes, seeds 19/23 and four poses per request. Each cohort used the same
existing scientist06 key and concurrency 1. No new budget, tolerance, precision,
pose filtering or artificial direct-Pod routing was introduced.

- 24 requests and 96 generated 3D poses passed service and artifact validation.
- 24 runtime identities matched actual response headers, operation attempts,
  Pod/node/GPU UUIDs, exact image/model revision, Service and EndpointSlices.
- Two actual H100 Pods served the calls; 10 of 12 repeated inputs switched Pods.
- All 12 new-cohort pairs met the original 0.01 Å / 0.001-confidence thresholds.
  Observed maximum differences were 0.002 Å and 0.000454664 respectively.
- Each unchanged Pod reported 12 lifetime accepted/completed requests and zero
  failures/rejections. Those counters match the 24 retained operations, supporting
  the explicit first/later per-Pod ordinals in the receipt.

Both Pods were already Ready before admission. Median client elapsed time was
22.80 seconds; median upstream HTTP duration was 6.38 seconds. Neither number is
a cold-start or GPU-kernel measurement. Snapshot and elasticity qualification
are not inherited from this result.

## Scientific results and preserved limitations

Only 12 of 24 requests had a top-ranked pose within 2 Å of the experimental ligand
in the receptor coordinate frame; top-four coverage was also 12/24. The largest
retained pose RMSD was approximately 705.87 Å. No poses were filtered out. A
well-formed, repeatable output is not an affinity measurement, a guarantee of a
physically plausible pose, NVIDIA NIM parity or reproduction of a paper.

Both new cohorts still differ from the unchanged historical release176-a
reference on 1A52/seed23: maximum coordinate difference 0.0336 Å and confidence
difference 0.0151792. That exceeds the same thresholds. The new result matches
the isolated r6/r7/wrapper references exactly; the mechanism behind the old
public-reference difference is unresolved. The favorable179-a/179-b comparison
does not erase that failed historical comparison or establish all-history
bitwise determinism.

All four failed-attribution 178 calls and 24 unattributed 176 calls remain in the
campaign. The 178 cause was a missing model-revision annotation in the immutable
legacy template; commit `c9d637d22` derives it from the exact owner revision and
tests the real renderer/verifier boundary. The strict verifier was not weakened.
An initial historical-log lookup used the admin API's default one-hour window.
An explicit 02:20–02:40 UTC reassessment recovered all eight relevant original
request bodies, each identical to the frozen arguments. This was a query-window
mistake, not lost platform logs.

## Evidence and handoff

`public-qualified179.json` is payload-free portable evidence with operation IDs,
input/result/evaluation hashes, actual runtime witnesses, first/later ordinals,
and independently verified source receipt references. Protected source root:
`/home/tux/secure-handoff/scientific-qualification-20260918`.

The underlying files are in `cohorts/diffdock-http-identity-public-r179-{a,b}`
and `diffdock-public-identity-r179`. The latter includes before/after cluster
captures, 24 actual upstream exchanges, both attribution gates, paired numerical
comparisons, source/environment inspections and lifetime-counter reconciliation.
The manager owns restoration of the temporary min 2 setting to the original
min 1; no source catalog default or snapshot preference was changed by this test.
