package boundary

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
)

// ExternalTrust is an immutable in-process copy of one independently accepted
// trust registry. Loading it once prevents a mutable pathname from becoming an
// authority after acceptance has measured different bytes. Rotation therefore
// requires a new accepted process/config generation; it cannot be smuggled in
// by atomically replacing a root-owned file while this process is running.
type ExternalTrust struct {
	raw      []byte
	registry TrustRegistry
	schema   string
	sha256   string
}

func LoadExternalTrust(
	trustPath string,
	expectedTrustSHA256 string,
	expectedTrustSchema string,
) (*ExternalTrust, error) {
	trustRaw, err := readProtectedRegular(trustPath, maxTrustBytes)
	if err != nil {
		return nil, fmt.Errorf("read external response trust: %w", err)
	}
	if !isSHA256(expectedTrustSHA256) || digestHex(trustRaw) != expectedTrustSHA256 {
		return nil, errors.New("external response trust differs from its independently accepted digest")
	}
	var trust TrustRegistry
	if err := decodeExactJSON(trustRaw, &trust); err != nil {
		return nil, fmt.Errorf("decode external response trust: %w", err)
	}
	if trust.Schema != expectedTrustSchema || len(trust.Issuers) == 0 {
		return nil, errors.New("external response trust is empty or has the wrong schema")
	}
	return &ExternalTrust{
		raw:      bytes.Clone(trustRaw),
		registry: trust,
		schema:   expectedTrustSchema,
		sha256:   expectedTrustSHA256,
	}, nil
}

func (t *ExternalTrust) CanonicalBytes() ([]byte, error) {
	if t == nil || len(t.raw) == 0 || t.registry.Schema != t.schema || digestHex(t.raw) != t.sha256 {
		return nil, errors.New("cached external response trust is unavailable or internally inconsistent")
	}
	return bytes.Clone(t.raw), nil
}

// VerifyEnvelope verifies an exact response against the immutable trust bytes
// loaded from the acceptance-bound file. It never reopens the original path.
func (t *ExternalTrust) VerifyEnvelope(
	expectedEnvelopeSchema string,
	expectedIssuerRole string,
	envelopeRaw []byte,
) ([]byte, error) {
	if t == nil || len(t.raw) == 0 || t.registry.Schema != t.schema || digestHex(t.raw) != t.sha256 {
		return nil, errors.New("cached external response trust is unavailable or internally inconsistent")
	}
	var envelope SignedEnvelope
	if err := decodeExactJSON(envelopeRaw, &envelope); err != nil {
		return nil, fmt.Errorf("decode external response envelope: %w", err)
	}
	if envelope.Schema != expectedEnvelopeSchema || envelope.Algorithm != "ed25519" {
		return nil, errors.New("external response envelope has an unsupported signature contract")
	}
	payloadRaw, err := decodeCanonicalBase64(envelope.PayloadBase64)
	if err != nil || digestHex(payloadRaw) != envelope.PayloadSHA256 {
		return nil, errors.New("external response payload does not match its exact bytes")
	}
	key, err := trustedKey(t.registry, envelope.Issuer, envelope.KeyID, expectedIssuerRole)
	if err != nil {
		return nil, err
	}
	signature, err := decodeCanonicalBase64URL(envelope.Signature, ed25519.SignatureSize)
	if err != nil {
		return nil, fmt.Errorf("decode external response signature: %w", err)
	}
	message := bytes.Join(
		[][]byte{
			[]byte(expectedEnvelopeSchema),
			[]byte(envelope.Issuer),
			[]byte(envelope.KeyID),
			[]byte(envelope.PayloadSHA256),
			payloadRaw,
		},
		[]byte("\n"),
	)
	if !ed25519.Verify(key, message, signature) {
		return nil, errors.New("external response signature verification failed")
	}
	return payloadRaw, nil
}

// VerifyExternalEnvelope remains as a fail-closed compatibility helper for
// callers that have not yet adopted process-lifetime trust caching. It rechecks
// the acceptance digest on the same bounded read before every verification.
func VerifyExternalEnvelope(
	trustPath string,
	expectedTrustSHA256 string,
	expectedTrustSchema string,
	expectedEnvelopeSchema string,
	expectedIssuerRole string,
	envelopeRaw []byte,
) ([]byte, error) {
	trust, err := LoadExternalTrust(trustPath, expectedTrustSHA256, expectedTrustSchema)
	if err != nil {
		return nil, err
	}
	return trust.VerifyEnvelope(expectedEnvelopeSchema, expectedIssuerRole, envelopeRaw)
}

func CanonicalJSON(raw []byte, destination any) ([]byte, error) {
	if err := decodeExactJSON(raw, destination); err != nil {
		return nil, err
	}
	canonical, err := json.Marshal(destination)
	if err != nil || !bytes.Equal(canonical, raw) {
		return nil, errors.New("signed response payload is not canonical JSON")
	}
	return canonical, nil
}

// InspectSignedEnvelopeSchema performs the same exact duplicate/unknown-field
// parsing as signature verification without interpreting a versioned payload.
// Callers use it only after the relevant trust path has authenticated the
// envelope bytes.
func InspectSignedEnvelopeSchema(raw []byte) (string, error) {
	var envelope SignedEnvelope
	if err := decodeExactJSON(raw, &envelope); err != nil {
		return "", err
	}
	return envelope.Schema, nil
}
