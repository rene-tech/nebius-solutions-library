# R02 terminal result publication race

Observed on deployed source `5f5061b28ee71a59432492a1bdf6106428a85367`.
This is an automatic-completion UI defect, not artifact loss or an authorization failure.
The original r02 cohort remains affected; the offline correction described below has
not been deployed or live-qualified.

For Protenix operation `4a4a2555-3901-4193-8a65-78ff01737af3`:

| UTC | Actual browser observation |
|---|---|
| 20:12:50 | Running; CPU prepare active, GPU stage pending. |
| 20:13:33.083 | Automatic detail query reported `succeeded`, but artifacts were empty and semantic validation was `not-run`. |
| 20:13:46.711 | Without navigation or refresh, the same page still showed no results; polling had stopped. |
| 20:14:14.784 | One explicitly recorded ordinary page navigation returned nine artifacts and validation `passed`. |
| 20:14:31.804 | Authorized browser download passed: 32,475 bytes; SHA-256 `42429e931def8218853e78179d34cb4b22c7fe0d9b6d0d4c3fda775e009cdaa3`. |

`ScientificRunDetailPage` stopped its five-second query interval solely on controller
`run.status`. The artifact adapter independently returns empty artifacts and `not-run`
when the durable result is not yet published. A browser observing that short interval
therefore never fetched the later result without manual interaction.

The narrow offline fix keeps the existing five-second interval for a successful run
whose semantic result is still `not-run`, and displays **Finalizing results**. Published
successful results stop polling; failed/cancelled runs also stop. Artifact count alone
is not used, so an intentionally empty published result is not treated as pending.
No backend, authorization, runtime, recipe, resource, or policy setting changes.

Verification: 151 admin UI tests passed across 24 files; TypeScript passed. The regression
uses the actual polling interval to observe controller-success/empty artifacts becoming
a published result without clicking refresh. A separate test covers failed/cancelled
termination and published empty results. Live acceptance awaits the release owner.

The already-deployed phase-duration correction passed on this same new run:
`4.196746 seconds` from actual lifecycle boundaries, displayed as `4.2s estimated`.
Separately, its one-GPU ledger partition was 35 occupied GPU-s = 16.803254 active +
18.196746 idle, including 4.196746 restore GPU-s. Equality of these restore values reflects
one GPU in this trial; the wall-time code does not derive seconds from GPU-seconds.

Private source receipt hashes (raw screenshots and API bodies remain private):

| Receipt | SHA-256 |
|---|---|
| `cohort-r02/query-0023.json` — terminal before publication | `65ea5275bf9350a45f5657f0192f0a2d49c1f433c895eef0a99d4133337577e1` |
| `cohort-r02/query-0025.json` — single later navigation | `19929a6f07c1064278501950a4b24131428e2a5af6d8ebea057da0f8e6243d43` |
| `cohort-r02/r02-protenix-auto-progress.json` | `dceb03cdb61a457bece2ecf9b2cb0191938a6ef805de6a5ac4f9b8119f06ee87` |
| `cohort-r02/r02-protenix-completion-once-read.json` | `f3f951e6d051df524388c361bf441207cea375537b36802744514ed5d754e7a1` |
