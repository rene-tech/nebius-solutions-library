# LibreChat running-instance consolidation — September 29, 2026

Owner requested fewer running clients, with one workbench per real user and
preserved Rene/KopraBio access. This operation stops obsolete compute; it does
not delete customer identities, API keys, S3 keys, buckets or model services.

## Executed scope

Project `project-e00rene`, Nebius profile `sandbox2`:

- Retired catalog previews **r16 through r39** (24 endpoints). Retained r40 as
  the single current QA/debugging candidate, **not a customer-qualified release**.
- Retired superseded speech clients `clinical-speech-selected-parent-client-20260927-r1`,
  `clinical-speech-separator-client-20260927-r1` and `clinical-speech-client-20260925-r9`.
  The fact-review client remains the current speech demo.
- Retired `video-paidf-temp-caption-20260921-canary` after archiving its two chats.

Each retired endpoint used the `4vcpu-16gb` shape: **28 endpoints / 112 vCPUs /
448 GB configured memory** removed from running allocations. No GPU model
endpoint was stopped. Endpoint definitions remain for traceability; their local
runtime disks are destroyed by Serverless stop.

## Retained workbenches

| Owner/purpose | Endpoint | Disposition |
| --- | --- | --- |
| Rene | `aiendpoint-e00mwh65yfkkg4t93n` | Customer URL, keys, image and bucket unchanged |
| KopraBio (existing `kopra`) | `aiendpoint-e00ya00dvaqck7jb4a` | Customer URL, keys, image and bucket unchanged |
| Current speech demo | `aiendpoint-e00w0d8y66wxepshs2` | Fact-review/speaker-aware demo preserved |
| Current QA/debugging | `aiendpoint-e00k5gb5sjb8536v2g` | R40 retained; outstanding catalog/recovery defects remain |
| Employee-only demo | `aiendpoint-e00makq9fefg5s5g66` | Retained pending owner's demo selection |
| Public-signup demo | `aiendpoint-e00mz3cecnnnsw7r6c` | Retained pending owner's demo selection and all-account preservation |
| Unassigned H100 LibreChat image | `aiendpoint-e00pny5mtm950gseev` | No published URL, workspace mount, env credentials or operator SSH; retirement decision requested |

Seven LibreChat-image endpoints remain, versus 35 before cleanup. Four other
running endpoints are the speech proxy and three speech model services; they are
not duplicate workbenches. Thus project-wide running endpoints fall from 39 to 11.
Do not infer that every remaining endpoint is a production-qualified client.

The two demo entry points are not interchangeable: employee-only signup and
public signup differ, and their local accounts/chats have not been merged.
Checking the public demo's seeded account does not establish that other
registered accounts are empty. Do not delete either without the owner's
selection and preserving its relevant account state.

## Preservation and verification

Private evidence and exports:
`/home/tux/secure-handoff/fs2-librechat-cleanup-20260929/`.

- Catalog previews: all MongoDB collections/documents/index metadata exported
  as EJSONL, plus non-database `/data` and uploads. Capture checks found no recent
  chat activity and unchanged user/message/conversation/file metadata. Hashes,
  tar structure, exact endpoint/image identity and private file permissions were
  verified before stop. These are logical exports, not transactional snapshots;
  restore was not rehearsed.
- Old speech/video clients lack operator SSH. Native APIs exported chats,
  settings, agent details, advertised clinical outputs and original inputs.
  All twelve old clinical jobs' original inputs were retained byte-for-byte,
  matching their input SHA256. The four clients contributed ten conversations
  and 34 messages. These are **not full MongoDB/VM backups**; ephemeral shell
  execution state is not preserved or resumable. Their workspace buckets remain.
- The video canary's upstream run-history request returned 401. Its chats and
  tool results were archived; existing central platform history and the bucket
  were not changed. No key was reissued merely to retire a canary.
- `verification-*.json` records exact endpoint STOPPED/no-instance states,
  Compute VM and boot-disk NOT_FOUND checks, comparison of untouched endpoint
  bindings, and retained clients' `/health` and `/api/config` results.
- Public health/config checks establish preserved accessibility, not a new
  all-model/scientific-workflow acceptance claim. No model inference or browser
  automation was needed for this cleanup.

Raw account exports, runtime encryption state, endpoint definitions and secret
selectors stay in the private evidence directory, not this repository.

## Rules for subsequent work

Use the existing tenant-lifecycle workflow: one client per real user, even where
users share a tenant bucket. Keep one QA candidate, not a running client for each
test iteration. Preserve source/image/test evidence and export local state, then
retire the superseded exact endpoint. Do not restart r16–r39 merely because an
older task log called them protected: this owner-requested cleanup supersedes
that temporary retention, while their evidence remains available.

This cleanup does not migrate legacy demo/speech tenant IDs into the target
`demo`/`speech-to-text` IDs, create the canonical seed bucket, or complete the
broader user/bucket consolidation. Those rollout boundaries remain documented
in the lifecycle README. No bucket contents were removed.
