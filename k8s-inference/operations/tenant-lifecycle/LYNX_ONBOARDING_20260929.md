# Lynx onboarding — 29 September 2026

## Owner-approved topology

Lynx has **one shared LibreChat instance with two separate local login accounts**.
This is an explicit exception to the normal per-user-instance deployment rule.
The owner clarified the shared-instance requirement during onboarding; do not
recreate separate instances on the next maintenance pass.

Both people use the same service principal, API key and shared workspace. Their
LibreChat conversation histories are account-scoped; workspace files, platform
operation history and inference usage are shared. Attribution is therefore to
Lynx, not independently billable inference users. Signup remains disabled.

| Binding | Value |
| --- | --- |
| Tenant / inference principal | `lynx` / `lynx` |
| Platform user ID | `995ea7fb-0dee-5a80-8c1a-bef338df7739` |
| Shared key ID | `1d51cba9-1310-4451-be7c-6dcb20943309` |
| Key limit | Eight concurrent model operations total, not eight per login or eight reserved GPUs |
| Apps | `amber`, `gromacs`, `gromacs-mpi`, `lammps`, `namd` |
| Shared bucket | `fs2-lynx-c327dcc386444425`, mounted at `/workspace`, 5 GB shared quota |
| Shared client | `aiendpoint-e00a5xqzhy0d3zjg56` in `project-e00rene` (migrated October 2) |
| Portal | <https://port3080-nh8b93sq9rprrv0.tunnel.applications.eu-north1.nebius.cloud> |
| Client image | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:e96a66501807a2c446c17413f66c042b4a6ae2bdee2c32b9b05adcd1d378fd63` |

AMBER26 access for both logins was explicitly requested by the owner. No
additional inference key, GPU resources, quota changes or private model copies
were created to add the second login. The unchanged image's `/app/seed-user.js`
enrolled it as a regular USER and populated its encrypted user-scoped MCP key.
The existing first login and credentials were preserved.

## Verified outcomes and limits

- Both real login identities authenticate at the same managed HTTPS URL.
  Their user IDs differ; neither has an admin role.
- Each sees exactly the five granted Apps and the corresponding typed MCP tools,
  including AMBER26. Unauthenticated workspace access is denied.
- Both logins uploaded/read workspace markers; each can read the other's marker,
  as intended for a shared tenant bucket. The starter manifest matches the
  qualified v3 hash; all 498 source objects / 53,900,577 bytes were verified.
- Each account's chat is readable to that account. The other login's direct
  message request returns 404 and its conversation does not appear in the list.
  This verifies application chat access, not hard isolation of trusted users
  sharing a compute instance, key and writable workspace.
- Two concurrent actual LibreChat agent conversations each completed with
  `unfinished=false`, `error=false`, exactly one `workbench_list_apps` call and
  all five MD App IDs in the result. These are authenticated HTTP/SSE client
  checks, not a fresh visual/browser qualification.
- The task found and fixed a client catalog-search defect: “molecular dynamics”
  had omitted AMBER and distributed GROMACS. The catalog-only image and 19 passing
  regression tests are recorded in the cookbook's `MD_CATALOG_FIX_20260929.md`.
- Eight customer-key native runs, two each of AMBER/GROMACS/NAMD/LAMMPS, completed.
  All eight overlapped for 74.413 seconds. Each ran minimization, 20 ps NVT,
  20 ps NPT and 20 ps production of the unchanged ff14SB/TIP3P alanine starter
  system. Native trajectory/geometry validators passed for all eight; 418 exported
  objects / 192,584,728 bytes were verified in the customer bucket.
- The eight-operation test establishes this cohort's successful concurrency,
  not a universal throughput guarantee. A ninth-reservation probe arrived after
  operations had ended and was cancelled; it does not prove overload rejection.
  Long simulations, multi-node GROMACS, all advanced protocols, new-node scaling
  and GPU snapshots were not requalified by this onboarding.
- All 28 tenant-lifecycle unit tests pass. Model runtimes, other customers'
  clients, public Gateway and cloud limits were not changed.

Native operation IDs, timings, artifact checksums, failures and client HTTP/SSE
evidence are retained privately. Short-run science checks do not establish
equilibrium, converged free energies, or an all-workflows production release.

## Retention and future maintenance

### October 2 migration

The owner requested migration to the qualified Kimi K3 default image. Both
existing accounts/password hashes, all five original conversations / 28 messages,
the same API key and bucket were preserved. The predecessor
`aiendpoint-e00kybbs8a1sbxcfyw` is stopped; do not restart it alongside the new
same-owner workbench. Both real browser logins, chat isolation, the five MD App
grants and a browser CSV download were checked. No model, quota or GPU capacity
change was made.

Fourteen replay requests completed, with thirteen clean technical cases and one
self-recovered Gemmi API mistake in an inventory query. Both hosted GROMACS runs
and all 318 output objects passed independent file checks. The recovered case
remains a failed clean-agent acceptance case, not a zero-error release claim.
Private evidence and verified full-database backups are under
`/home/tux/secure-handoff/fs2-lynx-kimi-migration-20261002/`; the cookbook report is
`templates/hcls-librechat/docs/lynx-kimi-migration-20261002.md`.

Do not stop the application container to copy a database on Serverless: the
provider terminates the underlying VM. The successful restore kept it alive,
verified a complete staged MongoDB import, and re-encrypted the same saved API
credentials with the new instance's local keys. First-user password bootstrap is
omitted to prevent password resets. Existing customer passwords and API/S3 keys
are unchanged. MongoDB needed a process open-file soft limit of 64000 for staging
duplicate indexes (not a cloud quota increase); retain that migration prerequisite.

### Original onboarding records

Private operational root:
`/home/tux/secure-handoff/fs2-lynx-onboarding-20260929/`.
`HANDOVER.md` contains the two login sections and shared API/S3 credentials.
It is mode 0600 in a mode 0700 directory, never committed here. No invitations
or credentials have been emailed automatically.

`receipt.json` binds both member records to the same final endpoint. Separate
login secrets remain in the existing private handover/MysteryBox records.
`shared_client.py` documents the exact idempotent native second-login enrollment;
`shared_chat_check.py` retains the separate-account/shared-storage checks.

Three unpublished predecessors were retired after preserving local state:
`aiendpoint-e00gdrgzcw3na2sqez`, `aiendpoint-e00qtnvrgwbvkaax93`, and
`aiendpoint-e00j3sgr527cy2ppmt`. See `shared-endpoint-states.json` for observed
terminal states. The last instance was redundant once both logins were verified
on the shared client. No bucket or key was deleted or revoked.

Serverless stop destroys local disk. The S3 mount is **not a MongoDB/chat backup**.
Before any future stop/replacement, drain both accounts and export the whole
database, runtime encryption secrets and uploads. Keep both accounts and their
encrypted plugin credentials together on restore. If creating a fresh database,
enroll **both** existing logins: the endpoint's legacy default-user environment
only bootstraps the first one. Reuse both private credential records; do not
rotate passwords, create a second API key or split the workspace implicitly.
Private logical database/runtime archives exist; a live archive-restore drill
was not part of this onboarding. There are no customer chats to migrate from
the retired pre-handover QA instances.
