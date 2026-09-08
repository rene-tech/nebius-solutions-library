# Serving app publication: long Kubernetes object names

The first serving-clone acceptance on release
`d21439d025c806f6a3bcb4167c57b95d84ff923f` exposed a real publication failure.
App `96c9e1e5-0d82-4532-a2e0-0218cd2a99e0` had a Ready runtime Pod by
2026-09-08 12:38:28 UTC, but its correctly scoped inference request at
12:38:36 returned HTTP 404 without creating an operation. The browser lane
preserves that failed request in `BROWSER-R01.md`; it is not a passing run.

The Kubernetes ModelDeployment status was Ready, with publication enabled and
matching generation/digest. The API bridge nevertheless reported
`status-unavailable`, because its observed-status schema rejected this valid
70-character Deployment name:

```text
app-96c9e1e50d824532a2e00218cd2a99e0-8d61f100652a-hot-h100-reserved-8x
```

The parser incorrectly imposed the 63-character DNS-label bound on each part
of a Kubernetes object name. Kubernetes object subdomain names allow up to
253 characters in total; Service DNS labels remain limited to 63.
See the official [Kubernetes object-name rules](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/).

The correction introduces a separate Kubernetes object-name validator for
observed resources/placements and generated Deployment names embedded in the
existing startup-retention query. It does not relax Service names, hostname
contracts or qualified source identities. It changes no generated names,
manifests, runtime arguments, scientific recipes or resource digests.

Validation completed before deployment:

- 81 focused Apps/controller/bridge/publication tests passed, including the
  exact 70-character live name through status parsing, bridge persistence and
  independent route publication. The fake Kubernetes writer records no writes.
- Six actual startup-retention tests passed; hold/deadline behavior is unchanged.
- Tests cover valid 253-character names, invalid 254-character and malformed
  names, and continued rejection of an oversized Service name.
- Strict mypy for both changed modules, Ruff, scientific recipe `--check`, and
  `git diff --check` passed.
- A fresh read-only fetch of the existing live CR passed the corrected local
  parser with phase Ready and name length 70. No cluster resource was changed.

The release owner must deploy the fix and authorize the bounded acceptance
rerun. The existing Ready clone must not be renamed or recreated as a workaround.
