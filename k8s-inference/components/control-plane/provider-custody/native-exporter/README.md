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
provider custody v7 document, and be installed root-owned and non-writable.
`inference-stack` reads it once through `O_NOFOLLOW`, verifies the enrolled
digest, rejects scripts and ELF `PT_INTERP`, copies the exact bytes to a sealed
memfd, and executes with a minimal environment. CA, client certificate, and
client key bytes are likewise inherited only through sealed memfds.

The binary supports the normal nonce-bound authority `snapshot` and a separate
read-only `settlement` request used after a fenced apply. Settlement asks the
provider authority API to enumerate every operation accepted under the aborted
apply's unique Terraform user-agent and credential epoch, including an empty
set, and to report active/terminal state plus an audit-log high-water mark.
`inference-stack` requires two stable observations with no active operation IDs
before it refreshes local Terraform state. The command never cancels or mutates
a provider operation.
