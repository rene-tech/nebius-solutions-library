package boundary

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"time"
)

const (
	LegacyRuntimeBootstrapEnvelopeSchema = "fs2-serve.nebius.ai/public-edge-legacy-runtime-bootstrap-envelope/v1"
	PreviousLegacyRuntimeBootstrapPayloadSchema = "fs2-serve.nebius.ai/public-edge-legacy-runtime-bootstrap/v1"
	LegacyRuntimeBootstrapPayloadSchema  = "fs2-serve.nebius.ai/public-edge-legacy-runtime-bootstrap/v2"
	legacyRuntimeBootstrapIssuerRole     = "platform-security-public-edge-boundary-legacy-bootstrap"
	maximumLegacyRuntimeBootstrapLifetime = 7 * 24 * time.Hour
)

// LegacyRuntimeBootstrap authenticates retained bytes only. It never turns a
// legacy snapshot into a Runtime and cannot authorize admission. Its sole
// purpose is to anchor the exact first v3 successor during a non-destructive
// rolling migration.
type LegacyRuntimeBootstrap struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	LegacySelectionSHA256 string `json:"legacy_selection_sha256"`
	LegacySnapshotEnvelopeSHA256 string `json:"legacy_snapshot_envelope_sha256"`
	LegacySnapshotTrustSHA256 string `json:"legacy_snapshot_trust_sha256"`
	LegacySnapshotEnvelopeSchema string `json:"legacy_snapshot_envelope_schema"`
	LegacySnapshotPayloadSchema string `json:"legacy_snapshot_payload_schema"`
	SuccessorAcceptanceEnvelopeSHA256 string `json:"successor_acceptance_envelope_sha256"`
	SuccessorAcceptanceTrustSHA256 string `json:"successor_acceptance_trust_sha256"`
	Generation uint64 `json:"generation"`
	PredecessorBootstrapEnvelopeSHA256 string `json:"predecessor_bootstrap_envelope_sha256"`
	IssuedAt string `json:"issued_at"`
	ExpiresAt string `json:"expires_at"`
	envelopeRaw []byte
}

type legacyRuntimeBootstrapV1 struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	LegacySelectionSHA256 string `json:"legacy_selection_sha256"`
	LegacySnapshotEnvelopeSHA256 string `json:"legacy_snapshot_envelope_sha256"`
	LegacySnapshotTrustSHA256 string `json:"legacy_snapshot_trust_sha256"`
	LegacySnapshotEnvelopeSchema string `json:"legacy_snapshot_envelope_schema"`
	LegacySnapshotPayloadSchema string `json:"legacy_snapshot_payload_schema"`
	SuccessorAcceptanceEnvelopeSHA256 string `json:"successor_acceptance_envelope_sha256"`
	SuccessorAcceptanceTrustSHA256 string `json:"successor_acceptance_trust_sha256"`
	IssuedAt string `json:"issued_at"`
	ExpiresAt string `json:"expires_at"`
}

func LoadLegacyRuntimeBootstrap(
	trustPath string,
	envelopePath string,
	acceptance Acceptance,
	now time.Time,
) (*LegacyRuntimeBootstrap, error) {
	trustRaw, err := readProtectedRegular(trustPath, maxTrustBytes)
	if err != nil {
		return nil, fmt.Errorf("read legacy bootstrap trust: %w", err)
	}
	envelopeRaw, err := readProtectedRegular(envelopePath, maxTrustBytes)
	if err != nil {
		return nil, fmt.Errorf("read legacy bootstrap envelope: %w", err)
	}
	bootstrap, err := VerifyLegacyRuntimeBootstrap(trustRaw, envelopeRaw, now, true)
	if err != nil {
		return nil, err
	}
	if err := bootstrap.RequireExactAcceptance(acceptance); err != nil {
		return nil, err
	}
	return bootstrap, nil
}

func LoadOptionalLegacyRuntimeBootstrap(
	trustPath string,
	envelopePath string,
	acceptance Acceptance,
	now time.Time,
) (*LegacyRuntimeBootstrap, error) {
	trustRaw, err := readProtectedRegular(trustPath, maxTrustBytes)
	if err != nil {
		return nil, fmt.Errorf("read legacy bootstrap trust: %w", err)
	}
	envelopeRaw, err := readProtectedRegular(envelopePath, maxTrustBytes)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read legacy bootstrap envelope: %w", err)
	}
	bootstrap, err := VerifyLegacyRuntimeBootstrap(trustRaw, envelopeRaw, now, false)
	if err != nil {
		return nil, err
	}
	if bootstrap.ClusterID != acceptance.ClusterID || bootstrap.DeploymentID != acceptance.DeploymentID {
		return nil, errors.New("legacy runtime bootstrap targets another deployment")
	}
	return bootstrap, nil
}

func VerifyLegacyRuntimeBootstrap(
	trustRaw []byte,
	envelopeRaw []byte,
	now time.Time,
	requireCurrent bool,
) (*LegacyRuntimeBootstrap, error) {
	payloadRaw, err := verifyLegacyBootstrapEnvelopeSignature(trustRaw, envelopeRaw)
	if err != nil {
		return nil, err
	}
	var schemaOnly struct {
		Schema string `json:"schema"`
	}
	if err := rejectDuplicateKeys(payloadRaw); err != nil || json.Unmarshal(payloadRaw, &schemaOnly) != nil {
		return nil, errors.New("legacy runtime bootstrap schema cannot be decoded exactly")
	}
	var bootstrap LegacyRuntimeBootstrap
	var canonical []byte
	var err error
	if schemaOnly.Schema == PreviousLegacyRuntimeBootstrapPayloadSchema {
		var previous legacyRuntimeBootstrapV1
		if err := decodeExactJSON(payloadRaw, &previous); err != nil {
			return nil, fmt.Errorf("decode legacy runtime bootstrap v1: %w", err)
		}
		canonical, err = json.Marshal(previous)
		bootstrap = LegacyRuntimeBootstrap{
			Schema: previous.Schema, ClusterID: previous.ClusterID, DeploymentID: previous.DeploymentID,
			LegacySelectionSHA256: previous.LegacySelectionSHA256,
			LegacySnapshotEnvelopeSHA256: previous.LegacySnapshotEnvelopeSHA256,
			LegacySnapshotTrustSHA256: previous.LegacySnapshotTrustSHA256,
			LegacySnapshotEnvelopeSchema: previous.LegacySnapshotEnvelopeSchema,
			LegacySnapshotPayloadSchema: previous.LegacySnapshotPayloadSchema,
			SuccessorAcceptanceEnvelopeSHA256: previous.SuccessorAcceptanceEnvelopeSHA256,
			SuccessorAcceptanceTrustSHA256: previous.SuccessorAcceptanceTrustSHA256,
			Generation: 1, IssuedAt: previous.IssuedAt, ExpiresAt: previous.ExpiresAt,
		}
	} else {
		if err := decodeExactJSON(payloadRaw, &bootstrap); err != nil {
			return nil, fmt.Errorf("decode legacy runtime bootstrap: %w", err)
		}
		canonical, err = json.Marshal(bootstrap)
	}
	issuedAt, issueErr := parseWholeUTC(bootstrap.IssuedAt)
	expiresAt, expiryErr := parseWholeUTC(bootstrap.ExpiresAt)
	now = now.UTC().Truncate(time.Second)
	if err != nil || !bytes.Equal(canonical, payloadRaw) ||
		(bootstrap.Schema != LegacyRuntimeBootstrapPayloadSchema && bootstrap.Schema != PreviousLegacyRuntimeBootstrapPayloadSchema) ||
		!safeText(bootstrap.ClusterID, false) || len(bootstrap.ClusterID) > maximumAcceptanceClusterIDBytes ||
		!safeText(bootstrap.DeploymentID, false) || len(bootstrap.DeploymentID) > maximumAcceptanceDeploymentIDBytes ||
		!isSHA256(bootstrap.LegacySelectionSHA256) || !isSHA256(bootstrap.LegacySnapshotEnvelopeSHA256) ||
		!isSHA256(bootstrap.LegacySnapshotTrustSHA256) || !isSHA256(bootstrap.SuccessorAcceptanceEnvelopeSHA256) ||
		!isSHA256(bootstrap.SuccessorAcceptanceTrustSHA256) || issueErr != nil || expiryErr != nil ||
		!legacySnapshotSchemaPair(bootstrap.LegacySnapshotEnvelopeSchema, bootstrap.LegacySnapshotPayloadSchema) ||
		bootstrap.Generation < 1 ||
		(bootstrap.Generation == 1 && bootstrap.PredecessorBootstrapEnvelopeSHA256 != "") ||
		(bootstrap.Generation > 1 && !isSHA256(bootstrap.PredecessorBootstrapEnvelopeSHA256)) ||
		!expiresAt.After(issuedAt) || expiresAt.Sub(issuedAt) > maximumLegacyRuntimeBootstrapLifetime ||
		issuedAt.After(now.Add(30*time.Second)) || requireCurrent && !expiresAt.After(now) {
		return nil, errors.New("legacy runtime bootstrap is non-canonical, expired or incomplete")
	}
	bootstrap.envelopeRaw = append([]byte(nil), envelopeRaw...)
	return &bootstrap, nil
}

func (bootstrap LegacyRuntimeBootstrap) SuccessorOf(predecessor LegacyRuntimeBootstrap, predecessorEnvelopeSHA256 string) error {
	if !isSHA256(predecessorEnvelopeSHA256) || bootstrap.Generation != predecessor.Generation+1 ||
		bootstrap.PredecessorBootstrapEnvelopeSHA256 != predecessorEnvelopeSHA256 ||
		bootstrap.ClusterID != predecessor.ClusterID || bootstrap.DeploymentID != predecessor.DeploymentID ||
		bootstrap.LegacySelectionSHA256 != predecessor.LegacySelectionSHA256 ||
		bootstrap.LegacySnapshotEnvelopeSHA256 != predecessor.LegacySnapshotEnvelopeSHA256 ||
		bootstrap.LegacySnapshotTrustSHA256 != predecessor.LegacySnapshotTrustSHA256 ||
		bootstrap.LegacySnapshotEnvelopeSchema != predecessor.LegacySnapshotEnvelopeSchema ||
		bootstrap.LegacySnapshotPayloadSchema != predecessor.LegacySnapshotPayloadSchema {
		return errors.New("legacy runtime bootstrap renewal does not bind its exact predecessor and legacy anchor")
	}
	return nil
}

func (bootstrap LegacyRuntimeBootstrap) RequireExactAcceptance(acceptance Acceptance) error {
	_, trustSHA256, trustErr := acceptance.TrustGeneration()
	_, envelopeSHA256, envelopeErr := acceptance.EnvelopeGeneration()
	if trustErr != nil || envelopeErr != nil || bootstrap.ClusterID != acceptance.ClusterID ||
		bootstrap.DeploymentID != acceptance.DeploymentID || bootstrap.SuccessorAcceptanceTrustSHA256 != trustSHA256 ||
		bootstrap.SuccessorAcceptanceEnvelopeSHA256 != envelopeSHA256 {
		return errors.New("legacy runtime bootstrap does not bind the exact accepted successor generation")
	}
	return nil
}

func (bootstrap LegacyRuntimeBootstrap) EnvelopeGeneration() ([]byte, string, error) {
	if len(bootstrap.envelopeRaw) == 0 {
		return nil, "", errors.New("legacy runtime bootstrap envelope bytes are unavailable")
	}
	raw := append([]byte(nil), bootstrap.envelopeRaw...)
	return raw, digestHex(raw), nil
}

func (bootstrap LegacyRuntimeBootstrap) Current(now time.Time) bool {
	issuedAt, issueErr := parseWholeUTC(bootstrap.IssuedAt)
	expiresAt, expiryErr := parseWholeUTC(bootstrap.ExpiresAt)
	now = now.UTC().Truncate(time.Second)
	return issueErr == nil && expiryErr == nil && !now.Before(issuedAt) && now.Before(expiresAt)
}

// InspectLegacyRuntimeBootstrapTrustSHA256 returns only the untrusted locator
// needed to reopen a content-addressed trust generation. Callers must then run
// VerifyLegacyRuntimeBootstrap with those exact bytes before using any field.
func InspectLegacyRuntimeBootstrapTrustSHA256(envelopeRaw []byte) (string, error) {
	var envelope SignedEnvelope
	if err := decodeExactJSON(envelopeRaw, &envelope); err != nil || envelope.Schema != LegacyRuntimeBootstrapEnvelopeSchema {
		return "", errors.New("legacy bootstrap envelope locator is invalid")
	}
	payloadRaw, err := decodeCanonicalBase64(envelope.PayloadBase64)
	if err != nil || digestHex(payloadRaw) != envelope.PayloadSHA256 {
		return "", errors.New("legacy bootstrap locator payload is not content addressed")
	}
	var bootstrap LegacyRuntimeBootstrap
	if err := decodeExactJSON(payloadRaw, &bootstrap); err != nil || !isSHA256(bootstrap.SuccessorAcceptanceTrustSHA256) {
		return "", errors.New("legacy bootstrap trust locator is invalid")
	}
	return bootstrap.SuccessorAcceptanceTrustSHA256, nil
}

func VerifyLegacyRuntimeAnchor(
	bootstrap LegacyRuntimeBootstrap,
	selectionRaw []byte,
	snapshotTrustRaw []byte,
	snapshotEnvelopeRaw []byte,
) error {
	if digestHex(selectionRaw) != bootstrap.LegacySelectionSHA256 ||
		digestHex(snapshotTrustRaw) != bootstrap.LegacySnapshotTrustSHA256 ||
		digestHex(snapshotEnvelopeRaw) != bootstrap.LegacySnapshotEnvelopeSHA256 {
		return errors.New("legacy runtime bytes differ from their independently signed bootstrap")
	}
	var trust TrustRegistry
	if err := decodeExactJSON(snapshotTrustRaw, &trust); err != nil || trust.Schema != TrustSchema || len(trust.Issuers) == 0 {
		return errors.New("legacy snapshot trust registry is invalid")
	}
	var envelope SignedEnvelope
	if err := decodeExactJSON(snapshotEnvelopeRaw, &envelope); err != nil ||
		envelope.Schema != bootstrap.LegacySnapshotEnvelopeSchema || envelope.Algorithm != "ed25519" {
		return errors.New("legacy bootstrap accepts only its exact retained snapshot envelope schema")
	}
	payloadRaw, err := decodeCanonicalBase64(envelope.PayloadBase64)
	if err != nil || digestHex(payloadRaw) != envelope.PayloadSHA256 {
		return errors.New("legacy snapshot payload differs from its signed digest")
	}
	key, err := trustedKey(trust, envelope.Issuer, envelope.KeyID, issuerRole)
	if err != nil {
		return err
	}
	signature, err := decodeCanonicalBase64URL(envelope.Signature, ed25519.SignatureSize)
	if err != nil {
		return err
	}
	message := bytes.Join([][]byte{
		[]byte(envelope.Schema), []byte(envelope.Issuer), []byte(envelope.KeyID),
		[]byte(envelope.PayloadSHA256), payloadRaw,
	}, []byte("\n"))
	if !ed25519.Verify(key, message, signature) {
		return errors.New("legacy snapshot v2 signature verification failed")
	}
	var discriminator struct {
		Schema string `json:"schema"`
	}
	if err := rejectDuplicateKeys(payloadRaw); err != nil || json.Unmarshal(payloadRaw, &discriminator) != nil ||
		discriminator.Schema != bootstrap.LegacySnapshotPayloadSchema {
		return errors.New("legacy snapshot payload schema differs from its signed bootstrap")
	}
	return nil
}

func legacySnapshotSchemaPair(envelopeSchema string, payloadSchema string) bool {
	return envelopeSchema == LegacyEnvelopeSchema && payloadSchema == LegacySnapshotSchema ||
		envelopeSchema == PreviousEnvelopeSchema && payloadSchema == PreviousSnapshotSchema
}

func verifyLegacyBootstrapEnvelopeSignature(trustRaw []byte, envelopeRaw []byte) ([]byte, error) {
	var trust TrustRegistry
	if err := decodeExactJSON(trustRaw, &trust); err != nil || trust.Schema != AcceptanceTrustSchema || len(trust.Issuers) == 0 {
		return nil, errors.New("legacy bootstrap trust registry is invalid")
	}
	var envelope SignedEnvelope
	if err := decodeExactJSON(envelopeRaw, &envelope); err != nil ||
		envelope.Schema != LegacyRuntimeBootstrapEnvelopeSchema || envelope.Algorithm != "ed25519" {
		return nil, errors.New("legacy bootstrap envelope has an unsupported signature contract")
	}
	payloadRaw, err := decodeCanonicalBase64(envelope.PayloadBase64)
	if err != nil || digestHex(payloadRaw) != envelope.PayloadSHA256 {
		return nil, errors.New("legacy bootstrap payload bytes do not match their digest")
	}
	key, err := trustedKey(trust, envelope.Issuer, envelope.KeyID, legacyRuntimeBootstrapIssuerRole)
	if err != nil {
		return nil, err
	}
	signature, err := decodeCanonicalBase64URL(envelope.Signature, ed25519.SignatureSize)
	if err != nil {
		return nil, err
	}
	message := bytes.Join([][]byte{
		[]byte(LegacyRuntimeBootstrapEnvelopeSchema), []byte(envelope.Issuer), []byte(envelope.KeyID),
		[]byte(envelope.PayloadSHA256), payloadRaw,
	}, []byte("\n"))
	if !ed25519.Verify(key, message, signature) {
		return nil, errors.New("legacy runtime bootstrap signature verification failed")
	}
	return payloadRaw, nil
}
