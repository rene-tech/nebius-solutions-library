package boundary

import "encoding/json"

const (
	TrustSchema    = "fs2-serve.nebius.ai/public-edge-boundary-trust/v1"
	EnvelopeSchema = "fs2-serve.nebius.ai/public-edge-boundary-snapshot-envelope/v1"
	SnapshotSchema = "fs2-serve.nebius.ai/public-edge-boundary-snapshot/v1"
)

type TrustRegistry struct {
	Schema  string        `json:"schema"`
	Issuers []TrustIssuer `json:"issuers"`
}

type TrustIssuer struct {
	ID        string `json:"id"`
	KeyID     string `json:"key_id"`
	PublicKey string `json:"public_key"`
	Role      string `json:"role"`
}

type SignedEnvelope struct {
	Algorithm     string `json:"algorithm"`
	Issuer        string `json:"issuer"`
	KeyID         string `json:"key_id"`
	PayloadBase64 string `json:"payload_base64"`
	PayloadSHA256 string `json:"payload_sha256"`
	Schema        string `json:"schema"`
	Signature     string `json:"signature"`
}

type Snapshot struct {
	Schema                    string           `json:"schema"`
	ClusterID                 string           `json:"cluster_id"`
	DeploymentID              string           `json:"deployment_id"`
	AuthoritySnapshotID       string           `json:"authority_snapshot_id"`
	AuthorityClosureSHA256    string           `json:"authority_closure_sha256"`
	EvidenceBundleSHA256      string           `json:"evidence_bundle_sha256"`
	SnapshotID                string           `json:"snapshot_id"`
	IssuedAt                  string           `json:"issued_at"`
	ExpiresAt                 string           `json:"expires_at"`
	MaximumAgeSeconds         int              `json:"maximum_age_seconds"`
	Controller                Actor            `json:"controller"`
	CredentialNamespaces      []string         `json:"credential_namespaces"`
	CredentialSecrets         []ObjectIdentity `json:"credential_secrets"`
	ControllerServiceAccounts []ObjectIdentity `json:"controller_service_accounts"`
	CredentialWorkloads       []ObjectIdentity `json:"credential_workloads"`
	ProtectedRoots            []ObjectIdentity `json:"protected_roots"`
	ResourceGuards            []ResourceGuard  `json:"resource_guards"`
	Transitions               []Transition     `json:"transitions"`
}

type Actor struct {
	Username string              `json:"username"`
	UID      string              `json:"uid"`
	Groups   []string            `json:"groups"`
	Extra    map[string][]string `json:"extra"`
}

type ObjectIdentity struct {
	APIGroup        string `json:"api_group"`
	APIVersion      string `json:"api_version"`
	Resource        string `json:"resource"`
	Namespace       string `json:"namespace"`
	Name            string `json:"name"`
	UID             string `json:"uid"`
	ResourceVersion string `json:"resource_version"`
	ObjectSHA256    string `json:"object_sha256"`
}

// ResourceGuard is derived from the native transitive authority inventory and
// signed as part of the snapshot. A wildcard selector still requires an exact
// Transition for the concrete AdmissionRequest; it cannot authorize a family
// of mutations by itself.
type ResourceGuard struct {
	APIGroup      string   `json:"api_group"`
	Resource      string   `json:"resource"`
	Subresources  []string `json:"subresources"`
	Namespaces    []string `json:"namespaces"`
	Names         []string `json:"names"`
	Operations    []string `json:"operations"`
	SemanticGuard string   `json:"semantic_guard"`
}

type AuthorityClosure struct {
	Controller                Actor            `json:"controller"`
	CredentialNamespaces      []string         `json:"credential_namespaces"`
	CredentialSecrets         []ObjectIdentity `json:"credential_secrets"`
	ControllerServiceAccounts []ObjectIdentity `json:"controller_service_accounts"`
	CredentialWorkloads       []ObjectIdentity `json:"credential_workloads"`
	ProtectedRoots            []ObjectIdentity `json:"protected_roots"`
	ResourceGuards            []ResourceGuard  `json:"resource_guards"`
}

type Transition struct {
	Operation    string `json:"operation"`
	APIGroup     string `json:"api_group"`
	APIVersion   string `json:"api_version"`
	Resource     string `json:"resource"`
	Subresource  string `json:"subresource"`
	Namespace    string `json:"namespace"`
	Name         string `json:"name"`
	ObjectSHA256 string `json:"object_sha256"`
	Actor        Actor  `json:"actor"`
}

type AdmissionReview struct {
	APIVersion string             `json:"apiVersion"`
	Kind       string             `json:"kind"`
	Request    *AdmissionRequest  `json:"request,omitempty"`
	Response   *AdmissionResponse `json:"response,omitempty"`
}

type AdmissionRequest struct {
	UID         string          `json:"uid"`
	Operation   string          `json:"operation"`
	Name        string          `json:"name"`
	Namespace   string          `json:"namespace"`
	Resource    GroupVersionRes `json:"resource"`
	Subresource string          `json:"subResource"`
	UserInfo    AdmissionUser   `json:"userInfo"`
	Object      json.RawMessage `json:"object"`
	OldObject   json.RawMessage `json:"oldObject"`
}

type GroupVersionRes struct {
	Group    string `json:"group"`
	Version  string `json:"version"`
	Resource string `json:"resource"`
}

type AdmissionUser struct {
	Username string              `json:"username"`
	UID      string              `json:"uid"`
	Groups   []string            `json:"groups"`
	Extra    map[string][]string `json:"extra"`
}

type AdmissionResponse struct {
	UID     string  `json:"uid"`
	Allowed bool    `json:"allowed"`
	Status  *Status `json:"status,omitempty"`
}

type Status struct {
	Code    int32  `json:"code"`
	Message string `json:"message"`
	Reason  string `json:"reason"`
}
