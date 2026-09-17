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
