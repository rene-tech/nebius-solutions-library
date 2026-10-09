# forge.nebius.cloud certificate repair — 2026-09-16

Scope: fix the certificate error after the Scientific AI DNS switch. No model,
application image, API authentication, quota, or workload rollout was changed.

## Cause and live fix

The A record correctly resolves to `89.169.99.188`, but the existing Let's Encrypt
certificate only contained the IP address as a Subject Alternative Name. Clients
connecting by hostname therefore rejected it with a hostname mismatch.

Added `spec.dnsNames: [forge.nebius.cloud]` to the existing cert-manager Certificate
`fs2-system/fs2-serve-control-plane-public`. Kept the existing IP SAN, issuer,
secret, Gateway listeners, and renewal settings. cert-manager issued revision 6
and Envoy loaded it without an application restart.

```yaml
# Narrow merge patch for the existing Certificate, not a complete manifest.
spec:
  dnsNames:
    - forge.nebius.cloud
```

Certificate secret: `fs2-system/fs2-serve-public-tls`.
Issuer: `fs2-system/fs2-serve-ip-acme-production`.
Validated SANs: `DNS:forge.nebius.cloud`, `IP Address:89.169.99.188`.
New expiry: `2026-09-23T07:32:14Z`.
Next automated renewal: `2026-09-20T15:32:14Z`.

## Verification

Fresh TLS connections with normal trust and hostname checking succeed for both
the hostname and IP. The domain landing page returns HTTP 200. Certificate
Ready=True, observed generation 3, revision 6. HTTP redirects to the same
hostname over HTTPS. No certificate verification bypass was used for acceptance.

## Deployment handoff

This was a narrow live Certificate repair during concurrent workshop development,
not a shared control-plane Helm upgrade. The current chart's `certificate.yaml`
only renders `ipAddresses`; keep the DNS SAN when reconciling or recreating this
resource, and add a configurable DNS SAN list when integrating the domain into
the deployment configuration. This side task did not modify shared chart files,
Terraform state, or the workshop worktree. Do not mistake a working certificate
for a completed canonical API/OAuth/base-URL migration: those remain unchanged.

Let's Encrypt's existing `shortlived` profile supports both DNS and IP identifiers:
<https://letsencrypt.org/ca/docs/profiles/#shortlived>.
