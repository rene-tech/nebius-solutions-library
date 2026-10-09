# Post-consolidation source repair — 9 October 2026

Scope: repair the independently reproduced source/contract failures after branch
consolidation. No deployment, customer workload, key, bucket, GPU allocation or
Forge DNS change is part of this repair. Work uses detached worktrees and is
integrated into the personal fork's maintained `main` through `single_main.py`.

## Repairs

- Restored the shared-model concurrency, capacity and scaling policy from the
  preserved pre-consolidation stash, without reverting internal test-identity or
  durable-key rules. A regression test keeps these requirements together.
- Updated the admin contract to match the existing Apps landing route and
  `/admin/overview`; scientific run route checks now tolerate source formatting.
- Separated the reusable-source portability check from immutable operator
  history. See [public export](public-export.md); no historical receipts were
  rewritten or deleted to obtain a passing test.
- Fixed recipe refresh carrying successful qualification over changed execution
  identities. A scheduler receipt must match its own hash and the current model,
  execution identity, map, public completion receipt and qualification timestamp.
  Mismatches leave serving active but require new public acceptance.
- In the workbench repository, tests now check the instructions actually loaded
  through deferred skills, and the scientific-batch skill retains strict v2 JSON
  transport and matched-sample comparison guidance.

## Seven stale qualification claims

`bindcraft`, `mosaic`, `rfdiffusion`, `esmfold2`, `esmfold2-fast`,
`protenix-v2` and `alphafold3` had qualified receipts for earlier identities.
Those mismatches predated this repair; merging did not create fresh qualification.

Their source profiles are now `active`, not `qualified`. Public routes and MCP
discovery stay enabled. Images, execution identities, execution maps and workload
definitions are unchanged. Model-owned projections agree with the aggregate.
Original receipt files remain byte-identical, with their historical hashes in
profile limitations; current completion/scheduler pointers are cleared. This is
truthful metadata repair, not a new GPU/customer benchmark and not a live rollout.
The retained `qualified_at` describes that historical receipt, not a fresh run;
tests verify it against the original receipt bytes. The admin projection shows
these revisions as candidates for qualification while serving remains published.

To check this narrow condition without regenerating runtime recipes:

```sh
PYTHONPATH=k8s-inference/components/control-plane/src:k8s-inference/catalog/runtime \
python3 k8s-inference/components/control-plane/scripts/refresh_scientific_recipes.py \
  --qualification-only --check
```

Remove `--check` only when intending the documented metadata reconciliation.
Tests cover check-mode immutability, owner synchronization, receipt preservation,
unchanged execution-map bytes and idempotence. The general all-model recipe
refresh is not newly qualified by this mode: newer model families still need
complete recipe-provider/owner coverage before a whole-fleet refresh.

## Validation and preservation

Final validation results:

| Suite | Passed | Skipped |
| --- | ---: | ---: |
| Control plane, complete rerun | 3,738 | 192 |
| LibreChat templates and bundled skills | 1,225 | 14 |
| Admin UI | 243 | 0 |
| Provider-free facade and source export | 324 | 0 |
| Admin contract | 14 | 0 |
| Add-on contracts | 4 | 0 |
| Maintained-main workflow | 10 | 0 |

TypeScript, Terraform formatting, targeted Python lint/format and Git whitespace
checks passed. The final export contains 4,874 verified file digests; all 943
inventoried original operator records remain unchanged. Known Starlette and
WebSockets deprecation warnings remain; skipped checks are not live acceptance.

The integration receipt records the exact commit and executed checks. Local
JUnit files, source export and receipts are retained under the operator's
`FS2_MERGE_REPAIR_EVIDENCE` directory. The regression runs cover backend,
provider-free facade, admin contracts and LibreChat/skill-bundle tests; these
are not GPU inference or deployed-browser acceptance.

The earlier branch inventory, verified bundles, stashes and explicit dispositions
remain preserved in the consolidation archive. Archived, superseded, rejected
or qualification-pending experiments are not relabeled as merged features.
No additional task branch was created for this repair.
