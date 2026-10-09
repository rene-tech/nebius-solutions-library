# Basel tenant and shared LibreChat — 7 October 2026

Scope: provision the requested Basel event workspace, enable registration,
preconfigure Token Factory/Tavily/Scientific AI, grant all current Apps, and
enforce one shared eight-operation Scientific AI key expiring after Friday.
This is onboarding/integration evidence, **not** a new all-model or eight-GPU
capacity certification. Machine-readable receipts: [verification.json](verification.json).

## Retained resources

- Tenant/principal: `basel`; display name: `Basel`.
- Serverless endpoint: `aiendpoint-e00qgee4t72w2k3q47`, project `project-e00rene`,
  eu-north1, CPU `4vcpu-16gb`.
- URL: https://port3080-b5t9zzb991hsk2v.tunnel.applications.eu-north1.nebius.cloud
- Shared 5 GB workspace: `fs2-basel-1b86fd1010c2f76c`, mounted at `/workspace`.
- Independent 32 GiB persistent state: `computefilesystem-e00zvjx7md6thxfyma`,
  mounted at `/data` with MongoDB in `/data/db`; uploads link into that volume.
- Admin customer binding: `3455850c-4d7b-4573-85be-9b56368eb83c`.
- Scientific AI key ID: `7cef9593-6b0b-454d-b640-1e36321bfa62`; models `*`,
  maximum concurrency 8 **shared by all participant logins**.
- Only this scientific key expires: `2026-10-09T22:00:00Z`, midnight entering
  Saturday October 10 in Basel (CEST). Access lasts through Friday locally.
  No client shutdown, S3 deletion/revocation or provider-key expiry was scheduled.
- Credentials and raw synthetic traces are private under
  `/home/tux/secure-handoff/fs2-basel-onboarding-20261007/`; no secrets in Git.

## Client change and release identity

The default client preconfigured the main model MCP but required a personal key
for the Apps/workbench helper of new registrations. Added an explicit
`SCIENTIFIC_SHARED_GATEWAY_ENABLED=true` opt-in to both render-config and the
authenticated workbench router. Existing personal settings remain preferred;
the default behavior is unchanged. `ALLOW_REGISTRATION=true` is also explicit.

Source: `rene-tech/serverless-ai-cookbook`, branch
`agent/fs2-basel-workbench-20261007`, implementation commit `90cf559`;
operator documentation commit `6c7cda6`.

Scoped image:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc:basel-shared-registration-20261007-r2`

Pinned deployment index:
`sha256:ce8fc5e3a87f4910951b3733a3be3d2f53a9d5ed3bede6921e8997bba6679496`.

The base is the qualified persistent-state image
`sha256:873be148673dec6dc191ba30d2933c01acee4622fd3fa14888388378539f13d3`.
The first local packaging attempt exceeded Docker's layer depth; it was never
deployed. The retained release adds a single layer and preserves the base
entrypoint, persistent-state handling, Kimi K3/high default and scientific skills.
No other endpoint or fleet-wide default was changed. The platform backend stayed
on `f64cd4b39a6eaf41e1762837e0a878ee7a78307f973ef6a822a29b118b5510df`.

## Verified live

- New public registration, immediate ordinary-user login without SMTP, owner
  login, healthy managed HTTPS and authenticated agent/tool routes.
- Newly registered participant needed **no manual API-key entry** in Apps or
  scientific-workbench MCP. Catalog returned all 46 current Apps.
- Real Kimi K3 response through LibreChat/Token Factory, not merely a key check.
- Actual Tavily MCP search returned primary GROMACS documentation URLs, request
  `cbdacf27-d851-4378-ae9c-8e4930d94230`.
- PhenoAge synthetic fixture invoked through the registered user's agent and
  typed MCP, polled to success, then retrieved and hash-verified through Workspace.
  Operation `f7a0cedf-768a-4930-a049-75db6e2f97aa`: prediction
  `41.90792243377997` equals the independent fixture reference; recorded cold
  activation 8.538559 s. This CPU formula is not a GPU capacity benchmark.
- Separate conversation visibility for owner and participant, intentional shared
  workspace visibility, upload/download and retained result bytes.
- All 498 starter objects, 53,900,577 bytes, verified by full S3 readback and
  individual SHA256, plus manifest
  `c8afd07ca1b6734c690839ba6d3eb4b461b65380852bcf705a863e7bd07a7709`.
- `/data` is virtiofs, `/workspace` is S3/FUSE, MongoDB state and upload linkage
  checked. Replacement/restore was not repeated for this new instance.
- 26 targeted Node tests passed: registration credential behavior, router and
  existing run/workspace service contracts. Bash deployment syntax passed.

The temporary registration-test account was deleted through its native account
API after exporting synthetic test evidence. Its login was rejected afterward;
the owner, shared key, starter pack, bucket and endpoint remained usable. The
test harness initially expected 401/403 after deletion; the actual legitimate
unknown-email response is 404. This was corrected in the receipt checker, not by
changing application authentication.

## Boundaries and operator notes

No browser automation or visual-layout certification was performed; tests used
the actual LibreChat HTTPS registration, login, chat, tool and workspace routes.
No all-46-App science requalification, eight-way saturation test, GPU reservation
or new scaling claim is made. Capacity waits remain possible on the shared
cluster. Open registration intentionally grants access to the shared tenant
key and files; this is not a per-participant private bucket.

The two provider integrations and S3 keep their existing credential policies.
Only the Scientific AI key has the explicit event expiry. After expiry, model
calls fail authorization, while LibreChat, Token Factory/Tavily and the bucket
remain in place. Operator renewal/replacement is a separate explicit action.

Rollback preserves the Basel bucket and state filesystem. Reverting to the
unmodified base removes the new-registration credential convenience; it is not
a functionally equivalent shared-registration release. Do not stop/reprovision
another customer's endpoint or attach their state filesystem.
