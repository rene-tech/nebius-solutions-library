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
	maxSnapshotBytes = 4 * 1024 * 1024
	issuerRole       = "platform-security-public-edge-boundary-runtime"
)

type Runtime struct {
	Snapshot        Snapshot
	issuedAt        time.Time
	expiresAt       time.Time
	credentialNS    map[string]struct{}
	credentialItems map[string]ObjectIdentity
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

	envelopeRaw, err := readProtectedRegular(snapshotPath, maxSnapshotBytes)
	if err != nil {
		return nil, fmt.Errorf("read boundary snapshot: %w", err)
	}
	var envelope SignedEnvelope
	if err := decodeExactJSON(envelopeRaw, &envelope); err != nil {
		return nil, fmt.Errorf("decode boundary snapshot: %w", err)
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
	)
}

func newRuntime(
	snapshot Snapshot,
	expectedClusterID string,
	expectedDeploymentID string,
	expectedAuthoritySnapshotID string,
	expectedAuthorityClosureSHA256 string,
	now time.Time,
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
	if snapshot.Schema != SnapshotSchema || snapshot.ClusterID != expectedClusterID || snapshot.DeploymentID != expectedDeploymentID ||
		snapshot.AuthoritySnapshotID != expectedAuthoritySnapshotID {
		return nil, errors.New("snapshot does not bind this exact cluster and deployment")
	}
	if !isSHA256(snapshot.AuthoritySnapshotID) || !isSHA256(snapshot.SnapshotID) || !isSHA256(snapshot.EvidenceBundleSHA256) ||
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
	if issuedAt.After(now.Add(30*time.Second)) || now.Sub(issuedAt) > time.Duration(snapshot.MaximumAgeSeconds)*time.Second || !expiresAt.After(now) || expiresAt.Sub(issuedAt) > 10*time.Minute {
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
		}
	}
	for _, identity := range snapshot.ProtectedRoots {
		if err := validateIdentity(identity, true); err != nil {
			return nil, err
		}
		key := identityKey(identity.APIGroup, identity.APIVersion, identity.Resource, identity.Namespace, identity.Name)
		if _, exists := runtime.protectedRoots[key]; exists {
			return nil, errors.New("snapshot repeats a protected root")
		}
		runtime.protectedRoots[key] = identity
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
	for _, transition := range snapshot.Transitions {
		if err := validateTransition(transition); err != nil {
			return nil, err
		}
		key := transitionKey(transition)
		if _, exists := runtime.transitions[key]; exists {
			return nil, errors.New("snapshot repeats an admission transition")
		}
		runtime.transitions[key] = transition
	}
	return runtime, nil
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
	if !safeText(transition.APIGroup, true) || !safeText(transition.APIVersion, false) ||
		!safeText(transition.Resource, false) || !safeText(transition.Subresource, true) ||
		!safeText(transition.Namespace, true) || !safeText(transition.Name, false) ||
		!isSHA256(transition.ObjectSHA256) {
		return errors.New("transition object identity is incomplete")
	}
	return validateActor(transition.Actor)
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
	raw, _ := json.Marshal(
		[]string{
			transition.Operation,
			transition.APIGroup,
			transition.APIVersion,
			transition.Resource,
			transition.Subresource,
			transition.Namespace,
			transition.Name,
			transition.ObjectSHA256,
		},
	)
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
