# Apps corrected-release browser acceptance

The retained serving clone passes real key-owned inference after the backend
correction. This is **not** a full Apps onboarding sign-off: the independent
scientific clone still failed its CPU preparation stage. The
[original failed attempt](BROWSER-R01.md) remains unchanged evidence.

## Exact release and scope

Backend source `2d170292037386f339fdc96fcf115a07adcf6932`, image
`sha256:fd80681a8a032645ec5f2e84c50bea21a21464e7f0a7b92701a92f69e91dec61`,
was verified ready by the release owner before this follow-up. The same signed-in
Chrome 149.0.7827.114 browser retained frontend source
`d21439d025c806f6a3bcb4167c57b95d84ff923f`, image
`sha256:50629c0274dc6a5b751e1e36224449186130945a91d90de8be1110cc44f041d8`.
The private `browser-r02/backend-cutover.json` binds the cutover; the original
session's source field is intentionally not relabeled. No clone was recreated,
renamed or given a different public route.

## Actual serving and UI results

- Existing app `96c9e1e5-0d82-4532-a2e0-0218cd2a99e0` changed from the prior
  unpublished/404 state to Ready. A protected existing credential independently
  verified exact clone membership in `/v1/models` at `13:01:04.265195Z`.
- A **new** exact-clone-scoped key was issued through Users. Its discovery request
  returned 200; the old helper retained only that status, not its list body.
  One new logical request produced operation
  `647661fa-8fd9-4c58-bb71-d7b6844371db` and exact answer **42**. POST 202 at
  `13:01:40.906Z`, one status GET 200, then result GET 200 at `13:01:42.220Z`;
  total client time **1.578853409 s**. There was no submission retry.
- The actual Runs page shows that original operation ID, succeeded, the correct
  owner, 28 input / 3 output tokens, and three observed HTTP exchanges. ASGI
  final-body response durations are explicitly distinct from client latency.
- Clone Usage shows **one successful logical run**, one owner, and **four actual
  exchanges: three 2xx plus the preserved initial 4xx**. Response bytes total
  **3,455**. Request total remains unknown because two GET request streams were
  not observed to completion; it is not fabricated as zero. Users shows the same
  accepted run and the new key's real last-use timestamp.
- Qwen Metrics now renders all **seven** available chart families with finite
  observed points. In the selected seven-day window: concurrent requests peak 4,
  GPU utilization peak 100%, and GPU memory 70,237,814,784 bytes. Missing earlier
  intervals remain gaps. This verifies actual plotted DOM, not only an API reply:
  `output/playwright/corrected-qwen-metrics-loaded.png` at `13:04:56.240Z`.
- Capacity renders its loaded current-fleet view and returns 200 at
  `13:05:02.881Z`: 16 reserved GPUs, 14 requested, 2 estimated free, 0 queued
  customer runs. Request allocation, utilization and configured headroom remain
  separately labeled. Screenshot: `corrected-capacity-loaded.png` at
  `13:05:13.109Z`. Immediate-navigation screenshots without `-loaded` show loading
  and are not used as the visual gate.

## Preserved scientific failure

App `95943840-d2b1-4a65-b5cd-aa4717b2cdc3`, operation
`ec70dbac-dce9-414d-a50b-025963a12035`, is visibly failed. The loaded Apps run page
at `13:05:24.233Z` shows failed CPU Prepare Data, cancelled GPU Sample Structure,
failed validation, and no invented GPU accounting. Only the input manifest is
available; no successful structure or active-to-publication transition is claimed.
The scientific worker owns its diagnosis and subsequent acceptance.

## Cleanup and retained harness limitations

The new key was revoked through the real Users UI at `13:05:34.530Z`, followed
by a 401 check at `13:05:34.912Z`. Both task keys are revoked and the task user is
disabled. The original Qwen app settings compare exactly unchanged.

The browser closed normally at **13:05:46.081Z**, after 591 captured admin query
responses. Its first finally cleanup correctly received 422 because the harness
attempted Disabled while retaining minimum replicas 1. The server's zero-hot-floor
validation was correct. The acceptance helper was corrected to set minimum 0
and clear warm windows, with a regression test; all **13 helper tests pass**.

One authorized corrected cleanup PATCH, using the fresh original revision/etag,
saved revision **3**. Controller observation at **13:09:00.709Z** matches its
digest `sha256:3d67a608f0d5151d052fca257e76683b892159f1260eeff6c30cd8cae62b370c`,
phase Cold, with desired/admitted/ready/available all zero. The subsequent actual
Containers response at **13:09:00.854Z** is available with zero items. The cleanup
observer initially inspected the wrong nested status field; its raw timeout must
not override these retained successful observations or be called a server defect.
The release owner removed the exact closed-browser network holder at 13:11 UTC.

The complete browser session retains the initial expected pre-login 401, two
navigation-related `ERR_ABORTED` reads, one response-body capture lost on
navigation, the time-range harness mismatch, initial inference 404, and cleanup
422. There were no JavaScript page exceptions or `ERR_NETWORK_CHANGED` events.
This is not a claim that the whole session had zero errors.

## Receipt integrity

Private evidence is under `h100/releases/admin-apps-20260908/experience/`;
credentials and one-time secret values are not exported. In `browser-r01/`:

| Receipt | SHA-256 |
| --- | --- |
| `session-report.json` | `431355bf57d746284ecfdb386de694451a4731f6abf7943f0c67b4e359b91a1c` |
| `corrected-key-inference-inference.json` | `3179219d7f880e055bad1b2f5adc0bc6947cbb28122bb036b19e9966311c2fb3` |
| `query-00567.json` (clone Usage) | `b7fb83f5551f367d3e1f1e35fe284e4858bb222ae8215b0c0e82fe8652aeb126` |
| `query-00572.json` (metrics) | `31911cad0ece704aa247c8eb03da67384d5a204976fff7b7f4c7e9bc653a6a75` |
| `query-00576.json` (Capacity) | `a35cd9548e9fde008daf135eccd875966a4fc4adc886b351641eed6255178ea3` |
| `query-00581.json` (failed scientific run) | `7f8c5488741246d54f61a158569fac44d0e70fb26e8c9b5cff9f5bb02d0417c0` |
