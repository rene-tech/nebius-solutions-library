# Scientific customer access acceptance — 2026-09-06

The retained H100 admin portal, deployed from `29b7e01a`, successfully created a
tenant-scoped operator principal through the browser. Creating its API key with
the allowlist `esmfold2-fast`, `alphafold3`, `bindcraft` failed with HTTP 404,
`model or operation was not found`. No credential was issued by this failed
attempt and no GPU workload was submitted.

The three token/key policy entry points resolved model IDs exclusively against
the interactive registry, which does not contain scientific batch profiles.
The correction uses a shared resolver: interactive IDs and aliases retain their
existing behavior; exact IDs from the configured scientific profile catalog are
also accepted; unknown IDs are still rejected. Storing a scientific ID does not
grant availability or a license. Existing discovery and submission checks still
control route exposure, qualification, tenant artifact access, and scheduling.
Rotation retains the previously validated model policy.

Browser checks also found ambiguous select labels and invalid HTML `pattern`
expressions under the browser's Unicode-sets regex mode. The access dialogs now
have concise accessible names and properly escaped ID patterns.

Focused regression results:

- 15 backend tests pass, including scientific key creation, policy editing,
  rotation, revocation, unknown-model rejection, out-of-scope submission, and
  candidate/tenant-license discovery and admission remaining unavailable.
- 5 Access page tests pass, including precise labels and HTML ID patterns.
- Admin TypeScript checking and focused Python lint/type checking pass.

The live lifecycle is **not yet accepted**: the correction must first be
deployed, then the browser must issue a key, verify the three-model scientific
catalog and out-of-scope rejection, revoke it and observe HTTP 401, and disable
the temporary principal. The prefilled dialog and test identity were retained
for that bounded follow-up. Private browser failure proof is retained by the
acceptance task; no API credential belongs in this evidence.
