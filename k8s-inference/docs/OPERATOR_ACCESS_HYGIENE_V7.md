# Authoritative predecessor adoption and backend isolation v7

> Superseded by [v8](OPERATOR_ACCESS_HYGIENE_V8.md), which derives
> feature-gated credential presence from exact authoritative Terraform state
> markers rather than treating every supported feature as unconditional.

This source-only successor preserves v6 and supersedes its custody contract. It
does not authorize integration or deployment. Production trust and provider
adapters remain unconfigured, accepted SAI-08/SAI-09 integration remains
absent, and SAI-06 has static SOURCE GO only. This revision makes no live or
external change.

## Exact, no-mutation generation-one adoption

Some fixed generation-one Kubernetes Secrets predate custody annotations and
were created mutable. They cannot be changed in place merely to satisfy a new
contract. The durable registry therefore carries a finite allowlist of exact
Terraform root, address, and credential-class triples. A legacy predecessor is
adopted only when canonical remote state and the provider-observed live object
agree on namespace, name, UID, resourceVersion, mutable/immutable state, and a
provider-produced decoded-data commitment. Missing annotations are tolerated;
any annotation that exists must agree with the registry class, generation one,
and the provider commitment.

This exception is read-only and generation-one-only. It cannot authorize a
move, alias, create, update, replacement, current-write selection, retirement,
or any generation-two-or-later object. Every versioned successor still requires
complete class/generation/content annotations, `immutable=true`, write-only
Terraform data, and the normal staged readiness fence. Rollback selects a
retained predecessor through a new additive configuration generation; it never
mutates the predecessor.

## Required and optional class activation

The registry separates required classes from explicitly optional classes.
Required classes must be present in authoritative state. An optional class is
disabled only when every registry-declared activation address is absent. If any
activation address exists, all must exist or reconciliation fails as a partial
installation.

The signed inventory records required, enabled, and absent-optional sets. A
readiness receipt invokes adapters only for enabled classes and embeds the full
externally verified inventory that proves each optional absence. Disabled
classes have no current generation, retained generations, source trust, Secret
bindings, rotation readiness, or consumer-readiness claim. Enabling an optional
class requires a fresh authoritative inventory and its full normal readiness
sequence.

## Closed AWS/S3 credential-provider chain

Every backend identity policy now pins a root-owned mode-0644 empty regular
file by the SHA-256 of an empty byte string. Terraform receives that path as
`AWS_SHARED_CREDENTIALS_FILE`; its environment removes home/config discovery
variables, strips inherited AWS/S3 variables, disables instance metadata, and
selects only the attested purpose profile and configuration.

The pinned backend-session adapter must attest the actual credential source as
a provider workload-identity exchange, a non-secret provider identity
commitment, and that environment, shared-file, and instance-metadata
credentials were not used. The authority binds those facts into the session
commitment that state custody and migration observations must echo. Neither a
workstation home directory nor an ambient shared-credentials file can satisfy
the contract.

## Rollback and stop condition

Rollback remains additive and forward-only. Preserve legacy state, every key
generation, every journal event, and all customer data. Do not delete, revoke,
replace, overwrite, mutate a fixed predecessor, or restore exposed plaintext
artifacts.

Only deletion-free static inspection is permitted for this revision. Dynamic
tests, formatting tools, Terraform, Helm, live providers, clusters, databases,
registries, credential operations, deployment, and cleanup remain blocked.
