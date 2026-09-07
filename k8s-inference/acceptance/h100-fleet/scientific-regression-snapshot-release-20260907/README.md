# Full scientific regression after the snapshot-capability release — 7 September 2026

All ten scientific profiles passed a fresh request through the deployed public
HTTPS API after the Terraform rollout from source
`7d0bab4619dc5d39082655b4ddc628c5b4708240`. The retained aggregate is run
`full-fleet-7d0bab46-scientific-r01`: ten succeeded, zero failed, with the
configured maximum of four concurrent workers. Every per-model receipt records
succeeded operation, result and batch states plus passed semantic validation.

The run used each model's committed public acceptance input and current runtime
recipe. All ten execution identity digests match the exact records in
`../../../catalog/runtime/contracts/scientific-execution-map.json`. The current
runtime-recipe digests intentionally differ from the earlier cohort because this
is qualification of the expanded release, not a relabeling of historical
receipts.

Temporary ESMFold2, ESMFold2-Fast and RFdiffusion snapshot-smoke policies had
been restored before this run. The runner did not modify any model policy and
used the active production configuration. These receipts validate the public
scientific workflow and its exact outputs; they are not startup benchmarks and
do not by themselves prove which startup backend was selected inside a Pod.
Backend-specific CUDA+CRIU evidence remains in `../snapshots/`.

This directory supplements, and does not replace or mutate, the earlier
`../scientific-regression-20260907/` cohort. The aggregate SHA-256 is
`620fdf58ca8eccf238a0459bde00610f163ebf1205c57d7755b6b8285c73f20f`.
