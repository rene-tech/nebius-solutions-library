# Tenant lifecycle tooling — verification, 29 September 2026

## Implemented and checked

- Installed the `scientific-ai-tenant-lifecycle` skill as a symlink to its
  versioned source in this directory. Skill validation passed. Both skill CLI
  launchers were executed with `--help` through that installed path.
- Patched the platform router, engineering and LibreChat skills and the shared
  workspace `AGENTS.md` to route identity/workspace work here and reuse system
  test identities rather than creating a new cohort per iteration.
- 28 stateful contract/behavior tests passed with Python 3. Ruff check and format
  checks passed for all five Python files. No cloud mutation is performed by
  those tests.
- Ran `lifecycle.py inventory --include-disabled` against the **actual public
  HTTPS admin API**, using the existing admin credential through an anonymous
  memory file descriptor. TLS verification remained enabled. Session login,
  authenticated listing, envelope parsing and logout worked. No token value was
  printed or stored in a new disk file.
- Live list: 41 discoverable identities, 19 enabled configured users. Rene was
  still `rene/rene` with two active keys; KopraBio remained `kopra/kopra` with one.
- Loaded the actual deployed starter pack using `StarterPack.load`, validating
  every local object and its metadata: v3, 498 objects, 53,900,577 bytes, manifest
  `c8afd07ca1b6734c690839ba6d3eb4b461b65380852bcf705a863e7bd07a7709`.
- Generated the consolidation plan from the preceding private inventory; it
  preserves Rene/Kopra bindings and proposes system/demo/speech-to-text targets.
  It schedules no implicit bucket deletion or endpoint stop.

The initial in-pod loopback API check received HTTP 421 because the server
requires its configured public Host authority. A public-address attempt from
inside the pod failed at transport. The successful management-host HTTPS check
above replaced neither host validation nor TLS policy. These diagnostics are not
evidence of a customer login failure or a platform network change.

## Not executed / not claimed

- No user creation, user retirement, key revocation/rotation, bucket creation or
  deletion, endpoint replacement, model deployment or tenant-data migration.
- No live shared/private-user isolation acceptance for these new operator
  scripts. The behavior is covered by contract tests and reuses the existing
  backend, but must still be qualified in the system test workspace before
  calling the mutation path cloud-accepted.
- No canonical source-bucket creation or live Nebius Data Transfer execution.
  Installed CLI 0.12.206 lacks `storage transfer` v1; the helper detects this
  before transfer creation and does not fall back to deprecated v1alpha1.
- No change to the existing automatic OCI-pack seeder. Its durable completion
  ledger and working customer onboarding remain intact. Switching its transport
  to the source bucket requires the rollout described in the workflow.
- No general permanent-user/bucket purge. Existing API limitations and legacy
  storage-only identities are documented; deleting database rows is not a fix.

## Next execution sequence

1. Reuse/create the intended system identities and qualify create/reuse/retire
   there, including an unaffected shared-bucket peer and actual S3 key state.
2. Provision the single system-owned source bucket through the platform's
   infrastructure owner, publish the qualified pack, and record its real binding.
3. Use a compatible isolated v1 CLI for one-shot copy and verify all hashes,
   replay behavior, customer-edit preservation and shared/private destinations.
   Integrate the transfer state into the existing durable seeder before changing
   automatic onboarding; retain its current transport until this is accepted.
4. Export/migrate current demo and speech clients, retaining Rene/Kopra exactly.
5. Retire the reviewed old identities, clients and storage access; decide data
   retention before any permanent bucket/IAM deletion. Record post-cleanup counts.
