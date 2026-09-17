# Native provider-authority exporter

This is the only production-admissible provider authority exporter. The Python
entry point in the control-plane package is a protocol reference, not a custody
binary.

The reviewed release process builds for Linux with no C toolchain dependency:

```text
CGO_ENABLED=0 go build -trimpath -tags netgo,osusergo -o fs2-provider-authority-exporter .
```

The build itself is intentionally not performed by this source-only task. The
published artifact must be built by the independently reviewed release job,
have its artifact/source commit/source tree/provenance hashes enrolled in the
provider custody v8 document, and be installed root-owned and non-writable.
`inference-stack` reads it once through `O_NOFOLLOW`, verifies the enrolled
digest, rejects scripts and ELF `PT_INTERP`, copies the exact bytes to a sealed
memfd, and executes with a minimal environment. CA, client certificate, and
client key bytes are likewise inherited only through sealed memfds.

The binary supports the normal nonce-bound authority `snapshot`, a separate
read-only `settlement` request, and `journal observe|begin|resolve` operations
against the provider-native apply journal. The journal request body is supplied
through an immutable sealed memfd. `begin` and `resolve` require the current
provider resourceVersion and are append-only CAS operations; the provider API
has no delete endpoint. The provider retains every history event and returns a
root-signed immutable checkpoint containing the journal resourceVersion, event
count, previous-checkpoint link, append-only history accumulator, unresolved
record count and unresolved-view accumulator. `observe` returns at most 256
globally ordered unresolved records per page. Every page is pinned to the same
checkpoint and supplies exact ordinals, a records digest, the incoming
accumulator and the resulting accumulator; the next request carries both its
opaque cursor and the checkpoint digest. `begin` and `resolve` return only the
affected signed record, after which the wrapper reads the complete unresolved
view at that exact checkpoint. Every marker, resolution and checkpoint carries
a detached signature under the root-enrolled provider custody key. The wrapper
validates those signatures, every contiguous range proof and the terminal
unresolved accumulator before every mutation gate; TLS alone is not treated as
a receipt. Historical rows are never deleted or replayed in an unbounded
response, so journal lifetime does not impose a fixed record-count outage.
Every CAS request binds both the current resourceVersion and checkpoint digest;
the signed successor advances exactly one event and links back to that digest.

Settlement asks the
provider authority API to enumerate every operation accepted under the aborted
apply's unique Terraform user-agent and credential epoch, including an empty
set, and to report active/terminal state plus an audit-log high-water mark.
`inference-stack` requires two stable observations with no active operation IDs
before it refreshes local Terraform state. The command never cancels or mutates
a provider operation.
