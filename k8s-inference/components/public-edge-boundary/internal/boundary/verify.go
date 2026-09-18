package boundary

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"
	"unicode"
)

const (
	maxTrustBytes    = 256 * 1024
	MaxSnapshotBytes = 4 * 1024 * 1024
	maxSnapshotBytes = MaxSnapshotBytes
	issuerRole       = "platform-security-public-edge-boundary-runtime"
)

type Runtime struct {
	Snapshot        Snapshot
	issuedAt        time.Time
	expiresAt       time.Time
	credentialNS    map[string]struct{}
	credentialItems map[string]ObjectIdentity
	authorityItems  map[string]ObjectIdentity
	protectedRoots  map[string]ObjectIdentity
	resourceGuards  []ResourceGuard
	transitions     map[string]Transition
}

func LoadRuntime(
	trustPath string,
	snapshotPath string,
	expectedTrustSHA256 string,
	expectedClusterID string,
	expectedDeploymentID string,
	expectedAuthoritySnapshotID string,
	expectedAuthorityClosureSHA256 string,
	now time.Time,
) (*Runtime, error) {
	trustRaw, err := readProtectedRegular(trustPath, maxTrustBytes)
	if err != nil {
		return nil, fmt.Errorf("read trust registry: %w", err)
	}
	envelopeRaw, err := readProtectedRegular(snapshotPath, maxSnapshotBytes)
	if err != nil {
		return nil, fmt.Errorf("read boundary snapshot: %w", err)
	}
	return LoadRuntimeFromBytes(
		trustRaw,
		envelopeRaw,
		expectedTrustSHA256,
		expectedClusterID,
		expectedDeploymentID,
		expectedAuthoritySnapshotID,
		expectedAuthorityClosureSHA256,
		now,
	)
}

// LoadRuntimeFromBytes verifies and constructs a Runtime from the exact trust
// and envelope bytes already read from protected descriptors. Callers that
// bind an activation receipt to an envelope must use this entry point rather
// than reopen a pathname between the digest check and signature verification.
func LoadRuntimeFromBytes(
	trustRaw []byte,
	envelopeRaw []byte,
	expectedTrustSHA256 string,
	expectedClusterID string,
	expectedDeploymentID string,
	expectedAuthoritySnapshotID string,
	expectedAuthorityClosureSHA256 string,
	now time.Time,
) (*Runtime, error) {
	return loadRuntimeFromBytes(
		trustRaw,
		envelopeRaw,
		expectedTrustSHA256,
		expectedClusterID,
		expectedDeploymentID,
		expectedAuthoritySnapshotID,
		expectedAuthorityClosureSHA256,
		now,
		true,
	)
}

// LoadRuntimeFromBytesForChainRecovery verifies the complete signed snapshot
// and authority closure without making it eligible to serve admission. It is
// used only to authenticate immutable selector predecessors while recovering
// the activation chain. Expired snapshots are permitted here; future-dated,
// malformed, wrongly pinned, or otherwise invalid snapshots remain rejected.
func LoadRuntimeFromBytesForChainRecovery(
	trustRaw []byte,
	envelopeRaw []byte,
	expectedTrustSHA256 string,
	expectedClusterID string,
	expectedDeploymentID string,
	expectedAuthoritySnapshotID string,
	expectedAuthorityClosureSHA256 string,
	now time.Time,
) (*Runtime, error) {
	return loadRuntimeFromBytes(
		trustRaw,
		envelopeRaw,
		expectedTrustSHA256,
		expectedClusterID,
		expectedDeploymentID,
		expectedAuthoritySnapshotID,
		expectedAuthorityClosureSHA256,
		now,
		false,
	)
}

func loadRuntimeFromBytes(
	trustRaw []byte,
	envelopeRaw []byte,
	expectedTrustSHA256 string,
	expectedClusterID string,
	expectedDeploymentID string,
	expectedAuthoritySnapshotID string,
	expectedAuthorityClosureSHA256 string,
	now time.Time,
	requireCurrent bool,
) (*Runtime, error) {
	if digestHex(trustRaw) != expectedTrustSHA256 {
		return nil, errors.New("trust registry digest differs from the deployed immutable value")
	}
	var trust TrustRegistry
	if err := decodeExactJSON(trustRaw, &trust); err != nil {
		return nil, fmt.Errorf("decode trust registry: %w", err)
	}
	if trust.Schema != TrustSchema || len(trust.Issuers) == 0 {
		return nil, errors.New("trust registry is empty or has an unsupported schema")
	}
	var envelope SignedEnvelope
	if err := decodeExactJSON(envelopeRaw, &envelope); err != nil {
		return nil, fmt.Errorf("decode boundary snapshot: %w", err)
	}
	if envelope.Schema == LegacyEnvelopeSchema {
		return nil, errors.New("legacy snapshot envelope v1 is retained evidence but cannot be reinterpreted as the one-use transition v2 contract")
	}
	if envelope.Schema == PreviousEnvelopeSchema {
		return nil, errors.New("snapshot envelope v2 is retained evidence but requires explicit read-only bootstrap before activation-chain v3")
	}
	if envelope.Schema != EnvelopeSchema || envelope.Algorithm != "ed25519" {
		return nil, errors.New("boundary snapshot envelope uses an unsupported signature contract")
	}
	payloadRaw, err := decodeCanonicalBase64(envelope.PayloadBase64)
	if err != nil {
		return nil, fmt.Errorf("decode boundary snapshot payload: %w", err)
	}
	if digestHex(payloadRaw) != envelope.PayloadSHA256 {
		return nil, errors.New("boundary snapshot payload digest does not match its exact bytes")
	}
	key, err := trustedKey(trust, envelope.Issuer, envelope.KeyID, issuerRole)
	if err != nil {
		return nil, err
	}
	signature, err := decodeCanonicalBase64URL(envelope.Signature, ed25519.SignatureSize)
	if err != nil {
		return nil, fmt.Errorf("decode boundary signature: %w", err)
	}
	message := bytes.Join(
		[][]byte{
			[]byte(EnvelopeSchema),
			[]byte(envelope.Issuer),
			[]byte(envelope.KeyID),
			[]byte(envelope.PayloadSHA256),
			payloadRaw,
		},
		[]byte("\n"),
	)
	if !ed25519.Verify(key, message, signature) {
		return nil, errors.New("boundary snapshot Ed25519 signature verification failed")
	}
	var snapshot Snapshot
	if err := decodeExactJSON(payloadRaw, &snapshot); err != nil {
		return nil, fmt.Errorf("decode signed boundary payload: %w", err)
	}
	if snapshot.Schema == LegacySnapshotSchema {
		return nil, errors.New("legacy snapshot payload v1 requires an additive v2 replacement and is never decoded as the changed transition contract")
	}
	if snapshot.Schema == PreviousSnapshotSchema {
		return nil, errors.New("snapshot payload v2 is retained evidence but cannot be reinterpreted as activation-chain v3")
	}
	canonicalPayload, err := json.Marshal(snapshot)
	if err != nil || !bytes.Equal(canonicalPayload, payloadRaw) {
		return nil, errors.New("signed boundary payload is not canonical JSON")
	}
	return newRuntime(
		snapshot,
		expectedClusterID,
		expectedDeploymentID,
		expectedAuthoritySnapshotID,
		expectedAuthorityClosureSHA256,
		now,
		requireCurrent,
	)
}

func newRuntime(
	snapshot Snapshot,
	expectedClusterID string,
	expectedDeploymentID string,
	expectedAuthoritySnapshotID string,
	expectedAuthorityClosureSHA256 string,
	now time.Time,
	requireCurrent bool,
) (*Runtime, error) {
	issuedAt, err := parseWholeUTC(snapshot.IssuedAt)
	if err != nil {
		return nil, fmt.Errorf("invalid snapshot issued_at: %w", err)
	}
	expiresAt, err := parseWholeUTC(snapshot.ExpiresAt)
	if err != nil {
		return nil, fmt.Errorf("invalid snapshot expires_at: %w", err)
	}
	now = now.UTC().Truncate(time.Second)
	activationIssuedAt, activationIssueErr := parseWholeUTC(snapshot.ActivationCycleIssuedAt)
	activationDeadlineAt, activationDeadlineErr := parseWholeUTC(snapshot.ActivationCycleDeadlineAt)
	if snapshot.Schema != SnapshotSchema || snapshot.ClusterID != expectedClusterID || snapshot.DeploymentID != expectedDeploymentID ||
		snapshot.AuthoritySnapshotID != expectedAuthoritySnapshotID {
		return nil, errors.New("snapshot does not bind this exact cluster and deployment")
	}
	if !isSHA256(snapshot.AuthoritySnapshotID) || !isSHA256(snapshot.SnapshotID) || !isSHA256(snapshot.EvidenceBundleSHA256) ||
		!isSHA256(snapshot.ActivationCycleContractSHA256) || !isSHA256(snapshot.ActivationCycleID) ||
		(snapshot.ActivationPredecessorSelectionSHA256 != "" && !isSHA256(snapshot.ActivationPredecessorSelectionSHA256)) ||
		activationIssueErr != nil || activationDeadlineErr != nil || !activationDeadlineAt.After(activationIssuedAt) ||
		issuedAt.Before(activationIssuedAt) || !issuedAt.Before(activationDeadlineAt) ||
		!isSHA256(snapshot.AuthorityClosureSHA256) || snapshot.AuthorityClosureSHA256 != expectedAuthorityClosureSHA256 ||
		snapshot.MaximumAgeSeconds < 1 || snapshot.MaximumAgeSeconds > 300 {
		return nil, errors.New("snapshot identity or maximum age is invalid")
	}
	closureRaw, _ := json.Marshal(AuthorityClosure{
		Controller:                snapshot.Controller,
		CredentialNamespaces:      snapshot.CredentialNamespaces,
		CredentialSecrets:         snapshot.CredentialSecrets,
		ControllerServiceAccounts: snapshot.ControllerServiceAccounts,
		CredentialWorkloads:       snapshot.CredentialWorkloads,
		ProtectedRoots:            snapshot.ProtectedRoots,
		AdmissionParameterRoots:   snapshot.AdmissionParameterRoots,
		EdgeProtectionRoots:       snapshot.EdgeProtectionRoots,
		ResourceGuards:            snapshot.ResourceGuards,
	})
	if digestHex(closureRaw) != snapshot.AuthorityClosureSHA256 {
		return nil, errors.New("snapshot authority closure differs from the independently pinned four-export evidence")
	}
	normalizedSnapshot := snapshot
	normalizedSnapshot.SnapshotID = ""
	normalizedRaw, _ := json.Marshal(normalizedSnapshot)
	if digestHex(normalizedRaw) != snapshot.SnapshotID {
		return nil, errors.New("snapshot_id does not identify the canonical normalized snapshot")
	}
	if issuedAt.After(now.Add(30*time.Second)) || !expiresAt.After(issuedAt) || expiresAt.Sub(issuedAt) > 10*time.Minute {
		return nil, errors.New("snapshot signed lifetime is invalid")
	}
	if requireCurrent && (now.Sub(issuedAt) > time.Duration(snapshot.MaximumAgeSeconds)*time.Second || !expiresAt.After(now)) {
		return nil, errors.New("snapshot is not current at the admission boundary")
	}
	if err := validateActor(snapshot.Controller); err != nil {
		return nil, fmt.Errorf("invalid controller actor: %w", err)
	}
	if !sortedUnique(snapshot.CredentialNamespaces) || len(snapshot.CredentialNamespaces) == 0 {
		return nil, errors.New("credential namespace closure is empty or non-canonical")
	}
	runtime := &Runtime{
		Snapshot:        snapshot,
		issuedAt:        issuedAt,
		expiresAt:       expiresAt,
		credentialNS:    map[string]struct{}{},
		credentialItems: map[string]ObjectIdentity{},
		authorityItems:  map[string]ObjectIdentity{},
		protectedRoots:  map[string]ObjectIdentity{},
		resourceGuards:  nil,
		transitions:     map[string]Transition{},
	}
	for _, namespace := range snapshot.CredentialNamespaces {
		if namespace == "" || namespace == "*" {
			return nil, errors.New("credential namespace contains an unsafe wildcard")
		}
		runtime.credentialNS[namespace] = struct{}{}
	}
	for _, collection := range [][]ObjectIdentity{
		snapshot.CredentialSecrets,
		snapshot.ControllerServiceAccounts,
		snapshot.CredentialWorkloads,
	} {
		for _, identity := range collection {
			if err := validateIdentity(identity, false); err != nil {
				return nil, err
			}
			key := identityKey(identity.APIGroup, identity.APIVersion, identity.Resource, identity.Namespace, identity.Name)
			if _, exists := runtime.credentialItems[key]; exists {
				return nil, errors.New("snapshot repeats a credential-reachable object")
			}
			runtime.credentialItems[key] = identity
			runtime.authorityItems[key] = identity
		}
	}
	hasApprovalCRD := false
	for _, identity := range snapshot.ProtectedRoots {
		if err := validateIdentity(identity, true); err != nil {
			return nil, err
		}
		key := identityKey(identity.APIGroup, identity.APIVersion, identity.Resource, identity.Namespace, identity.Name)
		if _, exists := runtime.protectedRoots[key]; exists {
			return nil, errors.New("snapshot repeats a protected root")
		}
		runtime.protectedRoots[key] = identity
		runtime.authorityItems[key] = identity
		if identity.APIGroup == "apiextensions.k8s.io" && identity.Resource == "customresourcedefinitions" &&
			identity.Namespace == "" && identity.Name == "publicedgenodeauthorityapprovals.security.fs2.nebius.ai" {
			hasApprovalCRD = true
		}
	}
	if !hasApprovalCRD {
		return nil, errors.New("snapshot protected roots omit the exact approval custom-resource definition")
	}
	parameterKeys := map[string]struct{}{}
	for _, identity := range snapshot.AdmissionParameterRoots {
		if err := validateIdentity(identity, true); err != nil {
			return nil, fmt.Errorf("invalid admission parameter root: %w", err)
		}
		key := identityKey(identity.APIGroup, identity.APIVersion, identity.Resource, identity.Namespace, identity.Name)
		protected, exists := runtime.protectedRoots[key]
		if !exists || protected.UID != identity.UID || protected.ResourceVersion != identity.ResourceVersion ||
			protected.ObjectSHA256 != identity.ObjectSHA256 {
			return nil, errors.New("admission parameter root is not the exact content-addressed protected object")
		}
		if _, duplicate := parameterKeys[key]; duplicate {
			return nil, errors.New("snapshot repeats an admission parameter root")
		}
		parameterKeys[key] = struct{}{}
	}
	if err := requireEdgeProtectionRoots(snapshot.EdgeProtectionRoots, runtime.protectedRoots); err != nil {
		return nil, err
	}
	hasGlobalNodeGuard := false
	guardKeys := map[string]struct{}{}
	for _, guard := range snapshot.ResourceGuards {
		if err := validateResourceGuard(guard); err != nil {
			return nil, err
		}
		encoded, _ := json.Marshal(guard)
		key := string(encoded)
		if _, exists := guardKeys[key]; exists {
			return nil, errors.New("snapshot repeats a resource guard")
		}
		guardKeys[key] = struct{}{}
		if guard.APIGroup == "" && guard.Resource == "nodes" &&
			equalStrings(guard.Subresources, []string{"", "proxy"}) &&
			equalStrings(guard.Namespaces, []string{"*"}) &&
			equalStrings(guard.Names, []string{"*"}) &&
			equalStrings(guard.Operations, []string{"CONNECT", "CREATE", "DELETE", "UPDATE"}) &&
			guard.SemanticGuard == "exact-node-authority-transition" {
			hasGlobalNodeGuard = true
		}
		runtime.resourceGuards = append(runtime.resourceGuards, guard)
	}
	if !hasGlobalNodeGuard {
		return nil, errors.New("snapshot omits the mandatory global nodes and nodes/proxy guard")
	}
	if err := requireMandatoryAuthorityGuards(snapshot.ResourceGuards, snapshot.CredentialNamespaces); err != nil {
		return nil, err
	}
	if err := requireProtectedRootGuards(snapshot.ResourceGuards, snapshot.ProtectedRoots); err != nil {
		return nil, err
	}
	transitionIDs := map[string]struct{}{}
	for _, transition := range snapshot.Transitions {
		if err := validateTransition(transition); err != nil {
			return nil, err
		}
		key := transitionKey(transition)
		if _, exists := runtime.transitions[key]; exists {
			return nil, errors.New("snapshot repeats an admission transition")
		}
		if _, exists := transitionIDs[transition.TransitionID]; exists {
			return nil, errors.New("snapshot repeats an admission transition ID")
		}
		transitionIDs[transition.TransitionID] = struct{}{}
		runtime.transitions[key] = transition
	}
	return runtime, nil
}

func requireEdgeProtectionRoots(roots []ObjectIdentity, protected map[string]ObjectIdentity) error {
	if len(roots) == 0 {
		return errors.New("snapshot omits the exact edge availability and denial-of-service protection roots")
	}
	required := map[string]bool{
		"policy/poddisruptionbudgets":                    false,
		"gateway.envoyproxy.io/backendtrafficpolicies":   false,
		"gateway.envoyproxy.io/clienttrafficpolicies":    false,
	}
	allowed := map[string]struct{}{
		"policy/poddisruptionbudgets":                    {},
		"gateway.envoyproxy.io/backendtrafficpolicies":   {},
		"gateway.envoyproxy.io/clienttrafficpolicies":    {},
		"gateway.envoyproxy.io/securitypolicies":         {},
		"gateway.envoyproxy.io/backends":                 {},
		"gateway.envoyproxy.io/envoyextensionpolicies":   {},
		"gateway.networking.k8s.io/backendtlspolicies":   {},
	}
	seen := map[string]struct{}{}
	for _, root := range roots {
		if err := validateIdentity(root, true); err != nil {
			return fmt.Errorf("invalid edge protection root: %w", err)
		}
		resourceKind := root.APIGroup + "/" + root.Resource
		if _, accepted := allowed[resourceKind]; !accepted {
			return fmt.Errorf("edge protection root %s is outside the exact accepted policy taxonomy", resourceKind)
		}
		key := identityKey(root.APIGroup, root.APIVersion, root.Resource, root.Namespace, root.Name)
		protectedRoot, exists := protected[key]
		if !exists || protectedRoot.UID != root.UID || protectedRoot.ResourceVersion != root.ResourceVersion ||
			protectedRoot.ObjectSHA256 != root.ObjectSHA256 {
			return errors.New("edge protection root is not the exact content-addressed protected object")
		}
		if _, duplicate := seen[key]; duplicate {
			return errors.New("snapshot repeats an edge protection root")
		}
		seen[key] = struct{}{}
		if _, baseline := required[resourceKind]; baseline {
			required[resourceKind] = true
		}
	}
	for resourceKind, present := range required {
		if !present {
			return fmt.Errorf("snapshot edge protection roots omit required %s objects", resourceKind)
		}
	}
	return nil
}

type mandatoryGuardRequirement struct {
	apiGroup      string
	resource      string
	subresource   string
	operation     string
	semanticGuard string
	clusterScoped bool
}

func requireMandatoryAuthorityGuards(guards []ResourceGuard, credentialNamespaces []string) error {
	requirements := []mandatoryGuardRequirement{
		{"rbac.authorization.k8s.io", "roles", "", "CREATE", "exact-object-transition", false},
		{"rbac.authorization.k8s.io", "rolebindings", "", "CREATE", "exact-object-transition", false},
		{"rbac.authorization.k8s.io", "clusterroles", "", "CREATE", "exact-object-transition", true},
		{"rbac.authorization.k8s.io", "clusterrolebindings", "", "CREATE", "exact-object-transition", true},
		{"", "serviceaccounts", "", "UPDATE", "inspect-controller-credential-reachability", false},
		{"", "serviceaccounts", "token", "CREATE", "exact-object-transition", false},
		{"", "secrets", "", "CREATE", "classify-controller-credential-secret", false},
		{"", "secrets", "", "UPDATE", "classify-controller-credential-secret", false},
		{"", "pods", "exec", "CONNECT", "exact-object-transition", false},
		{"", "pods", "attach", "CONNECT", "exact-object-transition", false},
		{"", "pods", "portforward", "CONNECT", "exact-object-transition", false},
		{"", "pods", "ephemeralcontainers", "UPDATE", "inspect-controller-credential-reachability", false},
		{"", "pods", "", "CREATE", "inspect-controller-credential-reachability", false},
		{"", "replicationcontrollers", "", "CREATE", "inspect-controller-credential-reachability", false},
		{"apps", "deployments", "", "CREATE", "inspect-controller-credential-reachability", false},
		{"apps", "daemonsets", "", "CREATE", "inspect-controller-credential-reachability", false},
		{"apps", "statefulsets", "", "CREATE", "inspect-controller-credential-reachability", false},
		{"apps", "replicasets", "", "CREATE", "inspect-controller-credential-reachability", false},
		{"batch", "jobs", "", "CREATE", "inspect-controller-credential-reachability", false},
		{"batch", "cronjobs", "", "CREATE", "inspect-controller-credential-reachability", false},
		{"admissionregistration.k8s.io", "validatingwebhookconfigurations", "", "UPDATE", "exact-object-transition", true},
		{"admissionregistration.k8s.io", "mutatingwebhookconfigurations", "", "UPDATE", "exact-object-transition", true},
		{"admissionregistration.k8s.io", "validatingadmissionpolicies", "", "UPDATE", "exact-object-transition", true},
		{"admissionregistration.k8s.io", "validatingadmissionpolicybindings", "", "UPDATE", "exact-object-transition", true},
		{"admissionregistration.k8s.io", "mutatingadmissionpolicies", "", "UPDATE", "exact-object-transition", true},
		{"admissionregistration.k8s.io", "mutatingadmissionpolicybindings", "", "UPDATE", "exact-object-transition", true},
		{"certificates.k8s.io", "certificatesigningrequests", "", "CREATE", "exact-object-transition", true},
		{"certificates.k8s.io", "certificatesigningrequests", "approval", "UPDATE", "exact-object-transition", true},
		{"certificates.k8s.io", "certificatesigningrequests", "status", "UPDATE", "exact-object-transition", true},
		{"security.fs2.nebius.ai", "publicedgenodeauthorityapprovals", "", "UPDATE", "exact-object-transition", true},
	}
	for _, requirement := range requirements {
		if requirement.clusterScoped {
			if !guardRequirementCovered(guards, requirement, "*") {
				return fmt.Errorf("snapshot omits mandatory authority guard for %s/%s/%s", requirement.apiGroup, requirement.resource, requirement.subresource)
			}
			continue
		}
		for _, namespace := range credentialNamespaces {
			if !guardRequirementCovered(guards, requirement, namespace) {
				return fmt.Errorf("snapshot omits mandatory credential-reachability guard for %s/%s/%s in namespace %s", requirement.apiGroup, requirement.resource, requirement.subresource, namespace)
			}
		}
	}
	return nil
}

func guardRequirementCovered(guards []ResourceGuard, requirement mandatoryGuardRequirement, namespace string) bool {
	operations := []string{requirement.operation}
	if requirement.subresource == "" && (requirement.operation == "CREATE" || requirement.operation == "UPDATE") {
		operations = []string{"CREATE", "DELETE", "UPDATE"}
	}
	for _, operation := range operations {
		covered := false
		for _, guard := range guards {
			if guard.APIGroup == requirement.apiGroup && guard.Resource == requirement.resource &&
				guard.SemanticGuard == requirement.semanticGuard && containsString(guard.Subresources, requirement.subresource) &&
				containsString(guard.Operations, operation) && containsString(guard.Names, "*") &&
				(containsString(guard.Namespaces, "*") || containsString(guard.Namespaces, namespace)) {
				covered = true
				break
			}
		}
		if !covered {
			return false
		}
	}
	return true
}

func requireProtectedRootGuards(guards []ResourceGuard, roots []ObjectIdentity) error {
	for _, root := range roots {
		namespace := root.Namespace
		if namespace == "" {
			namespace = "*"
		}
		for _, operation := range []string{"CREATE", "DELETE", "UPDATE"} {
			covered := false
			for _, guard := range guards {
				if guard.APIGroup == root.APIGroup && guard.Resource == root.Resource &&
					guard.SemanticGuard == "exact-object-transition" && containsString(guard.Subresources, "") &&
					containsString(guard.Operations, operation) && containsString(guard.Names, root.Name) &&
					(containsString(guard.Namespaces, namespace) || containsString(guard.Namespaces, "*")) {
					covered = true
					break
				}
			}
			if !covered {
				return fmt.Errorf("snapshot protected root %s/%s/%s lacks exact %s admission guard", root.APIGroup, root.Resource, root.Name, operation)
			}
		}
	}
	return nil
}

func containsString(values []string, expected string) bool {
	for _, value := range values {
		if value == expected {
			return true
		}
	}
	return false
}

func validateResourceGuard(guard ResourceGuard) error {
	if !safeText(guard.APIGroup, true) || !safeText(guard.Resource, false) || strings.Contains(guard.Resource, "/") ||
		!safeText(guard.SemanticGuard, false) {
		return errors.New("resource guard has an invalid resource")
	}
	if !sortedUniqueAllowEmptyValue(guard.Subresources) ||
		!sortedUnique(guard.Namespaces) || !sortedUnique(guard.Names) ||
		!sortedUnique(guard.Operations) {
		return errors.New("resource guard selectors are empty or non-canonical")
	}
	for _, operation := range guard.Operations {
		if operation != "CONNECT" && operation != "CREATE" && operation != "DELETE" && operation != "UPDATE" {
			return errors.New("resource guard contains an unsupported operation")
		}
	}
	for _, value := range guard.Subresources {
		if strings.Contains(value, "*") {
			return errors.New("resource guard subresources cannot contain wildcards")
		}
	}
	if guard.SemanticGuard != "exact-object-transition" &&
		guard.SemanticGuard != "exact-node-authority-transition" &&
		guard.SemanticGuard != "inspect-controller-credential-reachability" &&
		guard.SemanticGuard != "classify-controller-credential-secret" {
		return errors.New("resource guard semantic is unsupported")
	}
	return nil
}

func (r *Runtime) Current(now time.Time) bool {
	now = now.UTC().Truncate(time.Second)
	return !now.Before(r.issuedAt) && now.Before(r.expiresAt) && now.Sub(r.issuedAt) <= time.Duration(r.Snapshot.MaximumAgeSeconds)*time.Second
}

func readProtectedRegular(path string, maximum int64) ([]byte, error) {
	return readProtectedRegularMode(path, maximum, false)
}

func readProtectedPrivateKey(path string, maximum int64) ([]byte, error) {
	return readProtectedRegularMode(path, maximum, true)
}

func readProtectedRegularMode(path string, maximum int64, privateKey bool) ([]byte, error) {
	if err := requireProtectedDirectoryAncestry(filepath.Dir(path)); err != nil {
		return nil, err
	}
	before, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !before.Mode().IsRegular() || before.Mode().Perm()&0o022 != 0 || before.Size() < 1 || before.Size() > maximum {
		return nil, errors.New("input is not a bounded non-writable regular file")
	}
	if details, ok := before.Sys().(*syscall.Stat_t); !ok || details.Uid != 0 {
		return nil, errors.New("input is not owned by root")
	}
	if privateKey {
		details := before.Sys().(*syscall.Stat_t)
		rootOnly := before.Mode().Perm() == 0o400 || before.Mode().Perm() == 0o600
		boundaryGroupOnly := os.Geteuid() != 0 && before.Mode().Perm() == 0o440 && details.Gid == uint32(os.Getegid())
		if !rootOnly && !boundaryGroupOnly {
			return nil, errors.New("private key is not confined to root or the exact boundary process group")
		}
	}
	fileDescriptor, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fileDescriptor), path)
	defer file.Close()
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(raw)) > maximum {
		return nil, errors.New("input exceeds its bounded read")
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(before, after) || int64(len(raw)) != after.Size() {
		return nil, errors.New("input changed while it was read")
	}
	return raw, nil
}

func requireProtectedDirectoryAncestry(path string) error {
	clean := filepath.Clean(path)
	if !filepath.IsAbs(clean) || clean == "/" {
		return errors.New("protected input parent is not a dedicated absolute directory")
	}
	current := string(filepath.Separator)
	for _, part := range strings.Split(strings.TrimPrefix(clean, string(filepath.Separator)), string(filepath.Separator)) {
		if part == "" {
			continue
		}
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if err != nil {
			return err
		}
		details, ok := info.Sys().(*syscall.Stat_t)
		if !ok || details.Uid != 0 || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0o022 != 0 {
			return errors.New("protected input ancestry is not root-owned and non-writable")
		}
	}
	return nil
}

func decodeExactJSON(raw []byte, destination any) error {
	if err := rejectDuplicateKeys(raw); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	if err := ensureJSONEOF(decoder); err != nil {
		return err
	}
	return nil
}

func rejectDuplicateKeys(raw []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	var visit func() error
	visit = func() error {
		token, err := decoder.Token()
		if err != nil {
			return err
		}
		delimiter, composite := token.(json.Delim)
		if !composite {
			return nil
		}
		switch delimiter {
		case '{':
			seen := map[string]struct{}{}
			for decoder.More() {
				keyToken, err := decoder.Token()
				if err != nil {
					return err
				}
				key, ok := keyToken.(string)
				if !ok {
					return errors.New("JSON object key is not a string")
				}
				if _, exists := seen[key]; exists {
					return fmt.Errorf("duplicate JSON key %q", key)
				}
				seen[key] = struct{}{}
				if err := visit(); err != nil {
					return err
				}
			}
			_, err = decoder.Token()
			return err
		case '[':
			for decoder.More() {
				if err := visit(); err != nil {
					return err
				}
			}
			_, err = decoder.Token()
			return err
		default:
			return errors.New("unsupported JSON delimiter")
		}
	}
	if err := visit(); err != nil {
		return err
	}
	return ensureJSONEOF(decoder)
}

func ensureJSONEOF(decoder *json.Decoder) error {
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("JSON contains trailing data")
		}
		return err
	}
	return nil
}

func trustedKey(registry TrustRegistry, issuer string, keyID string, expectedRole string) (ed25519.PublicKey, error) {
	var match ed25519.PublicKey
	seen := map[string]struct{}{}
	for _, candidate := range registry.Issuers {
		identity := candidate.ID + "\x00" + candidate.KeyID
		if _, exists := seen[identity]; exists {
			return nil, errors.New("trust registry contains a duplicate issuer key")
		}
		seen[identity] = struct{}{}
		key, err := decodeCanonicalBase64URL(candidate.PublicKey, ed25519.PublicKeySize)
		if err != nil || candidate.KeyID != "sha256:"+digestHex(key) || candidate.Role != expectedRole {
			return nil, errors.New("trust registry contains an invalid boundary authority")
		}
		if candidate.ID == issuer && candidate.KeyID == keyID {
			if match != nil {
				return nil, errors.New("snapshot issuer is not unique")
			}
			match = ed25519.PublicKey(key)
		}
	}
	if match == nil {
		return nil, errors.New("snapshot issuer is not independently enrolled")
	}
	return match, nil
}

func validateActor(actor Actor) error {
	if !safeText(actor.Username, false) || !safeText(actor.UID, true) || !sortedUnique(actor.Groups) {
		return errors.New("actor identity is incomplete or non-canonical")
	}
	for key, values := range actor.Extra {
		if !safeText(key, false) || !sortedUnique(values) {
			return errors.New("actor extra identity is non-canonical")
		}
	}
	return nil
}

func validateIdentity(identity ObjectIdentity, allowClusterScoped bool) error {
	if !safeText(identity.APIGroup, true) || !safeText(identity.APIVersion, false) ||
		!safeText(identity.Resource, false) || !safeText(identity.Namespace, allowClusterScoped) ||
		!safeText(identity.Name, false) || !safeText(identity.UID, false) ||
		!safeText(identity.ResourceVersion, false) || !isSHA256(identity.ObjectSHA256) {
		return errors.New("snapshot object identity is incomplete")
	}
	if !allowClusterScoped && identity.Namespace == "" {
		return errors.New("credential-reachable object is not namespace scoped")
	}
	if strings.Contains(identity.Name, "*") || strings.Contains(identity.Namespace, "*") {
		return errors.New("snapshot object identity contains a wildcard")
	}
	return nil
}

func validateTransition(transition Transition) error {
	if transition.Operation != "CREATE" && transition.Operation != "UPDATE" && transition.Operation != "DELETE" && transition.Operation != "CONNECT" {
		return errors.New("transition operation is unsupported")
	}
	if !isSHA256(transition.TransitionID) || !validGroupVersionKind(transition.Kind, false) ||
		!validOriginalRequestTuple(transition.RequestKind, transition.RequestResource, transition.RequestSubresource) ||
		!safeText(transition.APIGroup, true) || !safeText(transition.APIVersion, false) ||
		!safeText(transition.Resource, false) || !safeText(transition.Subresource, true) ||
		!safeText(transition.Namespace, true) || !safeText(transition.Name, true) || !safeText(transition.GenerateName, true) ||
		!optionalSHA256(transition.RequestObjectSHA256) || !optionalSHA256(transition.OptionsSHA256) {
		return errors.New("transition object identity is incomplete")
	}
	switch transition.Operation {
	case "CREATE":
		serviceAccountToken := transition.APIGroup == "" && transition.Resource == "serviceaccounts" && transition.Subresource == "token"
		if serviceAccountToken {
			if transition.Name == "" || transition.GenerateName != "" || transition.RequestObjectSHA256 == "" ||
				!bindingEmpty(transition.NewObject) || !bindingEmpty(transition.OldObject) || !bindingComplete(transition.TargetObject) ||
				!sortedUnique(transition.TokenAudiences) || transition.TokenExpirationSeconds < 1 || transition.TokenExpirationSeconds > 600 {
				return errors.New("serviceaccount TokenRequest transition lacks exact request bytes and target service-account identity")
			}
			break
		}
		if transition.RequestObjectSHA256 == "" || !bindingForCreate(transition.NewObject) ||
			transition.RequestObjectSHA256 != transition.NewObject.ObjectSHA256 || !bindingEmpty(transition.OldObject) || !bindingEmpty(transition.TargetObject) ||
			(transition.Name == "" && transition.GenerateName == "") || (transition.Name != "" && transition.GenerateName != "") {
			return errors.New("CREATE transition lacks its exact new-object binding")
		}
	case "UPDATE":
		if transition.Name == "" || transition.GenerateName != "" || !bindingComplete(transition.OldObject) || !bindingComplete(transition.NewObject) || !bindingEmpty(transition.TargetObject) ||
			transition.RequestObjectSHA256 != transition.NewObject.ObjectSHA256 || transition.OldObject.UID != transition.NewObject.UID ||
			transition.OldObject.ResourceVersion != transition.NewObject.ResourceVersion {
			return errors.New("UPDATE transition lacks an exact old-to-new CAS binding")
		}
	case "DELETE":
		if transition.Name == "" || transition.GenerateName != "" || !bindingComplete(transition.OldObject) || !bindingEmpty(transition.NewObject) || !bindingEmpty(transition.TargetObject) {
			return errors.New("DELETE transition lacks its exact old-object binding")
		}
	case "CONNECT":
		if transition.Name == "" || transition.GenerateName != "" || transition.Subresource == "" ||
			transition.RequestObjectSHA256 != "" || !isSHA256(transition.OptionsSHA256) || !bindingComplete(transition.TargetObject) ||
			!bindingEmpty(transition.OldObject) || !bindingEmpty(transition.NewObject) {
			return errors.New("CONNECT transition lacks exact options and target UID/resourceVersion binding")
		}
	}
	if !(transition.Operation == "CREATE" && transition.APIGroup == "" && transition.Resource == "serviceaccounts" && transition.Subresource == "token") &&
		(len(transition.TokenAudiences) != 0 || transition.TokenExpirationSeconds != 0) {
		return errors.New("non-TokenRequest transition contains token scope")
	}
	return validateActor(transition.Actor)
}

func validOriginalRequestTuple(kind GroupVersionKind, resource GroupVersionRes, subresource string) bool {
	kindAbsent := kind.Group == "" && kind.Version == "" && kind.Kind == ""
	resourceAbsent := resource.Group == "" && resource.Version == "" && resource.Resource == ""
	if kindAbsent || resourceAbsent {
		return kindAbsent && resourceAbsent && subresource == ""
	}
	return validGroupVersionKind(kind, false) && validGroupVersionResource(resource, false) && safeText(subresource, true)
}

func validGroupVersionKind(value GroupVersionKind, allowAbsent bool) bool {
	absent := value.Group == "" && value.Version == "" && value.Kind == ""
	return allowAbsent && absent || safeText(value.Group, true) && safeText(value.Version, false) && safeText(value.Kind, false)
}

func validGroupVersionResource(value GroupVersionRes, allowAbsent bool) bool {
	absent := value.Group == "" && value.Version == "" && value.Resource == ""
	return allowAbsent && absent || safeText(value.Group, true) && safeText(value.Version, false) && safeText(value.Resource, false)
}

func optionalSHA256(value string) bool {
	return value == "" || isSHA256(value)
}

func bindingEmpty(value AdmissionObjectBinding) bool {
	return value.UID == "" && value.ResourceVersion == "" && value.ObjectSHA256 == ""
}

func bindingComplete(value AdmissionObjectBinding) bool {
	return safeText(value.UID, false) && safeText(value.ResourceVersion, false) && isSHA256(value.ObjectSHA256)
}

func bindingForCreate(value AdmissionObjectBinding) bool {
	return value.UID == "" && value.ResourceVersion == "" && isSHA256(value.ObjectSHA256)
}

func parseWholeUTC(value string) (time.Time, error) {
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil || parsed.Location() != time.UTC || parsed.Nanosecond() != 0 || !strings.HasSuffix(value, "Z") {
		return time.Time{}, errors.New("timestamp is not whole-second RFC3339 UTC")
	}
	return parsed, nil
}

func decodeCanonicalBase64(value string) ([]byte, error) {
	decoded, err := base64.StdEncoding.DecodeString(value)
	if err != nil || base64.StdEncoding.EncodeToString(decoded) != value {
		return nil, errors.New("value is not canonical base64")
	}
	return decoded, nil
}

func decodeCanonicalBase64URL(value string, size int) ([]byte, error) {
	decoded, err := base64.RawURLEncoding.DecodeString(value)
	if err != nil || len(decoded) != size || base64.RawURLEncoding.EncodeToString(decoded) != value {
		return nil, errors.New("value is not canonical base64url")
	}
	return decoded, nil
}

func digestHex(value []byte) string {
	sum := sha256.Sum256(value)
	return hex.EncodeToString(sum[:])
}

func isSHA256(value string) bool {
	if len(value) != 64 || value == strings.Repeat("0", 64) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func sortedUnique(values []string) bool {
	if len(values) == 0 || !sort.StringsAreSorted(values) {
		return false
	}
	for index, value := range values {
		if !safeText(value, false) || (index > 0 && values[index-1] == value) {
			return false
		}
	}
	return true
}

func sortedUniqueAllowEmptyValue(values []string) bool {
	if len(values) == 0 || !sort.StringsAreSorted(values) {
		return false
	}
	for index, value := range values {
		if !safeText(value, true) || index > 0 && values[index-1] == value {
			return false
		}
	}
	return true
}

func equalStrings(left []string, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func identityKey(group, version, resource, namespace, name string) string {
	raw, _ := json.Marshal([]string{group, version, resource, namespace, name})
	return string(raw)
}

func transitionKey(transition Transition) string {
	key := transition
	key.TransitionID = ""
	key.Actor = Actor{}
	raw, _ := json.Marshal(key)
	return string(raw)
}

func safeText(value string, allowEmpty bool) bool {
	if value == "" {
		return allowEmpty
	}
	for _, character := range value {
		if unicode.IsControl(character) {
			return false
		}
	}
	return true
}
