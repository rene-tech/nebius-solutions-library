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
	AcceptancePayloadSchema  = "fs2-serve.nebius.ai/public-edge-boundary-acceptance/v1"
	acceptanceIssuerRole     = "platform-security-public-edge-boundary-acceptance"
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
	BoundaryTLSCertificateSHA256 string `json:"boundary_tls_certificate_sha256"`
	BoundaryTLSPrivateKeySHA256 string `json:"boundary_tls_private_key_sha256"`
	BoundaryTLSSPKISHA256       string `json:"boundary_tls_spki_sha256"`
	SourceCommit              string `json:"source_commit"`
	SourceTree                string `json:"source_tree"`
}

func LoadAcceptance(trustPath string, envelopePath string, executableRole string) (Acceptance, error) {
	trustRaw, err := readProtectedRegular(trustPath, maxTrustBytes)
	if err != nil {
		return Acceptance{}, fmt.Errorf("read acceptance trust registry: %w", err)
	}
	var trust TrustRegistry
	if err := decodeExactJSON(trustRaw, &trust); err != nil {
		return Acceptance{}, fmt.Errorf("decode acceptance trust registry: %w", err)
	}
	if trust.Schema != AcceptanceTrustSchema || len(trust.Issuers) == 0 {
		return Acceptance{}, errors.New("acceptance trust registry is empty or unsupported")
	}
	envelopeRaw, err := readProtectedRegular(envelopePath, maxTrustBytes)
	if err != nil {
		return Acceptance{}, fmt.Errorf("read acceptance envelope: %w", err)
	}
	var envelope SignedEnvelope
	if err := decodeExactJSON(envelopeRaw, &envelope); err != nil {
		return Acceptance{}, fmt.Errorf("decode acceptance envelope: %w", err)
	}
	if envelope.Schema != AcceptanceEnvelopeSchema || envelope.Algorithm != "ed25519" {
		return Acceptance{}, errors.New("acceptance envelope has an unsupported signature contract")
	}
	payloadRaw, err := decodeCanonicalBase64(envelope.PayloadBase64)
	if err != nil || digestHex(payloadRaw) != envelope.PayloadSHA256 {
		return Acceptance{}, errors.New("acceptance payload bytes do not match their digest")
	}
	key, err := trustedKey(trust, envelope.Issuer, envelope.KeyID, acceptanceIssuerRole)
	if err != nil {
		return Acceptance{}, err
	}
	signature, err := decodeCanonicalBase64URL(envelope.Signature, ed25519.SignatureSize)
	if err != nil {
		return Acceptance{}, fmt.Errorf("decode acceptance signature: %w", err)
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
		return Acceptance{}, errors.New("acceptance Ed25519 signature verification failed")
	}
	var acceptance Acceptance
	if err := decodeExactJSON(payloadRaw, &acceptance); err != nil {
		return Acceptance{}, fmt.Errorf("decode acceptance payload: %w", err)
	}
	canonical, err := json.Marshal(acceptance)
	if err != nil || !bytes.Equal(canonical, payloadRaw) {
		return Acceptance{}, errors.New("acceptance payload is not canonical JSON")
	}
	if acceptance.Schema != AcceptancePayloadSchema || !safeText(acceptance.ClusterID, false) ||
		!safeText(acceptance.DeploymentID, false) || !isSHA256(acceptance.AuthoritySnapshotID) ||
		!isSHA256(acceptance.AuthorityClosureSHA256) || !isSHA256(acceptance.SnapshotTrustSHA256) ||
		!isSHA256(acceptance.NativeCollectorConfigSHA256) || !isSHA256(acceptance.NativeAuthorityConfigSHA256) ||
		!isSHA256(acceptance.NativeResponseTrustSHA256) || !isSHA256(acceptance.MandatoryCollectionsSHA256) || !isSHA256(acceptance.BoundaryExecutableSHA256) ||
		!isSHA256(acceptance.CollectorExecutableSHA256) || !isSHA256(acceptance.NativeAuthorityExecutableSHA256) ||
		!isSHA256(acceptance.BoundaryTLSCertificateSHA256) || !isSHA256(acceptance.BoundaryTLSPrivateKeySHA256) ||
		!isSHA256(acceptance.BoundaryTLSSPKISHA256) ||
		!isCommit(acceptance.SourceCommit) || !isCommit(acceptance.SourceTree) {
		return Acceptance{}, errors.New("acceptance payload does not bind exact source, executables and authority evidence")
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
	default:
		return Acceptance{}, errors.New("acceptance executable role is unsupported")
	}
	if executableSHA256 != expectedExecutableSHA256 {
		return Acceptance{}, errors.New("running executable bytes differ from the independently accepted digest")
	}
	return acceptance, nil
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
