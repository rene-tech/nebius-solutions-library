package boundary

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
)

const (
	AcceptanceTrustSchema    = "fs2-serve.nebius.ai/public-edge-boundary-acceptance-trust/v1"
	AcceptanceEnvelopeSchema = "fs2-serve.nebius.ai/public-edge-boundary-acceptance-envelope/v1"
	LegacyAcceptancePayloadSchema = "fs2-serve.nebius.ai/public-edge-boundary-acceptance/v1"
	PreviousAcceptancePayloadSchema = "fs2-serve.nebius.ai/public-edge-boundary-acceptance/v2"
	RotatingAcceptancePayloadSchema = "fs2-serve.nebius.ai/public-edge-boundary-acceptance/v3"
	AcceptancePayloadSchema  = "fs2-serve.nebius.ai/public-edge-boundary-acceptance/v4"
	acceptanceIssuerRole     = "platform-security-public-edge-boundary-acceptance"
	maximumAcceptanceClusterIDBytes = 128
	maximumAcceptanceDeploymentIDBytes = 128
)

type Acceptance struct {
	Schema                    string `json:"schema"`
	ClusterID                 string `json:"cluster_id"`
	DeploymentID              string `json:"deployment_id"`
	AuthoritySnapshotID       string `json:"authority_snapshot_id"`
	AuthorityClosureSHA256    string `json:"authority_closure_sha256"`
	SnapshotTrustSHA256       string `json:"snapshot_trust_sha256"`
	NativeCollectorConfigSHA256 string `json:"native_collector_config_sha256"`
	NativeAuthorityConfigSHA256 string `json:"native_authority_config_sha256"`
	NativeResponseTrustSHA256 string `json:"native_response_trust_sha256"`
	MandatoryCollectionsSHA256 string `json:"mandatory_collections_sha256"`
	BoundaryExecutableSHA256  string `json:"boundary_executable_sha256"`
	CollectorExecutableSHA256 string `json:"collector_executable_sha256"`
	NativeAuthorityExecutableSHA256 string `json:"native_authority_executable_sha256"`
	SnapshotAuthorityConfigSHA256 string `json:"snapshot_authority_config_sha256"`
	SnapshotAuthorityExecutableSHA256 string `json:"snapshot_authority_executable_sha256"`
	TransitionAuthorityConfigSHA256 string `json:"transition_authority_config_sha256"`
	TransitionAuthorityExecutableSHA256 string `json:"transition_authority_executable_sha256"`
	BoundaryTLSCertificateSHA256 string `json:"boundary_tls_certificate_sha256"`
	BoundaryTLSPrivateKeySHA256 string `json:"boundary_tls_private_key_sha256"`
	BoundaryTLSSPKISHA256       string `json:"boundary_tls_spki_sha256"`
	BoundaryAdmissionClientTrustSHA256 string `json:"boundary_admission_client_trust_sha256"`
	TransitionSettlementConfigSHA256 string `json:"transition_settlement_config_sha256"`
	BoundaryRuntimeReaderGID uint32 `json:"boundary_runtime_reader_gid"`
	AcceptanceTrustSHA256 string `json:"acceptance_trust_sha256"`
	PredecessorAcceptanceEnvelopeSHA256 string `json:"predecessor_acceptance_envelope_sha256"`
	PredecessorAcceptanceTrustSHA256 string `json:"predecessor_acceptance_trust_sha256"`
	SourceCommit              string `json:"source_commit"`
	SourceTree                string `json:"source_tree"`
	trustRaw                  []byte
	envelopeRaw               []byte
}

type acceptanceV3 struct {
	Schema                    string `json:"schema"`
	ClusterID                 string `json:"cluster_id"`
	DeploymentID              string `json:"deployment_id"`
	AuthoritySnapshotID       string `json:"authority_snapshot_id"`
	AuthorityClosureSHA256    string `json:"authority_closure_sha256"`
	SnapshotTrustSHA256       string `json:"snapshot_trust_sha256"`
	NativeCollectorConfigSHA256 string `json:"native_collector_config_sha256"`
	NativeAuthorityConfigSHA256 string `json:"native_authority_config_sha256"`
	NativeResponseTrustSHA256 string `json:"native_response_trust_sha256"`
	MandatoryCollectionsSHA256 string `json:"mandatory_collections_sha256"`
	BoundaryExecutableSHA256  string `json:"boundary_executable_sha256"`
	CollectorExecutableSHA256 string `json:"collector_executable_sha256"`
	NativeAuthorityExecutableSHA256 string `json:"native_authority_executable_sha256"`
	BoundaryTLSCertificateSHA256 string `json:"boundary_tls_certificate_sha256"`
	BoundaryTLSPrivateKeySHA256 string `json:"boundary_tls_private_key_sha256"`
	BoundaryTLSSPKISHA256       string `json:"boundary_tls_spki_sha256"`
	BoundaryAdmissionClientTrustSHA256 string `json:"boundary_admission_client_trust_sha256"`
	TransitionSettlementConfigSHA256 string `json:"transition_settlement_config_sha256"`
	BoundaryRuntimeReaderGID uint32 `json:"boundary_runtime_reader_gid"`
	AcceptanceTrustSHA256 string `json:"acceptance_trust_sha256"`
	PredecessorAcceptanceEnvelopeSHA256 string `json:"predecessor_acceptance_envelope_sha256"`
	PredecessorAcceptanceTrustSHA256 string `json:"predecessor_acceptance_trust_sha256"`
	SourceCommit              string `json:"source_commit"`
	SourceTree                string `json:"source_tree"`
}

type acceptanceV2 struct {
	Schema                    string `json:"schema"`
	ClusterID                 string `json:"cluster_id"`
	DeploymentID              string `json:"deployment_id"`
	AuthoritySnapshotID       string `json:"authority_snapshot_id"`
	AuthorityClosureSHA256    string `json:"authority_closure_sha256"`
	SnapshotTrustSHA256       string `json:"snapshot_trust_sha256"`
	NativeCollectorConfigSHA256 string `json:"native_collector_config_sha256"`
	NativeAuthorityConfigSHA256 string `json:"native_authority_config_sha256"`
	NativeResponseTrustSHA256 string `json:"native_response_trust_sha256"`
	MandatoryCollectionsSHA256 string `json:"mandatory_collections_sha256"`
	BoundaryExecutableSHA256  string `json:"boundary_executable_sha256"`
	CollectorExecutableSHA256 string `json:"collector_executable_sha256"`
	NativeAuthorityExecutableSHA256 string `json:"native_authority_executable_sha256"`
	BoundaryTLSCertificateSHA256 string `json:"boundary_tls_certificate_sha256"`
	BoundaryTLSPrivateKeySHA256 string `json:"boundary_tls_private_key_sha256"`
	BoundaryTLSSPKISHA256       string `json:"boundary_tls_spki_sha256"`
	BoundaryAdmissionClientTrustSHA256 string `json:"boundary_admission_client_trust_sha256"`
	TransitionSettlementConfigSHA256 string `json:"transition_settlement_config_sha256"`
	BoundaryRuntimeReaderGID uint32 `json:"boundary_runtime_reader_gid"`
	SourceCommit              string `json:"source_commit"`
	SourceTree                string `json:"source_tree"`
}

func LoadAcceptance(trustPath string, envelopePath string, executableRole string) (Acceptance, error) {
	trustRaw, err := readProtectedRegular(trustPath, maxTrustBytes)
	if err != nil {
		return Acceptance{}, fmt.Errorf("read acceptance trust registry: %w", err)
	}
	envelopeRaw, err := readProtectedRegular(envelopePath, maxTrustBytes)
	if err != nil {
		return Acceptance{}, fmt.Errorf("read acceptance envelope: %w", err)
	}
	acceptance, err := VerifyAcceptanceGeneration(trustRaw, envelopeRaw)
	if err != nil {
		return Acceptance{}, err
	}
	executableSHA256, err := runningExecutableSHA256()
	if err != nil {
		return Acceptance{}, fmt.Errorf("measure running executable: %w", err)
	}
	expectedExecutableSHA256 := ""
	switch executableRole {
	case "boundary":
		expectedExecutableSHA256 = acceptance.BoundaryExecutableSHA256
	case "collector":
		expectedExecutableSHA256 = acceptance.CollectorExecutableSHA256
	case "native-authority":
		expectedExecutableSHA256 = acceptance.NativeAuthorityExecutableSHA256
	case "snapshot-authority":
		if acceptance.Schema != AcceptancePayloadSchema {
			return Acceptance{}, errors.New("snapshot authority requires an additive acceptance v4 generation")
		}
		expectedExecutableSHA256 = acceptance.SnapshotAuthorityExecutableSHA256
	case "transition-authority":
		if acceptance.Schema != AcceptancePayloadSchema {
			return Acceptance{}, errors.New("transition authority requires an additive acceptance v4 generation")
		}
		expectedExecutableSHA256 = acceptance.TransitionAuthorityExecutableSHA256
	default:
		return Acceptance{}, errors.New("acceptance executable role is unsupported")
	}
	if executableSHA256 != expectedExecutableSHA256 {
		return Acceptance{}, errors.New("running executable bytes differ from the independently accepted digest")
	}
	return acceptance, nil
}

// VerifyAcceptanceGeneration authenticates retained acceptance bytes without
// comparing them to the currently running executable. Historical selectors
// use it only for signature/pin reconstruction; serving eligibility is still
// anchored in the current process acceptance and its signed predecessor chain.
func VerifyAcceptanceGeneration(trustRaw []byte, envelopeRaw []byte) (Acceptance, error) {
	payloadRaw, err := verifyAcceptanceEnvelopeSignature(trustRaw, envelopeRaw)
	if err != nil {
		return Acceptance{}, err
	}
	trustSHA256 := digestHex(trustRaw)
	var schemaOnly struct {
		Schema string `json:"schema"`
	}
	if err := rejectDuplicateKeys(payloadRaw); err != nil || json.Unmarshal(payloadRaw, &schemaOnly) != nil {
		return Acceptance{}, errors.New("acceptance payload schema cannot be decoded exactly")
	}
	if schemaOnly.Schema == LegacyAcceptancePayloadSchema {
		return Acceptance{}, errors.New("legacy acceptance v1 is retained evidence but cannot be reinterpreted as v2, v3 or v4; install a separately signed additive acceptance generation")
	}
	var acceptance Acceptance
	if schemaOnly.Schema == PreviousAcceptancePayloadSchema {
		var previous acceptanceV2
		if err := decodeExactJSON(payloadRaw, &previous); err != nil {
			return Acceptance{}, fmt.Errorf("decode acceptance v2 payload: %w", err)
		}
		canonical, marshalErr := json.Marshal(previous)
		if marshalErr != nil || !bytes.Equal(canonical, payloadRaw) || json.Unmarshal(payloadRaw, &acceptance) != nil {
			return Acceptance{}, errors.New("acceptance v2 payload is not canonical JSON")
		}
	} else if schemaOnly.Schema == RotatingAcceptancePayloadSchema {
		var previous acceptanceV3
		if err := decodeExactJSON(payloadRaw, &previous); err != nil {
			return Acceptance{}, fmt.Errorf("decode acceptance v3 payload: %w", err)
		}
		canonical, marshalErr := json.Marshal(previous)
		if marshalErr != nil || !bytes.Equal(canonical, payloadRaw) || json.Unmarshal(payloadRaw, &acceptance) != nil {
			return Acceptance{}, errors.New("acceptance v3 payload is not canonical JSON")
		}
	} else {
		if err := decodeExactJSON(payloadRaw, &acceptance); err != nil {
			return Acceptance{}, fmt.Errorf("decode acceptance payload: %w", err)
		}
		canonical, marshalErr := json.Marshal(acceptance)
		if marshalErr != nil || !bytes.Equal(canonical, payloadRaw) {
			return Acceptance{}, errors.New("acceptance payload is not canonical JSON")
		}
	}
	if acceptance.Schema != AcceptancePayloadSchema && acceptance.Schema != RotatingAcceptancePayloadSchema &&
		acceptance.Schema != PreviousAcceptancePayloadSchema {
		return Acceptance{}, errors.New("acceptance payload schema is unsupported")
	}
	predecessorPresent := acceptance.PredecessorAcceptanceEnvelopeSHA256 != "" || acceptance.PredecessorAcceptanceTrustSHA256 != ""
	rotatingSchema := AcceptanceSupportsRotation(acceptance.Schema)
	newAuthorityFieldsPresent := acceptance.SnapshotAuthorityConfigSHA256 != "" || acceptance.SnapshotAuthorityExecutableSHA256 != "" ||
		acceptance.TransitionAuthorityConfigSHA256 != "" || acceptance.TransitionAuthorityExecutableSHA256 != ""
	if !safeText(acceptance.ClusterID, false) || len(acceptance.ClusterID) > maximumAcceptanceClusterIDBytes ||
		!safeText(acceptance.DeploymentID, false) || len(acceptance.DeploymentID) > maximumAcceptanceDeploymentIDBytes || !isSHA256(acceptance.AuthoritySnapshotID) ||
		!isSHA256(acceptance.AuthorityClosureSHA256) || !isSHA256(acceptance.SnapshotTrustSHA256) ||
		!isSHA256(acceptance.NativeCollectorConfigSHA256) || !isSHA256(acceptance.NativeAuthorityConfigSHA256) ||
		!isSHA256(acceptance.NativeResponseTrustSHA256) || !isSHA256(acceptance.MandatoryCollectionsSHA256) || !isSHA256(acceptance.BoundaryExecutableSHA256) ||
		!isSHA256(acceptance.CollectorExecutableSHA256) || !isSHA256(acceptance.NativeAuthorityExecutableSHA256) ||
			!isSHA256(acceptance.BoundaryTLSCertificateSHA256) || !isSHA256(acceptance.BoundaryTLSPrivateKeySHA256) ||
			!isSHA256(acceptance.BoundaryTLSSPKISHA256) || !isSHA256(acceptance.BoundaryAdmissionClientTrustSHA256) ||
			!isSHA256(acceptance.TransitionSettlementConfigSHA256) || acceptance.BoundaryRuntimeReaderGID == 0 ||
			(acceptance.Schema == AcceptancePayloadSchema &&
				(!isSHA256(acceptance.SnapshotAuthorityConfigSHA256) || !isSHA256(acceptance.SnapshotAuthorityExecutableSHA256) ||
					!isSHA256(acceptance.TransitionAuthorityConfigSHA256) || !isSHA256(acceptance.TransitionAuthorityExecutableSHA256))) ||
			(acceptance.Schema != AcceptancePayloadSchema && newAuthorityFieldsPresent) ||
			(rotatingSchema && acceptance.AcceptanceTrustSHA256 != trustSHA256) ||
			(rotatingSchema && predecessorPresent &&
				(!isSHA256(acceptance.PredecessorAcceptanceEnvelopeSHA256) || !isSHA256(acceptance.PredecessorAcceptanceTrustSHA256))) ||
			(acceptance.Schema == PreviousAcceptancePayloadSchema &&
				(acceptance.AcceptanceTrustSHA256 != "" || predecessorPresent)) ||
		!isCommit(acceptance.SourceCommit) || !isCommit(acceptance.SourceTree) {
		return Acceptance{}, errors.New("acceptance payload does not bind exact source, executables and authority evidence")
	}
	acceptance.trustRaw = append([]byte(nil), trustRaw...)
	acceptance.envelopeRaw = append([]byte(nil), envelopeRaw...)
	return acceptance, nil
}

// AcceptanceSupportsRotation identifies the exact payload versions that bind
// their own trust generation and an optional signed predecessor. V2 remains
// readable historical evidence but cannot be reinterpreted as a rotatable
// authority generation.
func AcceptanceSupportsRotation(schema string) bool {
	return schema == RotatingAcceptancePayloadSchema || schema == AcceptancePayloadSchema
}

func canonicalRotatingAcceptancePayload(acceptance Acceptance) ([]byte, error) {
	if acceptance.Schema == RotatingAcceptancePayloadSchema {
		return json.Marshal(acceptanceV3{
			Schema: acceptance.Schema,
			ClusterID: acceptance.ClusterID,
			DeploymentID: acceptance.DeploymentID,
			AuthoritySnapshotID: acceptance.AuthoritySnapshotID,
			AuthorityClosureSHA256: acceptance.AuthorityClosureSHA256,
			SnapshotTrustSHA256: acceptance.SnapshotTrustSHA256,
			NativeCollectorConfigSHA256: acceptance.NativeCollectorConfigSHA256,
			NativeAuthorityConfigSHA256: acceptance.NativeAuthorityConfigSHA256,
			NativeResponseTrustSHA256: acceptance.NativeResponseTrustSHA256,
			MandatoryCollectionsSHA256: acceptance.MandatoryCollectionsSHA256,
			BoundaryExecutableSHA256: acceptance.BoundaryExecutableSHA256,
			CollectorExecutableSHA256: acceptance.CollectorExecutableSHA256,
			NativeAuthorityExecutableSHA256: acceptance.NativeAuthorityExecutableSHA256,
			BoundaryTLSCertificateSHA256: acceptance.BoundaryTLSCertificateSHA256,
			BoundaryTLSPrivateKeySHA256: acceptance.BoundaryTLSPrivateKeySHA256,
			BoundaryTLSSPKISHA256: acceptance.BoundaryTLSSPKISHA256,
			BoundaryAdmissionClientTrustSHA256: acceptance.BoundaryAdmissionClientTrustSHA256,
			TransitionSettlementConfigSHA256: acceptance.TransitionSettlementConfigSHA256,
			BoundaryRuntimeReaderGID: acceptance.BoundaryRuntimeReaderGID,
			AcceptanceTrustSHA256: acceptance.AcceptanceTrustSHA256,
			PredecessorAcceptanceEnvelopeSHA256: acceptance.PredecessorAcceptanceEnvelopeSHA256,
			PredecessorAcceptanceTrustSHA256: acceptance.PredecessorAcceptanceTrustSHA256,
			SourceCommit: acceptance.SourceCommit,
			SourceTree: acceptance.SourceTree,
		})
	}
	if acceptance.Schema == AcceptancePayloadSchema {
		return json.Marshal(acceptance)
	}
	return nil, errors.New("acceptance payload is not a rotatable generation")
}

func verifyAcceptanceEnvelopeSignature(trustRaw []byte, envelopeRaw []byte) ([]byte, error) {
	var trust TrustRegistry
	if err := decodeExactJSON(trustRaw, &trust); err != nil {
		return nil, fmt.Errorf("decode acceptance trust registry: %w", err)
	}
	if trust.Schema != AcceptanceTrustSchema || len(trust.Issuers) == 0 {
		return nil, errors.New("acceptance trust registry is empty or unsupported")
	}
	var envelope SignedEnvelope
	if err := decodeExactJSON(envelopeRaw, &envelope); err != nil {
		return nil, fmt.Errorf("decode acceptance envelope: %w", err)
	}
	if envelope.Schema != AcceptanceEnvelopeSchema || envelope.Algorithm != "ed25519" {
		return nil, errors.New("acceptance envelope has an unsupported signature contract")
	}
	payloadRaw, err := decodeCanonicalBase64(envelope.PayloadBase64)
	if err != nil || digestHex(payloadRaw) != envelope.PayloadSHA256 {
		return nil, errors.New("acceptance payload bytes do not match their digest")
	}
	key, err := trustedKey(trust, envelope.Issuer, envelope.KeyID, acceptanceIssuerRole)
	if err != nil {
		return nil, err
	}
	signature, err := decodeCanonicalBase64URL(envelope.Signature, ed25519.SignatureSize)
	if err != nil {
		return nil, fmt.Errorf("decode acceptance signature: %w", err)
	}
	message := bytes.Join(
		[][]byte{
			[]byte(AcceptanceEnvelopeSchema),
			[]byte(envelope.Issuer),
			[]byte(envelope.KeyID),
			[]byte(envelope.PayloadSHA256),
			payloadRaw,
		},
		[]byte("\n"),
	)
	if !ed25519.Verify(key, message, signature) {
		return nil, errors.New("acceptance Ed25519 signature verification failed")
	}
	return payloadRaw, nil
}

func (acceptance Acceptance) TrustGeneration() ([]byte, string, error) {
	if len(acceptance.trustRaw) == 0 {
		return nil, "", errors.New("acceptance trust generation bytes are unavailable")
	}
	raw := append([]byte(nil), acceptance.trustRaw...)
	return raw, digestHex(raw), nil
}

func (acceptance Acceptance) EnvelopeGeneration() ([]byte, string, error) {
	if len(acceptance.envelopeRaw) == 0 {
		return nil, "", errors.New("acceptance envelope generation bytes are unavailable")
	}
	raw := append([]byte(nil), acceptance.envelopeRaw...)
	return raw, digestHex(raw), nil
}

// AcceptanceGenerationsRelated proves that two independently signed
// acceptance envelopes are identical or connected by the versioned predecessor
// chain. The loader must reopen and verify the exact content-addressed
// predecessor generation. Both directions are checked so rolling old and new
// processes can verify one another during a bounded current/next handoff.
func AcceptanceGenerationsRelated(current Acceptance, candidate Acceptance, load func(string, string) (Acceptance, error)) error {
	currentTrustRaw, currentTrustDigest, currentTrustErr := current.TrustGeneration()
	_, currentDigest, currentErr := current.EnvelopeGeneration()
	_, candidateTrustDigest, candidateTrustErr := candidate.TrustGeneration()
	_, candidateDigest, candidateErr := candidate.EnvelopeGeneration()
	if currentErr != nil || candidateErr != nil || current.ClusterID != candidate.ClusterID || current.DeploymentID != candidate.DeploymentID {
		return errors.New("acceptance generations do not bind the same exact deployment")
	}
	if currentDigest == candidateDigest {
		if currentTrustErr != nil || candidateTrustErr != nil || currentTrustDigest != candidateTrustDigest {
			return errors.New("identical acceptance envelopes reference different trust generations")
		}
		return nil
	}
	if acceptanceChainContains(current, candidateDigest, candidateTrustDigest, load) {
		return nil
	}
	// A rolling old process may observe only its directly authorized next
	// generation. The next envelope must be signed by a key retained in both
	// trust generations, while its signed payload binds both trust digests and
	// the exact predecessor envelope. This prevents an untrusted replacement
	// registry from authorizing itself.
	if currentTrustErr == nil && AcceptanceSupportsRotation(candidate.Schema) &&
		candidate.PredecessorAcceptanceEnvelopeSHA256 == currentDigest &&
		candidate.PredecessorAcceptanceTrustSHA256 == currentTrustDigest {
		payloadRaw, err := verifyAcceptanceEnvelopeSignature(currentTrustRaw, candidate.envelopeRaw)
		canonical, marshalErr := canonicalRotatingAcceptancePayload(candidate)
		if err == nil && marshalErr == nil && bytes.Equal(payloadRaw, canonical) {
			return nil
		}
	}
	return errors.New("acceptance generation is not in the signed current-to-next predecessor chain")
}

func acceptanceChainContains(start Acceptance, targetDigest string, targetTrustDigest string, load func(string, string) (Acceptance, error)) bool {
	seen := map[string]struct{}{}
	current := start
	for depth := 0; depth < 1024; depth++ {
		predecessor := current.PredecessorAcceptanceEnvelopeSHA256
		predecessorTrust := current.PredecessorAcceptanceTrustSHA256
		if predecessor == "" || predecessorTrust == "" {
			return false
		}
		if predecessor == targetDigest && predecessorTrust == targetTrustDigest {
			return true
		}
		if !isSHA256(predecessor) || !isSHA256(predecessorTrust) {
			return false
		}
		if _, duplicate := seen[predecessor]; duplicate {
			return false
		}
		seen[predecessor] = struct{}{}
		next, err := load(predecessor, predecessorTrust)
		if err != nil || next.ClusterID != start.ClusterID || next.DeploymentID != start.DeploymentID {
			return false
		}
		_, digest, err := next.EnvelopeGeneration()
		_, trustDigest, trustErr := next.TrustGeneration()
		if err != nil || trustErr != nil || digest != predecessor || trustDigest != predecessorTrust {
			return false
		}
		current = next
	}
	return false
}

func runningExecutableSHA256() (string, error) {
	file, err := os.Open("/proc/self/exe")
	if err != nil {
		return "", err
	}
	defer file.Close()
	before, err := file.Stat()
	if err != nil || !before.Mode().IsRegular() {
		return "", errors.New("running executable descriptor is not a regular file")
	}
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(before, after) || before.Size() != after.Size() {
		return "", errors.New("running executable changed while it was measured")
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func isCommit(value string) bool {
	if len(value) != 40 {
		return false
	}
	for _, character := range value {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	return true
}
