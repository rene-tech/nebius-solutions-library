# Real-source offline handover preparation

On 2026-10-05 the **generic** `prepare_customer_import.py` completed against the
actual failed original operation `aa502153-3c75-422c-8040-82461fdbfcaa`, committed
generation 71. This is separate from the finite-horizon demo fixture and from
the still-running customer recovery `a42479f9-5ee0-4ed4-869b-0a094357403f`.

The source's real `failed` status and exact frozen request were obtained with a
single-operation, tenant-scoped read-only repository export. The latest
platform checkpoint was downloaded and checksum-verified, and its state and
complete file inventory matched the existing approved customer-bucket copy.
Native engine identity came from that original operation's retained native
result artifact, not the current catalog or a guessed container digest. The
helper verified the original request/job/engine recipe against the checkpoint.

| Check | Observed result |
| --- | --- |
| Source state | Generation 71; saved step 13,963,440 |
| Full target | Original 500,000,000 steps / 1 µs retained; no TPR rewrite or shortening |
| Original files | All 305 files / 82,674,112 bytes retained unchanged |
| New bundle | 96,722,411 bytes; 612 entries: complete working copy, complete preserved history, and two provenance documents |
| Independent hash verification | All 610 working/history file copies rehashed from the finished archive; all 305 source files rehashed afterward |
| Original XTC history | 70 parts retained, including all 42 zero-byte parts |
| Analysis selection | Actual worker expansion selects the 28 nonempty parts; one explicit `trjcat -f` selector adaptation recorded |
| Public request | Generated GROMACS parameter schema validates; normalization is idempotent |
| Native protocol | Original TPR hash and full target retained; only qualified performance options and explicit empty-input selection changed |
| Budget | Original source was 21,600 s; prepared **new** operation requests 1,209,600 s |
| External effects | No admission, cancellation, customer key, bucket write, GPU/native simulation, or runtime-source change |

The original TPR remains
`e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10`.
Prepared bundle SHA-256:
`876dec2ac678be11f22dcfc5c95a1e50153b7216d905f7026b1e247eeddb5ab9`.
Generated parameter-file SHA-256:
`48d5ba1ab84e8210d8569a3f7ee87ec54afc92673b78a6226862b98cffc39a05`.
Validated public-schema SHA-256:
`5f5dcb9ad685ead512e35602846336ea150b28bfc0fa2b9671c114af78b462ae`.

Private evidence and raw inputs/output are retained under:
`/home/tux/secure-handoff/fs2-lynx-customer-handover-20261005/aa502153-offline-qualification/`.
The main `qualification.json` SHA-256 is
`eb6a60090952f96dd3b7dbe7b94bede2911888e291d007a7d04cc8065bc09339`;
`analysis-selection-verification.json` retains the separate actual-file-expansion
check. `export-receipt.json` identifies the original native-result artifact and
exact checkpoint. Raw requests, manifests, files and the bundle are private,
not committed to Git.

No new generic-preparer defect was found in this real-input run. The export
harness initially compared a bare digest with the repository's `sha256:`-prefixed
digest; correcting that local comparison allowed the checks above to proceed.
It did not require a platform change or bypass any integrity check.

This proves complete, full-target **offline preparation**, not successful
execution of the full 1 µs trajectory, final scientific analysis, delivered
throughput, or an uninterrupted six-hour soak. Those remain separate public
acceptance gates. This old generation-71 bundle is a qualification artifact,
not the customer's future recovery source: actual handover must use the latest
committed checkpoint of their then-terminal recovery, with its matching frozen
request as described in [the runbook](README.md).
