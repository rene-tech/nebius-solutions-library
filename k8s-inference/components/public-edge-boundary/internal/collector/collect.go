package collector

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
)

const (
	maximumConfigBytes   = 1024 * 1024
	maximumEnvelopeBytes = 64 * 1024 * 1024
	maximumEvidenceReferenceBytes = 1024
)

type Runner struct {
	Config       Config
	Acceptance   boundary.Acceptance
	nativeTrustPath   string
	snapshotTrustPath string
	evidenceMu        sync.Mutex
}

func LoadRunner(
	configPath string,
	nativeTrustPath string,
	snapshotTrustPath string,
	acceptance boundary.Acceptance,
) (*Runner, error) {
	configRaw, err := readRootRegular(configPath, maximumConfigBytes)
	if err != nil {
		return nil, fmt.Errorf("read native collector config: %w", err)
	}
	if digest(configRaw) != acceptance.NativeCollectorConfigSHA256 {
		return nil, errors.New("native collector config differs from independently accepted bytes")
	}
	if err := rejectClosedIntegrationGate(configRaw, "native-collector"); err != nil {
		return nil, err
	}
	var config Config
	if err := canonicalJSON(configRaw, &config); err != nil {
		return nil, fmt.Errorf("decode native collector config: %w", err)
	}
	if err := validateConfig(config, acceptance); err != nil {
		return nil, err
	}
	if err := requireRootOwnedDirectory(config.EvidenceRoot); err != nil {
		return nil, fmt.Errorf("validate native evidence root: %w", err)
	}
	if err := requireRootOwnedDirectory(config.RuntimeRoot); err != nil {
		return nil, fmt.Errorf("validate native runtime root: %w", err)
	}
	if err := validateEvidenceCapacity(config, 0, 0); err != nil {
		return nil, fmt.Errorf("validate native evidence capacity horizon: %w", err)
	}
	nativeTrustRaw, err := readRootRegular(nativeTrustPath, maximumConfigBytes)
	if err != nil || digest(nativeTrustRaw) != acceptance.NativeResponseTrustSHA256 {
		return nil, errors.New("native response trust differs from independently accepted bytes")
	}
	snapshotTrustRaw, err := readRootRegular(snapshotTrustPath, maximumConfigBytes)
	if err != nil || digest(snapshotTrustRaw) != acceptance.SnapshotTrustSHA256 {
		return nil, errors.New("snapshot response trust differs from independently accepted bytes")
	}
	return &Runner{
		Config:            config,
		Acceptance:        acceptance,
		nativeTrustPath:   nativeTrustPath,
		snapshotTrustPath: snapshotTrustPath,
	}, nil
}

func rejectClosedIntegrationGate(raw []byte, component string) error {
	var discriminator map[string]json.RawMessage
	if err := json.Unmarshal(raw, &discriminator); err != nil {
		return err
	}
	var schema string
	if err := json.Unmarshal(discriminator["schema"], &schema); err != nil || schema != IntegrationGateSchema {
		return nil
	}
	var gate IntegrationGate
	if err := canonicalJSON(raw, &gate); err != nil {
		return err
	}
	if gate.Component != component || gate.Status != "blocked-pending-owner-enrollment" || len(gate.Blockers) == 0 || !sort.StringsAreSorted(gate.Blockers) {
		return errors.New("external enrollment gate is malformed")
	}
	return fmt.Errorf("%s external enrollment gate is closed: %s", component, strings.Join(gate.Blockers, ", "))
}

func (r *Runner) CollectAndInstall(ctx context.Context, now time.Time) error {
	now = now.UTC().Truncate(time.Second)
	bundle := EvidenceBundle{
		Schema:       EvidenceBundleSchema,
		ClusterID:    r.Config.ClusterID,
		DeploymentID: r.Config.DeploymentID,
		EvidenceStoreID: r.Config.EvidenceStoreID,
		RefreshIntervalSeconds: r.Config.RefreshIntervalSeconds,
		CollectionDeadlineSeconds: r.Config.CollectionDeadlineSeconds,
		CollectedAt:  now.Format(time.RFC3339),
	}
	results := make([]SourceEvidence, len(r.Config.Sources))
	collectionContext, cancel := context.WithCancel(ctx)
	defer cancel()
	semaphore := make(chan struct{}, r.Config.MaximumConcurrentSources)
	errorsByIndex := make([]error, len(r.Config.Sources))
	var group sync.WaitGroup
	for index, source := range r.Config.Sources {
		index := index
		source := source
		group.Add(1)
		go func() {
			defer group.Done()
			select {
			case semaphore <- struct{}{}:
				defer func() { <-semaphore }()
			case <-collectionContext.Done():
				errorsByIndex[index] = collectionContext.Err()
				return
			}
			results[index], errorsByIndex[index] = r.collectSource(collectionContext, source, now)
			if errorsByIndex[index] != nil {
				cancel()
			}
		}()
	}
	group.Wait()
	for index, err := range errorsByIndex {
		if err != nil {
			return fmt.Errorf("collect %s: %w", r.Config.Sources[index].ID, err)
		}
	}
	bundle.Sources = results
	bundleRaw, err := json.Marshal(bundle)
	if err != nil || int64(len(bundleRaw)) > r.Config.MaximumBundleBytes {
		return errors.New("native evidence bundle exceeds its canonical bound")
	}
	if err := r.appendEvidence("native-bundle-"+digest(bundleRaw)+".json", bundleRaw); err != nil {
		return err
	}
	envelopeRaw, err := r.requestSnapshot(ctx, bundleRaw)
	if err != nil {
		return err
	}
	return r.installSnapshot(envelopeRaw, now)
}

func (r *Runner) collectSource(ctx context.Context, source SourceSpec, now time.Time) (SourceEvidence, error) {
	authorityClient, err := mutualTLSClient(
		source.AttestationCABundlePath,
		source.AttestationCABundleSHA256,
		source.AttestationServerName,
		source.AttestationClientCertificate,
		source.AttestationClientCertificateSHA256,
		source.AttestationClientKey,
		source.AttestationClientKeySHA256,
		source.AttestationClientSPKISHA256,
		source.AttestationClientSPIFFEURI,
	)
	if err != nil {
		return SourceEvidence{}, err
	}
	evidence := SourceEvidence{SourceID: source.ID, Kind: source.Kind, SemanticCollection: source.SemanticCollection}
	sessionID, err := randomID()
	if err != nil {
		return SourceEvidence{}, err
	}
	pageToken := ""
	var totalBytes int64
	providerRequestIDs := map[string]struct{}{}
	for pageNumber := 0; pageNumber < source.MaximumPages; pageNumber++ {
		challenge, err := randomID()
		if err != nil {
			return SourceEvidence{}, err
		}
		directive := CollectionRequest{
			Schema:       CollectionRequestSchema,
			ClusterID:    r.Acceptance.ClusterID,
			DeploymentID: r.Acceptance.DeploymentID,
			SourceID:     source.ID,
			SessionID:    sessionID,
			PageIndex:    pageNumber,
			PageToken:    pageToken,
			Challenge:    challenge,
		}
		directiveRaw, _ := json.Marshal(directive)
		request, err := http.NewRequestWithContext(ctx, http.MethodPost, source.AttestationURL, bytes.NewReader(directiveRaw))
		if err != nil {
			return SourceEvidence{}, err
		}
		request.Header.Set("Content-Type", "application/fs2-native-collection-request+json")
		request.Header.Set("Accept", "application/fs2-native-response-envelope+json")
		response, err := authorityClient.Do(request)
		if err != nil {
			return SourceEvidence{}, err
		}
		maximumEnvelopeBytes := source.MaximumPageBytes*2 + 1024*1024
		envelopeRaw, readErr := io.ReadAll(io.LimitReader(response.Body, maximumEnvelopeBytes+1))
		closeErr := response.Body.Close()
		if readErr != nil || closeErr != nil || int64(len(envelopeRaw)) > maximumEnvelopeBytes || response.StatusCode != http.StatusOK ||
			response.TLS == nil || len(response.TLS.PeerCertificates) == 0 {
			return SourceEvidence{}, errors.New("independent native authority response is unauthenticated, unsuccessful or oversized")
		}
		payloadRaw, err := boundary.VerifyExternalEnvelope(
			r.nativeTrustPath,
			NativeTrustSchema,
			NativeEnvelopeSchema,
			NativeIssuerRole,
			envelopeRaw,
		)
		if err != nil {
			return SourceEvidence{}, err
		}
		var page NativePage
		if _, err := boundary.CanonicalJSON(payloadRaw, &page); err != nil {
			return SourceEvidence{}, err
		}
		if page.Schema != NativePageSchema || page.SourceID != source.ID || page.SourceKind != source.Kind ||
			page.SemanticCollection != source.SemanticCollection ||
			page.SessionID != sessionID || page.Challenge != challenge || page.PageIndex != pageNumber || page.ProviderRequestID == "" ||
			!isDigest(page.TLSPeerCertificateSHA256) || page.HTTPStatus != http.StatusOK {
			return SourceEvidence{}, errors.New("native authority response does not bind the collection directive")
		}
		if _, duplicate := providerRequestIDs[page.ProviderRequestID]; duplicate {
			return SourceEvidence{}, errors.New("native authority repeats a provider request identifier")
		}
		providerRequestIDs[page.ProviderRequestID] = struct{}{}
		providerRequestRaw, err := base64.StdEncoding.DecodeString(page.RequestBase64)
		if err != nil || base64.StdEncoding.EncodeToString(providerRequestRaw) != page.RequestBase64 || digest(providerRequestRaw) != page.RequestSHA256 {
			return SourceEvidence{}, errors.New("native authority request bytes are not content addressed")
		}
		var providerRequest NativeRequest
		if _, err := boundary.CanonicalJSON(providerRequestRaw, &providerRequest); err != nil ||
			providerRequest.SourceID != source.ID || providerRequest.URL != page.RequestedURL || providerRequest.Method != http.MethodGet {
			return SourceEvidence{}, errors.New("native authority provider request is not exact")
		}
		expectedURL, err := collectorPageURL(source.InitialURL, source.Pagination, pageToken)
		if err != nil || page.RequestedURL != expectedURL {
			return SourceEvidence{}, errors.New("native authority requested an unexpected provider URL")
		}
		rawResponse, err := base64.StdEncoding.DecodeString(page.RawResponseBase64)
		if err != nil || base64.StdEncoding.EncodeToString(rawResponse) != page.RawResponseBase64 || digest(rawResponse) != page.RawResponseSHA256 {
			return SourceEvidence{}, errors.New("native authority response bytes are not content addressed")
		}
		totalBytes += int64(len(rawResponse))
		if totalBytes > source.MaximumTotalBytes {
			return SourceEvidence{}, errors.New("native response set exceeds its total bound")
		}
		if err := validatePage(page, source, directive, providerRequest, providerRequestRaw, expectedURL, rawResponse, time.Now().UTC()); err != nil {
			return SourceEvidence{}, err
		}
		complete, nextToken, derivedNextURL, err := derivePagination(source, page.RequestedURL, rawResponse)
		if err != nil || page.Complete != complete || page.NextToken != nextToken || page.NextURL != derivedNextURL {
			return SourceEvidence{}, errors.New("native authority pagination differs from reopened provider bytes")
		}
		envelopeSHA256 := digest(envelopeRaw)
		envelopeObject := "native-page-" + envelopeSHA256 + ".json"
		if err := r.appendEvidence(envelopeObject, envelopeRaw); err != nil {
			return SourceEvidence{}, err
		}
		evidence.Pages = append(evidence.Pages, CapturedPage{
			EnvelopeObject: envelopeObject,
			EnvelopeSHA256: envelopeSHA256,
			PayloadSHA256:  digest(payloadRaw),
		})
		if page.Complete {
			return evidence, nil
		}
		if err := sameAuthorityURL(source.InitialURL, derivedNextURL); err != nil {
			return SourceEvidence{}, err
		}
		pageToken = nextToken
	}
	return SourceEvidence{}, errors.New("native source did not reach a signed terminal page")
}

func (r *Runner) requestSnapshot(ctx context.Context, bundleRaw []byte) ([]byte, error) {
	client, err := mutualTLSClient(
		r.Config.SnapshotAuthorityCAPath,
		r.Config.SnapshotAuthorityCASHA256,
		r.Config.SnapshotAuthorityName,
		r.Config.SnapshotClientCertificate,
		r.Config.SnapshotClientCertificateSHA256,
		r.Config.SnapshotClientKey,
		r.Config.SnapshotClientKeySHA256,
		r.Config.SnapshotClientSPKISHA256,
		r.Config.SnapshotClientSPIFFEURI,
	)
	if err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, r.Config.SnapshotAuthorityURL, bytes.NewReader(bundleRaw))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/fs2-native-evidence-bundle+json")
	request.Header.Set("Accept", "application/fs2-public-edge-boundary-envelope+json")
	response, err := client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	envelopeRaw, err := io.ReadAll(io.LimitReader(response.Body, maximumEnvelopeBytes+1))
	if err != nil || len(envelopeRaw) > maximumEnvelopeBytes || response.StatusCode != http.StatusOK {
		return nil, errors.New("snapshot authority response is unsuccessful or oversized")
	}
	payloadRaw, err := boundary.VerifyExternalEnvelope(
		r.snapshotTrustPath,
		boundary.TrustSchema,
		boundary.EnvelopeSchema,
		SnapshotIssuerRole,
		envelopeRaw,
	)
	if err != nil {
		return nil, err
	}
	var snapshot boundary.Snapshot
	if _, err := boundary.CanonicalJSON(payloadRaw, &snapshot); err != nil || snapshot.EvidenceBundleSHA256 != digest(bundleRaw) {
		return nil, errors.New("snapshot response is not bound to the exact native evidence request")
	}
	issuedAt, issueErr := time.Parse(time.RFC3339, snapshot.IssuedAt)
	expiresAt, expiryErr := time.Parse(time.RFC3339, snapshot.ExpiresAt)
	minimumLifetime := time.Duration(r.Config.RefreshIntervalSeconds+30) * time.Second
	responseTime := time.Now().UTC()
	if issueErr != nil || expiryErr != nil || issuedAt.Nanosecond() != 0 || expiresAt.Nanosecond() != 0 ||
		snapshot.MaximumAgeSeconds < r.Config.RefreshIntervalSeconds+30 ||
		expiresAt.Sub(issuedAt) < minimumLifetime || expiresAt.Sub(responseTime) < minimumLifetime ||
		issuedAt.After(responseTime.Add(30*time.Second)) || issuedAt.Before(responseTime.Add(-30*time.Second)) {
		return nil, errors.New("snapshot response cannot cover the enrolled refresh cadence and safety margin")
	}
	return envelopeRaw, nil
}

func (r *Runner) installSnapshot(envelopeRaw []byte, now time.Time) error {
	snapshotTrustRaw, err := readRootRegular(r.snapshotTrustPath, maximumConfigBytes)
	if err != nil {
		return err
	}
	if err := requireRootOwnedDirectory(r.Config.RuntimeRoot); err != nil {
		return err
	}
	if err := appendOrMatch(filepath.Join(r.Config.RuntimeRoot, "snapshot-trust.json"), snapshotTrustRaw, 0o444); err != nil {
		return err
	}
	digestValue := digest(envelopeRaw)
	if err := r.appendEvidence("snapshot-envelope-"+digestValue+".json", envelopeRaw); err != nil {
		return err
	}
	candidatePath := filepath.Join(r.Config.RuntimeRoot, "snapshot-envelope.candidate-"+digestValue+".json")
	if err := appendOrMatch(candidatePath, envelopeRaw, 0o444); err != nil {
		return err
	}
	if _, err := boundary.LoadRuntime(
		filepath.Join(r.Config.RuntimeRoot, "snapshot-trust.json"),
		candidatePath,
		r.Acceptance.SnapshotTrustSHA256,
		r.Acceptance.ClusterID,
		r.Acceptance.DeploymentID,
		r.Acceptance.AuthoritySnapshotID,
		r.Acceptance.AuthorityClosureSHA256,
		now,
	); err != nil {
		return fmt.Errorf("verify candidate snapshot before activation: %w", err)
	}
	activePath := filepath.Join(r.Config.RuntimeRoot, "snapshot-envelope.json")
	if activeRaw, readErr := readRootRegular(activePath, maximumEnvelopeBytes); readErr == nil {
		previousCandidate := filepath.Join(r.Config.RuntimeRoot, "snapshot-previous.candidate-"+digest(activeRaw)+".json")
		if err := appendOrMatch(previousCandidate, activeRaw, 0o444); err != nil {
			return err
		}
		if err := os.Rename(previousCandidate, filepath.Join(r.Config.RuntimeRoot, "snapshot-previous.json")); err != nil {
			return err
		}
		if err := fsyncDirectory(r.Config.RuntimeRoot); err != nil {
			return err
		}
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return errors.New("existing active snapshot cannot be preserved as a verified previous slot")
	}
	if err := os.Rename(candidatePath, activePath); err != nil {
		return err
	}
	return fsyncDirectory(r.Config.RuntimeRoot)
}

func validateConfig(config Config, acceptance boundary.Acceptance) error {
	if config.Schema != ConfigSchema || config.ClusterID != acceptance.ClusterID || config.DeploymentID != acceptance.DeploymentID ||
		config.MaximumBundleBytes < 1024 || config.MaximumBundleBytes > 16*1024*1024 || len(config.Sources) == 0 ||
		!isDigest(config.EvidenceStoreID) || config.RefreshIntervalSeconds < 240 || config.RefreshIntervalSeconds > 270 ||
		config.CollectionDeadlineSeconds < 30 || config.CollectionDeadlineSeconds >= config.RefreshIntervalSeconds ||
		config.MaximumConcurrentSources < 1 || config.MaximumConcurrentSources > 8 ||
		config.EvidenceDeviceID == 0 || config.EvidenceOperatingHorizonDays < 365 || config.EvidenceOperatingHorizonDays > 3660 ||
		config.EvidenceCapacityBytes == 0 || config.EvidenceMinimumFreeBytes < 64*1024*1024 ||
		config.EvidenceCapacityInodes == 0 || config.EvidenceMinimumFreeInodes < 1024 ||
		!sort.StringsAreSorted(config.MandatoryCollections) || digestJSON(config.MandatoryCollections) != acceptance.MandatoryCollectionsSHA256 ||
		!isDigest(config.SnapshotAuthorityCASHA256) || !isDigest(config.SnapshotClientCertificateSHA256) ||
		!isDigest(config.SnapshotClientKeySHA256) || !isDigest(config.SnapshotClientSPKISHA256) || config.SnapshotClientSPIFFEURI == "" ||
		config.SnapshotCredentialLaneID == "" || strings.ContainsAny(config.SnapshotCredentialLaneID, "\x00\r\n") {
		return errors.New("native collector config is incomplete or outside its bounds")
	}
	if err := requireHTTPS(config.SnapshotAuthorityURL, config.SnapshotAuthorityName); err != nil {
		return err
	}
	requiredCollections := mandatorySemanticCollections()
	collections := []string{}
	ids := map[string]struct{}{}
	type laneBinding struct {
		caSHA256 string
		certificateSHA256 string
		keySHA256 string
		spkiSHA256 string
		spiffeURI string
		serverName string
	}
	snapshotBinding := laneBinding{
		caSHA256: config.SnapshotAuthorityCASHA256,
		certificateSHA256: config.SnapshotClientCertificateSHA256,
		keySHA256: config.SnapshotClientKeySHA256,
		spkiSHA256: config.SnapshotClientSPKISHA256,
		spiffeURI: config.SnapshotClientSPIFFEURI,
		serverName: config.SnapshotAuthorityName,
	}
	credentialLanes := map[string]laneBinding{config.SnapshotCredentialLaneID: snapshotBinding}
	credentialDigests := map[string]string{config.SnapshotClientKeySHA256 + ":" + config.SnapshotClientSPKISHA256: config.SnapshotCredentialLaneID}
	for _, source := range config.Sources {
		if source.ID == "" || source.Kind == "" || source.SemanticCollection == "" || source.MaximumPages < 1 || source.MaximumPages > 1024 ||
			source.MaximumPageBytes < 1024 || source.MaximumPageBytes > 32*1024*1024 ||
			source.MaximumTotalBytes < source.MaximumPageBytes || source.MaximumTotalBytes > 128*1024*1024 ||
			(source.Pagination != "none" && source.Pagination != "kubernetes-continue" && source.Pagination != "provider-page-token") ||
			!isDigest(source.AttestationCABundleSHA256) || !isDigest(source.AttestationClientCertificateSHA256) ||
			!isDigest(source.AttestationClientKeySHA256) || !isDigest(source.AttestationClientSPKISHA256) || source.AttestationClientSPIFFEURI == "" ||
			source.AttestationCredentialLaneID == "" || strings.ContainsAny(source.AttestationCredentialLaneID, "\x00\r\n") {
			return errors.New("native source is incomplete or outside its bounds")
		}
		if source.Kind == "ca-revocation" && source.Pagination != "none" {
			return errors.New("native CRL collection must be exact and non-paginated")
		}
		if _, exists := ids[source.ID]; exists {
			return errors.New("native collector config repeats a source ID")
		}
		ids[source.ID] = struct{}{}
		binding := laneBinding{
			caSHA256: source.AttestationCABundleSHA256,
			certificateSHA256: source.AttestationClientCertificateSHA256,
			keySHA256: source.AttestationClientKeySHA256,
			spkiSHA256: source.AttestationClientSPKISHA256,
			spiffeURI: source.AttestationClientSPIFFEURI,
			serverName: source.AttestationServerName,
		}
		if enrolled, exists := credentialLanes[source.AttestationCredentialLaneID]; exists && enrolled != binding {
			return errors.New("collector credential lane contains inconsistent trust or client identity bytes")
		}
		credentialLanes[source.AttestationCredentialLaneID] = binding
		fingerprint := source.AttestationClientKeySHA256 + ":" + source.AttestationClientSPKISHA256
		if priorLane, reused := credentialDigests[fingerprint]; reused && priorLane != source.AttestationCredentialLaneID {
			return errors.New("collector credentials are reused across separately accepted trust lanes")
		}
		credentialDigests[fingerprint] = source.AttestationCredentialLaneID
		if err := requireHTTPS(source.InitialURL, source.ServerName); err != nil {
			return err
		}
		if err := requireHTTPS(source.AttestationURL, source.AttestationServerName); err != nil {
			return err
		}
		if source.ProviderRequestIDHeader == "" || strings.ContainsAny(source.ProviderRequestIDHeader, "\x00\r\n") {
			return errors.New("native source omits its provider request-ID header")
		}
		collections = append(collections, source.SemanticCollection)
	}
	sort.Strings(collections)
	if !containsAllStrings(collections, requiredCollections) || !equalStringLists(collections, config.MandatoryCollections) {
		return errors.New("native collector omits or duplicates a mandatory semantic collection")
	}
	manifestBound, err := maximumEvidenceManifestBytes(config)
	if err != nil || manifestBound > uint64(config.MaximumBundleBytes) {
		return errors.New("native collector manifest bound cannot contain every enrolled content-addressed page reference")
	}
	dailyBytes, dailyInodes, err := derivedEvidenceDailyBounds(config)
	if err != nil || dailyBytes > (^uint64(0)-config.EvidenceMinimumFreeBytes)/uint64(config.EvidenceOperatingHorizonDays) ||
		dailyInodes > (^uint64(0)-config.EvidenceMinimumFreeInodes)/uint64(config.EvidenceOperatingHorizonDays) ||
		config.EvidenceCapacityBytes < dailyBytes*uint64(config.EvidenceOperatingHorizonDays)+config.EvidenceMinimumFreeBytes ||
		config.EvidenceCapacityInodes < dailyInodes*uint64(config.EvidenceOperatingHorizonDays)+config.EvidenceMinimumFreeInodes {
		return errors.New("native evidence store cannot retain its source-derived byte and inode operating horizon")
	}
	return nil
}

func derivedEvidenceDailyBounds(config Config) (uint64, uint64, error) {
	cycles := uint64((86400 + config.RefreshIntervalSeconds - 1) / config.RefreshIntervalSeconds)
	perCycleBytes := uint64(config.MaximumBundleBytes + maximumEnvelopeBytes)
	perCycleInodes := uint64(3)
	for _, source := range config.Sources {
		total := uint64(source.MaximumTotalBytes)
		pages := uint64(source.MaximumPages)
		if total > (^uint64(0)-pages*1024*1024)/3 || perCycleBytes > ^uint64(0)-(total*3+pages*1024*1024) {
			return 0, 0, errors.New("native evidence byte horizon overflows")
		}
		perCycleBytes += total*3 + pages*1024*1024
		if perCycleInodes > ^uint64(0)-pages {
			return 0, 0, errors.New("native evidence inode horizon overflows")
		}
		perCycleInodes += pages
	}
	if perCycleBytes > ^uint64(0)/cycles || perCycleInodes > ^uint64(0)/cycles {
		return 0, 0, errors.New("native evidence daily horizon overflows")
	}
	return perCycleBytes * cycles, perCycleInodes * cycles, nil
}

func maximumEvidenceManifestBytes(config Config) (uint64, error) {
	bound := uint64(4096)
	for _, source := range config.Sources {
		pageBytes := uint64(source.MaximumPages) * maximumEvidenceReferenceBytes
		if uint64(source.MaximumPages) != 0 && pageBytes/uint64(source.MaximumPages) != maximumEvidenceReferenceBytes {
			return 0, errors.New("native evidence reference bound overflows")
		}
		if bound > ^uint64(0)-pageBytes-1024 {
			return 0, errors.New("native evidence manifest bound overflows")
		}
		bound += pageBytes + 1024
	}
	return bound, nil
}

func mandatorySemanticCollections() []string {
	return []string{
		"apiserver-admission-mutating-policies",
		"apiserver-admission-mutating-policy-bindings",
		"apiserver-admission-validating-policies",
		"apiserver-admission-validating-policy-bindings",
		"apiserver-admission-mutating-webhooks",
		"apiserver-admission-validating-webhooks",
		"apiserver-approval-objects",
		"apiserver-authentication-configuration",
		"apiserver-authorization-configuration",
		"apiserver-clusterrolebindings",
		"apiserver-clusterroles",
		"apiserver-crds",
		"apiserver-csrs",
		"apiserver-daemonsets",
		"apiserver-deployments",
		"apiserver-jobs",
		"apiserver-cronjobs",
		"apiserver-csidrivers",
		"apiserver-csinodes",
		"apiserver-nodes",
		"apiserver-namespaces",
		"apiserver-persistentvolumeclaims",
		"apiserver-persistentvolumes",
		"apiserver-pods",
		"apiserver-replicationcontrollers",
		"apiserver-replicasets",
		"apiserver-rolebindings",
		"apiserver-roles",
		"apiserver-secrets-metadata",
		"apiserver-serviceaccounts",
		"apiserver-statefulsets",
		"apiserver-storageclasses",
		"apiserver-volumeattachments",
		"ca-issued-credentials",
		"ca-revocation-status",
		"provider-iam-bindings",
		"provider-iam-policies",
		"provider-nodegroup-membership",
		"secret-kms-classification",
	}
}

func containsAllStrings(values []string, required []string) bool {
	set := map[string]struct{}{}
	for _, value := range values {
		if value == "" {
			return false
		}
		if _, duplicate := set[value]; duplicate {
			return false
		}
		set[value] = struct{}{}
	}
	for _, value := range required {
		if _, exists := set[value]; !exists {
			return false
		}
	}
	return true
}

func digestJSON(value any) string {
	raw, _ := json.Marshal(value)
	return digest(raw)
}

func validatePage(page NativePage, source SourceSpec, directive CollectionRequest, providerRequest NativeRequest, requestRaw []byte, requestedURL string, responseRaw []byte, now time.Time) error {
	collectedAt, err := time.Parse(time.RFC3339, page.CollectedAt)
	if err != nil || collectedAt.Nanosecond() != 0 || !strings.HasSuffix(page.CollectedAt, "Z") ||
		collectedAt.Before(now.Add(-2*time.Minute)) || collectedAt.After(now.Add(30*time.Second)) {
		return errors.New("native response timestamp is absent or stale")
	}
	expectedContentType := "application/json"
	if source.Kind == "ca-revocation" {
		expectedContentType = "application/pkix-crl"
	}
	if page.Schema != NativePageSchema || page.SourceID != source.ID || page.SourceKind != source.Kind ||
		page.SemanticCollection != source.SemanticCollection || page.RequestID != providerRequest.RequestID ||
		page.ProviderRequestID == "" || strings.ContainsAny(page.ProviderRequestID, "\x00\r\n") || page.SessionID != directive.SessionID ||
		page.Challenge != directive.Challenge || page.PageIndex != directive.PageIndex ||
		page.RequestSHA256 != digest(requestRaw) || page.RequestBase64 != base64.StdEncoding.EncodeToString(requestRaw) || page.RequestedURL != requestedURL ||
		!isDigest(page.TLSPeerCertificateSHA256) || page.HTTPStatus != http.StatusOK || page.ResponseContentType != expectedContentType ||
		page.RawResponseSHA256 != digest(responseRaw) || page.RawResponseBase64 != base64.StdEncoding.EncodeToString(responseRaw) ||
		page.Complete == (page.NextURL != "") || page.Complete == (page.NextToken != "") {
		return errors.New("signed native response does not match its exact request, TLS peer, page or terminal state")
	}
	if providerRequest.Schema != NativeRequestSchema || providerRequest.SourceID != source.ID || !canonicalNonce(providerRequest.RequestID) ||
		providerRequest.Method != http.MethodGet || providerRequest.URL != requestedURL || providerRequest.BodySHA256 != digest(nil) ||
		len(providerRequest.Headers) != 2 || providerRequest.Headers["accept"] != expectedContentType ||
		providerRequest.Headers["x-fs2-request-id"] != providerRequest.RequestID {
		return errors.New("signed native provider request projection is incomplete or inconsistent")
	}
	if source.Pagination == "none" && (!page.Complete || page.NextToken != "" || page.NextURL != "") {
		return errors.New("non-paginated native response is not terminal")
	}
	return nil
}

func derivePagination(source SourceSpec, currentURL string, raw []byte) (bool, string, string, error) {
	if source.Pagination == "none" {
		return true, "", "", nil
	}
	var response map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&response); err != nil {
		return false, "", "", err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return false, "", "", errors.New("native response contains trailing JSON")
	}
	if _, ok := response["items"].([]any); !ok {
		return false, "", "", errors.New("paginated native response omits its items array")
	}
	nextToken := ""
	parameter := ""
	switch source.Pagination {
	case "kubernetes-continue":
		metadata, ok := response["metadata"].(map[string]any)
		if !ok {
			return false, "", "", errors.New("Kubernetes response omits metadata")
		}
		nextToken, _ = metadata["continue"].(string)
		parameter = "continue"
		if nextToken == "" {
			if remaining, exists := metadata["remainingItemCount"]; exists && fmt.Sprint(remaining) != "0" {
				return false, "", "", errors.New("terminal Kubernetes page reports remaining items")
			}
		}
	case "provider-page-token":
		nextToken, _ = response["next_page_token"].(string)
		parameter = "page_token"
		if nextToken == "" {
			if remaining, exists := response["remaining_item_count"]; exists && fmt.Sprint(remaining) != "0" {
				return false, "", "", errors.New("terminal provider page reports remaining items")
			}
		}
	}
	if nextToken == "" {
		return true, "", "", nil
	}
	if strings.ContainsAny(nextToken, "\x00\r\n") || len(nextToken) > 4096 {
		return false, "", "", errors.New("native pagination token is malformed")
	}
	parsed, err := url.Parse(currentURL)
	if err != nil {
		return false, "", "", err
	}
	query := parsed.Query()
	query.Del("continue")
	query.Del("page_token")
	query.Set(parameter, nextToken)
	parsed.RawQuery = query.Encode()
	return false, nextToken, parsed.String(), nil
}

func sourceClient(source SourceSpec) (*http.Client, error) {
	return mutualTLSClient(
		source.AttestationCABundlePath,
		source.AttestationCABundleSHA256,
		source.AttestationServerName,
		source.AttestationClientCertificate,
		source.AttestationClientCertificateSHA256,
		source.AttestationClientKey,
		source.AttestationClientKeySHA256,
		source.AttestationClientSPKISHA256,
		source.AttestationClientSPIFFEURI,
	)
}

func mutualTLSClient(
	caPath string,
	expectedCASHA256 string,
	serverName string,
	certificatePath string,
	expectedCertificateSHA256 string,
	keyPath string,
	expectedKeySHA256 string,
	expectedSPKISHA256 string,
	expectedSPIFFEURI string,
) (*http.Client, error) {
	if !isDigest(expectedCASHA256) || !isDigest(expectedCertificateSHA256) || !isDigest(expectedKeySHA256) || !isDigest(expectedSPKISHA256) ||
		expectedSPIFFEURI == "" || strings.ContainsAny(expectedSPIFFEURI, "\x00\r\n") {
		return nil, errors.New("mTLS client identity contract contains an invalid digest or SPIFFE identity")
	}
	caRaw, err := readRootRegular(caPath, 4*1024*1024)
	if err != nil {
		return nil, err
	}
	if digest(caRaw) != expectedCASHA256 {
		return nil, errors.New("mTLS server CA differs from accepted bytes")
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caRaw) {
		return nil, errors.New("authority CA bundle contains no parseable certificate")
	}
	certificateRaw, err := readRootRegular(certificatePath, 4*1024*1024)
	if err != nil {
		return nil, err
	}
	if digest(certificateRaw) != expectedCertificateSHA256 {
		return nil, errors.New("mTLS client certificate differs from accepted bytes")
	}
	keyRaw, err := readRootPrivateKey(keyPath, 4*1024*1024)
	if err != nil {
		return nil, err
	}
	if digest(keyRaw) != expectedKeySHA256 {
		return nil, errors.New("mTLS client private key differs from accepted bytes")
	}
	certificate, err := tls.X509KeyPair(certificateRaw, keyRaw)
	if err != nil || len(certificate.Certificate) < 1 {
		return nil, errors.New("mTLS client certificate and private key do not form an exact pair")
	}
	leaf, err := x509.ParseCertificate(certificate.Certificate[0])
	if err != nil || digest(leaf.RawSubjectPublicKeyInfo) != expectedSPKISHA256 || len(leaf.URIs) != 1 ||
		leaf.URIs[0].String() != expectedSPIFFEURI || !certificateHasUsage(leaf, x509.ExtKeyUsageClientAuth) {
		return nil, errors.New("mTLS client certificate identity or clientAuth purpose differs from acceptance")
	}
	now := time.Now().UTC()
	if now.Before(leaf.NotBefore) || !now.Before(leaf.NotAfter) {
		return nil, errors.New("mTLS client certificate is not current")
	}
	certificate.Leaf = leaf
	configuration := &tls.Config{
		MinVersion:   tls.VersionTLS13,
		RootCAs:      roots,
		ServerName:   serverName,
		Certificates: []tls.Certificate{certificate},
	}
	transport := &http.Transport{
		Proxy:                 nil,
		TLSClientConfig:       configuration,
		DisableCompression:    true,
		DisableKeepAlives:     false,
		MaxIdleConns:          4,
		MaxIdleConnsPerHost:   1,
		MaxConnsPerHost:       1,
		IdleConnTimeout:       30 * time.Second,
		TLSHandshakeTimeout:   5 * time.Second,
		ResponseHeaderTimeout: 10 * time.Second,
	}
	return &http.Client{
		Transport: transport,
		Timeout:   30 * time.Second,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}, nil
}

func certificateHasUsage(certificate *x509.Certificate, expected x509.ExtKeyUsage) bool {
	for _, usage := range certificate.ExtKeyUsage {
		if usage == expected {
			return true
		}
	}
	return false
}

func normalizedContentType(value string) string {
	value = strings.TrimSpace(strings.SplitN(value, ";", 2)[0])
	if value == "application/json" || value == "application/pkix-crl" || value == "application/ocsp-response" {
		return value
	}
	return ""
}

func requireHTTPS(rawURL string, serverName string) error {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil || parsed.Fragment != "" || parsed.Hostname() != serverName {
		return errors.New("authority endpoint is not exact HTTPS with its enrolled server name")
	}
	return nil
}

func sameAuthorityURL(initial string, next string) error {
	initialURL, initialErr := url.Parse(initial)
	nextURL, nextErr := url.Parse(next)
	if initialErr != nil || nextErr != nil || nextURL.Scheme != "https" || nextURL.User != nil || nextURL.Fragment != "" ||
		initialURL.Scheme != nextURL.Scheme || initialURL.Host != nextURL.Host || nextURL.String() != next {
		return errors.New("signed pagination URL escapes its enrolled authority")
	}
	return nil
}

func collectorPageURL(initial string, pagination string, token string) (string, error) {
	parsed, err := url.Parse(initial)
	if err != nil {
		return "", err
	}
	query := parsed.Query()
	query.Del("continue")
	query.Del("page_token")
	if token != "" {
		if pagination == "kubernetes-continue" {
			query.Set("continue", token)
		} else if pagination == "provider-page-token" {
			query.Set("page_token", token)
		} else {
			return "", errors.New("non-paginated collection contains a token")
		}
	}
	parsed.RawQuery = query.Encode()
	return parsed.String(), nil
}

func canonicalJSON(raw []byte, destination any) error {
	canonicalRaw := raw
	if bytes.HasSuffix(canonicalRaw, []byte{'\n'}) {
		canonicalRaw = canonicalRaw[:len(canonicalRaw)-1]
	}
	if len(canonicalRaw) == 0 || bytes.HasSuffix(canonicalRaw, []byte{'\n'}) || bytes.HasSuffix(canonicalRaw, []byte{'\r'}) {
		return errors.New("JSON has non-canonical trailing whitespace")
	}
	decoder := json.NewDecoder(bytes.NewReader(canonicalRaw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return errors.New("JSON has trailing content")
	}
	canonical, err := json.Marshal(destination)
	if err != nil || !bytes.Equal(canonical, canonicalRaw) {
		return errors.New("JSON is not canonical")
	}
	return nil
}

func readRootRegular(path string, maximum int64) ([]byte, error) {
	return readRootRegularWithMode(path, maximum, false)
}

func readRootPrivateKey(path string, maximum int64) ([]byte, error) {
	return readRootRegularWithMode(path, maximum, true)
}

func readRootRegularWithMode(path string, maximum int64, privateKey bool) ([]byte, error) {
	if err := requireRootOwnedDirectory(filepath.Dir(path)); err != nil {
		return nil, err
	}
	before, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	stat, ok := before.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 || !before.Mode().IsRegular() || before.Mode().Perm()&0o022 != 0 || before.Size() < 1 || before.Size() > maximum {
		return nil, errors.New("collector input is not a bounded root-owned non-writable regular file")
	}
	if privateKey && before.Mode().Perm() != 0o400 && before.Mode().Perm() != 0o600 {
		return nil, errors.New("private key is not root-only mode 0400 or 0600")
	}
	fileDescriptor, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fileDescriptor), path)
	defer file.Close()
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	after, statErr := file.Stat()
	if err != nil || statErr != nil || int64(len(raw)) > maximum || !os.SameFile(before, after) || int64(len(raw)) != after.Size() {
		return nil, errors.New("collector input changed during its bounded read")
	}
	return raw, nil
}

func (r *Runner) appendEvidence(name string, raw []byte) error {
	r.evidenceMu.Lock()
	defer r.evidenceMu.Unlock()
	if err := requireRootOwnedDirectory(r.Config.EvidenceRoot); err != nil {
		return err
	}
	if filepath.Base(name) != name || name == "." || name == "" {
		return errors.New("append-only evidence name is not a single safe path component")
	}
	if err := validateEvidenceCapacity(r.Config, uint64(len(raw)), 1); err != nil {
		return err
	}
	return appendOrMatch(filepath.Join(r.Config.EvidenceRoot, name), raw, 0o400)
}

func validateEvidenceCapacity(config Config, upcomingBytes uint64, upcomingInodes uint64) error {
	rootInfo, err := os.Lstat(config.EvidenceRoot)
	if err != nil {
		return err
	}
	parentInfo, err := os.Lstat(filepath.Dir(config.EvidenceRoot))
	if err != nil {
		return err
	}
	rootStat, rootOK := rootInfo.Sys().(*syscall.Stat_t)
	parentStat, parentOK := parentInfo.Sys().(*syscall.Stat_t)
	if !rootOK || !parentOK || rootStat.Dev != config.EvidenceDeviceID || rootStat.Dev == parentStat.Dev {
		return errors.New("native evidence root is not its independently accepted dedicated filesystem")
	}
	var status syscall.Statfs_t
	if err := syscall.Statfs(config.EvidenceRoot, &status); err != nil {
		return err
	}
	blockSize := uint64(status.Bsize)
	if status.Blocks > ^uint64(0)/blockSize || status.Bavail > ^uint64(0)/blockSize ||
		upcomingBytes > ^uint64(0)-config.EvidenceMinimumFreeBytes || upcomingInodes > ^uint64(0)-config.EvidenceMinimumFreeInodes {
		return errors.New("native evidence filesystem capacity cannot be represented safely")
	}
	totalBytes := status.Blocks * blockSize
	availableBytes := status.Bavail * blockSize
	if totalBytes < config.EvidenceCapacityBytes || uint64(status.Files) < config.EvidenceCapacityInodes ||
		availableBytes < config.EvidenceMinimumFreeBytes+upcomingBytes || uint64(status.Ffree) < config.EvidenceMinimumFreeInodes+upcomingInodes {
		return errors.New("native evidence store cannot preserve its accepted byte and inode reserve")
	}
	return nil
}

func appendOrMatch(path string, raw []byte, mode os.FileMode) error {
	parent := filepath.Dir(path)
	if err := requireRootOwnedDirectory(parent); err != nil {
		return err
	}
	if _, err := os.Lstat(path); err == nil {
		existing, readErr := readRootRegular(path, int64(len(raw)))
		if readErr != nil || !bytes.Equal(existing, raw) {
			return errors.New("append-only evidence path already contains different bytes")
		}
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := ensureRootOwnedChildDirectory(parent, ".attempts"); err != nil {
		return err
	}
	attemptPath := filepath.Join(parent, ".attempts", filepath.Base(path)+"-"+digest(raw))
	fileDescriptor, err := syscall.Open(attemptPath, syscall.O_RDWR|syscall.O_CREAT|syscall.O_EXCL|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, uint32(mode.Perm()))
	if errors.Is(err, syscall.EEXIST) {
		fileDescriptor, err = syscall.Open(attemptPath, syscall.O_RDWR|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	}
	if err != nil {
		return err
	}
	file := os.NewFile(uintptr(fileDescriptor), attemptPath)
	info, statErr := file.Stat()
	if statErr != nil || info == nil {
		_ = file.Close()
		return errors.New("append-only evidence attempt cannot be inspected")
	}
	stat, statOK := info.Sys().(*syscall.Stat_t)
	if !statOK || stat.Uid != 0 || !info.Mode().IsRegular() || info.Mode().Perm() != mode.Perm() || info.Size() < 0 || info.Size() > int64(len(raw)) {
		_ = file.Close()
		return errors.New("append-only evidence attempt is not the exact resumable root-owned file")
	}
	written := int(info.Size())
	if written > 0 {
		prefix := make([]byte, written)
		count, readErr := file.ReadAt(prefix, 0)
		if readErr != nil || count != written || !bytes.Equal(prefix, raw[:written]) {
			_ = file.Close()
			return errors.New("append-only evidence attempt prefix differs from intended bytes")
		}
	}
	if _, err := file.Seek(int64(written), io.SeekStart); err != nil {
		_ = file.Close()
		return err
	}
	for written < len(raw) {
		count, writeErr := file.Write(raw[written:])
		if writeErr != nil || count < 1 {
			_ = file.Close()
			if writeErr != nil {
				return writeErr
			}
			return errors.New("append-only evidence attempt made no write progress")
		}
		written += count
	}
	if written != len(raw) {
		_ = file.Close()
		return errors.New("append-only evidence attempt was short-written")
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := fsyncDirectory(parent); err != nil {
		return err
	}
	if err := os.Link(attemptPath, path); err != nil {
		if !errors.Is(err, os.ErrExist) {
			return err
		}
		existing, readErr := readRootRegular(path, int64(len(raw)))
		if readErr != nil || !bytes.Equal(existing, raw) {
			return errors.New("append-only evidence publication raced with different bytes")
		}
		return nil
	}
	return fsyncDirectory(parent)
}

func fsyncDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func randomID() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}

func digest(raw []byte) string {
	hash := sha256.Sum256(raw)
	return hex.EncodeToString(hash[:])
}

func isDigest(value string) bool {
	if len(value) != 64 || value == strings.Repeat("0", 64) {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && hex.EncodeToString(decoded) == value
}

func equalStringLists(left []string, right []string) bool {
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
