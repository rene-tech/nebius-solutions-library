package snapshotauthority

import "github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"

const (
	PreviousConfigSchema = "fs2-serve.nebius.ai/public-edge-snapshot-authority-config/v2"
	ConfigSchema = "fs2-serve.nebius.ai/public-edge-snapshot-authority-config/v3"
	SettlementSchema = "fs2-serve.nebius.ai/public-edge-snapshot-authority-settlement/v1"
	SettlementEnvelopeSchema = "fs2-serve.nebius.ai/public-edge-snapshot-authority-settlement-envelope/v1"
	SettlementQuotaSchema = "fs2-serve.nebius.ai/public-edge-snapshot-authority-quota/v1"
	SettlementQuotaEnvelopeSchema = "fs2-serve.nebius.ai/public-edge-snapshot-authority-quota-envelope/v1"
	SettlementOutcomeSchema = "fs2-serve.nebius.ai/public-edge-snapshot-authority-outcome/v1"
	SettlementOutcomeEnvelopeSchema = "fs2-serve.nebius.ai/public-edge-snapshot-authority-outcome-envelope/v1"
	CollectorChannelEvidenceSchema = "fs2-serve.nebius.ai/public-edge-snapshot-collector-channel/v1"
	CollectorPossessionProofSchema = "fs2-serve.nebius.ai/public-edge-snapshot-collector-possession/v1"
	maximumConfigBytes = 4 * 1024 * 1024
	maximumRequestBytes = 256 * 1024 * 1024
	maximumSettlementBytes = 8 * 1024 * 1024
)

type Config struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	ListenAddress string `json:"listen_address"`
	RuntimeUID uint32 `json:"runtime_uid"`
	RuntimeGID uint32 `json:"runtime_gid"`
	SettlementRuntimeUID uint32 `json:"settlement_runtime_uid"`
	SettlementRuntimeGID uint32 `json:"settlement_runtime_gid"`
	SettlementSocketName string `json:"settlement_socket_name"`
	CollectorCredentialLaneID string `json:"collector_credential_lane_id"`
	NativeCollectorConfigPath string `json:"native_collector_config_path"`
	NativeResponseTrustPath string `json:"native_response_trust_path"`
	SnapshotTrustPath string `json:"snapshot_trust_path"`
	SigningKeyPath string `json:"signing_key_path"`
	SigningKeySHA256 string `json:"signing_key_sha256"`
	SigningIssuer string `json:"signing_issuer"`
	SigningKeyID string `json:"signing_key_id"`
	SettlementSigningKeyPath string `json:"settlement_signing_key_path"`
	SettlementSigningKeySHA256 string `json:"settlement_signing_key_sha256"`
	SettlementSigningIssuer string `json:"settlement_signing_issuer"`
	SettlementSigningKeyID string `json:"settlement_signing_key_id"`
	SettlementVerificationKeys []SettlementVerificationKey `json:"settlement_verification_keys"`
	SettlementRoot string `json:"settlement_root"`
	SettlementDeviceID uint64 `json:"settlement_device_id"`
	SettlementCapacityBytes uint64 `json:"settlement_capacity_bytes"`
	SettlementMinimumFreeBytes uint64 `json:"settlement_minimum_free_bytes"`
	SettlementCapacityInodes uint64 `json:"settlement_capacity_inodes"`
	SettlementMinimumFreeInodes uint64 `json:"settlement_minimum_free_inodes"`
	SettlementOperatingHorizonDays int `json:"settlement_operating_horizon_days"`
	MaximumSettlementsPerDay uint64 `json:"maximum_settlements_per_day"`
	MaximumConcurrentRequests int `json:"maximum_concurrent_requests"`
	SnapshotMaximumAgeSeconds int `json:"snapshot_maximum_age_seconds"`
	ServerIdentities []ServerIdentity `json:"server_identities"`
	CollectorIdentities []CollectorIdentity `json:"collector_identities"`
	Policy SemanticPolicy `json:"policy"`
}

type SettlementVerificationKey struct {
	Issuer string `json:"issuer"`
	KeyID string `json:"key_id"`
	PublicKeyPath string `json:"public_key_path"`
	PublicKeySHA256 string `json:"public_key_sha256"`
}

type ServerIdentity struct {
	Slot string `json:"slot"`
	DNSName string `json:"dns_name,omitempty"`
	CertificatePath string `json:"certificate_path"`
	CertificateSHA256 string `json:"certificate_sha256"`
	PrivateKeyPath string `json:"private_key_path"`
	PrivateKeySHA256 string `json:"private_key_sha256"`
	SPKISHA256 string `json:"spki_sha256"`
	IssuerBundlePath string `json:"issuer_bundle_path"`
	IssuerBundleSHA256 string `json:"issuer_bundle_sha256"`
	CRLPath string `json:"crl_path"`
	CRLSHA256 string `json:"crl_sha256"`
	NotBefore string `json:"not_before"`
	NotAfter string `json:"not_after"`
}

type CollectorIdentity struct {
	Slot string `json:"slot"`
	CABundlePath string `json:"ca_bundle_path"`
	CABundleSHA256 string `json:"ca_bundle_sha256"`
	SPKISHA256 string `json:"spki_sha256"`
	SPIFFEURI string `json:"spiffe_uri"`
	CRLPath string `json:"crl_path"`
	CRLSHA256 string `json:"crl_sha256"`
	NotBefore string `json:"not_before"`
	NotAfter string `json:"not_after"`
}

type SemanticPolicy struct {
	Controller boundary.Actor `json:"controller"`
	ControllerServiceAccount ObjectReference `json:"controller_service_account"`
	AllowedControllerImages []string `json:"allowed_controller_images"`
	AllowedDangerousSubjects []RBACSubject `json:"allowed_dangerous_subjects"`
	AllowedProviderPrincipals []string `json:"allowed_provider_principals"`
	ProtectedProviderResources []string `json:"protected_provider_resources"`
	AllowedClientCertificateSubjects []string `json:"allowed_client_certificate_subjects"`
	AllowedClientCARootSPKISHA256 []string `json:"allowed_client_ca_root_spki_sha256"`
	AllowedRequestHeaderCARootSPKISHA256 []string `json:"allowed_request_header_ca_root_spki_sha256"`
	AllowedRequestHeaderProxyCommonNames []string `json:"allowed_request_header_proxy_common_names"`
	RequestHeaderUsernameHeaders []string `json:"request_header_username_headers"`
	RequestHeaderGroupHeaders []string `json:"request_header_group_headers"`
	RequestHeaderExtraHeaderPrefixes []string `json:"request_header_extra_header_prefixes"`
	APIServerAuthenticationArgumentsSHA256 string `json:"api_server_authentication_arguments_sha256"`
	AllowedCSRSignerNames []string `json:"allowed_csr_signer_names"`
	AllowedNodeGroupIDs []string `json:"allowed_node_group_ids"`
	RequiredKMSKeyIDs []string `json:"required_kms_key_ids"`
	SecretClassifierRuntimeSHA256 string `json:"secret_classifier_runtime_sha256"`
	SecretClassifierPolicySHA256 string `json:"secret_classifier_policy_sha256"`
	ProtectedRoots []ObjectReference `json:"protected_roots"`
	AdmissionParameterRoots []ObjectReference `json:"admission_parameter_roots"`
	Edge EdgeGraphPolicy `json:"edge"`
}

type ObjectReference struct {
	SemanticCollection string `json:"semantic_collection"`
	Namespace string `json:"namespace"`
	Name string `json:"name"`
}

type RBACSubject struct {
	Kind string `json:"kind"`
	Namespace string `json:"namespace"`
	Name string `json:"name"`
}

type EdgeGraphPolicy struct {
	Namespace string `json:"namespace"`
	GatewayClassName string `json:"gateway_class_name"`
	GatewayName string `json:"gateway_name"`
	HTTPListenerName string `json:"http_listener_name"`
	HTTPSListenerName string `json:"https_listener_name"`
	HTTPClientTrafficPolicyName string `json:"http_client_traffic_policy_name"`
	HTTPSClientTrafficPolicyName string `json:"https_client_traffic_policy_name"`
	BackendTrafficPolicyName string `json:"backend_traffic_policy_name"`
	EnvoyProxyName string `json:"envoy_proxy_name"`
	ControlPlaneWorkload ObjectReference `json:"control_plane_workload"`
	PublicRoutes []ObjectReference `json:"public_routes"`
	PublicBackendServices []ObjectReference `json:"public_backend_services"`
	NetworkPolicies []ObjectReference `json:"network_policies"`
	NetworkPolicySpecs []NetworkPolicySpecContract `json:"network_policy_specs"`
	ControlPlanePDBName string `json:"control_plane_pdb_name"`
	RateLimitPDBNamespace string `json:"rate_limit_pdb_namespace"`
	RateLimitPDBName string `json:"rate_limit_pdb_name"`
	RedisPDBNamespace string `json:"redis_pdb_namespace"`
	RedisPDBName string `json:"redis_pdb_name"`
	RateLimitWorkloadNamespace string `json:"rate_limit_workload_namespace"`
	RateLimitWorkloadName string `json:"rate_limit_workload_name"`
	RedisWorkloadNamespace string `json:"redis_workload_namespace"`
	RedisWorkloadName string `json:"redis_workload_name"`
	RateLimitServiceNamespace string `json:"rate_limit_service_namespace"`
	RateLimitServiceName string `json:"rate_limit_service_name"`
	RedisServiceNamespace string `json:"redis_service_namespace"`
	RedisServiceName string `json:"redis_service_name"`
	ProviderLoadBalancerID string `json:"provider_load_balancer_id"`
	ProviderHTTPListenerID string `json:"provider_http_listener_id"`
	ProviderHTTPSListenerID string `json:"provider_https_listener_id"`
	ProviderBackendID string `json:"provider_backend_id"`
	ProviderHealthCheckID string `json:"provider_health_check_id"`
	RateLimitServicePort int64 `json:"rate_limit_service_port"`
	RedisServicePort int64 `json:"redis_service_port"`
	RedisTLSSecretName string `json:"redis_tls_secret_name"`
	MinimumPerSourceConnectionLimit int64 `json:"minimum_per_source_connection_limit"`
	MaximumPerSourceConnectionLimit int64 `json:"maximum_per_source_connection_limit"`
}

type NetworkPolicySpecContract struct {
	Namespace string `json:"namespace"`
	Name string `json:"name"`
	SpecSHA256 string `json:"spec_sha256"`
}

type Settlement struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	CycleID string `json:"cycle_id"`
	CycleContractSHA256 string `json:"cycle_contract_sha256"`
	CycleIssuedAt string `json:"cycle_issued_at"`
	CycleDeadlineAt string `json:"cycle_deadline_at"`
	EvidenceBundleSHA256 string `json:"evidence_bundle_sha256"`
	SnapshotEnvelopeSHA256 string `json:"snapshot_envelope_sha256"`
	SnapshotEnvelopeBase64 string `json:"snapshot_envelope_base64"`
	CollectorCredentialLaneID string `json:"collector_credential_lane_id"`
	AuthenticatedCollectorChannel string `json:"authenticated_collector_channel"`
	QuotaEnvelopeSHA256 string `json:"quota_envelope_sha256"`
	QuotaUTCDate string `json:"quota_utc_date"`
	QuotaSlot uint64 `json:"quota_slot"`
	Status string `json:"status"`
	PreparedAt string `json:"prepared_at"`
}

type SettlementQuota struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	CycleID string `json:"cycle_id"`
	CycleContractSHA256 string `json:"cycle_contract_sha256"`
	CycleIssuedAt string `json:"cycle_issued_at"`
	CycleDeadlineAt string `json:"cycle_deadline_at"`
	EvidenceBundleSHA256 string `json:"evidence_bundle_sha256"`
	AuthenticatedCollectorChannel string `json:"authenticated_collector_channel"`
	CollectorChannelAttestationSHA256 string `json:"collector_channel_attestation_sha256"`
	UTCDate string `json:"utc_date"`
	Slot uint64 `json:"slot"`
	ReservedAt string `json:"reserved_at"`
}

type SettlementEnvelope struct {
	Schema string `json:"schema"`
	Algorithm string `json:"algorithm"`
	Issuer string `json:"issuer"`
	KeyID string `json:"key_id"`
	PayloadSHA256 string `json:"payload_sha256"`
	PayloadBase64 string `json:"payload_base64"`
	Signature string `json:"signature"`
}

type SettlementOutcome struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	CycleID string `json:"cycle_id"`
	CycleContractSHA256 string `json:"cycle_contract_sha256"`
	EvidenceBundleSHA256 string `json:"evidence_bundle_sha256"`
	SettlementEnvelopeSHA256 string `json:"settlement_envelope_sha256"`
	QuotaEnvelopeSHA256 string `json:"quota_envelope_sha256"`
	FinalSettlementPresent bool `json:"final_settlement_present"`
	AuthenticatedCollectorChannel string `json:"authenticated_collector_channel"`
	Status string `json:"status"`
	CompletedAt string `json:"completed_at"`
}

type CollectorChannelEvidence struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	EvidenceBundleSHA256 string `json:"evidence_bundle_sha256"`
	Slot string `json:"slot"`
	SPKISHA256 string `json:"spki_sha256"`
	SPIFFEURI string `json:"spiffe_uri"`
	CRLSHA256 string `json:"crl_sha256"`
	VerifiedChainDERBase64 []string `json:"verified_chain_der_base64"`
	CollectorPossessionProofBase64 string `json:"collector_possession_proof_base64"`
	CollectorPossessionProofSHA256 string `json:"collector_possession_proof_sha256"`
	ObservedAt string `json:"observed_at"`
}

type CollectorPossessionProof struct {
	Schema string `json:"schema"`
	EvidenceBundleSHA256 string `json:"evidence_bundle_sha256"`
	TLSExporterSHA256 string `json:"tls_exporter_sha256"`
	Nonce string `json:"nonce"`
	ObservedAt string `json:"observed_at"`
	SignatureAlgorithm string `json:"signature_algorithm"`
	Signature string `json:"signature"`
}

type collectorPossessionStatement struct {
	Schema string `json:"schema"`
	EvidenceBundleSHA256 string `json:"evidence_bundle_sha256"`
	TLSExporterSHA256 string `json:"tls_exporter_sha256"`
	Nonce string `json:"nonce"`
	ObservedAt string `json:"observed_at"`
}
