# Static custody verifier

This is the source for the only signature/X.509 verifier accepted by
`inference-stack`. Build it for Linux with `CGO_ENABLED=0`, publish it as a
provenance-bound artifact, and install the reviewed bytes at
`/opt/fs2/bin/fs2-custody-verifier`. The deployment contract must pin that
artifact's exact SHA-256.

The launcher reads the root-owned artifact once with descriptor-relative
`O_NOFOLLOW`, rejects any ELF containing `PT_INTERP`, copies the exact bytes to
a sealed memfd, and executes only that copy with an empty/minimal environment.
The verifier uses only Go's statically linked crypto/X.509 implementation. It
does not load OpenSSL configuration, engines, provider modules, a dynamic
loader, shared libraries, a shell, or caller-selected code.

Its deliberately narrow interface supports detached SHA-256 signatures, exact
certificate DER normalization, and extraction of SPIFFE URI SANs. Signature
documents, signatures, and public keys must be inherited sealed memfds; X.509
input is the already single-read byte string supplied on stdin.
