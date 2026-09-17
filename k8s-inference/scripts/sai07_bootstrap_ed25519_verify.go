// Command sai07-bootstrap-ed25519-verify verifies exactly one Ed25519
// signature from fixed inherited descriptors. It accepts no arguments, reads
// no environment-controlled configuration, opens no path, loads no plugin,
// and emits no output. The reviewed build contract requires a CGO-disabled,
// non-PIE, statically linked ELF with no PT_INTERP or PT_DYNAMIC segment.
//
// Descriptor protocol:
//
//   204  canonical signed payload (1..8 MiB)
//   205  raw Ed25519 public key (exactly 32 bytes)
//   206  raw Ed25519 signature (exactly 64 bytes)
//
// Exit status is zero only for a valid signature and closed input contract.
package main

import (
	"crypto/ed25519"
	"io"
	"os"
)

const (
	payloadFD     = uintptr(204)
	publicKeyFD   = uintptr(205)
	signatureFD   = uintptr(206)
	maxPayloadLen = 8 * 1024 * 1024
)

func readBounded(fd uintptr, expected int, maximum int) ([]byte, bool) {
	file := os.NewFile(fd, "")
	if file == nil {
		return nil, false
	}
	payload, err := io.ReadAll(io.LimitReader(file, int64(maximum+1)))
	if err != nil || len(payload) == 0 || len(payload) > maximum {
		return nil, false
	}
	if expected >= 0 && len(payload) != expected {
		return nil, false
	}
	return payload, true
}

func main() {
	if len(os.Args) != 1 || len(os.Environ()) != 0 {
		os.Exit(64)
	}
	payload, ok := readBounded(payloadFD, -1, maxPayloadLen)
	if !ok {
		os.Exit(65)
	}
	publicKey, ok := readBounded(publicKeyFD, ed25519.PublicKeySize, ed25519.PublicKeySize)
	if !ok {
		os.Exit(66)
	}
	signature, ok := readBounded(signatureFD, ed25519.SignatureSize, ed25519.SignatureSize)
	if !ok {
		os.Exit(67)
	}
	if !ed25519.Verify(ed25519.PublicKey(publicKey), payload, signature) {
		os.Exit(1)
	}
}
