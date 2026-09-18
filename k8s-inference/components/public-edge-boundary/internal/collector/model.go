package collector

import "strings"

const (
	LegacyConfigSchema      = "fs2-serve.nebius.ai/public-edge-native-collector-config/v1"
	ConfigSchema            = "fs2-serve.nebius.ai/public-edge-native-collector-config/v2"
	NativeTrustSchema       = "fs2-serve.nebius.ai/public-edge-native-response-trust/v1"
	NativeEnvelopeSchema    = "fs2-serve.nebius.ai/public-edge-native-response-envelope/v1"
	NativePageSchema        = "fs2-serve.nebius.ai/public-edge-native-response/v2"
	NativeIssuerRole        = "provider-control-plane-native-response-authority"
	EvidenceBundleSchema    = "fs2-serve.nebius.ai/public-edge-native-evidence-bundle/v2"
	SnapshotIssuerRole      = "platform-security-public-edge-boundary-runtime"
	LegacyAuthorityConfigSchema = "fs2-serve.nebius.ai/public-edge-native-authority-config/v1"
	AuthorityConfigSchema   = "fs2-serve.nebius.ai/public-edge-native-authority-config/v2"
	CollectionRequestSchema = "fs2-serve.nebius.ai/public-edge-native-collection-request/v2"
	CollectorCycleSchema    = "fs2-serve.nebius.ai/public-edge-native-collection-cycle/v2"
	NativeRequestSchema      = "fs2-serve.nebius.ai/public-edge-native-request/v1"
	AuthoritySettlementSchema = "fs2-serve.nebius.ai/public-edge-native-authority-settlement/v2"
	AuthorityReservationSchema = "fs2-serve.nebius.ai/public-edge-native-authority-reservation/v2"
	AuthorityChallengeSchema  = "fs2-serve.nebius.ai/public-edge-native-authority-challenge/v2"
	IntegrationGateSchema     = "fs2-serve.nebius.ai/public-edge-external-enrollment-gate/v1"
	maximumClusterIDBytes              = 128
	maximumDeploymentIDBytes           = 128
	maximumSourceIDBytes               = 128
	maximumSourceKindBytes             = 64
	maximumSemanticCollectionBytes     = 160
	maximumSigningIssuerBytes          = 128
	maximumSigningKeyIDBytes           = 80
	maximumCredentialLaneIDBytes       = 128
	maximumServerNameBytes             = 253
	maximumSPIFFEURIBytes              = 512
	maximumNativeURLBytes              = 8192
	maximumProviderRequestHeaderBytes  = 128
	maximumProviderRequestIDBytes      = 512
	maximumPageTokenBytes              = 4096
	maximumBearerClaimBytes            = 512
	maximumBearerAlgorithmBytes        = 16
	maximumBearerKeyIDBytes            = 256
	maximumBearerGenerationBytes       = 64
)

func boundedProtocolText(value string, maximum int, allowEmpty bool) bool {
	if value == "" {
		return allowEmpty
	}
	return len(value) <= maximum && !strings.ContainsAny(value, "\x00\r\n")
}

type IntegrationGate struct {
	Schema    string   `json:"schema"`
	Component string   `json:"component"`
	Status    string   `json:"status"`
	Blockers  []string `json:"blockers"`
}

type Config struct {
	Schema                    string       `json:"schema"`
	ClusterID                 string       `json:"cluster_id"`
	DeploymentID              string       `json:"deployment_id"`
	SnapshotAuthorityURL      string       `json:"snapshot_authority_url"`
	SnapshotAuthorityCAPath   string       `json:"snapshot_authority_ca_path"`
	SnapshotAuthorityCASHA256 string       `json:"snapshot_authority_ca_sha256"`
	SnapshotAuthorityName     string       `json:"snapshot_authority_server_name"`
	SnapshotClientCertificate string       `json:"snapshot_client_certificate_path"`
	SnapshotClientCertificateSHA256 string `json:"snapshot_client_certificate_sha256"`
	SnapshotClientKey         string       `json:"snapshot_client_key_path"`
	SnapshotClientKeySHA256   string       `json:"snapshot_client_key_sha256"`
	SnapshotClientSPKISHA256  string       `json:"snapshot_client_spki_sha256"`
	SnapshotClientSPIFFEURI   string       `json:"snapshot_client_spiffe_uri"`
	SnapshotCredentialLaneID  string       `json:"snapshot_credential_lane_id"`
	EvidenceRoot              string       `json:"evidence_root"`
	EvidenceStoreID           string       `json:"evidence_store_id"`
	EvidenceDeviceID          uint64       `json:"evidence_device_id"`
	EvidenceCapacityBytes     uint64       `json:"evidence_capacity_bytes"`
	EvidenceMinimumFreeBytes  uint64       `json:"evidence_minimum_free_bytes"`
	EvidenceCapacityInodes    uint64       `json:"evidence_capacity_inodes"`
	EvidenceMinimumFreeInodes uint64       `json:"evidence_minimum_free_inodes"`
	EvidenceOperatingHorizonDays int       `json:"evidence_operating_horizon_days"`
	RuntimeRoot               string       `json:"runtime_root"`
	RuntimeDeviceID           uint64       `json:"runtime_device_id"`
	RuntimeCapacityBytes      uint64       `json:"runtime_capacity_bytes"`
	RuntimeMinimumFreeBytes   uint64       `json:"runtime_minimum_free_bytes"`
	RuntimeCapacityInodes     uint64       `json:"runtime_capacity_inodes"`
	RuntimeMinimumFreeInodes  uint64       `json:"runtime_minimum_free_inodes"`
	RuntimeOperatingHorizonDays int        `json:"runtime_operating_horizon_days"`
	RuntimeFilesystemBlockBytes uint64     `json:"runtime_filesystem_block_bytes"`
	RuntimeReaderGID            uint32     `json:"runtime_reader_gid"`
	RuntimeDirectoryMode        uint32     `json:"runtime_directory_mode"`
	MaximumBundleBytes        int64        `json:"maximum_bundle_bytes"`
	RefreshIntervalSeconds    int          `json:"refresh_interval_seconds"`
	CollectionDeadlineSeconds int          `json:"collection_deadline_seconds"`
	MaximumConcurrentSources  int          `json:"maximum_concurrent_sources"`
	MandatoryCollections     []string     `json:"mandatory_collections"`
	Sources                   []SourceSpec `json:"sources"`
}

type SourceSpec struct {
	ID               string `json:"id"`
	Kind             string `json:"kind"`
	SemanticCollection string `json:"semantic_collection"`
	InitialURL       string `json:"initial_url"`
	ServerName       string `json:"server_name"`
	CABundlePath     string `json:"ca_bundle_path"`
	ClientCertificate string `json:"client_certificate_path"`
	ClientKey        string `json:"client_key_path"`
	BearerTokenPath  string `json:"bearer_token_path"`
	ProviderRequestIDHeader string `json:"provider_request_id_header"`
	AttestationURL          string `json:"attestation_url"`
	AttestationServerName   string `json:"attestation_server_name"`
	AttestationCABundlePath string `json:"attestation_ca_bundle_path"`
	AttestationCABundleSHA256 string `json:"attestation_ca_bundle_sha256"`
	AttestationClientCertificate string `json:"attestation_client_certificate_path"`
	AttestationClientCertificateSHA256 string `json:"attestation_client_certificate_sha256"`
	AttestationClientKey    string `json:"attestation_client_key_path"`
	AttestationClientKeySHA256 string `json:"attestation_client_key_sha256"`
	AttestationClientSPKISHA256 string `json:"attestation_client_spki_sha256"`
	AttestationClientSPIFFEURI string `json:"attestation_client_spiffe_uri"`
	AttestationCredentialLaneID string `json:"attestation_credential_lane_id"`
	ProviderCredentialLaneID   string `json:"provider_credential_lane_id"`
	ProviderBearerIssuer       string `json:"provider_bearer_issuer"`
	ProviderBearerAudience     string `json:"provider_bearer_audience"`
	ProviderBearerSubject      string `json:"provider_bearer_subject"`
	ProviderBearerAlgorithm    string `json:"provider_bearer_algorithm"`
	ProviderBearerKeyID        string `json:"provider_bearer_key_id"`
	ProviderBearerJWKSHA256    string `json:"provider_bearer_jwks_sha256"`
	MaximumPages     int    `json:"maximum_pages"`
	MaximumPageBytes int64  `json:"maximum_page_bytes"`
	MaximumTotalBytes int64 `json:"maximum_total_bytes"`
	Pagination       string `json:"pagination"`
}

type NativeRequest struct {
	Schema      string            `json:"schema"`
	SourceID    string            `json:"source_id"`
	RequestID   string            `json:"request_id"`
	Method      string            `json:"method"`
	URL         string            `json:"url"`
	BodySHA256  string            `json:"body_sha256"`
	Headers     map[string]string `json:"headers"`
	CredentialLaneID    string `json:"credential_lane_id"`
	CredentialIssuer    string `json:"credential_issuer"`
	CredentialAudience  string `json:"credential_audience"`
	CredentialSubject   string `json:"credential_subject"`
	CredentialExpiresAt string `json:"credential_expires_at"`
	CredentialGeneration string `json:"credential_generation"`
	CredentialAlgorithm string `json:"credential_algorithm"`
	CredentialKeyID string `json:"credential_key_id"`
	CredentialJWKSHA256 string `json:"credential_jwks_sha256"`
}

type CollectionRequest struct {
	Schema       string `json:"schema"`
	ClusterID    string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	SourceID     string `json:"source_id"`
	CycleContractSHA256 string `json:"cycle_contract_sha256"`
	CycleID      string `json:"cycle_id"`
	CycleIssuedAt string `json:"cycle_issued_at"`
	CycleDeadlineAt string `json:"cycle_deadline_at"`
	SessionID    string `json:"session_id"`
	PageIndex    int    `json:"page_index"`
	PageToken    string `json:"page_token"`
	Challenge    string `json:"challenge"`
}

type CollectorCycle struct {
	Schema                 string `json:"schema"`
	ClusterID              string `json:"cluster_id"`
	DeploymentID           string `json:"deployment_id"`
	ContractSHA256         string `json:"contract_sha256"`
	CycleID                 string `json:"cycle_id"`
	IssuedAt                string `json:"issued_at"`
	DeadlineAt              string `json:"deadline_at"`
	RefreshIntervalSeconds  int    `json:"refresh_interval_seconds"`
	CollectionDeadlineSeconds int  `json:"collection_deadline_seconds"`
	CollectorConfigSHA256  string `json:"collector_config_sha256"`
	AuthorityConfigSHA256  string `json:"authority_config_sha256"`
	NativeResponseTrustSHA256 string `json:"native_response_trust_sha256"`
	SnapshotTrustSHA256    string `json:"snapshot_trust_sha256"`
	MandatoryCollectionsSHA256 string `json:"mandatory_collections_sha256"`
	AuthoritySnapshotID    string `json:"authority_snapshot_id"`
	AuthorityClosureSHA256 string `json:"authority_closure_sha256"`
	ChallengeDerivation     string `json:"challenge_derivation"`
	Sources                 []CollectorCycleSource `json:"sources"`
}

type CollectorCycleSource struct {
	SourceID string `json:"source_id"`
	SessionID string `json:"session_id"`
}

type AuthoritySettlement struct {
	Schema                   string `json:"schema"`
	ClusterID                string `json:"cluster_id"`
	DeploymentID             string `json:"deployment_id"`
	SourceID                 string `json:"source_id"`
	CycleContractSHA256      string `json:"cycle_contract_sha256"`
	CycleID                  string `json:"cycle_id"`
	CycleIssuedAt            string `json:"cycle_issued_at"`
	CycleDeadlineAt          string `json:"cycle_deadline_at"`
	SessionID                string `json:"session_id"`
	PageIndex                int    `json:"page_index"`
	PageToken                string `json:"page_token"`
	Challenge                string `json:"challenge"`
	DirectiveSHA256          string `json:"directive_sha256"`
	PreviousSettlementSHA256 string `json:"previous_settlement_sha256"`
	EnvelopeBase64           string `json:"envelope_base64"`
	EnvelopeSHA256           string `json:"envelope_sha256"`
	NextToken                string `json:"next_token"`
	Terminal                 bool   `json:"terminal"`
	SettledAt                string `json:"settled_at"`
}

type AuthorityChallenge struct {
	Schema           string `json:"schema"`
	ClusterID        string `json:"cluster_id"`
	DeploymentID     string `json:"deployment_id"`
	CycleContractSHA256 string `json:"cycle_contract_sha256"`
	CycleID          string `json:"cycle_id"`
	CycleIssuedAt    string `json:"cycle_issued_at"`
	CycleDeadlineAt  string `json:"cycle_deadline_at"`
	DirectiveSHA256  string `json:"directive_sha256"`
	Challenge        string `json:"challenge"`
	SessionID        string `json:"session_id"`
	PageIndex        int    `json:"page_index"`
	SettlementSHA256 string `json:"settlement_sha256"`
}

type AuthorityReservation struct {
	Schema          string `json:"schema"`
	ClusterID       string `json:"cluster_id"`
	DeploymentID    string `json:"deployment_id"`
	SourceID        string `json:"source_id"`
	CycleContractSHA256 string `json:"cycle_contract_sha256"`
	CycleID         string `json:"cycle_id"`
	CycleIssuedAt   string `json:"cycle_issued_at"`
	CycleDeadlineAt string `json:"cycle_deadline_at"`
	SessionID       string `json:"session_id"`
	PageIndex       int    `json:"page_index"`
	PageToken       string `json:"page_token"`
	Challenge       string `json:"challenge"`
	DirectiveSHA256 string `json:"directive_sha256"`
}

type AuthorityConfig struct {
	Schema               string                `json:"schema"`
	ClusterID            string                `json:"cluster_id"`
	DeploymentID         string                `json:"deployment_id"`
	ListenAddress        string                `json:"listen_address"`
	TLSCertificatePath   string                `json:"tls_certificate_path"`
	TLSCertificateSHA256 string                `json:"tls_certificate_sha256"`
	TLSPrivateKeyPath    string                `json:"tls_private_key_path"`
	TLSPrivateKeySHA256  string                `json:"tls_private_key_sha256"`
	TLSSPKISHA256        string                `json:"tls_spki_sha256"`
	ClientCAPath         string                `json:"client_ca_path"`
	ClientCASHA256       string                `json:"client_ca_sha256"`
	CollectorSPKISHA256  string                `json:"collector_spki_sha256"`
	CollectorSPIFFEURI   string                `json:"collector_spiffe_uri"`
	SigningKeyPath       string                `json:"signing_key_path"`
	SigningKeySHA256     string                `json:"signing_key_sha256"`
	SigningIssuer        string                `json:"signing_issuer"`
	SigningKeyID         string                `json:"signing_key_id"`
	SettlementRoot       string                `json:"settlement_root"`
	LedgerEpochID        string                `json:"ledger_epoch_id"`
	PreviousLedgerEpochID string               `json:"previous_ledger_epoch_id"`
	PreviousLedgerRoot    string               `json:"previous_ledger_root"`
	PreviousLedgerDeviceID uint64              `json:"previous_ledger_device_id"`
	PreviousLedgerConfigPath string             `json:"previous_ledger_config_path"`
	PreviousLedgerConfigSHA256 string           `json:"previous_ledger_config_sha256"`
	PreviousLedgerFinalSegment string           `json:"previous_ledger_final_segment"`
	PreviousLedgerFinalHeadSHA256 string        `json:"previous_ledger_final_head_sha256"`
	PreviousLedgerSigningIssuer string          `json:"previous_ledger_signing_issuer"`
	PreviousLedgerSigningKeyID string           `json:"previous_ledger_signing_key_id"`
	PreviousLedgerSigningPublicKey string       `json:"previous_ledger_signing_public_key"`
	PreviousLedgerPredecessorEpochID string     `json:"previous_ledger_predecessor_epoch_id"`
	PreviousLedgerPredecessorHeadSHA256 string  `json:"previous_ledger_predecessor_head_sha256"`
	PreviousLedgerRetirementReceiptPath string  `json:"previous_ledger_retirement_receipt_path"`
	PreviousLedgerRetirementReceiptSHA256 string `json:"previous_ledger_retirement_receipt_sha256"`
	PreviousLedgerRetirementIssuer string       `json:"previous_ledger_retirement_issuer"`
	PreviousLedgerRetirementKeyID string        `json:"previous_ledger_retirement_key_id"`
	PreviousLedgerRetirementPublicKey string    `json:"previous_ledger_retirement_public_key"`
	LedgerCapacityBytes  uint64                `json:"ledger_capacity_bytes"`
	LedgerMinimumFreeBytes uint64              `json:"ledger_minimum_free_bytes"`
	LedgerOperatingHorizonDays int             `json:"ledger_operating_horizon_days"`
	LedgerExpectedMaximumBytesPerDay uint64    `json:"ledger_expected_maximum_bytes_per_day"`
	LedgerCapacityInodes uint64                `json:"ledger_capacity_inodes"`
	LedgerMinimumFreeInodes uint64             `json:"ledger_minimum_free_inodes"`
	LedgerExpectedMaximumEventsPerDay uint64   `json:"ledger_expected_maximum_events_per_day"`
	LedgerExpectedMaximumInodesPerDay uint64   `json:"ledger_expected_maximum_inodes_per_day"`
	// LedgerExpectedMaximumRecordsPerDay is retained only so an older config
	// fails explicitly instead of silently reinterpreting an inode estimate as
	// a semantic-event limit. New accepted configs must leave it zero.
	LedgerExpectedMaximumRecordsPerDay uint64  `json:"ledger_expected_maximum_records_per_day,omitempty"`
	CollectorRefreshIntervalSeconds int        `json:"collector_refresh_interval_seconds"`
	CollectorCollectionDeadlineSeconds int     `json:"collector_collection_deadline_seconds"`
	CollectorConfigSHA256 string                `json:"collector_config_sha256"`
	LedgerDeviceID uint64                       `json:"ledger_device_id"`
	Sources              []AuthoritySourceSpec `json:"sources"`
}

type AuthoritySourceSpec struct {
	ID                    string `json:"id"`
	Kind                  string `json:"kind"`
	SemanticCollection    string `json:"semantic_collection"`
	InitialURL            string `json:"initial_url"`
	ServerName            string `json:"server_name"`
	CABundlePath          string `json:"ca_bundle_path"`
	CABundleSHA256        string `json:"ca_bundle_sha256"`
	ClientCertificatePath string `json:"client_certificate_path"`
	ClientCertificateSHA256 string `json:"client_certificate_sha256"`
	ClientKeyPath         string `json:"client_key_path"`
	ClientKeySHA256       string `json:"client_key_sha256"`
	ClientSPKISHA256      string `json:"client_spki_sha256"`
	ClientSPIFFEURI       string `json:"client_spiffe_uri"`
	CredentialLaneID      string `json:"credential_lane_id"`
	BearerTokenPath       string `json:"bearer_token_path"`
	BearerTokenIssuer     string `json:"bearer_token_issuer"`
	BearerTokenAudience   string `json:"bearer_token_audience"`
	BearerTokenSubject    string `json:"bearer_token_subject"`
	BearerTokenAlgorithm  string `json:"bearer_token_algorithm"`
	BearerTokenKeyID      string `json:"bearer_token_key_id"`
	BearerTokenJWKSPath   string `json:"bearer_token_jwks_path"`
	BearerTokenJWKSSHA256 string `json:"bearer_token_jwks_sha256"`
	BearerTokenMaximumLifetimeSeconds int `json:"bearer_token_maximum_lifetime_seconds"`
	BearerTokenMinimumRemainingSeconds int `json:"bearer_token_minimum_remaining_seconds"`
	ProviderRequestIDHeader string `json:"provider_request_id_header"`
	Pagination            string `json:"pagination"`
	MaximumPages          int    `json:"maximum_pages"`
	MaximumPageBytes      int64  `json:"maximum_page_bytes"`
	MaximumTotalBytes     int64  `json:"maximum_total_bytes"`
	RevocationIssuerBundlePath string `json:"revocation_issuer_bundle_path"`
	RevocationIssuerBundleSHA256 string `json:"revocation_issuer_bundle_sha256"`
	MaximumRevocationAgeSeconds int `json:"maximum_revocation_age_seconds"`
}

type NativePage struct {
	Schema              string `json:"schema"`
	SourceID            string `json:"source_id"`
	SourceKind          string `json:"source_kind"`
	SemanticCollection  string `json:"semantic_collection"`
	CycleContractSHA256 string `json:"cycle_contract_sha256"`
	CycleID            string `json:"cycle_id"`
	CycleIssuedAt      string `json:"cycle_issued_at"`
	CycleDeadlineAt    string `json:"cycle_deadline_at"`
	RequestID           string `json:"request_id"`
	ProviderRequestID   string `json:"provider_request_id"`
	SessionID           string `json:"session_id"`
	Challenge           string `json:"challenge"`
	PageIndex           int    `json:"page_index"`
	RequestBase64       string `json:"request_base64"`
	RequestSHA256       string `json:"request_sha256"`
	RequestedURL        string `json:"requested_url"`
	CollectedAt         string `json:"collected_at"`
	TLSPeerCertificateSHA256 string `json:"tls_peer_certificate_sha256"`
	HTTPStatus          int    `json:"http_status"`
	ResponseContentType string `json:"response_content_type"`
	RawResponseBase64   string `json:"raw_response_base64"`
	RawResponseSHA256   string `json:"raw_response_sha256"`
	Complete            bool   `json:"complete"`
	NextToken           string `json:"next_token"`
	NextURL             string `json:"next_url"`
}

type CapturedPage struct {
	CycleContractSHA256 string    `json:"cycle_contract_sha256"`
	CycleID        string      `json:"cycle_id"`
	CycleIssuedAt  string      `json:"cycle_issued_at"`
	CycleDeadlineAt string     `json:"cycle_deadline_at"`
	DirectiveSHA256 string     `json:"directive_sha256"`
	CollectedAt     string     `json:"collected_at"`
	EnvelopeObject string      `json:"envelope_object"`
	EnvelopeSHA256 string      `json:"envelope_sha256"`
	PayloadSHA256  string      `json:"payload_sha256"`
	EnvelopeBase64 string      `json:"envelope_base64,omitempty"`
	Page           *NativePage `json:"page,omitempty"`
}

type SourceEvidence struct {
	SourceID string         `json:"source_id"`
	Kind     string         `json:"kind"`
	SemanticCollection string `json:"semantic_collection"`
	Pages    []CapturedPage `json:"pages"`
}

type EvidenceBundle struct {
	Schema       string           `json:"schema"`
	ClusterID    string           `json:"cluster_id"`
	DeploymentID string           `json:"deployment_id"`
	EvidenceStoreID string        `json:"evidence_store_id"`
	ActivationPredecessorSelectionSHA256 string `json:"activation_predecessor_selection_sha256"`
	CycleContractSHA256 string    `json:"cycle_contract_sha256"`
	CycleID        string         `json:"cycle_id"`
	CycleIssuedAt  string         `json:"cycle_issued_at"`
	CycleDeadlineAt string        `json:"cycle_deadline_at"`
	RefreshIntervalSeconds int    `json:"refresh_interval_seconds"`
	CollectionDeadlineSeconds int `json:"collection_deadline_seconds"`
	CollectedAt  string           `json:"collected_at"`
	Cycle        CollectorCycle   `json:"cycle"`
	Sources      []SourceEvidence `json:"sources"`
}
