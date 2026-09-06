# Solution acceptance gates — 2026-09-06

## Final release resolution

On exact source `adf1d842`, the final ten-model public campaign and genuine
qualification promotion closed all ten identity checks described below.
[Final fleet evidence](final-fleet-acceptance-h100-20260906.md) records the
requests and immutable receipt hashes. Runtime identities and historical
receipts were preserved. Recipe refresh, 36 scientific contract tests,
38 fleet harness tests, 10 primary activation tests and adapter checks passed.
The complete solution suite ran 307 tests in 78.632 s: 306 passed; only
`test_export_contains_no_private_references` failed, with 47 findings across
historical resource IDs/layouts and documentation checkout paths. That
publication-cleanup issue remains separate from functional cluster readiness;
the test has not been suppressed. Earlier counts and sequencing below are
retained as the repair history, not the final release status.

## Initial repair

Scope: local Terraform, scheduling and source-contract validation based on
revision `29b7e01a`. No cloud resources, limits, model recipes, live discovery
semantics or historical qualification receipts were changed by this repair.

## Fixed and verified

- Both the default committed scientific execution map and an explicit override
  survive a real local Terraform plan without losing fields. In particular,
  `mosaic.aggregate` and `rfdiffusion.collect` retain
  `image_role = "scientific-tools"`. The workload-stage input is `optional(any)`
  and passes the object directly to Helm. The existing exact-byte Helm render
  test also passes.
- Selecting the effective execution map now selects JSON strings before
  decoding. An empty override therefore reaches the intended non-empty-map
  preflight error instead of Terraform failing to unify different tuple types.
- The academic raw AlphaFold3 example and enabled-scientific test fixtures now
  explicitly enable the runtime cache used by the committed model recipes.
  No capacity, quota or request envelope was increased.
- Scheduling source assertions follow the shared reference-pool backing and
  both reference CPU classes. They retain the collision, ownership and minimum
  resource checks without depending on old names or single-line formatting.

Verification from the solution directory:

```bash
terraform fmt -check locals.tf examples/scheduling-academic-raw-af3.tfvars
python3 -m unittest discover -s tests -p test_deployment_contract.py
python3 -m unittest discover -s tests -p test_scheduling_observability_contract.py
python -m pytest -q components/control-plane/tests/test_helm_chart.py -k committed
python3 -m unittest discover -s tests
```

Use the control-plane Python environment for the Helm test. Results: 72
deployment tests passed, 32 scheduling tests passed, one exact-byte Helm test
passed. Full discovery ran 307 tests in 78.502 seconds: 11 failing assertions,
no errors, restricted to the two categories below. The baseline had 25 failing
assertions and one error. These are failure counts, not counts of failed test
methods: ten model subtests belong to one qualification test method.

## Initial state: current-release qualification was pending

All ten profiles retain genuine historical scheduler/completion receipt
pointers, but refreshed runtime recipes changed their current execution
identities. The ten equality failures in
`test_scientific_fleet_is_schema_valid_and_evidence_state_is_consistent` correctly
detect this. Those checks remain unchanged; old receipts are not proof that the
new release has passed acceptance.

Do not just change the live catalog to `active`: direct scientific submission
supports that state, but the current MCP and tenant-admin discovery projection
requires complete `qualified` evidence. A refresh-only demotion would hide
models. No such demotion or discovery change was made here.

The existing promotion command also refuses to overwrite an already-qualified
profile with different evidence (`already_qualified_with_other_evidence`). To
close the remaining gates using the existing workflow:

1. Finish implementation and deploy the runtime to be tested. Preserve an exact
   checkout of its recipes and execution map. Record current receipt pointers
   and leave every immutable `activation/qualification/` receipt untouched.
2. In an **isolated, offline promotion checkout**, prepare the changed models as
   `active` in both the canonical profile and their model-owned projections.
   Set semantic state to `active`, clear only the current public-completion and
   scheduler-eligibility pointers, and retain `qualified_at`, the semantic
   receipt, route/MCP exposure and complete current execution identity. For
   primary fragments, set `accepted_evidence.h100.state` to
   `semantic-qualified-active-awaiting-public-acceptance` and
   `activation_gate.public_platform_run_required` to `true`, matching the
   promoter's existing active-owner representation. Do not build or deploy this
   intermediate metadata.
3. Prepare that offline state **before** collecting the final fleet receipts:
   primary aggregate rows pin the exact activation-fragment bytes. Resetting
   the profile only after the run would invalidate those input hashes. Execute
   the runner from this checkout against the deployed runtime; metadata changes
   must not change its model execution identity or map. Keep the aggregate and
   all ten successful model receipts together with their original private file
   modes. Separate varied-input, concurrency and cancellation evidence remains
   necessary and must not be replaced by the fixed qualification canaries.
4. Run the read-only promotion plan, require a successful action for each model,
   then apply that exact evidence. A failed or skipped model is not accepted.
   Review and commit only the final re-qualified catalog plus new immutable
   receipts; deploy no intermediate `active` catalog.

After step 2, from that checkout's solution directory, using the existing token
environment variable without putting its value in command arguments:

```bash
python components/control-plane/scripts/refresh_scientific_recipes.py --check
python3 acceptance/scientific-fleet/run_fleet_acceptance.py \
  --endpoint https://inference.example \
  --run-id scientific-final-release \
  --receipt-root /secure/fs2-acceptance \
  --max-parallel 8
python3 acceptance/scientific-fleet/promote_qualifications.py \
  --aggregate /secure/fs2-acceptance/scientific-final-release/aggregate.json
python3 acceptance/scientific-fleet/promote_qualifications.py \
  --aggregate /secure/fs2-acceptance/scientific-final-release/aggregate.json --write
python components/control-plane/scripts/refresh_scientific_recipes.py --check
python3 -m unittest discover -s tests -p test_scientific_workload_contracts.py
acceptance/scientific-fleet/run_checks.sh
```

Do not use `--acceptance-repository-root` to excuse a changed target recipe. It
only supports the documented unchanged-model case with unrelated fleet-map
drift. Controller/admin-only release metadata is not itself a model acceptance
identity; do not invent a requirement to match an unrelated source commit.

## Separate historical export issue

`test_export_contains_no_private_references` still reports 41 existing
references in documentation and historical evidence: developer checkout paths,
legacy source layout and provider resource identifiers. Its assertion was not
disabled, and immutable evidence was not rewritten to hide the failure. This
is a separately recorded publication-cleanup issue, not a Terraform deployment
or model execution failure. No export-hardening project was added to this task.
