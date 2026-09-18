package snapshotauthority

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/collector"
)

type collectionContract struct {
	apiGroup string
	resource string
	kind string
	namespaced bool
}

type inventoryObject struct {
	collection string
	identity boundary.ObjectIdentity
	kind string
	value map[string]any
	raw []byte
}

type semanticInventory struct {
	byCollection map[string][]inventoryObject
	byReference map[string]inventoryObject
}

type authorityScopeProjection struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	NativeCollectorConfigSHA256 string `json:"native_collector_config_sha256"`
	SnapshotAuthorityConfigSHA256 string `json:"snapshot_authority_config_sha256"`
	MandatoryCollectionsSHA256 string `json:"mandatory_collections_sha256"`
	AuthorityClosurePolicySHA256 string `json:"authority_closure_policy_sha256"`
}

var kubernetesCollections = map[string]collectionContract{
	"apiserver-admission-mutating-policies": {"admissionregistration.k8s.io", "mutatingadmissionpolicies", "MutatingAdmissionPolicy", false},
	"apiserver-admission-mutating-policy-bindings": {"admissionregistration.k8s.io", "mutatingadmissionpolicybindings", "MutatingAdmissionPolicyBinding", false},
	"apiserver-admission-validating-policies": {"admissionregistration.k8s.io", "validatingadmissionpolicies", "ValidatingAdmissionPolicy", false},
	"apiserver-admission-validating-policy-bindings": {"admissionregistration.k8s.io", "validatingadmissionpolicybindings", "ValidatingAdmissionPolicyBinding", false},
	"apiserver-admission-mutating-webhooks": {"admissionregistration.k8s.io", "mutatingwebhookconfigurations", "MutatingWebhookConfiguration", false},
	"apiserver-admission-validating-webhooks": {"admissionregistration.k8s.io", "validatingwebhookconfigurations", "ValidatingWebhookConfiguration", false},
	"apiserver-approval-objects": {"security.fs2.nebius.ai", "publicedgenodeauthorityapprovals", "PublicEdgeNodeAuthorityApproval", false},
	"apiserver-backend-tls-policies": {"gateway.networking.k8s.io", "backendtlspolicies", "BackendTLSPolicy", true},
	"apiserver-backend-traffic-policies": {"gateway.envoyproxy.io", "backendtrafficpolicies", "BackendTrafficPolicy", true},
	"apiserver-client-traffic-policies": {"gateway.envoyproxy.io", "clienttrafficpolicies", "ClientTrafficPolicy", true},
	"apiserver-clusterrolebindings": {"rbac.authorization.k8s.io", "clusterrolebindings", "ClusterRoleBinding", false},
	"apiserver-clusterroles": {"rbac.authorization.k8s.io", "clusterroles", "ClusterRole", false},
	"apiserver-configmaps": {"", "configmaps", "ConfigMap", true},
	"apiserver-crds": {"apiextensions.k8s.io", "customresourcedefinitions", "CustomResourceDefinition", false},
	"apiserver-csrs": {"certificates.k8s.io", "certificatesigningrequests", "CertificateSigningRequest", false},
	"apiserver-daemonsets": {"apps", "daemonsets", "DaemonSet", true},
	"apiserver-deployments": {"apps", "deployments", "Deployment", true},
	"apiserver-endpoints": {"", "endpoints", "Endpoints", true},
	"apiserver-endpointslices": {"discovery.k8s.io", "endpointslices", "EndpointSlice", true},
	"apiserver-envoy-backends": {"gateway.envoyproxy.io", "backends", "Backend", true},
	"apiserver-envoy-extension-policies": {"gateway.envoyproxy.io", "envoyextensionpolicies", "EnvoyExtensionPolicy", true},
	"apiserver-envoyproxies": {"gateway.envoyproxy.io", "envoyproxies", "EnvoyProxy", true},
	"apiserver-gatewayclasses": {"gateway.networking.k8s.io", "gatewayclasses", "GatewayClass", false},
	"apiserver-gateways": {"gateway.networking.k8s.io", "gateways", "Gateway", true},
	"apiserver-grpcroutes": {"gateway.networking.k8s.io", "grpcroutes", "GRPCRoute", true},
	"apiserver-httproutes": {"gateway.networking.k8s.io", "httproutes", "HTTPRoute", true},
	"apiserver-ingresses": {"networking.k8s.io", "ingresses", "Ingress", true},
	"apiserver-jobs": {"batch", "jobs", "Job", true},
	"apiserver-cronjobs": {"batch", "cronjobs", "CronJob", true},
	"apiserver-csidrivers": {"storage.k8s.io", "csidrivers", "CSIDriver", false},
	"apiserver-csinodes": {"storage.k8s.io", "csinodes", "CSINode", false},
	"apiserver-nodes": {"", "nodes", "Node", false},
	"apiserver-namespaces": {"", "namespaces", "Namespace", false},
	"apiserver-networkpolicies": {"networking.k8s.io", "networkpolicies", "NetworkPolicy", true},
	"apiserver-persistentvolumeclaims": {"", "persistentvolumeclaims", "PersistentVolumeClaim", true},
	"apiserver-persistentvolumes": {"", "persistentvolumes", "PersistentVolume", false},
	"apiserver-poddisruptionbudgets": {"policy", "poddisruptionbudgets", "PodDisruptionBudget", true},
	"apiserver-pods": {"", "pods", "Pod", true},
	"apiserver-replicationcontrollers": {"", "replicationcontrollers", "ReplicationController", true},
	"apiserver-replicasets": {"apps", "replicasets", "ReplicaSet", true},
	"apiserver-referencegrants": {"gateway.networking.k8s.io", "referencegrants", "ReferenceGrant", true},
	"apiserver-rolebindings": {"rbac.authorization.k8s.io", "rolebindings", "RoleBinding", true},
	"apiserver-roles": {"rbac.authorization.k8s.io", "roles", "Role", true},
	"apiserver-secrets-metadata": {"", "secrets", "Secret", true},
	"apiserver-security-policies": {"gateway.envoyproxy.io", "securitypolicies", "SecurityPolicy", true},
	"apiserver-serviceaccounts": {"", "serviceaccounts", "ServiceAccount", true},
	"apiserver-services": {"", "services", "Service", true},
	"apiserver-statefulsets": {"apps", "statefulsets", "StatefulSet", true},
	"apiserver-storageclasses": {"storage.k8s.io", "storageclasses", "StorageClass", false},
	"apiserver-tcproutes": {"gateway.networking.k8s.io", "tcproutes", "TCPRoute", true},
	"apiserver-tlsroutes": {"gateway.networking.k8s.io", "tlsroutes", "TLSRoute", true},
	"apiserver-tls-certificate-evidence": {"", "secrets", "Secret", true},
	"apiserver-udproutes": {"gateway.networking.k8s.io", "udproutes", "UDPRoute", true},
	"apiserver-volumeattachments": {"storage.k8s.io", "volumeattachments", "VolumeAttachment", false},
}

type kubernetesList struct {
	APIVersion string `json:"apiVersion"`
	Kind string `json:"kind"`
	Metadata kubernetesListMetadata `json:"metadata"`
	Items []json.RawMessage `json:"items"`
}

type secretMetadataProjectionList struct {
	Schema string `json:"schema"`
	APIVersion string `json:"apiVersion"`
	Kind string `json:"kind"`
	Metadata kubernetesListMetadata `json:"metadata"`
	Items []json.RawMessage `json:"items"`
}

type kubernetesListMetadata struct {
	ResourceVersion string `json:"resourceVersion"`
	Continue string `json:"continue,omitempty"`
	RemainingItemCount *int64 `json:"remainingItemCount,omitempty"`
	SelfLink string `json:"selfLink,omitempty"`
}

func DeriveSnapshot(
	verified *collector.VerifiedEvidenceBundle,
	bundleRaw []byte,
	config Config,
	collectorConfig collector.Config,
	acceptance boundary.Acceptance,
) (boundary.Snapshot, error) {
	if verified == nil || verified.Bundle.ClusterID != config.ClusterID || verified.Bundle.DeploymentID != config.DeploymentID {
		return boundary.Snapshot{}, errors.New("verified native evidence targets another snapshot authority")
	}
	inventory, err := deriveInventory(verified, collectorConfig)
	if err != nil {
		return boundary.Snapshot{}, err
	}
	if digest(bundleRaw) != digestJSON(verified.Bundle) {
		return boundary.Snapshot{}, errors.New("verified evidence does not identify the exact canonical request bytes")
	}
	if err := validateAcceptedSemanticPolicy(config, acceptance); err != nil {
		return boundary.Snapshot{}, err
	}
	if err := validateAdmissionAndCSRAuthority(inventory, config.Policy); err != nil {
		return boundary.Snapshot{}, err
	}
	serviceAccount, err := inventory.require(config.Policy.ControllerServiceAccount)
	if err != nil || serviceAccount.identity.Resource != "serviceaccounts" {
		return boundary.Snapshot{}, errors.New("controller ServiceAccount is absent from the native inventory")
	}
	controller, err := deriveControllerActor(serviceAccount, config.Policy.Controller)
	if err != nil {
		return boundary.Snapshot{}, err
	}
	workloads, credentialServiceAccounts, secretReferences, err := deriveCredentialWorkloads(inventory, serviceAccount, config.Policy.AllowedControllerImages)
	if err != nil {
		return boundary.Snapshot{}, err
	}
	if err := validateRBACClosure(inventory, config.Policy, credentialServiceAccounts); err != nil {
		return boundary.Snapshot{}, err
	}
	secrets := make([]boundary.ObjectIdentity, 0, len(secretReferences))
	for _, reference := range secretReferences {
		object, err := inventory.require(ObjectReference{
			SemanticCollection: "apiserver-secrets-metadata",
			Namespace: reference.namespace,
			Name: reference.name,
		})
		if err != nil {
			return boundary.Snapshot{}, fmt.Errorf("credential Secret %s/%s is absent from the complete metadata inventory", reference.namespace, reference.name)
		}
		secrets = append(secrets, object.identity)
	}
	protectedRoots, err := inventory.identities(config.Policy.ProtectedRoots)
	if err != nil {
		return boundary.Snapshot{}, err
	}
	parameterRoots, err := inventory.identities(config.Policy.AdmissionParameterRoots)
	if err != nil {
		return boundary.Snapshot{}, err
	}
	edgeRoots, err := validateEdgeGraph(inventory, verified, config.Policy.Edge)
	if err != nil {
		return boundary.Snapshot{}, err
	}
	protectedRoots = mergeIdentities(protectedRoots, edgeRoots)
	if err := validateSupplementalAuthorityEvidence(verified, collectorConfig, config.Policy, inventory, serviceAccount, secrets); err != nil {
		return boundary.Snapshot{}, err
	}
	credentialNamespaceValues := []string{serviceAccount.identity.Namespace}
	for _, identity := range workloads { credentialNamespaceValues = append(credentialNamespaceValues, identity.Namespace) }
	for _, identity := range secrets { credentialNamespaceValues = append(credentialNamespaceValues, identity.Namespace) }
	for _, identity := range credentialServiceAccounts { credentialNamespaceValues = append(credentialNamespaceValues, identity.Namespace) }
	credentialNamespaces := uniqueSortedStrings(credentialNamespaceValues)
	guards := deriveResourceGuards(credentialNamespaces, protectedRoots)
	closure := boundary.AuthorityClosure{
		Controller: controller,
		CredentialNamespaces: credentialNamespaces,
		CredentialSecrets: sortIdentities(secrets),
		ControllerServiceAccounts: sortIdentities(credentialServiceAccounts),
		CredentialWorkloads: sortIdentities(workloads),
		ProtectedRoots: sortIdentities(protectedRoots),
		AdmissionParameterRoots: sortIdentities(parameterRoots),
		EdgeProtectionRoots: sortIdentities(edgeRoots),
		ResourceGuards: guards,
	}
	closureRaw, err := json.Marshal(closure)
	if err != nil {
		return boundary.Snapshot{}, errors.New("derived native authority closure cannot be serialized")
	}
	derivedClosureSHA256 := digest(closureRaw)
	issuedAt, err := time.Parse(time.RFC3339, verified.Bundle.CollectedAt)
	if err != nil || issuedAt.Nanosecond() != 0 {
		return boundary.Snapshot{}, errors.New("native evidence has a non-canonical terminal timestamp")
	}
	expiresAt := issuedAt.Add(time.Duration(config.SnapshotMaximumAgeSeconds) * time.Second)
	snapshot := boundary.Snapshot{
		Schema: boundary.SnapshotSchema,
		ClusterID: config.ClusterID,
		DeploymentID: config.DeploymentID,
		AuthoritySnapshotID: acceptance.AuthoritySnapshotID,
		AuthorityClosureSHA256: acceptance.AuthorityClosureSHA256,
		DerivedAuthorityClosureSHA256: derivedClosureSHA256,
		EvidenceBundleSHA256: digest(bundleRaw),
		ActivationCycleContractSHA256: verified.Bundle.CycleContractSHA256,
		ActivationCycleID: verified.Bundle.CycleID,
		ActivationCycleIssuedAt: verified.Bundle.CycleIssuedAt,
		ActivationCycleDeadlineAt: verified.Bundle.CycleDeadlineAt,
		ActivationPredecessorSelectionSHA256: verified.Bundle.ActivationPredecessorSelectionSHA256,
		IssuedAt: issuedAt.UTC().Format(time.RFC3339),
		ExpiresAt: expiresAt.UTC().Format(time.RFC3339),
		MaximumAgeSeconds: config.SnapshotMaximumAgeSeconds,
		Controller: closure.Controller,
		CredentialNamespaces: closure.CredentialNamespaces,
		CredentialSecrets: closure.CredentialSecrets,
		ControllerServiceAccounts: closure.ControllerServiceAccounts,
		CredentialWorkloads: closure.CredentialWorkloads,
		ProtectedRoots: closure.ProtectedRoots,
		AdmissionParameterRoots: closure.AdmissionParameterRoots,
		EdgeProtectionRoots: closure.EdgeProtectionRoots,
		ResourceGuards: closure.ResourceGuards,
		Transitions: []boundary.Transition{},
	}
	normalizedRaw, err := json.Marshal(snapshot)
	if err != nil {
		return boundary.Snapshot{}, err
	}
	snapshot.SnapshotID = digest(normalizedRaw)
	return snapshot, nil
}

func validateAcceptedSemanticPolicy(config Config, acceptance boundary.Acceptance) error {
	policyRaw, err := json.Marshal(config.Policy)
	if err != nil || digest(policyRaw) != acceptance.AuthorityClosureSHA256 {
		return errors.New("snapshot authority semantic policy differs from the independently accepted closure-policy identity")
	}
	projection := authorityScopeProjection{
		Schema: "fs2-serve.nebius.ai/public-edge-authority-static-scope/v1",
		ClusterID: config.ClusterID,
		DeploymentID: config.DeploymentID,
		NativeCollectorConfigSHA256: acceptance.NativeCollectorConfigSHA256,
		SnapshotAuthorityConfigSHA256: acceptance.SnapshotAuthorityConfigSHA256,
		MandatoryCollectionsSHA256: acceptance.MandatoryCollectionsSHA256,
		AuthorityClosurePolicySHA256: acceptance.AuthorityClosureSHA256,
	}
	raw, err := json.Marshal(projection)
	if err != nil || digest(raw) != acceptance.AuthoritySnapshotID {
		return errors.New("snapshot authority static semantic scope differs from the independently accepted identity")
	}
	return nil
}

func deriveInventory(verified *collector.VerifiedEvidenceBundle, config collector.Config) (*semanticInventory, error) {
	inventory := &semanticInventory{byCollection: map[string][]inventoryObject{}, byReference: map[string]inventoryObject{}}
	sourcePlans := map[string]collector.SourceSpec{}
	for _, source := range config.Sources {
		sourcePlans[source.ID] = source
	}
	for _, source := range verified.Bundle.Sources {
		contract, typed := kubernetesCollections[source.SemanticCollection]
		if !typed {
			continue
		}
		plan, enrolled := sourcePlans[source.SourceID]
		if !enrolled || plan.SemanticCollection != source.SemanticCollection || validateKubernetesListSource(plan, contract) != nil {
			return nil, fmt.Errorf("semantic collection %s lacks an exact all-scope Kubernetes request plan", source.SemanticCollection)
		}
		if source.SemanticCollection == "apiserver-secrets-metadata" && plan.Kind != "kubernetes-secret-metadata-projection" {
			return nil, errors.New("Secret inventory is not the explicit independently authenticated metadata-only projection")
		}
		if source.SemanticCollection == "apiserver-tls-certificate-evidence" && plan.Kind != "kubernetes-tls-certificate-projection" {
			return nil, errors.New("TLS certificate inventory is not the explicit public-material-only projection")
		}
		responses := verified.RawResponses[source.SourceID]
		if len(responses) != len(source.Pages) {
			return nil, errors.New("verified native response inventory is incomplete")
		}
		resourceVersion := ""
		for pageIndex, raw := range responses {
			var list kubernetesList
			if source.SemanticCollection == "apiserver-secrets-metadata" {
				var projection secretMetadataProjectionList
				if err := boundary.DecodeExactJSON(raw, &projection); err != nil || projection.Schema != "fs2-serve.nebius.ai/kubernetes-secret-metadata-projection/v1" {
					return nil, errors.New("Secret inventory is not the exact independently authenticated metadata-only response schema")
				}
				list = kubernetesList{APIVersion: projection.APIVersion, Kind: projection.Kind, Metadata: projection.Metadata, Items: projection.Items}
			} else if source.SemanticCollection == "apiserver-tls-certificate-evidence" {
				var projection secretMetadataProjectionList
				if err := boundary.DecodeExactJSON(raw, &projection); err != nil || projection.Schema != "fs2-serve.nebius.ai/kubernetes-tls-certificate-projection/v1" {
					return nil, errors.New("TLS certificate inventory is not the exact independently authenticated public-material-only response schema")
				}
				list = kubernetesList{APIVersion: projection.APIVersion, Kind: projection.Kind, Metadata: projection.Metadata, Items: projection.Items}
			} else if err := boundary.DecodeExactJSON(raw, &list); err != nil {
				return nil, fmt.Errorf("semantic collection %s is not an exact Kubernetes List response", source.SemanticCollection)
			}
			if list.APIVersion == "" ||
				list.Kind != contract.kind+"List" || list.Metadata.ResourceVersion == "" {
				return nil, fmt.Errorf("semantic collection %s is not an exact Kubernetes List response", source.SemanticCollection)
			}
			if pageIndex == 0 {
				resourceVersion = list.Metadata.ResourceVersion
			} else if list.Metadata.ResourceVersion != resourceVersion {
				return nil, fmt.Errorf("semantic collection %s crosses Kubernetes list resourceVersion snapshots", source.SemanticCollection)
			}
			for _, itemRaw := range list.Items {
				object, err := decodeInventoryObject(source.SemanticCollection, contract, itemRaw)
				if err != nil {
					return nil, err
				}
				key := referenceKey(ObjectReference{source.SemanticCollection, object.identity.Namespace, object.identity.Name})
				if _, exists := inventory.byReference[key]; exists {
					return nil, fmt.Errorf("semantic collection %s repeats object %s/%s", source.SemanticCollection, object.identity.Namespace, object.identity.Name)
				}
				inventory.byReference[key] = object
				inventory.byCollection[source.SemanticCollection] = append(inventory.byCollection[source.SemanticCollection], object)
			}
		}
		sort.Slice(inventory.byCollection[source.SemanticCollection], func(left int, right int) bool {
			first := inventory.byCollection[source.SemanticCollection][left].identity
			second := inventory.byCollection[source.SemanticCollection][right].identity
			return first.Namespace+"/"+first.Name < second.Namespace+"/"+second.Name
		})
	}
	return inventory, nil
}

func validateKubernetesListSource(source collector.SourceSpec, contract collectionContract) error {
	if source.Pagination != "kubernetes-continue" {
		return errors.New("Kubernetes semantic inventory is not protected by continue-token pagination")
	}
	parsed, err := url.Parse(source.InitialURL)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.Fragment != "" {
		return errors.New("Kubernetes list source URL is invalid")
	}
	expectedPath := "/api/" + contractVersionPath(source.InitialURL) + "/" + contract.resource
	if source.SemanticCollection == "apiserver-secrets-metadata" {
		expectedPath = "/fs2-native/v1/secret-metadata"
	}
	if source.SemanticCollection == "apiserver-tls-certificate-evidence" {
		expectedPath = "/fs2-native/v1/tls-certificates"
	}
	if contract.apiGroup != "" {
		expectedPath = "/apis/" + contract.apiGroup + "/" + contractVersionPath(source.InitialURL) + "/" + contract.resource
	}
	if parsed.Path != expectedPath || strings.Contains(parsed.Path, "/namespaces/") {
		return errors.New("Kubernetes list source is not the exact cluster/all-namespace resource URL")
	}
	for key, values := range parsed.Query() {
		if (key != "limit" && key != "resourceVersion" && key != "resourceVersionMatch") || len(values) != 1 || values[0] == "" {
			return errors.New("Kubernetes list source uses a narrowing or ambiguous query")
		}
	}
	return nil
}

func contractVersionPath(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return ""
	}
	parts := strings.Split(strings.Trim(parsed.Path, "/"), "/")
	if len(parts) >= 2 && parts[0] == "api" {
		return parts[1]
	}
	if len(parts) >= 3 && parts[0] == "apis" {
		return parts[2]
	}
	return ""
}

func decodeInventoryObject(collection string, contract collectionContract, raw []byte) (inventoryObject, error) {
	var fields map[string]json.RawMessage
	if err := boundary.DecodeExactJSON(raw, &fields); err != nil {
		return inventoryObject{}, err
	}
	var apiVersion string
	var kind string
	if json.Unmarshal(fields["apiVersion"], &apiVersion) != nil || json.Unmarshal(fields["kind"], &kind) != nil || kind != contract.kind {
		return inventoryObject{}, fmt.Errorf("semantic collection %s contains the wrong native kind", collection)
	}
	group, version := splitAPIVersion(apiVersion)
	if group != contract.apiGroup || version == "" {
		return inventoryObject{}, fmt.Errorf("semantic collection %s contains the wrong API group", collection)
	}
	var metadata map[string]json.RawMessage
	if err := boundary.DecodeExactJSON(fields["metadata"], &metadata); err != nil {
		return inventoryObject{}, fmt.Errorf("semantic collection %s object metadata is invalid", collection)
	}
	var name, namespace, uid, resourceVersion string
	_ = json.Unmarshal(metadata["name"], &name)
	_ = json.Unmarshal(metadata["namespace"], &namespace)
	_ = json.Unmarshal(metadata["uid"], &uid)
	_ = json.Unmarshal(metadata["resourceVersion"], &resourceVersion)
	if name == "" || uid == "" || resourceVersion == "" || contract.namespaced && namespace == "" || !contract.namespaced && namespace != "" {
		return inventoryObject{}, fmt.Errorf("semantic collection %s object identity is incomplete", collection)
	}
	if collection == "apiserver-secrets-metadata" || collection == "apiserver-tls-certificate-evidence" {
		if _, exists := fields["data"]; exists {
			return inventoryObject{}, errors.New("Secret metadata collection contains customer secret data")
		}
		if _, exists := fields["stringData"]; exists {
			return inventoryObject{}, errors.New("Secret metadata collection contains customer secret stringData")
		}
	}
	objectSHA256, err := boundary.CanonicalObjectSHA256(raw)
	if err != nil {
		return inventoryObject{}, err
	}
	var value any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return inventoryObject{}, err
	}
	objectValue, ok := value.(map[string]any)
	if !ok {
		return inventoryObject{}, errors.New("native inventory object is not a JSON object")
	}
	return inventoryObject{
		collection: collection,
		identity: boundary.ObjectIdentity{
			APIGroup: group, APIVersion: version, Resource: contract.resource,
			Namespace: namespace, Name: name, UID: uid, ResourceVersion: resourceVersion,
			ObjectSHA256: objectSHA256,
		},
		kind: kind,
		value: objectValue,
		raw: append([]byte(nil), raw...),
	}, nil
}

func (inventory *semanticInventory) require(reference ObjectReference) (inventoryObject, error) {
	object, exists := inventory.byReference[referenceKey(reference)]
	if !exists {
		return inventoryObject{}, fmt.Errorf("native inventory omits %s %s/%s", reference.SemanticCollection, reference.Namespace, reference.Name)
	}
	return object, nil
}

func (inventory *semanticInventory) identities(references []ObjectReference) ([]boundary.ObjectIdentity, error) {
	result := make([]boundary.ObjectIdentity, 0, len(references))
	seen := map[string]struct{}{}
	for _, reference := range references {
		key := referenceKey(reference)
		if _, duplicate := seen[key]; duplicate {
			return nil, errors.New("semantic policy repeats an object reference")
		}
		seen[key] = struct{}{}
		object, err := inventory.require(reference)
		if err != nil {
			return nil, err
		}
		result = append(result, object.identity)
	}
	return sortIdentities(result), nil
}

func referenceKey(reference ObjectReference) string {
	return reference.SemanticCollection + "\x00" + reference.Namespace + "\x00" + reference.Name
}

func splitAPIVersion(value string) (string, string) {
	parts := strings.Split(value, "/")
	if len(parts) == 1 {
		return "", parts[0]
	}
	if len(parts) == 2 {
		return parts[0], parts[1]
	}
	return "", ""
}

func digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func digestJSON(value any) string {
	raw, _ := json.Marshal(value)
	return digest(raw)
}

func number(value any) (int64, bool) {
	switch item := value.(type) {
	case json.Number:
		parsed, err := item.Int64()
		return parsed, err == nil
	case string:
		parsed, err := strconv.ParseInt(item, 10, 64)
		return parsed, err == nil
	default:
		return 0, false
	}
}

func object(value any) (map[string]any, bool) {
	result, ok := value.(map[string]any)
	return result, ok
}

func array(value any) []any {
	result, _ := value.([]any)
	return result
}

func stringValue(value any) string {
	result, _ := value.(string)
	return result
}
