# Corrected Apps registration: Settings now available

The registration correction is visible in both loaded Apps Settings pages.
This bounded browser attempt is **partial**, not a successful public model
acceptance: the concurrent public-r02 test used an academic-tenant key against
default-tenant Apps and stopped at empty scoped discovery before inference.
The workload owner identified that test-tenant mismatch; no model failure is
inferred from it. Its original failed receipts remain preserved.

Backend source `1f4015bcc45064fd0b272bff872956f3df6a42f6`, image
`sha256:dd403b999f4de5c562aec2eae140e35f4155f7521361bb373fe2a792533cee02`;
UI remains source `853f54868ec7efd0752c9df21201f03235b4135b`.
Chrome 149.0.7827.114 signed in at **14:27:02.941 UTC** on September 8,
signed out through the actual console button at **14:29:35.162**, and closed
normally at **14:29:35.299**, after 73 observed admin query responses (five
response bodies were lost during navigation/logout, as retained below).

## Loaded page evidence

- Clinical PhenoAge app `38aad847-9010-51d0-bed7-bcb404bc755d` now has usable
  Settings, desired revision 2 and compatible pool `batch-cpu`. The workload
  owner's saved min0/max1, idle300/startup900 and actual Cold/zero-worker state
  are visible. The explicit min0 warning correctly says elasticity has not yet
  been benchmark-qualified; it does not block the setting or promote evidence.
- AltumAge app `197fa990-6f65-539d-a1d8-240877ce861b` shows the same usable
  scaling controls and its actual `h100-1x, h100-reserved-8x` pool choices. Both
  Apps retain conventional loading with no offered qualified GPU snapshot.
- Runs and Usage are loaded for both exact Apps and retain zero logical runs
  during this pre-inference attempt. No fake request history is introduced.
- The Apps-specific form still lacked a per-worker CPU/RAM or GPU-count summary,
  presented a generic GPU-snapshot selector on the CPU App, and did not expose
  the existing autoscaler cooldown. These display gaps were reported, not
  mistaken for completed CPU-resource UX acceptance. Root authorized a narrow
  follow-up to that form; it is not part of the frontend tested here.

The browser did not save settings, create keys/users, or submit model requests.
Two additional authorized exact-label Kubernetes reads found no aging Pods after
the public client's initial natural zero transition; no hot hardware witness
exists for public-r02. Later public-r03 evidence uses new files and identities.

## Errors and closure

No JavaScript page exception was recorded. Preserve the expected initial and
post-sign-out session401s, four response-body capture losses across page
navigation, and one session-body loss/`ERR_ABORTED` during logout. These are
retained browser/harness observations, not silently recast as zero errors.
The actual login form was observed after Sign out. The browser context and
process closed, while root requested holding the one owned local network
container for the next corrected cohort.

Private evidence: `h100/releases/aging-20260908/browser-r03/`, including loaded
Settings/Runs/Usage JSON snapshots and `output/playwright/` PNGs. The unchanged
`session-report.json` SHA-256 is
`7793ef04146eb5ac3db575443bcfa8845b24b0d79ada5f2dbeb7bc08bedc6d0b`.
No credentials or raw signed URLs are exported.

## Offline Apps form follow-up

The narrow AppSettingsTab change adds actual configured per-worker CPU/RAM or
GPU counts, removes the CPU-only GPU selector in favor of a not-applicable label,
and exposes existing `availability.cooldownSeconds` without changing a default.
Three rendered regressions verify GPU behavior, CPU hidden-setting preservation,
and an exact cooldown-only save delta. Four focused files / **24 tests** pass,
as do TypeScript checking and diff checks. Live verification awaits root's UI
release; these offline tests are not a live form-save claim.
