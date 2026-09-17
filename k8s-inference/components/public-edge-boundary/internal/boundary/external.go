package boundary

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
)

// VerifyExternalEnvelope verifies an exact response envelope against a
// separately custodied, root-owned trust registry. It is used by the native
// collector for provider, control-plane, KMS/classifier, CRL/OCSP and snapshot
// authority responses; digest-shaped assertions are never accepted in place
// of the signed response bytes.
func VerifyExternalEnvelope(
	trustPath string,
	expectedTrustSchema string,
	expectedEnvelopeSchema string,
	expectedIssuerRole string,
	envelopeRaw []byte,
) ([]byte, error) {
	trustRaw, err := readProtectedRegular(trustPath, maxTrustBytes)
	if err != nil {
		return nil, fmt.Errorf("read external response trust: %w", err)
	}
	var trust TrustRegistry
	if err := decodeExactJSON(trustRaw, &trust); err != nil {
		return nil, fmt.Errorf("decode external response trust: %w", err)
	}
	if trust.Schema != expectedTrustSchema || len(trust.Issuers) == 0 {
		return nil, errors.New("external response trust is empty or has the wrong schema")
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
	key, err := trustedKey(trust, envelope.Issuer, envelope.KeyID, expectedIssuerRole)
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
