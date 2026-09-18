package snapshotauthority

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/collector"
)

type Authority struct {
	Config Config
	Acceptance boundary.Acceptance
	collectorConfig collector.Config
	nativeTrust *boundary.ExternalTrust
	snapshotTrust *boundary.ExternalTrust
	privateKey ed25519.PrivateKey
	channels *channelAuthenticator
	settlements settlementBackend
	slots chan struct{}
}

func LoadAuthority(configPath string, acceptance boundary.Acceptance) (*Authority, error) {
	if acceptance.Schema != boundary.AcceptancePayloadSchema { return nil, errors.New("semantic snapshot authority requires acceptance v5") }
	raw, err := readAcceptedRegular(configPath, maximumConfigBytes, acceptance.SnapshotAuthorityConfigSHA256, false)
	if err != nil { return nil, fmt.Errorf("read snapshot authority config: %w", err) }
	var config Config
	if _, err := boundary.CanonicalJSON(raw, &config); err != nil { return nil, fmt.Errorf("decode snapshot authority config: %w", err) }
	if err := validateConfig(config, acceptance); err != nil { return nil, err }
	if uint32(os.Geteuid()) != config.RuntimeUID || uint32(os.Getegid()) != config.RuntimeGID { return nil, errors.New("snapshot authority is not running as its distinct accepted UID/GID") }
	if err := requireNoSupplementaryGroups(); err != nil { return nil, err }
	collectorConfig, nativeTrust, err := collector.LoadEvidenceVerificationContract(config.NativeCollectorConfigPath, config.NativeResponseTrustPath, acceptance)
	if err != nil { return nil, err }
	expectedDailySettlements := uint64((86400 + collectorConfig.RefreshIntervalSeconds - 1) / collectorConfig.RefreshIntervalSeconds)
	if config.MaximumSettlementsPerDay != expectedDailySettlements { return nil, errors.New("snapshot settlement daily quota differs from the accepted collector cadence") }
	if collectorConfig.SnapshotCredentialLaneID != config.CollectorCredentialLaneID { return nil, errors.New("snapshot authority collector lane differs from the accepted collector source plan") }
	snapshotTrust, err := boundary.LoadExternalTrust(config.SnapshotTrustPath, acceptance.SnapshotTrustSHA256, boundary.TrustSchema)
	if err != nil { return nil, err }
	privateKey, err := loadSigningKey(config)
	if err != nil { return nil, err }
	channels, err := loadChannelAuthenticator(config)
	if err != nil { return nil, err }
	settlements, err := loadSettlementClient(config)
	if err != nil { return nil, err }
	return &Authority{
		Config: config, Acceptance: acceptance, collectorConfig: collectorConfig,
		nativeTrust: nativeTrust, snapshotTrust: snapshotTrust, privateKey: privateKey,
		channels: channels, settlements: settlements, slots: make(chan struct{}, config.MaximumConcurrentRequests),
	}, nil
}

func requireNoSupplementaryGroups() error {
	groups, err := os.Getgroups()
	if err != nil { return err }
	if len(groups) != 0 { return errors.New("snapshot authority process has forbidden supplementary groups") }
	return nil
}

func (authority *Authority) Serve(ctx context.Context) error {
	listener, err := net.Listen("tcp", authority.Config.ListenAddress)
	if err != nil { return err }
	tlsListener := tls.NewListener(listener, authority.channels.tlsConfig())
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet { http.Error(response, "method not allowed", http.StatusMethodNotAllowed); return }
		response.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/readyz", authority.handleReady)
	mux.HandleFunc("/v1/snapshot", authority.handleSnapshot)
	server := &http.Server{Handler: mux, ReadHeaderTimeout: 5*time.Second, ReadTimeout: 30*time.Second, WriteTimeout: 30*time.Second, IdleTimeout: 30*time.Second, MaxHeaderBytes: 32*1024}
	shutdownDone := make(chan error, 1)
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		shutdownDone <- server.Shutdown(shutdown)
	}()
	serveErr := server.Serve(tlsListener)
	if ctx.Err() != nil {
		shutdownErr := <-shutdownDone
		if shutdownErr != nil { return shutdownErr }
		if errors.Is(serveErr, http.ErrServerClosed) { return nil }
	}
	return serveErr
}

func (authority *Authority) handleReady(response http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet { http.Error(response, "method not allowed", http.StatusMethodNotAllowed); return }
	now := time.Now().UTC()
	if !authority.Acceptance.ProductionTrustCurrent(now) { http.Error(response, "production trust provenance is stale", http.StatusServiceUnavailable); return }
	if _, err := authority.channels.currentServer(now); err != nil || authority.channels.currentCollector(now) != nil || authority.settlements.ready() != nil || authority.verifySigningTrust() != nil { http.Error(response, "authority dependency unavailable", http.StatusServiceUnavailable); return }
	response.WriteHeader(http.StatusNoContent)
}

func (authority *Authority) handleSnapshot(response http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodPost { http.Error(response, "method not allowed", http.StatusMethodNotAllowed); return }
	if !authority.Acceptance.ProductionTrustCurrent(time.Now().UTC()) { http.Error(response, "production trust provenance is stale", http.StatusServiceUnavailable); return }
	select { case authority.slots <- struct{}{}: defer func(){ <-authority.slots }(); default: http.Error(response, "snapshot authority busy", http.StatusTooManyRequests); return }
	if request.TLS == nil { http.Error(response, "collector channel rejected", http.StatusUnauthorized); return }
	channelEvidence, err := authority.channels.authenticateCollector(*request.TLS, time.Now().UTC())
	if err != nil { http.Error(response, "collector channel rejected", http.StatusUnauthorized); return }
	maximum := authority.collectorConfig.MaximumBundleBytes
	if maximum < 1 || maximum > maximumRequestBytes { http.Error(response, "request bound unavailable", http.StatusServiceUnavailable); return }
	raw, err := io.ReadAll(io.LimitReader(request.Body, maximum+1))
	if err != nil || int64(len(raw)) < 1 || int64(len(raw)) > maximum { http.Error(response, "evidence bundle invalid", http.StatusBadRequest); return }
	requestDigest := digestBytes(raw)
	proofRaw, err := authority.channels.verifyCollectorPossession(request, requestDigest, time.Now().UTC())
	if err != nil { http.Error(response, "collector proof of possession rejected", http.StatusUnauthorized); return }
	channelEvidence.Schema = CollectorChannelEvidenceSchema
	channelEvidence.ClusterID = authority.Config.ClusterID
	channelEvidence.DeploymentID = authority.Config.DeploymentID
	channelEvidence.EvidenceBundleSHA256 = requestDigest
	channelEvidence.CollectorPossessionProofBase64 = base64.StdEncoding.EncodeToString(proofRaw)
	channelEvidence.CollectorPossessionProofSHA256 = digestBytes(proofRaw)
	channelAttestation, err := authority.signCollectorChannelEvidence(channelEvidence)
	if err != nil { http.Error(response, "collector channel attestation failed", http.StatusServiceUnavailable); return }
	channel := settlementChannel{Identity: channelEvidence.Slot+":"+channelEvidence.SPKISHA256, AttestationEnvelopeRaw: channelAttestation}
	if retained, err := authority.settlements.lookup(requestDigest, raw, channel); err == nil {
		if err := authority.validateSnapshotResponse(retained, raw, time.Now().UTC()); err != nil { http.Error(response, "retained settlement is no longer current", http.StatusConflict); return }
		response.Header().Set("Content-Type", "application/fs2-boundary-snapshot-envelope+json")
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write(retained)
		return
	} else if !errors.Is(err, os.ErrNotExist) {
		http.Error(response, "retained settlement conflict", http.StatusConflict)
		return
	}
	if recovered, err := authority.settlements.recover(raw, channel); err == nil {
		if err := authority.validateSnapshotResponse(recovered, raw, time.Now().UTC()); err != nil { http.Error(response, "recovered settlement is no longer current", http.StatusConflict); return }
		response.Header().Set("Content-Type", "application/fs2-boundary-snapshot-envelope+json")
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write(recovered)
		return
	} else if !errors.Is(err, os.ErrNotExist) {
		http.Error(response, "retained settlement recovery refused", http.StatusConflict)
		return
	}
	now := time.Now().UTC().Truncate(time.Second)
	verified, err := collector.VerifyEvidenceBundle(raw, authority.collectorConfig, authority.Acceptance, authority.nativeTrust, now)
	if err != nil { http.Error(response, "native evidence rejected", http.StatusForbidden); return }
	snapshot, err := DeriveSnapshot(verified, raw, authority.Config, authority.collectorConfig, authority.Acceptance)
	if err != nil { http.Error(response, "semantic authority derivation rejected", http.StatusForbidden); return }
	envelopeRaw, err := authority.signSnapshot(snapshot)
	if err != nil { http.Error(response, "snapshot signing failed", http.StatusServiceUnavailable); return }
	deadline, _ := time.Parse(time.RFC3339, verified.Bundle.CycleDeadlineAt)
	if !time.Now().UTC().Before(deadline) { http.Error(response, "collection deadline elapsed", http.StatusGatewayTimeout); return }
	settled, err := authority.settlements.settle(verified.Bundle, raw, envelopeRaw, channel)
	if err != nil { http.Error(response, "snapshot settlement failed", http.StatusServiceUnavailable); return }
	if err := authority.validateSnapshotResponse(settled, raw, time.Now().UTC()); err != nil { http.Error(response, "settled snapshot is not current", http.StatusServiceUnavailable); return }
	response.Header().Set("Content-Type", "application/fs2-boundary-snapshot-envelope+json")
	response.WriteHeader(http.StatusOK)
	_, _ = response.Write(settled)
}

func (authority *Authority) signCollectorChannelEvidence(evidence CollectorChannelEvidence) ([]byte, error) {
	if !authority.Acceptance.ProductionTrustCurrent(time.Now().UTC()) { return nil, errors.New("production trust provenance expired before channel signing") }
	payloadRaw, err := json.Marshal(evidence)
	if err != nil { return nil, err }
	payloadSHA256 := digestBytes(payloadRaw)
	message := bytes.Join([][]byte{[]byte(boundary.EnvelopeSchema), []byte(authority.Config.SigningIssuer), []byte(authority.Config.SigningKeyID), []byte(payloadSHA256), payloadRaw}, []byte("\n"))
	envelope := boundary.SignedEnvelope{Schema: boundary.EnvelopeSchema, Algorithm: "ed25519", Issuer: authority.Config.SigningIssuer, KeyID: authority.Config.SigningKeyID, PayloadSHA256: payloadSHA256, PayloadBase64: base64.StdEncoding.EncodeToString(payloadRaw), Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(authority.privateKey, message))}
	return json.Marshal(envelope)
}

func (authority *Authority) validateSnapshotResponse(envelopeRaw []byte, bundleRaw []byte, now time.Time) error {
	return validateSnapshotResponse(authority.snapshotTrust, authority.Config, authority.Acceptance, envelopeRaw, bundleRaw, now)
}

func validateSnapshotResponse(snapshotTrust *boundary.ExternalTrust, config Config, acceptance boundary.Acceptance, envelopeRaw []byte, bundleRaw []byte, now time.Time) error {
	payloadRaw, err := snapshotTrust.VerifyEnvelope(boundary.EnvelopeSchema, collector.SnapshotIssuerRole, envelopeRaw)
	if err != nil { return err }
	snapshot, err := boundary.DecodeSnapshotPayload(payloadRaw)
	if err != nil || snapshot.Schema != boundary.SnapshotSchema || snapshot.ClusterID != config.ClusterID || snapshot.DeploymentID != config.DeploymentID ||
		snapshot.AuthoritySnapshotID != acceptance.AuthoritySnapshotID || snapshot.AuthorityClosureSHA256 != acceptance.AuthorityClosureSHA256 ||
		snapshot.EvidenceBundleSHA256 != digestBytes(bundleRaw) {
		return errors.New("snapshot response differs from the current accepted authority")
	}
	var bundle collector.EvidenceBundle
	if _, err := boundary.CanonicalJSON(bundleRaw, &bundle); err != nil || snapshot.ActivationCycleID != bundle.CycleID ||
		snapshot.ActivationCycleContractSHA256 != bundle.CycleContractSHA256 || snapshot.ActivationCycleIssuedAt != bundle.CycleIssuedAt ||
		snapshot.ActivationCycleDeadlineAt != bundle.CycleDeadlineAt || snapshot.ActivationPredecessorSelectionSHA256 != bundle.ActivationPredecessorSelectionSHA256 {
		return errors.New("snapshot response differs from its exact evidence cycle")
	}
	issuedAt, issueErr := time.Parse(time.RFC3339, snapshot.IssuedAt)
	expiresAt, expiryErr := time.Parse(time.RFC3339, snapshot.ExpiresAt)
	cycleDeadline, cycleDeadlineErr := time.Parse(time.RFC3339, snapshot.ActivationCycleDeadlineAt)
	now = now.UTC().Truncate(time.Second)
	if issueErr != nil || expiryErr != nil || cycleDeadlineErr != nil || issuedAt.Nanosecond() != 0 || expiresAt.Nanosecond() != 0 || cycleDeadline.Nanosecond() != 0 ||
		issuedAt.After(now.Add(30*time.Second)) || !now.Before(expiresAt) || !now.Before(cycleDeadline) {
		return errors.New("snapshot response is not currently valid")
	}
	copySnapshot := snapshot
	copySnapshot.SnapshotID = ""
	canonical, err := json.Marshal(copySnapshot)
	if err != nil || digestBytes(canonical) != snapshot.SnapshotID { return errors.New("snapshot response ID is invalid") }
	return nil
}

func (authority *Authority) verifySigningTrust() error {
	payloadRaw := []byte("{}")
	payloadSHA256 := digestBytes(payloadRaw)
	message := bytes.Join([][]byte{[]byte(boundary.EnvelopeSchema), []byte(authority.Config.SigningIssuer), []byte(authority.Config.SigningKeyID), []byte(payloadSHA256), payloadRaw}, []byte("\n"))
	envelope := boundary.SignedEnvelope{Schema: boundary.EnvelopeSchema, Algorithm: "ed25519", Issuer: authority.Config.SigningIssuer, KeyID: authority.Config.SigningKeyID, PayloadSHA256: payloadSHA256, PayloadBase64: base64.StdEncoding.EncodeToString(payloadRaw), Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(authority.privateKey, message))}
	raw, err := json.Marshal(envelope)
	if err != nil { return err }
	verified, err := authority.snapshotTrust.VerifyEnvelope(boundary.EnvelopeSchema, collector.SnapshotIssuerRole, raw)
	if err != nil || !bytes.Equal(verified, payloadRaw) { return errors.New("snapshot signing identity is absent from current accepted trust") }
	return nil
}

func (authority *Authority) signSnapshot(snapshot boundary.Snapshot) ([]byte, error) {
	if !authority.Acceptance.ProductionTrustCurrent(time.Now().UTC()) { return nil, errors.New("production trust provenance expired before snapshot signing") }
	payloadRaw, err := json.Marshal(snapshot)
	if err != nil { return nil, err }
	payloadSHA256 := digestBytes(payloadRaw)
	message := bytes.Join([][]byte{[]byte(boundary.EnvelopeSchema), []byte(authority.Config.SigningIssuer), []byte(authority.Config.SigningKeyID), []byte(payloadSHA256), payloadRaw}, []byte("\n"))
	envelope := boundary.SignedEnvelope{Schema: boundary.EnvelopeSchema, Algorithm: "ed25519", Issuer: authority.Config.SigningIssuer, KeyID: authority.Config.SigningKeyID, PayloadSHA256: payloadSHA256, PayloadBase64: base64.StdEncoding.EncodeToString(payloadRaw), Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(authority.privateKey, message))}
	raw, err := json.Marshal(envelope)
	if err != nil { return nil, err }
	verifiedPayload, err := authority.snapshotTrust.VerifyEnvelope(boundary.EnvelopeSchema, collector.SnapshotIssuerRole, raw)
	if err != nil || !bytes.Equal(verifiedPayload, payloadRaw) { return nil, errors.New("snapshot signing key is absent from independently accepted runtime trust") }
	return raw, nil
}

func validateConfig(config Config, acceptance boundary.Acceptance) error {
	if config.Schema != ConfigSchema && config.Schema != PreviousConfigSchema || config.ClusterID != acceptance.ClusterID || config.DeploymentID != acceptance.DeploymentID ||
		config.RuntimeUID == 0 || config.RuntimeGID == 0 || config.SettlementRuntimeUID == 0 || config.SettlementRuntimeGID == 0 ||
		config.RuntimeUID == config.SettlementRuntimeUID || config.RuntimeGID == config.SettlementRuntimeGID || config.SettlementSocketName == "" || len(config.SettlementSocketName) > 100 || !strings.HasPrefix(config.SettlementSocketName, "@fs2-") || strings.ContainsAny(config.SettlementSocketName, "\x00\r\n") ||
		config.ListenAddress == "" || strings.ContainsAny(config.ListenAddress, "\x00\r\n") ||
		config.CollectorCredentialLaneID == "" || strings.ContainsAny(config.CollectorCredentialLaneID, "\x00\r\n") ||
		!acceptedRuntimePath(config.NativeCollectorConfigPath) || !acceptedRuntimePath(config.NativeResponseTrustPath) || !acceptedRuntimePath(config.SnapshotTrustPath) ||
		!acceptedRuntimePath(config.SigningKeyPath) || !isDigestText(config.SigningKeySHA256) || config.SigningIssuer == "" || config.SigningKeyID == "" || strings.ContainsAny(config.SigningIssuer+config.SigningKeyID, "\x00\r\n") ||
		!acceptedRuntimePath(config.SettlementSigningKeyPath) || !isDigestText(config.SettlementSigningKeySHA256) || config.SettlementSigningIssuer == "" || config.SettlementSigningKeyID == "" || strings.ContainsAny(config.SettlementSigningIssuer+config.SettlementSigningKeyID, "\x00\r\n") ||
		config.SigningKeySHA256 == config.SettlementSigningKeySHA256 || config.SigningKeyPath == config.SettlementSigningKeyPath || config.SigningKeyID == config.SettlementSigningKeyID ||
		config.SettlementDeviceID == 0 || config.SettlementCapacityBytes == 0 || config.SettlementCapacityInodes == 0 ||
		config.SettlementMinimumFreeBytes < 64*1024*1024 || config.SettlementMinimumFreeInodes < 1024 ||
		config.SettlementOperatingHorizonDays < 365 || config.SettlementOperatingHorizonDays > 3660 ||
		config.MaximumSettlementsPerDay < 1 || config.MaximumConcurrentRequests < 1 || config.MaximumConcurrentRequests > 64 ||
		config.SnapshotMaximumAgeSeconds < 270 || config.SnapshotMaximumAgeSeconds > 300 ||
		len(config.ServerIdentities) < 1 || len(config.ServerIdentities) > 2 || len(config.CollectorIdentities) < 1 || len(config.CollectorIdentities) > 2 {
		return errors.New("snapshot authority config is incomplete or outside its exact bounds")
	}
	if !sort.StringsAreSorted(config.Policy.AllowedControllerImages) || !sort.StringsAreSorted(config.Policy.AllowedProviderPrincipals) ||
		!sort.StringsAreSorted(config.Policy.ProtectedProviderResources) ||
		!sort.StringsAreSorted(config.Policy.AllowedClientCertificateSubjects) ||
		!sort.StringsAreSorted(config.Policy.AllowedClientCARootSPKISHA256) ||
		!sort.StringsAreSorted(config.Policy.AllowedRequestHeaderCARootSPKISHA256) ||
		!sort.StringsAreSorted(config.Policy.AllowedRequestHeaderProxyCommonNames) ||
		!sort.StringsAreSorted(config.Policy.RequestHeaderUsernameHeaders) ||
		!sort.StringsAreSorted(config.Policy.RequestHeaderGroupHeaders) ||
		!sort.StringsAreSorted(config.Policy.RequestHeaderExtraHeaderPrefixes) ||
		!sort.StringsAreSorted(config.Policy.AllowedCSRSignerNames) ||
		!sort.StringsAreSorted(config.Policy.AllowedNodeGroupIDs) || !sort.StringsAreSorted(config.Policy.RequiredKMSKeyIDs) ||
		len(config.Policy.AllowedControllerImages) == 0 || len(config.Policy.AllowedProviderPrincipals) == 0 || len(config.Policy.ProtectedProviderResources) == 0 ||
		len(config.Policy.AllowedClientCertificateSubjects) == 0 || len(config.Policy.AllowedClientCARootSPKISHA256) == 0 || len(config.Policy.AllowedRequestHeaderCARootSPKISHA256) == 0 || len(config.Policy.AllowedRequestHeaderProxyCommonNames) == 0 || len(config.Policy.RequestHeaderUsernameHeaders) == 0 || len(config.Policy.RequestHeaderGroupHeaders) == 0 || len(config.Policy.RequestHeaderExtraHeaderPrefixes) == 0 || len(config.Policy.AllowedCSRSignerNames) == 0 || len(config.Policy.AllowedNodeGroupIDs) == 0 || len(config.Policy.RequiredKMSKeyIDs) == 0 ||
		!isDigestText(config.Policy.APIServerAuthenticationArgumentsSHA256) || !isDigestText(config.Policy.SecretClassifierRuntimeSHA256) || !isDigestText(config.Policy.SecretClassifierPolicySHA256) {
		return errors.New("snapshot semantic policy allowlists are empty or non-canonical")
	}
	if len(config.SettlementVerificationKeys) < 1 || len(config.SettlementVerificationKeys) > 16 { return errors.New("snapshot settlement verification keyring is empty or outside its bound") }
	currentSettlementKey := false
	previousSettlementKey := ""
	for _, enrolled := range config.SettlementVerificationKeys {
		identity := enrolled.Issuer+"\x00"+enrolled.KeyID
		if enrolled.Issuer == "" || enrolled.KeyID == "" || strings.ContainsAny(enrolled.Issuer, "\x00\r\n") || strings.ContainsAny(enrolled.KeyID, "\x00\r\n") || !acceptedRuntimePath(enrolled.PublicKeyPath) || !isDigestText(enrolled.PublicKeySHA256) || previousSettlementKey != "" && identity <= previousSettlementKey { return errors.New("snapshot settlement verification keyring is not canonical and strictly ordered") }
		if enrolled.Issuer == config.SettlementSigningIssuer && enrolled.KeyID == config.SettlementSigningKeyID { currentSettlementKey = true }
		previousSettlementKey = identity
	}
	if !currentSettlementKey { return errors.New("current snapshot settlement signing identity is absent from its historical verification keyring") }
	identitySlots := map[string]bool{}
	for index, identity := range config.ServerIdentities {
		if identity.Slot == "" || len(identity.Slot) > 64 || strings.ContainsAny(identity.Slot, ":\x00\r\n") || identitySlots["server\x00"+identity.Slot] ||
			!acceptedRuntimePath(identity.CertificatePath) || !isDigestText(identity.CertificateSHA256) ||
			!acceptedRuntimePath(identity.PrivateKeyPath) || !isDigestText(identity.PrivateKeySHA256) ||
			!isDigestText(identity.SPKISHA256) || !acceptedRuntimePath(identity.IssuerBundlePath) || !isDigestText(identity.IssuerBundleSHA256) ||
			!acceptedRuntimePath(identity.CRLPath) || !isDigestText(identity.CRLSHA256) { return errors.New("snapshot authority server identity is incomplete or duplicated") }
		if _, _, err := acceptedWindow(identity.NotBefore, identity.NotAfter, time.Time{}, time.Time{}); err != nil { return errors.New("snapshot authority server identity validity window is invalid") }
		if config.Schema == ConfigSchema {
			expectedSlot := "current"
			if index == 1 { expectedSlot = "next" }
			if identity.Slot != expectedSlot || identity.DNSName == "" || len(identity.DNSName) > 253 || strings.ContainsAny(identity.DNSName, "\x00\r\n") { return errors.New("snapshot authority v3 server identity lacks its exact current/next role or DNS SAN") }
			if parsed, err := url.Parse("https://"+identity.DNSName); err != nil || parsed.Hostname() != identity.DNSName { return errors.New("snapshot authority server DNS identity is invalid") }
		}
		identitySlots["server\x00"+identity.Slot] = true
	}
	for index, identity := range config.CollectorIdentities {
		if identity.Slot == "" || len(identity.Slot) > 64 || strings.ContainsAny(identity.Slot, ":\x00\r\n") || identitySlots["collector\x00"+identity.Slot] ||
			!acceptedRuntimePath(identity.CABundlePath) || !isDigestText(identity.CABundleSHA256) || !isDigestText(identity.SPKISHA256) ||
			!acceptedRuntimePath(identity.CRLPath) || !isDigestText(identity.CRLSHA256) || identity.SPIFFEURI == "" || len(identity.SPIFFEURI) > 512 || strings.ContainsAny(identity.SPIFFEURI, "\x00\r\n") { return errors.New("snapshot authority collector identity is incomplete or duplicated") }
		if _, _, err := acceptedWindow(identity.NotBefore, identity.NotAfter, time.Time{}, time.Time{}); err != nil { return errors.New("snapshot authority collector identity validity window is invalid") }
		if parsed, err := url.ParseRequestURI(identity.SPIFFEURI); err != nil || parsed.Scheme != "spiffe" || parsed.Host == "" { return errors.New("snapshot authority collector SPIFFE SAN is invalid") }
		if config.Schema == ConfigSchema {
			expectedSlot := "current"
			if index == 1 { expectedSlot = "next" }
			if identity.Slot != expectedSlot { return errors.New("snapshot authority v3 collector identity lacks its exact current/next role") }
		}
		identitySlots["collector\x00"+identity.Slot] = true
	}
	for index, value := range config.Policy.AllowedClientCARootSPKISHA256 {
		if !isDigestText(value) || index > 0 && config.Policy.AllowedClientCARootSPKISHA256[index-1] == value { return errors.New("snapshot client CA root allowlist is invalid or duplicated") }
	}
	for index, value := range config.Policy.AllowedRequestHeaderCARootSPKISHA256 {
		if !isDigestText(value) || index > 0 && config.Policy.AllowedRequestHeaderCARootSPKISHA256[index-1] == value || containsString(config.Policy.AllowedClientCARootSPKISHA256, value) { return errors.New("snapshot request-header CA root allowlist is invalid, duplicated or overlaps the client-certificate roots") }
	}
	for _, values := range [][]string{config.Policy.AllowedControllerImages, config.Policy.AllowedProviderPrincipals, config.Policy.ProtectedProviderResources, config.Policy.AllowedClientCertificateSubjects, config.Policy.AllowedClientCARootSPKISHA256, config.Policy.AllowedRequestHeaderCARootSPKISHA256, config.Policy.AllowedRequestHeaderProxyCommonNames, config.Policy.RequestHeaderUsernameHeaders, config.Policy.RequestHeaderGroupHeaders, config.Policy.RequestHeaderExtraHeaderPrefixes, config.Policy.AllowedCSRSignerNames, config.Policy.AllowedNodeGroupIDs, config.Policy.RequiredKMSKeyIDs} {
		if !canonicalNonemptyStrings(values) { return errors.New("snapshot semantic policy contains an empty or duplicate accepted value") }
	}
	for _, values := range [][]string{config.Policy.AllowedRequestHeaderProxyCommonNames, config.Policy.RequestHeaderUsernameHeaders, config.Policy.RequestHeaderGroupHeaders, config.Policy.RequestHeaderExtraHeaderPrefixes} {
		for _, value := range values { if len(value) > 256 || strings.ContainsAny(value, ",\x00\r\n") { return errors.New("snapshot request-header identity mapping contains an unsafe or ambiguous value") } }
	}
	if err := validateAcceptedSemanticPolicy(config, acceptance); err != nil { return err }
	return validateSettlementCapacityContract(config)
}

// ValidateEnrollmentConfig validates the complete canonical configuration before
// an owner signs the corresponding custody handoff. It performs no file or
// network access and does not weaken the runtime validation performed at load.
func ValidateEnrollmentConfig(config Config, acceptance boundary.Acceptance) error {
	if config.Schema != ConfigSchema { return errors.New("new enrollment requires snapshot authority config v3; retained v2 is read-only compatibility") }
	return validateConfig(config, acceptance)
}

func acceptedRuntimePath(path string) bool {
	return filepath.IsAbs(path) && filepath.Clean(path) == path && strings.HasPrefix(path, "/var/run/fs2-boundary/") && !strings.ContainsAny(path, "\x00\r\n")
}

func loadSigningKey(config Config) (ed25519.PrivateKey, error) {
	return loadAcceptedSigningKey(config.SigningKeyPath, config.SigningKeySHA256)
}

func loadSettlementSigningKey(config Config) (ed25519.PrivateKey, error) {
	return loadAcceptedSigningKey(config.SettlementSigningKeyPath, config.SettlementSigningKeySHA256)
}

func loadSettlementVerificationKeys(config Config, currentPrivateKey ed25519.PrivateKey) (map[string]ed25519.PublicKey, error) {
	result := make(map[string]ed25519.PublicKey, len(config.SettlementVerificationKeys))
	for _, enrolled := range config.SettlementVerificationKeys {
		raw, err := readAcceptedRegular(enrolled.PublicKeyPath, 256*1024, enrolled.PublicKeySHA256, false)
		if err != nil { return nil, err }
		block, rest := pem.Decode(raw)
		if block == nil || len(rest) != 0 || block.Type != "PUBLIC KEY" { return nil, errors.New("snapshot settlement verification key is not one canonical public-key PEM object") }
		parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
		key, ok := parsed.(ed25519.PublicKey)
		if err != nil || !ok || len(key) != ed25519.PublicKeySize { return nil, errors.New("snapshot settlement verification key is not Ed25519") }
		identity := enrolled.Issuer+"\x00"+enrolled.KeyID
		if _, duplicate := result[identity]; duplicate { return nil, errors.New("snapshot settlement verification key identity is duplicated") }
		result[identity] = append(ed25519.PublicKey(nil), key...)
	}
	current, exists := result[config.SettlementSigningIssuer+"\x00"+config.SettlementSigningKeyID]
	if !exists || !bytes.Equal(current, currentPrivateKey.Public().(ed25519.PublicKey)) { return nil, errors.New("snapshot settlement current private key differs from its accepted verification key") }
	return result, nil
}

func loadAcceptedSigningKey(path string, digest string) (ed25519.PrivateKey, error) {
	raw, err := readAcceptedRegular(path, 256*1024, digest, true)
	if err != nil { return nil, err }
	block, rest := pem.Decode(raw)
	if block == nil || len(rest) != 0 || block.Type != "PRIVATE KEY" { return nil, errors.New("authority signing key is not one canonical PKCS8 PEM object") }
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	key, ok := parsed.(ed25519.PrivateKey)
	if err != nil || !ok || len(key) != ed25519.PrivateKeySize { return nil, errors.New("authority signing key is not Ed25519 PKCS8") }
	return append(ed25519.PrivateKey(nil), key...), nil
}

func requireSettlementRoot(config Config) error {
	info, err := os.Lstat(config.SettlementRoot)
	if err != nil || info == nil || !info.IsDir() || info.Mode().Perm() != 0o700 { return errors.New("snapshot settlement root is not its exact private directory") }
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != config.SettlementRuntimeUID || uint64(stat.Dev) != config.SettlementDeviceID { return errors.New("snapshot settlement root differs from its accepted UID or device") }
	return nil
}
