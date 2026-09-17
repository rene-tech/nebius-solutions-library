package collector

const (
	ConfigSchema            = "fs2-serve.nebius.ai/public-edge-native-collector-config/v1"
	NativeTrustSchema       = "fs2-serve.nebius.ai/public-edge-native-response-trust/v1"
	NativeEnvelopeSchema    = "fs2-serve.nebius.ai/public-edge-native-response-envelope/v1"
	NativePageSchema        = "fs2-serve.nebius.ai/public-edge-native-response/v1"
	NativeIssuerRole        = "provider-control-plane-native-response-authority"
	EvidenceBundleSchema    = "fs2-serve.nebius.ai/public-edge-native-evidence-bundle/v1"
	SnapshotIssuerRole      = "platform-security-public-edge-boundary-runtime"
	AuthorityConfigSchema   = "fs2-serve.nebius.ai/public-edge-native-authority-config/v1"
	CollectionRequestSchema = "fs2-serve.nebius.ai/public-edge-native-collection-request/v1"
	NativeRequestSchema      = "fs2-serve.nebius.ai/public-edge-native-request/v1"
	AuthoritySettlementSchema = "fs2-serve.nebius.ai/public-edge-native-authority-settlement/v1"
	AuthorityReservationSchema = "fs2-serve.nebius.ai/public-edge-native-authority-reservation/v1"
	AuthorityChallengeSchema  = "fs2-serve.nebius.ai/public-edge-native-authority-challenge/v1"
	IntegrationGateSchema     = "fs2-serve.nebius.ai/public-edge-external-enrollment-gate/v1"
)

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
}

type CollectionRequest struct {
	Schema       string `json:"schema"`
	ClusterID    string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	SourceID     string `json:"source_id"`
	SessionID    string `json:"session_id"`
	PageIndex    int    `json:"page_index"`
	PageToken    string `json:"page_token"`
	Challenge    string `json:"challenge"`
}

type AuthoritySettlement struct {
	Schema                   string `json:"schema"`
	ClusterID                string `json:"cluster_id"`
	DeploymentID             string `json:"deployment_id"`
	SourceID                 string `json:"source_id"`
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
	LedgerCapacityBytes  uint64                `json:"ledger_capacity_bytes"`
	LedgerMinimumFreeBytes uint64              `json:"ledger_minimum_free_bytes"`
	LedgerOperatingHorizonDays int             `json:"ledger_operating_horizon_days"`
	LedgerExpectedMaximumBytesPerDay uint64    `json:"ledger_expected_maximum_bytes_per_day"`
	LedgerCapacityInodes uint64                `json:"ledger_capacity_inodes"`
	LedgerMinimumFreeInodes uint64             `json:"ledger_minimum_free_inodes"`
	LedgerExpectedMaximumRecordsPerDay uint64  `json:"ledger_expected_maximum_records_per_day"`
	CollectorRefreshIntervalSeconds int        `json:"collector_refresh_interval_seconds"`
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
	RefreshIntervalSeconds int    `json:"refresh_interval_seconds"`
	CollectionDeadlineSeconds int `json:"collection_deadline_seconds"`
	CollectedAt  string           `json:"collected_at"`
	Sources      []SourceEvidence `json:"sources"`
}
