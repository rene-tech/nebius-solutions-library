package boundary

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"sort"
	"time"
)

const ProductionProvenanceTrustSchema = "fs2-serve.nebius.ai/public-edge-production-trust-provenance-trust/v1"
const ProductionProvenanceEnvelopeSchema = "fs2-serve.nebius.ai/public-edge-production-trust-provenance-envelope/v1"
const ProductionProvenancePayloadSchema = "fs2-serve.nebius.ai/public-edge-production-trust-provenance/v1"
const ProductionProvenanceHeadEnvelopeSchema = "fs2-serve.nebius.ai/public-edge-production-trust-head-envelope/v1"
const ProductionProvenanceHeadPayloadSchema = "fs2-serve.nebius.ai/public-edge-production-trust-head/v1"
const productionProvenanceIssuerRole = "platform-security-public-edge-production-trust-provenance"

const productionProvenanceRootPath = "/usr/local/share/fs2-boundary/trusted-production-trust-provenance-issuers.json"
const productionProvenanceHeadPath = "/usr/local/share/fs2-boundary/accepted-production-trust-head-envelope.json"
const productionProvenanceEnvelopePath = "/usr/local/share/fs2-boundary/production-trust-provenance-envelope.json"
const productionProvenancePredecessorPath = "/usr/local/share/fs2-boundary/production-trust-provenance-predecessor-envelope.json"
const productionAcceptanceTrustPath = "/usr/local/share/fs2-boundary/trusted-acceptance-issuers.json"
const productionNativeTrustPath = "/usr/local/share/fs2-boundary/trusted-native-response-issuers.json"
const productionSnapshotTrustPath = "/usr/local/share/fs2-boundary/trusted-snapshot-issuers.json"

type ProductionTrustState struct {
	Generation uint64
	MinimumGeneration uint64
	EnvelopeSHA256 string
	HeadEnvelopeSHA256 string
	ExpiresAt time.Time
}

type productionProvenanceHeadPayload struct {
	Schema string `json:"schema"`
	MinimumGeneration uint64 `json:"minimum_generation"`
	CurrentGeneration uint64 `json:"current_generation"`
	CurrentEnvelopeSHA256 string `json:"current_envelope_sha256"`
	IssuedAt string `json:"issued_at"`
	ExpiresAt string `json:"expires_at"`
}

type productionProvenancePayload struct {
	Schema string `json:"schema"`
	Generation uint64 `json:"generation"`
	IssuedAt string `json:"issued_at"`
	ExpiresAt string `json:"expires_at"`
	PredecessorEnvelopeSHA256 string `json:"predecessor_envelope_sha256"`
	PredecessorRetiredAt string `json:"predecessor_retired_at"`
	Registries []productionRegistryProvenance `json:"registries"`
}

type productionRegistryProvenance struct {
	Schema string `json:"schema"`
	SHA256 string `json:"sha256"`
	Roles []productionRoleProvenance `json:"roles"`
}

type productionRoleProvenance struct {
	Role string `json:"role"`
	CurrentIssuer string `json:"current_issuer"`
	CurrentKeyID string `json:"current_key_id"`
	NextIssuer string `json:"next_issuer"`
	NextKeyID string `json:"next_key_id"`
}

type productionRegistryContract struct {
	Schema string
	Roles []string
	Raw []byte
}

func VerifyProductionTrustProvenance(rootRaw []byte, headEnvelopeRaw []byte, envelopeRaw []byte, predecessorRaw []byte, acceptanceTrustRaw []byte, nativeTrustRaw []byte, snapshotTrustRaw []byte, now time.Time) (ProductionTrustState, error) {
	headPayloadRaw, err := verifyProductionSignedEnvelope(rootRaw, headEnvelopeRaw, ProductionProvenanceHeadEnvelopeSchema)
	if err != nil { return ProductionTrustState{}, err }
	var head productionProvenanceHeadPayload
	if err := decodeExactJSON(headPayloadRaw, &head); err != nil || head.Schema != ProductionProvenanceHeadPayloadSchema { return ProductionTrustState{}, errors.New("production trust head payload is invalid") }
	headExpiresAt, err := validateProductionProvenanceHead(head, envelopeRaw, now)
	if err != nil { return ProductionTrustState{}, err }
	payloadRaw, err := verifyProductionSignedEnvelope(rootRaw, envelopeRaw, ProductionProvenanceEnvelopeSchema)
	if err != nil { return ProductionTrustState{}, err }
	var payload productionProvenancePayload
	if err := decodeExactJSON(payloadRaw, &payload); err != nil || payload.Schema != ProductionProvenancePayloadSchema { return ProductionTrustState{}, errors.New("production trust provenance payload is invalid") }
	expiresAt, err := validateCurrentProductionProvenance(payload, now)
	if err != nil { return ProductionTrustState{}, err }
	if payload.Generation != head.CurrentGeneration || payload.Generation < head.MinimumGeneration { return ProductionTrustState{}, errors.New("production trust provenance is below or differs from the independently accepted head") }
	if payload.Generation == 1 {
		if payload.PredecessorEnvelopeSHA256 != "" || payload.PredecessorRetiredAt != "" { return ProductionTrustState{}, errors.New("production trust provenance genesis names a predecessor") }
	} else {
		if digestHex(predecessorRaw) != payload.PredecessorEnvelopeSHA256 { return ProductionTrustState{}, errors.New("production trust provenance predecessor digest differs") }
		predecessorPayloadRaw, verifyErr := verifyProductionSignedEnvelope(rootRaw, predecessorRaw, ProductionProvenanceEnvelopeSchema)
		if verifyErr != nil { return ProductionTrustState{}, verifyErr }
		var predecessor productionProvenancePayload
		if decodeErr := decodeExactJSON(predecessorPayloadRaw, &predecessor); decodeErr != nil || predecessor.Schema != ProductionProvenancePayloadSchema { return ProductionTrustState{}, errors.New("production trust predecessor payload is invalid") }
		if err := validateProductionProvenanceSuccessor(predecessor, payload); err != nil { return ProductionTrustState{}, err }
	}
	contracts := []productionRegistryContract{
		{Schema: AcceptanceTrustSchema, Roles: []string{acceptanceIssuerRole, custodyEnrollmentIssuerRole, legacyRuntimeBootstrapIssuerRole}, Raw: acceptanceTrustRaw},
		{Schema: "fs2-serve.nebius.ai/public-edge-native-response-trust/v1", Roles: []string{"provider-control-plane-native-response-authority"}, Raw: nativeTrustRaw},
		{Schema: TrustSchema, Roles: []string{issuerRole}, Raw: snapshotTrustRaw},
	}
	if len(payload.Registries) != len(contracts) { return ProductionTrustState{}, errors.New("production trust provenance registry set is incomplete") }
	for index, contract := range contracts {
		if err := verifyProductionRegistry(contract, payload.Registries[index]); err != nil { return ProductionTrustState{}, err }
	}
	if headExpiresAt.Before(expiresAt) { expiresAt = headExpiresAt }
	return ProductionTrustState{Generation: payload.Generation, MinimumGeneration: head.MinimumGeneration, EnvelopeSHA256: digestHex(envelopeRaw), HeadEnvelopeSHA256: digestHex(headEnvelopeRaw), ExpiresAt: expiresAt}, nil
}

func verifyProductionSignedEnvelope(rootRaw []byte, envelopeRaw []byte, expectedSchema string) ([]byte, error) {
	var root TrustRegistry
	if err := decodeExactJSON(rootRaw, &root); err != nil || root.Schema != ProductionProvenanceTrustSchema || len(root.Issuers) != 2 { return nil, errors.New("production provenance root must contain exact current and next keys") }
	keys := map[string]ed25519.PublicKey{}
	seenPublicKeys := map[string]bool{}
	previous := ""
	for _, issuer := range root.Issuers {
		identity := issuer.Role+"\x00"+issuer.ID+"\x00"+issuer.KeyID
		key, err := decodeCanonicalBase64URL(issuer.PublicKey, ed25519.PublicKeySize)
		if err != nil || issuer.Role != productionProvenanceIssuerRole || !safeText(issuer.ID, false) || len(issuer.ID) > 256 || issuer.KeyID != "sha256:"+digestHex(key) || seenPublicKeys[issuer.PublicKey] || previous != "" && identity <= previous { return nil, errors.New("production provenance root is invalid, reused or non-canonical") }
		keys[issuer.ID+"\x00"+issuer.KeyID] = ed25519.PublicKey(key)
		seenPublicKeys[issuer.PublicKey] = true
		previous = identity
	}
	var envelope SignedEnvelope
	if err := decodeExactJSON(envelopeRaw, &envelope); err != nil || envelope.Schema != expectedSchema || envelope.Algorithm != "ed25519" { return nil, errors.New("production provenance envelope is invalid") }
	payloadRaw, err := decodeCanonicalBase64(envelope.PayloadBase64)
	if err != nil || digestHex(payloadRaw) != envelope.PayloadSHA256 { return nil, errors.New("production provenance payload bytes are invalid") }
	key := keys[envelope.Issuer+"\x00"+envelope.KeyID]
	signature, err := decodeCanonicalBase64URL(envelope.Signature, ed25519.SignatureSize)
	message := bytes.Join([][]byte{[]byte(envelope.Schema), []byte(envelope.Issuer), []byte(envelope.KeyID), []byte(envelope.PayloadSHA256), payloadRaw}, []byte("\n"))
	if err != nil || len(key) != ed25519.PublicKeySize || !ed25519.Verify(key, message, signature) { return nil, errors.New("production provenance signature is invalid") }
	return payloadRaw, nil
}

func validateProductionProvenanceHead(head productionProvenanceHeadPayload, envelopeRaw []byte, now time.Time) (time.Time, error) {
	issuedAt, issueErr := time.Parse(time.RFC3339, head.IssuedAt)
	expiresAt, expiryErr := time.Parse(time.RFC3339, head.ExpiresAt)
	now = now.UTC().Truncate(time.Second)
	if head.MinimumGeneration == 0 || head.CurrentGeneration < head.MinimumGeneration ||
		!isSHA256(head.CurrentEnvelopeSHA256) || head.CurrentEnvelopeSHA256 != digestHex(envelopeRaw) ||
		issueErr != nil || expiryErr != nil || issuedAt.Nanosecond() != 0 || expiresAt.Nanosecond() != 0 ||
		!expiresAt.After(issuedAt) || expiresAt.Sub(issuedAt) > 370*24*time.Hour ||
		issuedAt.After(now.Add(5*time.Minute)) || !now.Before(expiresAt) {
		return time.Time{}, errors.New("production trust head is invalid, stale or does not select the exact provenance generation")
	}
	return expiresAt.UTC(), nil
}

func validateCurrentProductionProvenance(payload productionProvenancePayload, now time.Time) (time.Time, error) {
	issuedAt, issueErr := time.Parse(time.RFC3339, payload.IssuedAt)
	expiresAt, expiryErr := time.Parse(time.RFC3339, payload.ExpiresAt)
	retiredAt := time.Time{}
	retirementErr := error(nil)
	if payload.Generation > 1 { retiredAt, retirementErr = time.Parse(time.RFC3339, payload.PredecessorRetiredAt) }
	now = now.UTC().Truncate(time.Second)
	if payload.Generation == 0 || issueErr != nil || expiryErr != nil || issuedAt.Nanosecond() != 0 || expiresAt.Nanosecond() != 0 || !expiresAt.After(issuedAt) || expiresAt.Sub(issuedAt) > 370*24*time.Hour || issuedAt.After(now.Add(5*time.Minute)) || !now.Before(expiresAt) || payload.Generation > 1 && (!isSHA256(payload.PredecessorEnvelopeSHA256) || retirementErr != nil || retiredAt.Nanosecond() != 0 || !retiredAt.Equal(issuedAt)) { return time.Time{}, errors.New("production trust provenance lifecycle is invalid or expired") }
	return expiresAt.UTC(), nil
}

func validateProductionProvenanceSuccessor(predecessor productionProvenancePayload, successor productionProvenancePayload) error {
	predecessorIssuedAt, predecessorErr := time.Parse(time.RFC3339, predecessor.IssuedAt)
	successorIssuedAt, successorErr := time.Parse(time.RFC3339, successor.IssuedAt)
	if predecessorErr != nil || successorErr != nil || successor.Generation != predecessor.Generation+1 || !successorIssuedAt.After(predecessorIssuedAt) || len(predecessor.Registries) != len(successor.Registries) { return errors.New("production trust provenance successor does not extend its exact predecessor") }
	for registryIndex := range predecessor.Registries {
		before := predecessor.Registries[registryIndex]
		after := successor.Registries[registryIndex]
		if before.Schema != after.Schema || len(before.Roles) != len(after.Roles) { return errors.New("production trust provenance successor changes its registry taxonomy") }
		for roleIndex := range before.Roles {
			beforeRole := before.Roles[roleIndex]
			afterRole := after.Roles[roleIndex]
			if beforeRole.Role != afterRole.Role || afterRole.CurrentIssuer != beforeRole.NextIssuer || afterRole.CurrentKeyID != beforeRole.NextKeyID || afterRole.NextIssuer == beforeRole.CurrentIssuer && afterRole.NextKeyID == beforeRole.CurrentKeyID { return errors.New("production trust provenance successor did not promote next and retire prior current identity") }
		}
	}
	return nil
}

func verifyProductionRegistry(contract productionRegistryContract, provenance productionRegistryProvenance) error {
	if provenance.Schema != contract.Schema || provenance.SHA256 != digestHex(contract.Raw) || len(provenance.Roles) != len(contract.Roles) { return errors.New("production trust registry differs from signed provenance") }
	var registry TrustRegistry
	if err := decodeExactJSON(contract.Raw, &registry); err != nil || registry.Schema != contract.Schema || len(registry.Issuers) != len(contract.Roles)*2 { return errors.New("production trust registry is not exact current/next canonical JSON") }
	byIdentity := map[string]TrustIssuer{}
	roleCounts := map[string]int{}
	seenKeys := map[string]bool{}
	previous := ""
	for _, enrolled := range registry.Issuers {
		identity := enrolled.Role+"\x00"+enrolled.ID+"\x00"+enrolled.KeyID
		key, err := decodeCanonicalBase64URL(enrolled.PublicKey, ed25519.PublicKeySize)
		if err != nil || !containsProductionRole(contract.Roles, enrolled.Role) || !safeText(enrolled.ID, false) || len(enrolled.ID) > 256 || enrolled.KeyID != "sha256:"+digestHex(key) || seenKeys[enrolled.PublicKey] || previous != "" && identity <= previous { return errors.New("production trust registry issuer is invalid, reused or non-canonical") }
		byIdentity[identity] = enrolled
		roleCounts[enrolled.Role]++
		seenKeys[enrolled.PublicKey] = true
		previous = identity
	}
	for index, role := range contract.Roles {
		enrolled := provenance.Roles[index]
		if enrolled.Role != role || roleCounts[role] != 2 || enrolled.CurrentIssuer == "" || enrolled.NextIssuer == "" || enrolled.CurrentIssuer == enrolled.NextIssuer && enrolled.CurrentKeyID == enrolled.NextKeyID { return errors.New("production role lacks exact distinct current and next provenance") }
		current := role+"\x00"+enrolled.CurrentIssuer+"\x00"+enrolled.CurrentKeyID
		next := role+"\x00"+enrolled.NextIssuer+"\x00"+enrolled.NextKeyID
		if byIdentity[current].ID == "" || byIdentity[next].ID == "" { return errors.New("production current/next identity is absent from its signed registry") }
	}
	return nil
}

func containsProductionRole(values []string, expected string) bool { index := sort.SearchStrings(values, expected); return index < len(values) && values[index] == expected }

func verifyRuntimeProductionTrust(acceptance Acceptance, acceptanceTrustRaw []byte, now time.Time) (ProductionTrustState, error) {
	if !AcceptanceSupportsRotation(acceptance.Schema) { return ProductionTrustState{}, errors.New("production runtime requires a rotatable signed acceptance generation") }
	state, bakedAcceptanceTrustRaw, nativeTrustRaw, snapshotTrustRaw, err := loadInstalledProductionTrust(now)
	if err != nil { return ProductionTrustState{}, err }
	if !bytes.Equal(acceptanceTrustRaw, bakedAcceptanceTrustRaw) || acceptance.AcceptanceTrustSHA256 != digestHex(bakedAcceptanceTrustRaw) ||
		acceptance.NativeResponseTrustSHA256 != digestHex(nativeTrustRaw) || acceptance.SnapshotTrustSHA256 != digestHex(snapshotTrustRaw) {
		return ProductionTrustState{}, errors.New("runtime acceptance trust identities differ from the production provenance registries")
	}
	return state, nil
}

// VerifyInstalledProductionTrust authenticates the immutable production head,
// its exact selected provenance generation and all three active trust
// registries. Privileged bootstrap processes that do not consume an acceptance
// payload call this before every bounded filesystem mutation.
func VerifyInstalledProductionTrust(now time.Time) (ProductionTrustState, error) {
	state, _, _, _, err := loadInstalledProductionTrust(now)
	return state, err
}

func loadInstalledProductionTrust(now time.Time) (ProductionTrustState, []byte, []byte, []byte, error) {
	rootRaw, err := readProtectedRegular(productionProvenanceRootPath, maxTrustBytes)
	if err != nil { return ProductionTrustState{}, nil, nil, nil, errors.New("production provenance root is unavailable") }
	headRaw, err := readProtectedRegular(productionProvenanceHeadPath, maxTrustBytes)
	if err != nil { return ProductionTrustState{}, nil, nil, nil, errors.New("production provenance accepted head is unavailable") }
	envelopeRaw, err := readProtectedRegular(productionProvenanceEnvelopePath, maxTrustBytes)
	if err != nil { return ProductionTrustState{}, nil, nil, nil, errors.New("production provenance envelope is unavailable") }
	predecessorRaw, err := readProtectedRegular(productionProvenancePredecessorPath, maxTrustBytes)
	if err != nil { return ProductionTrustState{}, nil, nil, nil, errors.New("production provenance predecessor is unavailable") }
	bakedAcceptanceTrustRaw, err := readProtectedRegular(productionAcceptanceTrustPath, maxTrustBytes)
	if err != nil { return ProductionTrustState{}, nil, nil, nil, errors.New("production acceptance trust is unavailable") }
	nativeTrustRaw, err := readProtectedRegular(productionNativeTrustPath, maxTrustBytes)
	if err != nil { return ProductionTrustState{}, nil, nil, nil, errors.New("production native-response trust is unavailable") }
	snapshotTrustRaw, err := readProtectedRegular(productionSnapshotTrustPath, maxTrustBytes)
	if err != nil { return ProductionTrustState{}, nil, nil, nil, errors.New("production snapshot trust is unavailable") }
	state, err := VerifyProductionTrustProvenance(rootRaw, headRaw, envelopeRaw, predecessorRaw, bakedAcceptanceTrustRaw, nativeTrustRaw, snapshotTrustRaw, now)
	if err != nil { return ProductionTrustState{}, nil, nil, nil, err }
	return state, bakedAcceptanceTrustRaw, nativeTrustRaw, snapshotTrustRaw, nil
}
