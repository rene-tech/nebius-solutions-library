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
	// Must remain identical to boundary.maxSnapshotBytes: the collector must
	// never durably select bytes that the non-root admission runtime cannot open.
	maximumEnvelopeBytes = boundary.MaxSnapshotBytes
	maximumEvidenceReferenceBytes = 1024
	maximumCollectorCycleBytes = 256 * 1024
	maximumSnapshotActivationReceiptBytes = 4 * 1024
	maximumSnapshotRuntimeSelectionBytes = 16 * 1024
	snapshotCycleSettlementSchema = "fs2-serve.nebius.ai/public-edge-snapshot-cycle-settlement/v2"
	snapshotActivationReceiptSchema = "fs2-serve.nebius.ai/public-edge-snapshot-activation-receipt/v1"
	legacySnapshotRuntimeSelectionSchema = "fs2-serve.nebius.ai/public-edge-snapshot-runtime-selection/v1"
	snapshotRuntimeSelectionSchema = "fs2-serve.nebius.ai/public-edge-snapshot-runtime-selection/v2"
	snapshotSelectionCommitSchema = "fs2-serve.nebius.ai/public-edge-snapshot-selection-commit/v1"
	snapshotCyclePredecessorSchema = "fs2-serve.nebius.ai/public-edge-snapshot-cycle-predecessor/v1"
	snapshotActivationSafetyMargin = 5 * time.Second
)

type snapshotCycleSettlement struct {
	Schema string `json:"schema"`
	CycleContractSHA256 string `json:"cycle_contract_sha256"`
	CycleID string `json:"cycle_id"`
	CycleIssuedAt string `json:"cycle_issued_at"`
	CycleDeadlineAt string `json:"cycle_deadline_at"`
	EvidenceBundleSHA256 string `json:"evidence_bundle_sha256"`
	SnapshotEnvelopeSHA256 string `json:"snapshot_envelope_sha256"`
}

type snapshotCyclePredecessor struct {
	Schema string `json:"schema"`
	CycleContractSHA256 string `json:"cycle_contract_sha256"`
	CycleID string `json:"cycle_id"`
	PredecessorSelectionSHA256 string `json:"predecessor_selection_sha256"`
}

type snapshotActivationReceipt struct {
	Schema                 string `json:"schema"`
	ClusterID              string `json:"cluster_id"`
	DeploymentID           string `json:"deployment_id"`
	CycleContractSHA256    string `json:"cycle_contract_sha256"`
	CycleID                string `json:"cycle_id"`
	CycleIssuedAt          string `json:"cycle_issued_at"`
	CycleDeadlineAt        string `json:"cycle_deadline_at"`
	EvidenceBundleSHA256   string `json:"evidence_bundle_sha256"`
	SnapshotEnvelopeSHA256 string `json:"snapshot_envelope_sha256"`
	PredecessorSelectionSHA256 string `json:"predecessor_selection_sha256"`
	ActivatedAt            string `json:"activated_at"`
	Status                 string `json:"status"`
}

type snapshotRuntimeSelection struct {
	Schema                 string                    `json:"schema"`
	SnapshotEnvelopeName   string                    `json:"snapshot_envelope_name"`
	SnapshotEnvelopeSHA256 string                    `json:"snapshot_envelope_sha256"`
	SnapshotTrustName      string                    `json:"snapshot_trust_name"`
	SnapshotTrustSHA256    string                    `json:"snapshot_trust_sha256"`
	AcceptanceTrustName    string                    `json:"acceptance_trust_name"`
	AcceptanceTrustSHA256  string                    `json:"acceptance_trust_sha256"`
	AcceptanceEnvelopeName string                    `json:"acceptance_envelope_name"`
	AcceptanceEnvelopeSHA256 string                  `json:"acceptance_envelope_sha256"`
	AuthoritySnapshotID    string                    `json:"authority_snapshot_id"`
	AuthorityClosureSHA256 string                    `json:"authority_closure_sha256"`
	PredecessorSelectionSHA256 string                `json:"predecessor_selection_sha256"`
	Activation             snapshotActivationReceipt `json:"activation"`
}

type legacySnapshotRuntimeSelection struct {
	Schema                 string                    `json:"schema"`
	SnapshotEnvelopeName   string                    `json:"snapshot_envelope_name"`
	SnapshotEnvelopeSHA256 string                    `json:"snapshot_envelope_sha256"`
	PredecessorSelectionSHA256 string                `json:"predecessor_selection_sha256"`
	Activation             snapshotActivationReceipt `json:"activation"`
}

type snapshotSelectionCommit struct {
	Schema                    string `json:"schema"`
	ClusterID                 string `json:"cluster_id"`
	DeploymentID              string `json:"deployment_id"`
	PredecessorSelectionSHA256 string `json:"predecessor_selection_sha256"`
	SuccessorSelectionSHA256   string `json:"successor_selection_sha256"`
	CycleID                    string `json:"cycle_id"`
	CommittedAt                string `json:"committed_at"`
	Status                     string `json:"status"`
}

type Runner struct {
	Config       Config
	Acceptance   boundary.Acceptance
	nativeTrust       *boundary.ExternalTrust
	snapshotTrust     *boundary.ExternalTrust
	legacyBootstrap  *boundary.LegacyRuntimeBootstrap
	evidenceMu        sync.Mutex
	runtimeMu         sync.Mutex
}

type snapshotActivationPlan struct {
	selectionPath     string
	envelopePath      string
	alreadyActive     bool
	cycle             CollectorCycle
	bundleSHA256      string
	envelopeSHA256    string
	predecessorSelectionSHA256 string
	snapshotTrustName string
	snapshotTrustSHA256 string
	acceptanceTrustName string
	acceptanceTrustSHA256 string
	acceptanceEnvelopeName string
	acceptanceEnvelopeSHA256 string
	legacyBootstrapEnvelopeSHA256 string
	legacyBootstrapExpiresAt string
}

func (r *Runner) AcceptedCycle(now time.Time) (CollectorCycle, error) {
	return collectorCycleFor(
		r.Acceptance,
		r.Config.RefreshIntervalSeconds,
		r.Config.CollectionDeadlineSeconds,
		collectorSourceIDs(r.Config.Sources),
		now.UTC().Truncate(time.Second),
	)
}

func LoadRunner(
	configPath string,
	nativeTrustPath string,
	snapshotTrustPath string,
	acceptance boundary.Acceptance,
	legacyBootstrap *boundary.LegacyRuntimeBootstrap,
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
	var configDiscriminator struct {
		Schema string `json:"schema"`
	}
	if err := json.Unmarshal(configRaw, &configDiscriminator); err == nil && configDiscriminator.Schema == LegacyConfigSchema {
		return nil, errors.New("legacy native-collector config v1 is retained evidence but cannot be reinterpreted as the cadence/evidence v2 contract")
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
	if err := validateRuntimeCapacity(config, 0, 0); err != nil {
		return nil, fmt.Errorf("validate native runtime capacity horizon: %w", err)
	}
	nativeTrust, err := boundary.LoadExternalTrust(nativeTrustPath, acceptance.NativeResponseTrustSHA256, NativeTrustSchema)
	if err != nil {
		return nil, fmt.Errorf("load immutable native response trust: %w", err)
	}
	snapshotTrust, err := boundary.LoadExternalTrust(snapshotTrustPath, acceptance.SnapshotTrustSHA256, boundary.TrustSchema)
	if err != nil {
		return nil, fmt.Errorf("load immutable snapshot response trust: %w", err)
	}
	return &Runner{
		Config:        config,
		Acceptance:    acceptance,
		nativeTrust:   nativeTrust,
		snapshotTrust: snapshotTrust,
		legacyBootstrap: legacyBootstrap,
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
	cycle, err := r.AcceptedCycle(now)
	if err != nil {
		return err
	}
	deadlineAt, _ := time.Parse(time.RFC3339, cycle.DeadlineAt)
	if !now.Before(deadlineAt) {
		return errors.New("collector cycle deadline has elapsed; wait for the next accepted cadence window")
	}
	cycleRaw, err := json.Marshal(cycle)
	if err != nil || len(cycleRaw) > maximumCollectorCycleBytes {
		return errors.New("collector cycle exceeds its canonical retained bound")
	}
	if err := r.appendEvidence("collection-cycle-"+cycle.CycleID+".json", cycleRaw); err != nil {
		return err
	}
	r.runtimeMu.Lock()
	_, _, observedPredecessorSHA256, _, predecessorErr := r.loadRuntimeSelection(now)
	r.runtimeMu.Unlock()
	if predecessorErr != nil {
		return fmt.Errorf("load exact runtime predecessor before evidence collection: %w", predecessorErr)
	}
	predecessorSelectionSHA256, err := r.settleCyclePredecessor(cycle, observedPredecessorSHA256)
	if err != nil {
		return err
	}
	bundle := EvidenceBundle{
		Schema:       EvidenceBundleSchema,
		ClusterID:    r.Config.ClusterID,
		DeploymentID: r.Config.DeploymentID,
		EvidenceStoreID: r.Config.EvidenceStoreID,
		ActivationPredecessorSelectionSHA256: predecessorSelectionSHA256,
		CycleContractSHA256: cycle.ContractSHA256,
		CycleID: cycle.CycleID,
		CycleIssuedAt: cycle.IssuedAt,
		CycleDeadlineAt: cycle.DeadlineAt,
		RefreshIntervalSeconds: r.Config.RefreshIntervalSeconds,
		CollectionDeadlineSeconds: r.Config.CollectionDeadlineSeconds,
		Cycle: cycle,
	}
	results := make([]SourceEvidence, len(r.Config.Sources))
	collectionContext, cancel := context.WithDeadline(ctx, deadlineAt)
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
			results[index], errorsByIndex[index] = r.collectSource(collectionContext, source, cycle, now)
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
	bundle.CollectedAt, err = deterministicBundleCollectedAt(cycle, results)
	if err != nil {
		return err
	}
	bundleRaw, err := json.Marshal(bundle)
	if err != nil || int64(len(bundleRaw)) > r.Config.MaximumBundleBytes {
		return errors.New("native evidence bundle exceeds its canonical bound")
	}
	if _, err := VerifyEvidenceBundle(bundleRaw, r.Config, r.Acceptance, r.nativeTrust, time.Now().UTC()); err != nil {
		return fmt.Errorf("reopen exact native evidence before snapshot authority handoff: %w", err)
	}
	if err := r.appendEvidence("native-bundle-"+digest(bundleRaw)+".json", bundleRaw); err != nil {
		return err
	}
	envelopeRaw, settled, err := r.settledSnapshotForCycle(cycle, bundleRaw)
	if err != nil {
		return err
	}
	if !settled {
		if !time.Now().UTC().Before(deadlineAt) {
			return errors.New("snapshot request cannot begin after the accepted collection deadline")
		}
		envelopeRaw, err = r.requestSnapshot(collectionContext, bundleRaw)
		if err != nil {
			return err
		}
		if !time.Now().UTC().Before(deadlineAt) {
			return errors.New("snapshot response completed after the accepted collection deadline")
		}
		if err := r.settleSnapshotCycle(cycle, bundleRaw, envelopeRaw); err != nil {
			return err
		}
	}
	runtimeBytes, runtimeInodes, err := runtimePerCycleStorageBounds(r.Config)
	if err != nil {
		return err
	}
	r.runtimeMu.Lock()
	defer r.runtimeMu.Unlock()
	if err := validateRuntimeCapacity(r.Config, runtimeBytes, runtimeInodes); err != nil {
		return fmt.Errorf("reserve native runtime cycle capacity: %w", err)
	}
	activationPlan, err := r.stageSnapshot(cycle, envelopeRaw, bundleRaw, time.Now().UTC().Truncate(time.Second))
	if err != nil {
		return err
	}
	if err := r.activateStagedSnapshot(activationPlan, deadlineAt); err != nil {
		return err
	}
	return nil
}

func (r *Runner) settleCyclePredecessor(cycle CollectorCycle, observed string) (string, error) {
	if observed != "" && !isDigest(observed) {
		return "", errors.New("runtime predecessor is not content addressed")
	}
	name := "snapshot-cycle-predecessor-" + cycle.CycleID + ".json"
	path := filepath.Join(r.Config.EvidenceRoot, name)
	if raw, err := readRootRegular(path, maximumConfigBytes); err == nil {
		var retained snapshotCyclePredecessor
		if canonicalJSON(raw, &retained) != nil || retained.Schema != snapshotCyclePredecessorSchema ||
			retained.CycleContractSHA256 != cycle.ContractSHA256 || retained.CycleID != cycle.CycleID ||
			(retained.PredecessorSelectionSHA256 != "" && !isDigest(retained.PredecessorSelectionSHA256)) {
			return "", errors.New("retained cycle predecessor is invalid")
		}
		return retained.PredecessorSelectionSHA256, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	record := snapshotCyclePredecessor{
		Schema: snapshotCyclePredecessorSchema,
		CycleContractSHA256: cycle.ContractSHA256,
		CycleID: cycle.CycleID,
		PredecessorSelectionSHA256: observed,
	}
	raw, err := json.Marshal(record)
	if err != nil {
		return "", err
	}
	if err := r.appendEvidence(name, raw); err != nil {
		return "", fmt.Errorf("settle immutable cycle activation predecessor: %w", err)
	}
	return observed, nil
}

func (r *Runner) snapshotCycleSettlementName(cycleID string) (string, error) {
	if !isDigest(cycleID) {
		return "", errors.New("snapshot cycle settlement identity is invalid")
	}
	return "snapshot-cycle-settlement-" + cycleID + ".json", nil
}

func (r *Runner) settledSnapshotForCycle(cycle CollectorCycle, bundleRaw []byte) ([]byte, bool, error) {
	name, err := r.snapshotCycleSettlementName(cycle.CycleID)
	if err != nil {
		return nil, false, err
	}
	raw, err := readRootRegular(filepath.Join(r.Config.EvidenceRoot, name), maximumConfigBytes)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	var settlement snapshotCycleSettlement
	if err := canonicalJSON(raw, &settlement); err != nil || settlement.Schema != snapshotCycleSettlementSchema ||
		settlement.CycleContractSHA256 != cycle.ContractSHA256 || settlement.CycleID != cycle.CycleID ||
		settlement.CycleIssuedAt != cycle.IssuedAt || settlement.CycleDeadlineAt != cycle.DeadlineAt ||
		settlement.EvidenceBundleSHA256 != digest(bundleRaw) || !isDigest(settlement.SnapshotEnvelopeSHA256) {
		return nil, false, errors.New("retained snapshot cycle settlement conflicts with the exact cycle or evidence bundle")
	}
	envelopeRaw, err := readRootRegular(
		filepath.Join(r.Config.EvidenceRoot, "snapshot-envelope-"+settlement.SnapshotEnvelopeSHA256+".json"),
		maximumEnvelopeBytes,
	)
	if err != nil || digest(envelopeRaw) != settlement.SnapshotEnvelopeSHA256 {
		return nil, false, errors.New("retained snapshot cycle settlement lacks its exact content-addressed envelope")
	}
	if err := r.verifySnapshotEnvelope(bundleRaw, envelopeRaw, time.Now().UTC(), false); err != nil {
		return nil, false, fmt.Errorf("verify retained snapshot cycle settlement: %w", err)
	}
	return envelopeRaw, true, nil
}

func (r *Runner) settleSnapshotCycle(cycle CollectorCycle, bundleRaw []byte, envelopeRaw []byte) error {
	envelopeSHA256 := digest(envelopeRaw)
	if err := r.appendEvidence("snapshot-envelope-"+envelopeSHA256+".json", envelopeRaw); err != nil {
		return err
	}
	settlement := snapshotCycleSettlement{
		Schema: snapshotCycleSettlementSchema,
		CycleContractSHA256: cycle.ContractSHA256,
		CycleID: cycle.CycleID,
		CycleIssuedAt: cycle.IssuedAt,
		CycleDeadlineAt: cycle.DeadlineAt,
		EvidenceBundleSHA256: digest(bundleRaw),
		SnapshotEnvelopeSHA256: envelopeSHA256,
	}
	raw, err := json.Marshal(settlement)
	if err != nil {
		return err
	}
	name, err := r.snapshotCycleSettlementName(cycle.CycleID)
	if err != nil {
		return err
	}
	return r.appendEvidence(name, raw)
}

func (r *Runner) collectSource(ctx context.Context, source SourceSpec, cycle CollectorCycle, now time.Time) (SourceEvidence, error) {
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
	sessionID, err := cycleSessionID(cycle, source.ID)
	if err != nil {
		return SourceEvidence{}, err
	}
	pageToken := ""
	var totalBytes int64
	providerRequestIDs := map[string]struct{}{}
	for pageNumber := 0; pageNumber < source.MaximumPages; pageNumber++ {
		challenge := cycleChallenge(cycle, source.ID, sessionID, pageNumber, pageToken)
		directive := CollectionRequest{
			Schema:       CollectionRequestSchema,
			ClusterID:    r.Acceptance.ClusterID,
			DeploymentID: r.Acceptance.DeploymentID,
			SourceID:     source.ID,
			CycleContractSHA256: cycle.ContractSHA256,
			CycleID: cycle.CycleID,
			CycleIssuedAt: cycle.IssuedAt,
			CycleDeadlineAt: cycle.DeadlineAt,
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
		payloadRaw, err := r.nativeTrust.VerifyEnvelope(
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
			page.CycleContractSHA256 != cycle.ContractSHA256 || page.CycleID != cycle.CycleID ||
			page.CycleIssuedAt != cycle.IssuedAt || page.CycleDeadlineAt != cycle.DeadlineAt ||
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
			CycleContractSHA256: cycle.ContractSHA256,
			CycleID: cycle.CycleID,
			CycleIssuedAt: cycle.IssuedAt,
			CycleDeadlineAt: cycle.DeadlineAt,
			DirectiveSHA256: digest(directiveRaw),
			CollectedAt: page.CollectedAt,
				EnvelopeObject: envelopeObject,
				EnvelopeSHA256: envelopeSHA256,
				PayloadSHA256:  digest(payloadRaw),
				// The remote snapshot authority cannot dereference the collector's
				// private EvidenceRoot. Carry the exact independently signed native
				// envelope as bounded canonical base64 while retaining the local
				// content-addressed object for audit and recovery.
				EnvelopeBase64: base64.StdEncoding.EncodeToString(envelopeRaw),
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

type collectorCycleIdentity struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	ContractSHA256 string `json:"contract_sha256"`
	IssuedAt string `json:"issued_at"`
	DeadlineAt string `json:"deadline_at"`
	RefreshIntervalSeconds int `json:"refresh_interval_seconds"`
	CollectionDeadlineSeconds int `json:"collection_deadline_seconds"`
	ChallengeDerivation string `json:"challenge_derivation"`
	Sources []CollectorCycleSource `json:"sources"`
}

type collectorCycleContract struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	IssuedAt string `json:"issued_at"`
	DeadlineAt string `json:"deadline_at"`
	RefreshIntervalSeconds int `json:"refresh_interval_seconds"`
	CollectionDeadlineSeconds int `json:"collection_deadline_seconds"`
	CollectorConfigSHA256 string `json:"collector_config_sha256"`
	AuthorityConfigSHA256 string `json:"authority_config_sha256"`
	NativeResponseTrustSHA256 string `json:"native_response_trust_sha256"`
	SnapshotTrustSHA256 string `json:"snapshot_trust_sha256"`
	MandatoryCollectionsSHA256 string `json:"mandatory_collections_sha256"`
	AuthoritySnapshotID string `json:"authority_snapshot_id"`
	AuthorityClosureSHA256 string `json:"authority_closure_sha256"`
	SourceIDs []string `json:"source_ids"`
}

func collectorSourceIDs(sources []SourceSpec) []string {
	ids := make([]string, 0, len(sources))
	for _, source := range sources {
		ids = append(ids, source.ID)
	}
	return ids
}

func authoritySourceIDs(sources []AuthoritySourceSpec) []string {
	ids := make([]string, 0, len(sources))
	for _, source := range sources {
		ids = append(ids, source.ID)
	}
	return ids
}

func collectorCycleFor(acceptance boundary.Acceptance, refreshSeconds int, deadlineSeconds int, sourceIDs []string, now time.Time) (CollectorCycle, error) {
	if refreshSeconds < 1 || deadlineSeconds < 1 || deadlineSeconds >= refreshSeconds || len(sourceIDs) == 0 {
		return CollectorCycle{}, errors.New("collector cadence cannot derive a bounded cycle")
	}
	issuedAt := time.Unix((now.UTC().Unix()/int64(refreshSeconds))*int64(refreshSeconds), 0).UTC()
	deadlineAt := issuedAt.Add(time.Duration(deadlineSeconds) * time.Second)
	sortedIDs := append([]string(nil), sourceIDs...)
	sort.Strings(sortedIDs)
	sources := make([]CollectorCycleSource, 0, len(sortedIDs))
	for index, sourceID := range sortedIDs {
		if !boundedProtocolText(sourceID, maximumSourceIDBytes, false) || index > 0 && sortedIDs[index-1] == sourceID {
			return CollectorCycle{}, errors.New("collector cycle source inventory is incomplete or duplicated")
		}
	}
	contract := collectorCycleContract{
		Schema: CollectorCycleSchema, ClusterID: acceptance.ClusterID, DeploymentID: acceptance.DeploymentID,
		IssuedAt: issuedAt.Format(time.RFC3339), DeadlineAt: deadlineAt.Format(time.RFC3339),
		RefreshIntervalSeconds: refreshSeconds, CollectionDeadlineSeconds: deadlineSeconds,
		CollectorConfigSHA256: acceptance.NativeCollectorConfigSHA256,
		AuthorityConfigSHA256: acceptance.NativeAuthorityConfigSHA256,
		NativeResponseTrustSHA256: acceptance.NativeResponseTrustSHA256,
		SnapshotTrustSHA256: acceptance.SnapshotTrustSHA256,
		MandatoryCollectionsSHA256: acceptance.MandatoryCollectionsSHA256,
		AuthoritySnapshotID: acceptance.AuthoritySnapshotID,
		AuthorityClosureSHA256: acceptance.AuthorityClosureSHA256,
		SourceIDs: sortedIDs,
	}
	contractRaw, err := json.Marshal(contract)
	if err != nil {
		return CollectorCycle{}, err
	}
	contractSHA256 := digest(contractRaw)
	for _, sourceID := range sortedIDs {
		sessionID := digest([]byte(strings.Join([]string{
			"fs2-serve.nebius.ai/public-edge-native-collection-session/v2",
			contractSHA256, sourceID,
		}, "\n")))
		sources = append(sources, CollectorCycleSource{SourceID: sourceID, SessionID: sessionID})
	}
	identity := collectorCycleIdentity{
		Schema: CollectorCycleSchema, ClusterID: acceptance.ClusterID, DeploymentID: acceptance.DeploymentID,
		ContractSHA256: contractSHA256,
		IssuedAt: issuedAt.Format(time.RFC3339), DeadlineAt: deadlineAt.Format(time.RFC3339),
		RefreshIntervalSeconds: refreshSeconds, CollectionDeadlineSeconds: deadlineSeconds,
		ChallengeDerivation: "sha256-cycle-contract-source-session-page-token/v2", Sources: sources,
	}
	identityRaw, err := json.Marshal(identity)
	if err != nil {
		return CollectorCycle{}, err
	}
	return CollectorCycle{
		Schema: identity.Schema, ClusterID: identity.ClusterID, DeploymentID: identity.DeploymentID,
		ContractSHA256: identity.ContractSHA256,
		CycleID: digest(identityRaw), IssuedAt: identity.IssuedAt, DeadlineAt: identity.DeadlineAt,
		RefreshIntervalSeconds: identity.RefreshIntervalSeconds,
		CollectionDeadlineSeconds: identity.CollectionDeadlineSeconds,
		CollectorConfigSHA256: contract.CollectorConfigSHA256,
		AuthorityConfigSHA256: contract.AuthorityConfigSHA256,
		NativeResponseTrustSHA256: contract.NativeResponseTrustSHA256,
		SnapshotTrustSHA256: contract.SnapshotTrustSHA256,
		MandatoryCollectionsSHA256: contract.MandatoryCollectionsSHA256,
		AuthoritySnapshotID: contract.AuthoritySnapshotID,
		AuthorityClosureSHA256: contract.AuthorityClosureSHA256,
		ChallengeDerivation: identity.ChallengeDerivation, Sources: identity.Sources,
	}, nil
}

func cycleSessionID(cycle CollectorCycle, sourceID string) (string, error) {
	for _, source := range cycle.Sources {
		if source.SourceID == sourceID && canonicalNonce(source.SessionID) {
			return source.SessionID, nil
		}
	}
	return "", errors.New("collector cycle omits the exact source session")
}

func cycleChallenge(cycle CollectorCycle, sourceID string, sessionID string, pageIndex int, pageToken string) string {
	return digest([]byte(strings.Join([]string{
		"fs2-serve.nebius.ai/public-edge-native-collection-challenge/v2",
		cycle.ContractSHA256, cycle.CycleID, cycle.IssuedAt, cycle.DeadlineAt, sourceID, sessionID,
		fmt.Sprintf("%d", pageIndex), pageToken,
	}, "\n")))
}

func deterministicBundleCollectedAt(cycle CollectorCycle, sources []SourceEvidence) (string, error) {
	issuedAt, issuedErr := time.Parse(time.RFC3339, cycle.IssuedAt)
	deadlineAt, deadlineErr := time.Parse(time.RFC3339, cycle.DeadlineAt)
	latest := time.Time{}
	pageCount := 0
	for _, source := range sources {
		for _, page := range source.Pages {
			collectedAt, err := time.Parse(time.RFC3339, page.CollectedAt)
			if err != nil || collectedAt.Nanosecond() != 0 || !strings.HasSuffix(page.CollectedAt, "Z") ||
				page.CycleContractSHA256 != cycle.ContractSHA256 || page.CycleID != cycle.CycleID ||
				page.CycleIssuedAt != cycle.IssuedAt || page.CycleDeadlineAt != cycle.DeadlineAt {
				return "", errors.New("captured page does not bind the exact deterministic collection cycle")
			}
			if latest.IsZero() || collectedAt.After(latest) {
				latest = collectedAt
			}
			pageCount++
		}
	}
	if issuedErr != nil || deadlineErr != nil || pageCount == 0 || latest.Before(issuedAt) || !latest.Before(deadlineAt) {
		return "", errors.New("native evidence bundle lacks a bounded terminal collection time")
	}
	return latest.Format(time.RFC3339), nil
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
	if err := r.verifySnapshotEnvelope(bundleRaw, envelopeRaw, time.Now().UTC(), true); err != nil {
		return nil, err
	}
	return envelopeRaw, nil
}

func (r *Runner) verifySnapshotEnvelope(bundleRaw []byte, envelopeRaw []byte, now time.Time, requireFreshIssuance bool) error {
	var bundle EvidenceBundle
	if err := canonicalJSON(bundleRaw, &bundle); err != nil || bundle.Schema != EvidenceBundleSchema ||
		bundle.ClusterID != r.Acceptance.ClusterID || bundle.DeploymentID != r.Acceptance.DeploymentID ||
		bundle.CycleContractSHA256 != bundle.Cycle.ContractSHA256 || bundle.CycleID != bundle.Cycle.CycleID ||
		bundle.CycleIssuedAt != bundle.Cycle.IssuedAt || bundle.CycleDeadlineAt != bundle.Cycle.DeadlineAt ||
		bundle.RefreshIntervalSeconds != bundle.Cycle.RefreshIntervalSeconds ||
		bundle.CollectionDeadlineSeconds != bundle.Cycle.CollectionDeadlineSeconds ||
		(bundle.ActivationPredecessorSelectionSHA256 != "" && !isDigest(bundle.ActivationPredecessorSelectionSHA256)) {
		return errors.New("snapshot evidence bundle does not contain its exact collection cycle")
	}
	cycleIssuedAt, cycleIssueErr := time.Parse(time.RFC3339, bundle.CycleIssuedAt)
	expectedCycle, cycleErr := r.AcceptedCycle(cycleIssuedAt)
	expectedCycleRaw, expectedMarshalErr := json.Marshal(expectedCycle)
	actualCycleRaw, actualMarshalErr := json.Marshal(bundle.Cycle)
	expectedCollectedAt, collectedAtErr := deterministicBundleCollectedAt(bundle.Cycle, bundle.Sources)
	if cycleIssueErr != nil || cycleErr != nil || expectedMarshalErr != nil || actualMarshalErr != nil ||
		collectedAtErr != nil || expectedCollectedAt != bundle.CollectedAt ||
		!bytes.Equal(expectedCycleRaw, actualCycleRaw) {
		return errors.New("snapshot evidence bundle cycle differs from the independently accepted config and source plan")
	}
	payloadRaw, err := r.snapshotTrust.VerifyEnvelope(
		boundary.EnvelopeSchema,
		SnapshotIssuerRole,
		envelopeRaw,
	)
	if err != nil {
		return err
	}
	var snapshot boundary.Snapshot
	if _, err := boundary.CanonicalJSON(payloadRaw, &snapshot); err != nil || snapshot.Schema != boundary.SnapshotSchema ||
		snapshot.EvidenceBundleSHA256 != digest(bundleRaw) ||
		snapshot.ActivationCycleContractSHA256 != bundle.CycleContractSHA256 ||
		snapshot.ActivationCycleID != bundle.CycleID || snapshot.ActivationCycleIssuedAt != bundle.CycleIssuedAt ||
		snapshot.ActivationCycleDeadlineAt != bundle.CycleDeadlineAt ||
		snapshot.ActivationPredecessorSelectionSHA256 != bundle.ActivationPredecessorSelectionSHA256 {
		return errors.New("snapshot response is not bound to the exact native evidence request")
	}
	issuedAt, issueErr := time.Parse(time.RFC3339, snapshot.IssuedAt)
	expiresAt, expiryErr := time.Parse(time.RFC3339, snapshot.ExpiresAt)
	minimumLifetime := time.Duration(r.Config.RefreshIntervalSeconds+30) * time.Second
	responseTime := now.UTC()
	requiredThrough := cycleIssuedAt.Add(time.Duration(r.Config.RefreshIntervalSeconds+30) * time.Second)
	if issueErr != nil || expiryErr != nil || issuedAt.Nanosecond() != 0 || expiresAt.Nanosecond() != 0 ||
		snapshot.MaximumAgeSeconds < r.Config.RefreshIntervalSeconds+30 ||
		expiresAt.Sub(issuedAt) < minimumLifetime || !responseTime.Before(expiresAt) || expiresAt.Before(requiredThrough) ||
		issuedAt.After(responseTime.Add(30*time.Second)) ||
		(requireFreshIssuance && issuedAt.Before(responseTime.Add(-30*time.Second))) {
		return errors.New("snapshot response cannot cover the enrolled refresh cadence and safety margin")
	}
	return nil
}

func (r *Runner) stageSnapshot(cycle CollectorCycle, envelopeRaw []byte, bundleRaw []byte, now time.Time) (snapshotActivationPlan, error) {
	plan := snapshotActivationPlan{
		cycle: cycle,
		bundleSHA256: digest(bundleRaw),
		envelopeSHA256: digest(envelopeRaw),
	}
	deadlineAt, deadlineErr := time.Parse(time.RFC3339, cycle.DeadlineAt)
	if deadlineErr != nil || deadlineAt.Nanosecond() != 0 || !now.Before(deadlineAt) {
		return plan, errors.New("snapshot candidate cannot be staged outside its exact accepted cycle")
	}
	if err := r.verifySnapshotEnvelope(bundleRaw, envelopeRaw, now, false); err != nil {
		return plan, fmt.Errorf("verify exact snapshot bundle binding before staging: %w", err)
	}
	var bundle EvidenceBundle
	if err := canonicalJSON(bundleRaw, &bundle); err != nil {
		return plan, err
	}
	plan.predecessorSelectionSHA256 = bundle.ActivationPredecessorSelectionSHA256
	snapshotTrustRaw, err := r.snapshotTrust.CanonicalBytes()
	if err != nil {
		return plan, err
	}
	if err := requireRootOwnedDirectory(r.Config.RuntimeRoot); err != nil {
		return plan, err
	}
	if err := r.ensureRuntimeChildDirectory("snapshot-generations"); err != nil {
		return plan, err
	}
	plan.selectionPath = filepath.Join(r.Config.RuntimeRoot, "snapshot-runtime-selection.json")
	selected, activeRaw, predecessorSelectionSHA256, activeExists, err := r.loadRuntimeSelection(now)
	if err != nil {
		return plan, err
	}
	if activeExists && bytes.Equal(activeRaw, envelopeRaw) {
		selectedTrustRaw, selectedAuthoritySnapshotID, selectedAuthorityClosureSHA256, authorityErr := r.runtimeSelectionAuthority(selected)
		if authorityErr != nil {
			return plan, authorityErr
		}
		if _, err := boundary.LoadRuntimeFromBytes(
			selectedTrustRaw,
			activeRaw,
			digest(selectedTrustRaw),
			r.Acceptance.ClusterID,
			r.Acceptance.DeploymentID,
			selectedAuthoritySnapshotID,
			selectedAuthorityClosureSHA256,
			now,
		); err != nil {
			return plan, fmt.Errorf("verify identical selected snapshot before no-op: %w", err)
		}
		if !r.selectionUsesCurrentAcceptance(selected) {
			return plan, errors.New("identical snapshot retry belongs to a historical acceptance generation; a fresh signed successor is required")
		}
		if !r.activationReceiptIsExact(selected.Activation, plan, now) {
			return plan, errors.New("identical immutable runtime envelope lacks its exact committed selection receipt")
		}
		plan.alreadyActive = true
		return plan, nil
	}
	if predecessorSelectionSHA256 != plan.predecessorSelectionSHA256 {
		return plan, errors.New("signed snapshot activation predecessor changed during evidence collection")
	}
	if activeExists && !r.currentAcceptanceMayFollowSelection(selected) {
		return plan, errors.New("collector acceptance generation is not the exact current or directly signed next generation")
	}
	if activeExists && selected.Schema == legacySnapshotRuntimeSelectionSchema {
		requiresBootstrap, err := legacySelectorRequiresBootstrap(activeRaw)
		if err != nil {
			return plan, err
		}
		if requiresBootstrap {
			bootstrap, bootstrapSHA256, err := r.retainLegacyRuntimeBootstrap(predecessorSelectionSHA256, activeRaw, now)
			if err != nil {
				return plan, err
			}
			plan.legacyBootstrapEnvelopeSHA256 = bootstrapSHA256
			plan.legacyBootstrapExpiresAt = bootstrap.ExpiresAt
		}
	}
	if err := r.prepareRuntimeAuthorityGenerations(&plan, snapshotTrustRaw); err != nil {
		return plan, err
	}
	digestValue := plan.envelopeSHA256
	if err := r.appendEvidence("snapshot-envelope-"+digestValue+".json", envelopeRaw); err != nil {
		return plan, err
	}
	plan.envelopePath = filepath.Join(r.Config.RuntimeRoot, "snapshot-generations", "snapshot-envelope-"+digestValue+".json")
	if err := r.appendRuntimeOrMatch(plan.envelopePath, envelopeRaw, 0o444); err != nil {
		return plan, err
	}
	candidateRuntime, err := boundary.LoadRuntimeFromBytes(
		snapshotTrustRaw,
		envelopeRaw,
		r.Acceptance.SnapshotTrustSHA256,
		r.Acceptance.ClusterID,
		r.Acceptance.DeploymentID,
		r.Acceptance.AuthoritySnapshotID,
		r.Acceptance.AuthorityClosureSHA256,
		now,
	)
	if err != nil {
		return plan, fmt.Errorf("verify candidate snapshot before activation: %w", err)
	}
	if activeExists && selected.Schema != legacySnapshotRuntimeSelectionSchema {
		activeTrustRaw, activeAuthoritySnapshotID, activeAuthorityClosureSHA256, authorityErr := r.runtimeSelectionAuthority(selected)
		if authorityErr != nil {
			return plan, authorityErr
		}
		activeRuntime, err := boundary.LoadRuntimeFromBytesForChainRecovery(
			activeTrustRaw,
			activeRaw,
			digest(activeTrustRaw),
			r.Acceptance.ClusterID,
			r.Acceptance.DeploymentID,
			activeAuthoritySnapshotID,
			activeAuthorityClosureSHA256,
			now,
		)
		if err != nil {
			return plan, fmt.Errorf("verify active snapshot before monotonic replacement: %w", err)
		}
		candidateIssuedAt, candidateErr := time.Parse(time.RFC3339, candidateRuntime.Snapshot.IssuedAt)
		activeIssuedAt, activeErr := time.Parse(time.RFC3339, activeRuntime.Snapshot.IssuedAt)
		if candidateErr != nil || activeErr != nil || !candidateIssuedAt.After(activeIssuedAt) {
			return plan, errors.New("snapshot activation refuses a different candidate that is not strictly newer than the active signed runtime")
		}
	}
	return plan, nil
}

func (r *Runner) prepareRuntimeAuthorityGenerations(plan *snapshotActivationPlan, snapshotTrustRaw []byte) error {
	if plan == nil || len(snapshotTrustRaw) == 0 {
		return errors.New("runtime authority generation is incomplete")
	}
	acceptanceTrustRaw, acceptanceTrustSHA256, err := r.Acceptance.TrustGeneration()
	if err != nil {
		return err
	}
	acceptanceEnvelopeRaw, acceptanceEnvelopeSHA256, err := r.Acceptance.EnvelopeGeneration()
	if err != nil {
		return err
	}
	snapshotTrustSHA256 := digest(snapshotTrustRaw)
	plan.snapshotTrustName = "snapshot-trust-" + snapshotTrustSHA256 + ".json"
	plan.snapshotTrustSHA256 = snapshotTrustSHA256
	plan.acceptanceTrustName = "acceptance-trust-" + acceptanceTrustSHA256 + ".json"
	plan.acceptanceTrustSHA256 = acceptanceTrustSHA256
	plan.acceptanceEnvelopeName = "acceptance-envelope-" + acceptanceEnvelopeSHA256 + ".json"
	plan.acceptanceEnvelopeSHA256 = acceptanceEnvelopeSHA256
	for _, name := range []string{"snapshot-trust-generations", "acceptance-trust-generations", "acceptance-envelope-generations"} {
		if err := r.ensureRuntimeChildDirectory(name); err != nil {
			return err
		}
	}
	publications := []struct {
		directory string
		name      string
		raw       []byte
	}{
		{"snapshot-trust-generations", plan.snapshotTrustName, snapshotTrustRaw},
		{"acceptance-trust-generations", plan.acceptanceTrustName, acceptanceTrustRaw},
		{"acceptance-envelope-generations", plan.acceptanceEnvelopeName, acceptanceEnvelopeRaw},
	}
	for _, publication := range publications {
		if err := r.appendRuntimeOrMatch(filepath.Join(r.Config.RuntimeRoot, publication.directory, publication.name), publication.raw, 0o444); err != nil {
			return err
		}
	}
	return nil
}

func (r *Runner) loadRuntimeSelection(now time.Time) (snapshotRuntimeSelection, []byte, string, bool, error) {
	selectionPath := filepath.Join(r.Config.RuntimeRoot, "snapshot-runtime-selection.json")
	raw, err := readRootRegular(selectionPath, maximumSnapshotRuntimeSelectionBytes)
	headPath := selectionPath
	fixedRaw := raw
	fixedExists := err == nil
	if errors.Is(err, os.ErrNotExist) {
		headPath, err = r.snapshotSuccessorPath("")
		if err != nil {
			return snapshotRuntimeSelection{}, nil, "", false, err
		}
		raw, err = readRootRegular(headPath, maximumSnapshotRuntimeSelectionBytes)
		if errors.Is(err, os.ErrNotExist) {
			return snapshotRuntimeSelection{}, nil, "", false, nil
		}
	}
	if err != nil {
		return snapshotRuntimeSelection{}, nil, "", false, err
	}
	if fixedExists {
		var hint snapshotRuntimeSelection
		if decoded, decodeErr := decodeSnapshotRuntimeSelection(raw); decodeErr != nil {
			return snapshotRuntimeSelection{}, nil, "", false, errors.New("runtime selection accelerator is not canonical")
		} else {
			hint = decoded
		}
		committedPath, pathErr := r.snapshotSuccessorPath(hint.PredecessorSelectionSHA256)
		if pathErr != nil {
			return snapshotRuntimeSelection{}, nil, "", false, pathErr
		}
		committedRaw, readErr := readRootRegular(committedPath, maximumSnapshotRuntimeSelectionBytes)
		if errors.Is(readErr, os.ErrNotExist) && hint.Schema == legacySnapshotRuntimeSelectionSchema {
			// A pre-successor-chain selector can enter the immutable v2 chain only
			// through an independently signed bootstrap over its exact retained
			// selector, envelope and trust bytes. Retain those authority bytes first,
			// then no-replace publish the identical selector as the chain anchor.
			// The legacy payload is never decoded into or served as a current Runtime.
			_, envelopeRaw, decodeErr := r.decodeRuntimeSelection(raw, now)
			if decodeErr != nil {
				return snapshotRuntimeSelection{}, nil, "", false, decodeErr
			}
			requiresBootstrap, schemaErr := legacySelectorRequiresBootstrap(envelopeRaw)
			if schemaErr != nil {
				return snapshotRuntimeSelection{}, nil, "", false, schemaErr
			}
			if requiresBootstrap {
				if _, _, err := r.retainLegacyRuntimeBootstrap(digest(raw), envelopeRaw, now); err != nil {
					return snapshotRuntimeSelection{}, nil, "", false, err
				}
			}
			if err := r.appendRuntimeOrMatch(committedPath, raw, 0o444); err != nil {
				return snapshotRuntimeSelection{}, nil, "", false, err
			}
			committedRaw, readErr = readRootRegular(committedPath, maximumSnapshotRuntimeSelectionBytes)
		}
		if readErr != nil || !bytes.Equal(committedRaw, raw) {
			return snapshotRuntimeSelection{}, nil, "", false, errors.New("runtime selection accelerator is not its exact immutable committed successor")
		}
		raw = committedRaw
		headPath = committedPath
	}
	var selection snapshotRuntimeSelection
	var envelopeRaw []byte
	for depth := 0; depth < 1024; depth++ {
		selection, envelopeRaw, err = r.decodeRuntimeSelection(raw, now)
		if err != nil {
			return snapshotRuntimeSelection{}, nil, "", false, err
		}
		nextPath, pathErr := r.snapshotSuccessorPath(digest(raw))
		if pathErr != nil {
			return snapshotRuntimeSelection{}, nil, "", false, pathErr
		}
		nextRaw, readErr := readRootRegular(nextPath, maximumSnapshotRuntimeSelectionBytes)
		if errors.Is(readErr, os.ErrNotExist) {
			if len(fixedRaw) == 0 || !bytes.Equal(fixedRaw, raw) {
				if err := r.promoteRuntimeSelectionAccelerator(headPath, selectionPath, raw); err != nil {
					return snapshotRuntimeSelection{}, nil, "", false, err
				}
			}
			return selection, envelopeRaw, digest(raw), true, nil
		}
		if readErr != nil {
			return snapshotRuntimeSelection{}, nil, "", false, readErr
		}
		next, decodeErr := decodeSnapshotRuntimeSelection(nextRaw)
		if decodeErr != nil || next.PredecessorSelectionSHA256 != digest(raw) {
			return snapshotRuntimeSelection{}, nil, "", false, errors.New("runtime selection successor does not bind its exact predecessor")
		}
		raw = nextRaw
		headPath = nextPath
	}
	return snapshotRuntimeSelection{}, nil, "", false, errors.New("runtime selection successor chain exceeds its bounded recovery depth")
}

func (r *Runner) decodeRuntimeSelection(raw []byte, now time.Time) (snapshotRuntimeSelection, []byte, error) {
	selection, decodeErr := decodeSnapshotRuntimeSelection(raw)
	if decodeErr != nil || selection.Schema != snapshotRuntimeSelectionSchema && selection.Schema != legacySnapshotRuntimeSelectionSchema ||
		!isDigest(selection.SnapshotEnvelopeSHA256) || selection.SnapshotEnvelopeName != "snapshot-envelope-"+selection.SnapshotEnvelopeSHA256+".json" ||
		filepath.Base(selection.SnapshotEnvelopeName) != selection.SnapshotEnvelopeName ||
		selection.Activation.SnapshotEnvelopeSHA256 != selection.SnapshotEnvelopeSHA256 ||
		selection.Activation.PredecessorSelectionSHA256 != selection.PredecessorSelectionSHA256 ||
		(selection.PredecessorSelectionSHA256 != "" && !isDigest(selection.PredecessorSelectionSHA256)) {
		return snapshotRuntimeSelection{}, nil, errors.New("runtime selection is not one canonical immutable envelope/receipt pair")
	}
	envelopeRaw, err := readRootRegular(filepath.Join(r.Config.RuntimeRoot, "snapshot-generations", selection.SnapshotEnvelopeName), maximumEnvelopeBytes)
	if err != nil || digest(envelopeRaw) != selection.SnapshotEnvelopeSHA256 {
		return snapshotRuntimeSelection{}, nil, errors.New("runtime selection lacks its exact immutable envelope")
	}
	issuedAt, issueErr := time.Parse(time.RFC3339, selection.Activation.CycleIssuedAt)
	deadlineAt, deadlineErr := time.Parse(time.RFC3339, selection.Activation.CycleDeadlineAt)
	activatedAt, activationErr := time.Parse(time.RFC3339, selection.Activation.ActivatedAt)
	if issueErr != nil || deadlineErr != nil || activationErr != nil || selection.Activation.Schema != snapshotActivationReceiptSchema ||
		selection.Activation.ClusterID != r.Config.ClusterID || selection.Activation.DeploymentID != r.Config.DeploymentID ||
		!isDigest(selection.Activation.CycleContractSHA256) || !isDigest(selection.Activation.CycleID) ||
		!isDigest(selection.Activation.EvidenceBundleSHA256) ||
		selection.Activation.Status != "activated-before-deadline" || !activatedAt.Before(deadlineAt) ||
		activatedAt.Before(issuedAt) || now.Before(activatedAt.Add(-30*time.Second)) {
		return snapshotRuntimeSelection{}, nil, errors.New("runtime selection activation receipt is invalid")
	}
	snapshotTrustRaw, authoritySnapshotID, authorityClosureSHA256, trustErr := r.runtimeSelectionAuthority(selection)
	if trustErr != nil {
		if selection.Schema == legacySnapshotRuntimeSelectionSchema {
			if bootstrapErr := r.verifyLegacyRuntimeSelection(raw, envelopeRaw, now); bootstrapErr == nil {
				return selection, envelopeRaw, nil
			}
		}
		return snapshotRuntimeSelection{}, nil, trustErr
	}
	runtime, runtimeErr := boundary.LoadRuntimeFromBytesForChainRecovery(
		snapshotTrustRaw, envelopeRaw, digest(snapshotTrustRaw), r.Acceptance.ClusterID,
		r.Acceptance.DeploymentID, authoritySnapshotID, authorityClosureSHA256, now,
	)
	if runtimeErr != nil && selection.Schema == legacySnapshotRuntimeSelectionSchema {
		if bootstrapErr := r.verifyLegacyRuntimeSelection(raw, envelopeRaw, now); bootstrapErr == nil {
			return selection, envelopeRaw, nil
		}
	}
	if runtimeErr != nil || runtime.Snapshot.ActivationCycleContractSHA256 != selection.Activation.CycleContractSHA256 ||
		runtime.Snapshot.ActivationCycleID != selection.Activation.CycleID ||
		runtime.Snapshot.ActivationCycleIssuedAt != selection.Activation.CycleIssuedAt ||
		runtime.Snapshot.ActivationCycleDeadlineAt != selection.Activation.CycleDeadlineAt ||
		runtime.Snapshot.ActivationPredecessorSelectionSHA256 != selection.PredecessorSelectionSHA256 ||
		runtime.Snapshot.EvidenceBundleSHA256 != selection.Activation.EvidenceBundleSHA256 {
		return snapshotRuntimeSelection{}, nil, errors.New("runtime selection differs from its signed activation transition")
	}
	return selection, envelopeRaw, nil
}

func (r *Runner) verifyLegacyRuntimeSelection(selectionRaw []byte, envelopeRaw []byte, now time.Time) error {
	bootstrap, _, legacyTrustRaw, err := r.legacyRuntimeBootstrap(selectionRaw, now)
	if err != nil {
		return err
	}
	return boundary.VerifyLegacyRuntimeAnchor(*bootstrap, selectionRaw, legacyTrustRaw, envelopeRaw)
}

func legacySelectorRequiresBootstrap(envelopeRaw []byte) (bool, error) {
	schema, err := boundary.InspectSignedEnvelopeSchema(envelopeRaw)
	if err != nil {
		return false, errors.New("legacy selector snapshot envelope cannot be decoded exactly")
	}
	switch schema {
	case boundary.EnvelopeSchema:
		return false, nil
	case boundary.LegacyEnvelopeSchema, boundary.PreviousEnvelopeSchema:
		return true, nil
	default:
		return false, errors.New("legacy selector references an unsupported snapshot envelope schema")
	}
}

func (r *Runner) legacyRuntimeBootstrap(
	selectionRaw []byte,
	now time.Time,
) (*boundary.LegacyRuntimeBootstrap, []byte, []byte, error) {
	selectionSHA256 := digest(selectionRaw)
	var enrolledRaw []byte
	var enrolled *boundary.LegacyRuntimeBootstrap
	var err error
	if r.legacyBootstrap != nil {
		enrolledRaw, _, err = r.legacyBootstrap.EnvelopeGeneration()
		if err != nil {
			return nil, nil, nil, err
		}
		enrolled, err = r.verifyLegacyBootstrapCandidate(enrolledRaw, selectionSHA256, now)
		if err != nil {
			return nil, nil, nil, err
		}
	}
	stored, storedRaw, storedErr := r.storedLegacyBootstrapHead(selectionSHA256, now)
	if storedErr != nil && !errors.Is(storedErr, os.ErrNotExist) {
		return nil, nil, nil, storedErr
	}
	if errors.Is(storedErr, os.ErrNotExist) {
		stored = nil
		storedRaw = nil
	}
	bootstrap, bootstrapRaw, err := boundary.ReconcileLegacyRuntimeBootstrap(enrolled, enrolledRaw, stored, storedRaw)
	if err != nil {
		return nil, nil, nil, err
	}
	legacyTrustRaw, err := readRootRegular(
		filepath.Join(r.Config.RuntimeRoot, "snapshot-trust-generations", "snapshot-trust-"+bootstrap.LegacySnapshotTrustSHA256+".json"),
		maximumConfigBytes,
	)
	if errors.Is(err, os.ErrNotExist) {
		legacyTrustRaw, err = readRootRegular(filepath.Join(r.Config.RuntimeRoot, "snapshot-trust.json"), maximumConfigBytes)
	}
	if err != nil {
		return nil, nil, nil, err
	}
	return bootstrap, bootstrapRaw, legacyTrustRaw, nil
}

func (r *Runner) storedLegacyBootstrapHead(
	selectionSHA256 string,
	now time.Time,
) (*boundary.LegacyRuntimeBootstrap, []byte, error) {
	root := filepath.Join(r.Config.RuntimeRoot, "legacy-bootstrap-generations")
	raw, err := readRootRegular(
		filepath.Join(root, "legacy-bootstrap-for-"+selectionSHA256+"-successor-of-genesis.json"),
		maximumConfigBytes,
	)
	if errors.Is(err, os.ErrNotExist) {
		raw, err = readRootRegular(filepath.Join(root, "legacy-bootstrap-for-"+selectionSHA256+".json"), maximumConfigBytes)
	}
	if err != nil {
		return nil, nil, err
	}
	current, err := r.verifyLegacyBootstrapCandidate(raw, selectionSHA256, now)
	if err != nil {
		return nil, nil, err
	}
	for depth := 0; depth < boundary.MaximumLegacyRuntimeBootstrapGeneration; depth++ {
		nextPath := filepath.Join(root, "legacy-bootstrap-for-"+selectionSHA256+"-successor-of-"+digest(raw)+".json")
		nextRaw, readErr := readRootRegular(nextPath, maximumConfigBytes)
		if errors.Is(readErr, os.ErrNotExist) {
			return current, raw, nil
		}
		if readErr != nil {
			return nil, nil, readErr
		}
		next, verifyErr := r.verifyLegacyBootstrapCandidate(nextRaw, selectionSHA256, now)
		if verifyErr != nil || next.SuccessorOf(*current, digest(raw)) != nil {
			return nil, nil, errors.New("retained legacy bootstrap successor chain is invalid")
		}
		current = next
		raw = nextRaw
	}
	return nil, nil, errors.New("legacy bootstrap renewal chain exceeds its bounded depth")
}

func (r *Runner) verifyLegacyBootstrapCandidate(
	bootstrapRaw []byte,
	selectionSHA256 string,
	now time.Time,
) (*boundary.LegacyRuntimeBootstrap, error) {
	acceptanceTrustSHA256, err := boundary.InspectLegacyRuntimeBootstrapTrustSHA256(bootstrapRaw)
	if err != nil {
		return nil, err
	}
	acceptanceTrustRaw, err := readRootRegular(
		filepath.Join(r.Config.RuntimeRoot, "acceptance-trust-generations", "acceptance-trust-"+acceptanceTrustSHA256+".json"),
		maximumConfigBytes,
	)
	if errors.Is(err, os.ErrNotExist) {
		currentTrustRaw, currentTrustSHA256, currentErr := r.Acceptance.TrustGeneration()
		if currentErr == nil && currentTrustSHA256 == acceptanceTrustSHA256 {
			acceptanceTrustRaw = currentTrustRaw
			err = nil
		}
	}
	if err != nil || digest(acceptanceTrustRaw) != acceptanceTrustSHA256 {
		return nil, errors.New("legacy bootstrap acceptance trust generation is missing")
	}
	bootstrap, err := boundary.VerifyLegacyRuntimeBootstrap(acceptanceTrustRaw, bootstrapRaw, now, false)
	if err != nil || bootstrap.LegacySelectionSHA256 != selectionSHA256 {
		return nil, errors.New("legacy bootstrap does not bind the exact retained selector")
	}
	accepted, err := r.acceptanceGeneration(
		bootstrap.SuccessorAcceptanceEnvelopeSHA256,
		bootstrap.SuccessorAcceptanceTrustSHA256,
	)
	if err != nil {
		return nil, err
	}
	if err := boundary.AcceptanceGenerationsRelated(r.Acceptance, accepted, r.acceptanceGeneration); err != nil {
		return nil, err
	}
	return bootstrap, nil
}

func (r *Runner) legacyBootstrapGeneration(
	envelopeSHA256 string,
	selectionSHA256 string,
) ([]byte, error) {
	if !isDigest(envelopeSHA256) || !isDigest(selectionSHA256) {
		return nil, errors.New("legacy bootstrap generation locator is invalid")
	}
	path := filepath.Join(r.Config.RuntimeRoot, "legacy-bootstrap-generations", "legacy-bootstrap-"+envelopeSHA256+".json")
	raw, err := readRootRegular(path, maximumConfigBytes)
	if errors.Is(err, os.ErrNotExist) {
		legacyPath := filepath.Join(r.Config.RuntimeRoot, "legacy-bootstrap-generations", "legacy-bootstrap-for-"+selectionSHA256+".json")
		raw, err = readRootRegular(legacyPath, maximumConfigBytes)
	}
	if err != nil || digest(raw) != envelopeSHA256 {
		return nil, errors.New("legacy bootstrap predecessor generation is missing or changed")
	}
	return raw, nil
}

func (r *Runner) acceptanceGeneration(envelopeDigest string, trustDigest string) (boundary.Acceptance, error) {
	if !isDigest(envelopeDigest) || !isDigest(trustDigest) {
		return boundary.Acceptance{}, errors.New("acceptance generation digest pair is invalid")
	}
	_, currentTrustDigest, currentTrustErr := r.Acceptance.TrustGeneration()
	_, currentEnvelopeDigest, currentEnvelopeErr := r.Acceptance.EnvelopeGeneration()
	if currentTrustErr == nil && currentEnvelopeErr == nil && currentTrustDigest == trustDigest && currentEnvelopeDigest == envelopeDigest {
		return r.Acceptance, nil
	}
	trustRaw, trustErr := readRootRegular(
		filepath.Join(r.Config.RuntimeRoot, "acceptance-trust-generations", "acceptance-trust-"+trustDigest+".json"),
		maximumConfigBytes,
	)
	envelopeRaw, envelopeErr := readRootRegular(
		filepath.Join(r.Config.RuntimeRoot, "acceptance-envelope-generations", "acceptance-envelope-"+envelopeDigest+".json"),
		maximumConfigBytes,
	)
	if trustErr != nil || envelopeErr != nil || digest(trustRaw) != trustDigest || digest(envelopeRaw) != envelopeDigest {
		return boundary.Acceptance{}, errors.New("acceptance generation is missing or changed")
	}
	return boundary.VerifyAcceptanceGeneration(trustRaw, envelopeRaw)
}

func (r *Runner) retainLegacyRuntimeBootstrap(
	selectionSHA256 string,
	envelopeRaw []byte,
	now time.Time,
) (*boundary.LegacyRuntimeBootstrap, string, error) {
	if !isDigest(selectionSHA256) {
		return nil, "", errors.New("legacy runtime selection digest is invalid")
	}
	selectionRaw, err := readRootRegular(filepath.Join(r.Config.RuntimeRoot, "snapshot-runtime-selection.json"), maximumSnapshotRuntimeSelectionBytes)
	if err != nil || digest(selectionRaw) != selectionSHA256 {
		return nil, "", errors.New("legacy runtime selector changed before bootstrap retention")
	}
	bootstrap, bootstrapRaw, legacyTrustRaw, err := r.legacyRuntimeBootstrap(selectionRaw, now)
	if err != nil {
		return nil, "", err
	}
	if !bootstrap.Current(now) {
		return nil, "", errors.New("legacy runtime bootstrap expired before the migration successor was committed")
	}
	if err := bootstrap.RequireExactAcceptance(r.Acceptance); err != nil {
		return nil, "", err
	}
	if err := boundary.VerifyLegacyRuntimeAnchor(*bootstrap, selectionRaw, legacyTrustRaw, envelopeRaw); err != nil {
		return nil, "", err
	}
	accepted, err := r.acceptanceGeneration(
		bootstrap.SuccessorAcceptanceEnvelopeSHA256,
		bootstrap.SuccessorAcceptanceTrustSHA256,
	)
	if err != nil {
		return nil, "", err
	}
	acceptanceTrustRaw, acceptanceTrustSHA256, err := accepted.TrustGeneration()
	if err != nil || acceptanceTrustSHA256 != bootstrap.SuccessorAcceptanceTrustSHA256 {
		return nil, "", errors.New("legacy bootstrap successor acceptance trust generation is unavailable")
	}
	acceptanceEnvelopeRaw, acceptanceEnvelopeSHA256, err := accepted.EnvelopeGeneration()
	if err != nil || acceptanceEnvelopeSHA256 != bootstrap.SuccessorAcceptanceEnvelopeSHA256 {
		return nil, "", errors.New("legacy bootstrap successor acceptance envelope generation is unavailable")
	}
	if digest(legacyTrustRaw) != bootstrap.LegacySnapshotTrustSHA256 {
		return nil, "", errors.New("legacy bootstrap snapshot trust generation differs from its signed digest")
	}
	for _, name := range []string{
		"legacy-bootstrap-generations",
		"snapshot-trust-generations",
		"acceptance-trust-generations",
		"acceptance-envelope-generations",
	} {
		if err := r.ensureRuntimeChildDirectory(name); err != nil {
			return nil, "", err
		}
	}
	// Publish and fsync every byte needed to authenticate this renewal before
	// making the renewal reachable from its immutable successor-head link. A
	// crash can therefore leave only harmless, content-addressed dependencies;
	// it can never expose a head whose trust or acceptance generation is absent.
	dependencies := []struct {
		directory string
		name      string
		raw       []byte
	}{
		{
			"snapshot-trust-generations",
			"snapshot-trust-" + bootstrap.LegacySnapshotTrustSHA256 + ".json",
			legacyTrustRaw,
		},
		{
			"acceptance-trust-generations",
			"acceptance-trust-" + bootstrap.SuccessorAcceptanceTrustSHA256 + ".json",
			acceptanceTrustRaw,
		},
		{
			"acceptance-envelope-generations",
			"acceptance-envelope-" + bootstrap.SuccessorAcceptanceEnvelopeSHA256 + ".json",
			acceptanceEnvelopeRaw,
		},
	}
	for _, dependency := range dependencies {
		if err := r.appendRuntimeOrMatch(
			filepath.Join(r.Config.RuntimeRoot, dependency.directory, dependency.name),
			dependency.raw,
			0o444,
		); err != nil {
			return nil, "", err
		}
	}
	bootstrapSHA256 := digest(bootstrapRaw)
	if bootstrap.Generation > 1 {
		predecessorRaw, err := r.legacyBootstrapGeneration(bootstrap.PredecessorBootstrapEnvelopeSHA256, selectionSHA256)
		if err != nil {
			return nil, "", err
		}
		if err := r.appendRuntimeOrMatch(
			filepath.Join(r.Config.RuntimeRoot, "legacy-bootstrap-generations", "legacy-bootstrap-"+digest(predecessorRaw)+".json"),
			predecessorRaw,
			0o444,
		); err != nil {
			return nil, "", err
		}
	}
	if err := r.appendRuntimeOrMatch(
		filepath.Join(r.Config.RuntimeRoot, "legacy-bootstrap-generations", "legacy-bootstrap-"+bootstrapSHA256+".json"),
		bootstrapRaw,
		0o444,
	); err != nil {
		return nil, "", err
	}
	predecessorKey := bootstrap.PredecessorBootstrapEnvelopeSHA256
	if predecessorKey == "" {
		predecessorKey = "genesis"
	}
	if err := r.appendRuntimeOrMatch(
		filepath.Join(r.Config.RuntimeRoot, "legacy-bootstrap-generations", "legacy-bootstrap-for-"+selectionSHA256+"-successor-of-"+predecessorKey+".json"),
		bootstrapRaw,
		0o444,
	); err != nil {
		return nil, "", err
	}
	return bootstrap, bootstrapSHA256, nil
}

func (r *Runner) runtimeSelectionAuthority(selection snapshotRuntimeSelection) ([]byte, string, string, error) {
	if selection.Schema == legacySnapshotRuntimeSelectionSchema {
		trustRaw, err := r.snapshotTrust.CanonicalBytes()
		return trustRaw, r.Acceptance.AuthoritySnapshotID, r.Acceptance.AuthorityClosureSHA256, err
	}
	if selection.Schema != snapshotRuntimeSelectionSchema || !isDigest(selection.SnapshotTrustSHA256) ||
		!isDigest(selection.AcceptanceTrustSHA256) || !isDigest(selection.AcceptanceEnvelopeSHA256) ||
		!isDigest(selection.AuthoritySnapshotID) || !isDigest(selection.AuthorityClosureSHA256) ||
		selection.SnapshotTrustName != "snapshot-trust-"+selection.SnapshotTrustSHA256+".json" ||
		selection.AcceptanceTrustName != "acceptance-trust-"+selection.AcceptanceTrustSHA256+".json" ||
		selection.AcceptanceEnvelopeName != "acceptance-envelope-"+selection.AcceptanceEnvelopeSHA256+".json" {
		return nil, "", "", errors.New("runtime selector authority generation references are invalid")
	}
	snapshotTrustRaw, err := readRootRegular(
		filepath.Join(r.Config.RuntimeRoot, "snapshot-trust-generations", selection.SnapshotTrustName),
		maximumConfigBytes,
	)
	if err != nil || digest(snapshotTrustRaw) != selection.SnapshotTrustSHA256 {
		return nil, "", "", errors.New("runtime selector snapshot trust generation is missing or changed")
	}
	acceptanceTrustRaw, err := readRootRegular(
		filepath.Join(r.Config.RuntimeRoot, "acceptance-trust-generations", selection.AcceptanceTrustName),
		maximumConfigBytes,
	)
	if err != nil || digest(acceptanceTrustRaw) != selection.AcceptanceTrustSHA256 {
		return nil, "", "", errors.New("runtime selector acceptance trust generation is missing or changed")
	}
	acceptanceRaw, err := readRootRegular(
		filepath.Join(r.Config.RuntimeRoot, "acceptance-envelope-generations", selection.AcceptanceEnvelopeName),
		maximumConfigBytes,
	)
	if err != nil || digest(acceptanceRaw) != selection.AcceptanceEnvelopeSHA256 {
		return nil, "", "", errors.New("runtime selector acceptance envelope generation is missing or changed")
	}
	accepted, err := boundary.VerifyAcceptanceGeneration(acceptanceTrustRaw, acceptanceRaw)
	if err != nil || accepted.ClusterID != r.Config.ClusterID || accepted.DeploymentID != r.Config.DeploymentID ||
		accepted.SnapshotTrustSHA256 != selection.SnapshotTrustSHA256 ||
		accepted.AuthoritySnapshotID != selection.AuthoritySnapshotID || accepted.AuthorityClosureSHA256 != selection.AuthorityClosureSHA256 {
		return nil, "", "", errors.New("runtime selector authority pins differ from its signed acceptance generation")
	}
	if err := boundary.AcceptanceGenerationsRelated(r.Acceptance, accepted, r.acceptanceGeneration); err != nil {
		return nil, "", "", err
	}
	return snapshotTrustRaw, selection.AuthoritySnapshotID, selection.AuthorityClosureSHA256, nil
}

func (r *Runner) selectionUsesCurrentAcceptance(selection snapshotRuntimeSelection) bool {
	if selection.Schema != snapshotRuntimeSelectionSchema {
		return false
	}
	_, currentTrustSHA256, trustErr := r.Acceptance.TrustGeneration()
	_, currentEnvelopeSHA256, envelopeErr := r.Acceptance.EnvelopeGeneration()
	return trustErr == nil && envelopeErr == nil && selection.AcceptanceTrustSHA256 == currentTrustSHA256 &&
		selection.AcceptanceEnvelopeSHA256 == currentEnvelopeSHA256
}

func (r *Runner) currentAcceptanceMayFollowSelection(selection snapshotRuntimeSelection) bool {
	if r.selectionUsesCurrentAcceptance(selection) {
		return true
	}
	if selection.Schema == legacySnapshotRuntimeSelectionSchema {
		// Legacy selectors are accepted only under the currently source-owned
		// trust and pins by runtimeSelectionAuthority. A fresh v2 selector is the
		// additive migration record and preserves the legacy bytes unchanged.
		return true
	}
	_, currentTrustSHA256, trustErr := r.Acceptance.TrustGeneration()
	_, currentEnvelopeSHA256, envelopeErr := r.Acceptance.EnvelopeGeneration()
	return trustErr == nil && envelopeErr == nil && boundary.AcceptanceSupportsRotation(r.Acceptance.Schema) &&
		r.Acceptance.AcceptanceTrustSHA256 == currentTrustSHA256 &&
		r.Acceptance.PredecessorAcceptanceEnvelopeSHA256 == selection.AcceptanceEnvelopeSHA256 &&
		r.Acceptance.PredecessorAcceptanceTrustSHA256 == selection.AcceptanceTrustSHA256 &&
		currentEnvelopeSHA256 != selection.AcceptanceEnvelopeSHA256
}

func decodeSnapshotRuntimeSelection(raw []byte) (snapshotRuntimeSelection, error) {
	var schema struct {
		Schema string `json:"schema"`
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		return snapshotRuntimeSelection{}, err
	}
	if schema.Schema == legacySnapshotRuntimeSelectionSchema {
		var legacy legacySnapshotRuntimeSelection
		if err := canonicalJSON(raw, &legacy); err != nil {
			return snapshotRuntimeSelection{}, err
		}
		return snapshotRuntimeSelection{
			Schema: legacy.Schema,
			SnapshotEnvelopeName: legacy.SnapshotEnvelopeName,
			SnapshotEnvelopeSHA256: legacy.SnapshotEnvelopeSHA256,
			PredecessorSelectionSHA256: legacy.PredecessorSelectionSHA256,
			Activation: legacy.Activation,
		}, nil
	}
	var selection snapshotRuntimeSelection
	if err := canonicalJSON(raw, &selection); err != nil {
		return snapshotRuntimeSelection{}, err
	}
	return selection, nil
}

func (r *Runner) snapshotSuccessorPath(predecessorSHA256 string) (string, error) {
	key := predecessorSHA256
	if key == "" {
		key = "genesis"
	} else if !isDigest(key) {
		return "", errors.New("snapshot selection predecessor is not content addressed")
	}
	return filepath.Join(r.Config.RuntimeRoot, "snapshot-selection-successor-of-"+key+".json"), nil
}

func (r *Runner) promoteRuntimeSelectionAccelerator(headPath string, selectionPath string, raw []byte) error {
	if filepath.Dir(headPath) != r.Config.RuntimeRoot || filepath.Dir(selectionPath) != r.Config.RuntimeRoot || digest(raw) == "" {
		return errors.New("runtime selection accelerator recovery escaped its accepted root")
	}
	candidate := filepath.Join(r.Config.RuntimeRoot, "snapshot-runtime-selection.recovery-"+digest(raw)+".json")
	if err := validateRuntimeCapacity(r.Config, r.Config.RuntimeFilesystemBlockBytes, 0); err != nil {
		return err
	}
	if err := os.Link(headPath, candidate); err != nil {
		if !errors.Is(err, os.ErrExist) {
			return err
		}
		retained, readErr := readRootRegular(candidate, maximumSnapshotRuntimeSelectionBytes)
		if readErr != nil || !bytes.Equal(retained, raw) {
			return errors.New("runtime selection recovery candidate conflicts with the committed head")
		}
	}
	if err := fsyncDirectory(r.Config.RuntimeRoot); err != nil {
		return err
	}
	if err := os.Rename(candidate, selectionPath); err != nil {
		return err
	}
	return fsyncDirectory(r.Config.RuntimeRoot)
}

func (r *Runner) activationReceiptIsExact(receipt snapshotActivationReceipt, plan snapshotActivationPlan, now time.Time) bool {
	activatedAt, activationErr := time.Parse(time.RFC3339, receipt.ActivatedAt)
	deadlineAt, deadlineErr := time.Parse(time.RFC3339, receipt.CycleDeadlineAt)
	return activationErr == nil && deadlineErr == nil && receipt.Schema == snapshotActivationReceiptSchema &&
		receipt.ClusterID == r.Config.ClusterID && receipt.DeploymentID == r.Config.DeploymentID &&
		receipt.CycleContractSHA256 == plan.cycle.ContractSHA256 && receipt.CycleID == plan.cycle.CycleID &&
		receipt.CycleIssuedAt == plan.cycle.IssuedAt && receipt.CycleDeadlineAt == plan.cycle.DeadlineAt &&
		receipt.EvidenceBundleSHA256 == plan.bundleSHA256 && receipt.SnapshotEnvelopeSHA256 == plan.envelopeSHA256 &&
		receipt.PredecessorSelectionSHA256 == plan.predecessorSelectionSHA256 &&
		receipt.Status == "activated-before-deadline" && activatedAt.Before(deadlineAt) && !now.Before(activatedAt)
}

func (r *Runner) activationReceiptMatches(plan snapshotActivationPlan, now time.Time) (bool, error) {
	raw, err := readRootRegular(filepath.Join(r.Config.RuntimeRoot, "snapshot-activation.json"), maximumSnapshotActivationReceiptBytes)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var receipt snapshotActivationReceipt
	if err := canonicalJSON(raw, &receipt); err != nil {
		return false, err
	}
	activatedAt, activationErr := time.Parse(time.RFC3339, receipt.ActivatedAt)
	deadlineAt, deadlineErr := time.Parse(time.RFC3339, receipt.CycleDeadlineAt)
	if activationErr != nil || deadlineErr != nil || activatedAt.Nanosecond() != 0 || deadlineAt.Nanosecond() != 0 {
		return false, errors.New("retained snapshot activation receipt timestamp is not canonical")
	}
	return receipt.Schema == snapshotActivationReceiptSchema && receipt.ClusterID == r.Config.ClusterID &&
		receipt.DeploymentID == r.Config.DeploymentID && receipt.CycleContractSHA256 == plan.cycle.ContractSHA256 &&
		receipt.CycleID == plan.cycle.CycleID && receipt.CycleIssuedAt == plan.cycle.IssuedAt &&
		receipt.CycleDeadlineAt == plan.cycle.DeadlineAt && receipt.EvidenceBundleSHA256 == plan.bundleSHA256 &&
		receipt.SnapshotEnvelopeSHA256 == plan.envelopeSHA256 && receipt.Status == "activated-before-deadline" &&
		receipt.PredecessorSelectionSHA256 == plan.predecessorSelectionSHA256 &&
		activatedAt.Before(deadlineAt) && !now.Before(activatedAt), nil
}

func (r *Runner) activateStagedSnapshot(plan snapshotActivationPlan, deadlineAt time.Time) error {
	if deadlineAt.IsZero() || deadlineAt.Nanosecond() != 0 {
		return errors.New("snapshot activation deadline is not canonical")
	}
	if plan.alreadyActive {
		if !time.Now().UTC().Before(deadlineAt) {
			return errors.New("identical snapshot retry reached the cycle deadline before durable acknowledgement")
		}
		return nil
	}
	if plan.selectionPath == "" || plan.envelopePath == "" ||
		!isDigest(plan.cycle.ContractSHA256) || !isDigest(plan.cycle.CycleID) || !isDigest(plan.bundleSHA256) || !isDigest(plan.envelopeSHA256) {
		return errors.New("snapshot activation plan is incomplete")
	}
	// The immutable envelope is already durable. The complete selector is staged
	// first; its no-replace successor link is the only reader-visible commit.
	// A crash before that link consumes no predecessor, while a crash after it is
	// recovered by following the immutable successor chain.
	if time.Until(deadlineAt) <= snapshotActivationSafetyMargin {
		return errors.New("snapshot candidate retained but activation refused because the cycle commit window elapsed")
	}
	activatedAt := time.Now().UTC().Truncate(time.Second)
	if !activatedAt.Before(deadlineAt) {
		return errors.New("snapshot envelope was retained immutably, but activation was refused at its deadline")
	}
	return r.publishSnapshotActivationReceipt(plan, deadlineAt, activatedAt)
}

func (r *Runner) publishSnapshotActivationReceipt(plan snapshotActivationPlan, deadlineAt time.Time, activatedAt time.Time) error {
	receipt := snapshotActivationReceipt{
		Schema: snapshotActivationReceiptSchema,
		ClusterID: r.Config.ClusterID,
		DeploymentID: r.Config.DeploymentID,
		CycleContractSHA256: plan.cycle.ContractSHA256,
		CycleID: plan.cycle.CycleID,
		CycleIssuedAt: plan.cycle.IssuedAt,
		CycleDeadlineAt: plan.cycle.DeadlineAt,
		EvidenceBundleSHA256: plan.bundleSHA256,
		SnapshotEnvelopeSHA256: plan.envelopeSHA256,
		PredecessorSelectionSHA256: plan.predecessorSelectionSHA256,
		ActivatedAt: activatedAt.Format(time.RFC3339),
		Status: "activated-before-deadline",
	}
	selection := snapshotRuntimeSelection{
		Schema: snapshotRuntimeSelectionSchema,
		SnapshotEnvelopeName: filepath.Base(plan.envelopePath),
		SnapshotEnvelopeSHA256: plan.envelopeSHA256,
		SnapshotTrustName: plan.snapshotTrustName,
		SnapshotTrustSHA256: plan.snapshotTrustSHA256,
		AcceptanceTrustName: plan.acceptanceTrustName,
		AcceptanceTrustSHA256: plan.acceptanceTrustSHA256,
		AcceptanceEnvelopeName: plan.acceptanceEnvelopeName,
		AcceptanceEnvelopeSHA256: plan.acceptanceEnvelopeSHA256,
		AuthoritySnapshotID: r.Acceptance.AuthoritySnapshotID,
		AuthorityClosureSHA256: r.Acceptance.AuthorityClosureSHA256,
		PredecessorSelectionSHA256: plan.predecessorSelectionSHA256,
		Activation: receipt,
	}
	selectionRaw, err := json.Marshal(selection)
	if err != nil || len(selectionRaw) > maximumSnapshotRuntimeSelectionBytes {
		return errors.New("snapshot runtime selection exceeds its exact canonical bound")
	}
	selectionCandidate := filepath.Join(r.Config.RuntimeRoot, "snapshot-runtime-selection.candidate-"+plan.cycle.CycleID+".json")
	if retainedRaw, readErr := readRootRegular(selectionCandidate, maximumSnapshotRuntimeSelectionBytes); readErr == nil {
		decoded, decodeErr := decodeSnapshotRuntimeSelection(retainedRaw)
		if decodeErr != nil || decoded.Schema != snapshotRuntimeSelectionSchema ||
			decoded.SnapshotEnvelopeName != filepath.Base(plan.envelopePath) || decoded.SnapshotEnvelopeSHA256 != plan.envelopeSHA256 ||
			decoded.SnapshotTrustName != plan.snapshotTrustName || decoded.SnapshotTrustSHA256 != plan.snapshotTrustSHA256 ||
			decoded.AcceptanceTrustName != plan.acceptanceTrustName || decoded.AcceptanceTrustSHA256 != plan.acceptanceTrustSHA256 ||
			decoded.AcceptanceEnvelopeName != plan.acceptanceEnvelopeName || decoded.AcceptanceEnvelopeSHA256 != plan.acceptanceEnvelopeSHA256 ||
			decoded.AuthoritySnapshotID != r.Acceptance.AuthoritySnapshotID || decoded.AuthorityClosureSHA256 != r.Acceptance.AuthorityClosureSHA256 ||
			decoded.PredecessorSelectionSHA256 != plan.predecessorSelectionSHA256 ||
			!r.activationReceiptIsExact(decoded.Activation, plan, time.Now().UTC()) {
			return errors.New("retained cycle selector candidate conflicts with the exact immutable activation plan")
		}
		selection = decoded
		selectionRaw = retainedRaw
		receipt = decoded.Activation
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return readErr
	}
	receiptRaw, err := json.Marshal(receipt)
	if err != nil || len(receiptRaw) > maximumSnapshotActivationReceiptBytes {
		if err == nil {
			err = errors.New("snapshot activation receipt exceeds its accepted exact bound")
		}
		return err
	}
	if err := r.appendEvidence("snapshot-activation-receipt-"+digest(receiptRaw)+".json", receiptRaw); err != nil {
		return err
	}
	if priorRaw, readErr := readRootRegular(plan.selectionPath, maximumSnapshotRuntimeSelectionBytes); readErr == nil {
		if bytes.Equal(priorRaw, selectionRaw) {
			return nil
		}
		if err := r.appendEvidence("snapshot-runtime-selection-"+digest(priorRaw)+".json", priorRaw); err != nil {
			return err
		}
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return readErr
	}
	if err := r.appendEvidence("snapshot-runtime-selection-"+digest(selectionRaw)+".json", selectionRaw); err != nil {
		return err
	}
	if err := r.appendRuntimeOrMatch(selectionCandidate, selectionRaw, 0o444); err != nil {
		return err
	}
	if time.Until(deadlineAt) <= snapshotActivationSafetyMargin {
		return errors.New("snapshot envelope and complete runtime selector were retained, but publication was refused at the cycle deadline")
	}
	currentRaw, readErr := readRootRegular(plan.selectionPath, maximumSnapshotRuntimeSelectionBytes)
	if plan.predecessorSelectionSHA256 == "" {
		if readErr == nil || !errors.Is(readErr, os.ErrNotExist) {
			return errors.New("snapshot selection genesis CAS lost to another committed generation")
		}
	} else if readErr != nil || digest(currentRaw) != plan.predecessorSelectionSHA256 {
		return errors.New("snapshot selection predecessor changed before atomic commit")
	}
	if err := r.revalidateLegacyBootstrapForCommit(plan, currentRaw, time.Now().UTC().Truncate(time.Second)); err != nil {
		return err
	}
	if err := validateRuntimeCapacity(r.Config, 0, 0); err != nil {
		return err
	}
	// The successor link is the one atomic, cross-process commit. Its contents
	// are the complete selector (including the signed-envelope-bound predecessor),
	// not a reservation marker. A crash before Link leaves the predecessor free;
	// a crash after Link exposes a complete successor that readers can follow.
	successorPath, err := r.snapshotSuccessorPath(plan.predecessorSelectionSHA256)
	if err != nil {
		return err
	}
	if err := os.Link(selectionCandidate, successorPath); err != nil {
		if !errors.Is(err, os.ErrExist) {
			return err
		}
		retained, readErr := readRootRegular(successorPath, maximumSnapshotRuntimeSelectionBytes)
		if readErr != nil || !bytes.Equal(retained, selectionRaw) {
			return errors.New("snapshot selection predecessor already has a different committed successor")
		}
	}
	if err := fsyncDirectory(r.Config.RuntimeRoot); err != nil {
		return err
	}
	// The fixed selector is only a bounded lookup accelerator. Every overwritten
	// generation remains linked by its immutable successor path and in evidence.
	// Readers follow successors, so a crash before this rename still observes the
	// committed head and a crash after it observes the same complete selector.
	if err := os.Rename(selectionCandidate, plan.selectionPath); err != nil {
		return fmt.Errorf("advance runtime selection accelerator after successor commit: %w", err)
	}
	if err := fsyncDirectory(r.Config.RuntimeRoot); err != nil {
		return err
	}
	return nil
}

func (r *Runner) revalidateLegacyBootstrapForCommit(
	plan snapshotActivationPlan,
	predecessorRaw []byte,
	now time.Time,
) error {
	if plan.legacyBootstrapEnvelopeSHA256 == "" && plan.legacyBootstrapExpiresAt == "" {
		return nil
	}
	if !isDigest(plan.legacyBootstrapEnvelopeSHA256) || plan.legacyBootstrapExpiresAt == "" ||
		digest(predecessorRaw) != plan.predecessorSelectionSHA256 {
		return errors.New("snapshot activation plan lost its exact legacy bootstrap authorization")
	}
	bootstrap, bootstrapRaw, _, err := r.legacyRuntimeBootstrap(predecessorRaw, now)
	if err != nil || digest(bootstrapRaw) != plan.legacyBootstrapEnvelopeSHA256 ||
		bootstrap.ExpiresAt != plan.legacyBootstrapExpiresAt || !bootstrap.Current(now) ||
		bootstrap.RequireExactAcceptance(r.Acceptance) != nil {
		return errors.New("legacy bootstrap changed or expired before atomic successor publication")
	}
	expiresAt, err := time.Parse(time.RFC3339, bootstrap.ExpiresAt)
	if err != nil || expiresAt.Sub(now) <= snapshotActivationSafetyMargin {
		return errors.New("legacy bootstrap cannot cover the atomic successor publication safety margin")
	}
	return nil
}

// archiveRuntimeSnapshotLink preserves the exact retained inode, not merely a
// byte copy. This is required for legacy active/previous slots that predate the
// append-only attempt namespace and may otherwise have no second link when a
// fixed publication name is rotated.
func (r *Runner) archiveRuntimeSnapshotLink(slot string, path string) (string, error) {
	if slot != "active" && slot != "previous" {
		return "", errors.New("runtime snapshot archive slot is unsupported")
	}
	raw, err := readRootRegular(path, maximumEnvelopeBytes)
	if err != nil {
		return "", err
	}
	if err := r.appendEvidence("snapshot-envelope-"+digest(raw)+".json", raw); err != nil {
		return "", err
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() {
		return "", errors.New("runtime snapshot archive source is not a regular retained inode")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 {
		return "", errors.New("runtime snapshot archive source lacks root custody")
	}
	if err := r.ensureRuntimeChildDirectory("snapshot-history"); err != nil {
		return "", err
	}
	historyRoot := filepath.Join(r.Config.RuntimeRoot, "snapshot-history")
	historyName := fmt.Sprintf("%s-%s-%d-%d.json", slot, digest(raw), stat.Dev, stat.Ino)
	historyPath := filepath.Join(historyRoot, historyName)
	if err := validateRuntimeCapacity(r.Config, r.Config.RuntimeFilesystemBlockBytes, 0); err != nil {
		return "", err
	}
	if err := os.Link(path, historyPath); err != nil {
		if !errors.Is(err, os.ErrExist) {
			return "", err
		}
		historyInfo, historyErr := os.Lstat(historyPath)
		if historyErr != nil || !os.SameFile(info, historyInfo) {
			return "", errors.New("runtime snapshot immutable history conflicts with the retained inode")
		}
	}
	if err := fsyncDirectory(historyRoot); err != nil {
		return "", err
	}
	return historyPath, nil
}

func validateConfig(config Config, acceptance boundary.Acceptance) error {
	if config.Schema != ConfigSchema || config.ClusterID != acceptance.ClusterID || config.DeploymentID != acceptance.DeploymentID ||
		!boundedProtocolText(config.ClusterID, maximumClusterIDBytes, false) || !boundedProtocolText(config.DeploymentID, maximumDeploymentIDBytes, false) ||
		!boundedProtocolText(config.SnapshotAuthorityURL, maximumNativeURLBytes, false) ||
		!boundedProtocolText(config.SnapshotAuthorityName, maximumServerNameBytes, false) ||
		!boundedProtocolText(config.SnapshotClientSPIFFEURI, maximumSPIFFEURIBytes, false) ||
		!boundedProtocolText(config.SnapshotCredentialLaneID, maximumCredentialLaneIDBytes, false) ||
		config.MaximumBundleBytes < 1024 || config.MaximumBundleBytes > 256*1024*1024 || len(config.Sources) == 0 ||
		!isDigest(config.EvidenceStoreID) || config.RefreshIntervalSeconds < 240 || config.RefreshIntervalSeconds > 270 ||
		config.CollectionDeadlineSeconds < 30 || config.CollectionDeadlineSeconds >= config.RefreshIntervalSeconds ||
		config.MaximumConcurrentSources < 1 || config.MaximumConcurrentSources > 8 ||
		config.EvidenceDeviceID == 0 || config.EvidenceOperatingHorizonDays < 365 || config.EvidenceOperatingHorizonDays > 3660 ||
		config.EvidenceCapacityBytes == 0 || config.EvidenceMinimumFreeBytes < 64*1024*1024 ||
		config.EvidenceCapacityInodes == 0 || config.EvidenceMinimumFreeInodes < 1024 ||
		config.RuntimeDeviceID == 0 || config.RuntimeDeviceID == config.EvidenceDeviceID ||
		config.RuntimeOperatingHorizonDays < 365 || config.RuntimeOperatingHorizonDays > 3660 ||
		config.RuntimeCapacityBytes == 0 || config.RuntimeMinimumFreeBytes < 64*1024*1024 ||
		config.RuntimeCapacityInodes == 0 || config.RuntimeMinimumFreeInodes < 1024 ||
		config.RuntimeFilesystemBlockBytes < 512 || config.RuntimeFilesystemBlockBytes > 1024*1024 ||
		config.RuntimeFilesystemBlockBytes&(config.RuntimeFilesystemBlockBytes-1) != 0 ||
		config.RuntimeReaderGID == 0 || config.RuntimeReaderGID != acceptance.BoundaryRuntimeReaderGID || config.RuntimeDirectoryMode != 0o750 ||
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
		if !boundedProtocolText(source.ID, maximumSourceIDBytes, false) || !boundedProtocolText(source.Kind, maximumSourceKindBytes, false) ||
			!boundedProtocolText(source.SemanticCollection, maximumSemanticCollectionBytes, false) ||
			!boundedProtocolText(source.InitialURL, maximumNativeURLBytes, false) || !boundedProtocolText(source.ServerName, maximumServerNameBytes, false) ||
			!boundedProtocolText(source.AttestationURL, maximumNativeURLBytes, false) || !boundedProtocolText(source.AttestationServerName, maximumServerNameBytes, false) ||
			!boundedProtocolText(source.ProviderRequestIDHeader, maximumProviderRequestHeaderBytes, false) ||
			!boundedProtocolText(source.AttestationClientSPIFFEURI, maximumSPIFFEURIBytes, false) ||
			!boundedProtocolText(source.AttestationCredentialLaneID, maximumCredentialLaneIDBytes, false) ||
			!boundedProtocolText(source.ProviderCredentialLaneID, maximumCredentialLaneIDBytes, false) ||
			source.MaximumPages < 1 || source.MaximumPages > 1024 ||
			source.MaximumPageBytes < 1024 || source.MaximumPageBytes > 32*1024*1024 ||
			source.MaximumTotalBytes < source.MaximumPageBytes || source.MaximumTotalBytes > 128*1024*1024 ||
			(source.Pagination != "none" && source.Pagination != "kubernetes-continue" && source.Pagination != "provider-page-token") ||
			!isDigest(source.AttestationCABundleSHA256) || !isDigest(source.AttestationClientCertificateSHA256) ||
			!isDigest(source.AttestationClientKeySHA256) || !isDigest(source.AttestationClientSPKISHA256) || source.AttestationClientSPIFFEURI == "" ||
			source.AttestationCredentialLaneID == "" || strings.ContainsAny(source.AttestationCredentialLaneID, "\x00\r\n") {
			return errors.New("native source is incomplete or outside its bounds")
		}
		bearerFields := []bool{
			source.ProviderBearerIssuer != "",
			source.ProviderBearerAudience != "",
			source.ProviderBearerSubject != "",
			source.ProviderBearerAlgorithm != "",
			source.ProviderBearerKeyID != "",
			source.ProviderBearerJWKSHA256 != "",
		}
		bearerPresent := bearerFields[0]
		for _, present := range bearerFields {
			if present != bearerPresent {
				return errors.New("native source provider bearer identity is only partially enrolled")
			}
		}
		if bearerPresent && (!boundedProtocolText(source.ProviderBearerIssuer, maximumBearerClaimBytes, false) ||
			!boundedProtocolText(source.ProviderBearerAudience, maximumBearerClaimBytes, false) ||
			!boundedProtocolText(source.ProviderBearerSubject, maximumBearerClaimBytes, false) ||
			!boundedProtocolText(source.ProviderBearerAlgorithm, maximumBearerAlgorithmBytes, false) ||
			!boundedProtocolText(source.ProviderBearerKeyID, maximumBearerKeyIDBytes, false) ||
			!isDigest(source.ProviderBearerJWKSHA256)) {
			return errors.New("native source provider bearer identity exceeds its exact accepted bounds")
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
		collections = append(collections, source.SemanticCollection)
	}
	sort.Strings(collections)
	if !containsAllStrings(collections, requiredCollections) || !equalStringLists(collections, config.MandatoryCollections) {
		return errors.New("native collector omits or duplicates a mandatory semantic collection")
	}
	cycle, err := collectorCycleFor(
		acceptance,
		config.RefreshIntervalSeconds,
		config.CollectionDeadlineSeconds,
		collectorSourceIDs(config.Sources),
		time.Unix(0, 0).UTC(),
	)
	cycleRaw, cycleMarshalErr := json.Marshal(cycle)
	if err != nil || cycleMarshalErr != nil || len(cycleRaw) > maximumCollectorCycleBytes {
		return errors.New("native collector source inventory cannot fit its accepted canonical cycle bound")
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
	runtimeHorizonBytes, runtimeHorizonInodes, err := derivedRuntimeHorizonBounds(config)
	if err != nil || runtimeHorizonBytes > ^uint64(0)-config.RuntimeMinimumFreeBytes ||
		runtimeHorizonInodes > ^uint64(0)-config.RuntimeMinimumFreeInodes ||
		config.RuntimeCapacityBytes < runtimeHorizonBytes+config.RuntimeMinimumFreeBytes ||
		config.RuntimeCapacityInodes < runtimeHorizonInodes+config.RuntimeMinimumFreeInodes {
		return errors.New("native runtime store cannot retain its source-derived byte and inode operating horizon")
	}
	return nil
}

func derivedEvidenceDailyBounds(config Config) (uint64, uint64, error) {
	cycles := uint64((86400 + config.RefreshIntervalSeconds - 1) / config.RefreshIntervalSeconds)
	perCycleBytes := uint64(config.MaximumBundleBytes + maximumEnvelopeBytes + maximumCollectorCycleBytes +
		2*maximumConfigBytes + maximumSnapshotActivationReceiptBytes + maximumSnapshotRuntimeSelectionBytes)
	perCycleInodes := uint64(7)
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

func runtimePerCycleStorageBounds(config Config) (uint64, uint64, error) {
	block := config.RuntimeFilesystemBlockBytes
	envelopeBytes, err := runtimeAllocatedBytes(maximumEnvelopeBytes, block)
	if err != nil {
		return 0, 0, err
	}
	selectionBytes, err := runtimeAllocatedBytes(maximumSnapshotRuntimeSelectionBytes, block)
	if err != nil {
		return 0, 0, err
	}
	authorityGenerationBytes, err := runtimeAllocatedBytes(maximumConfigBytes, block)
	if err != nil {
		return 0, 0, err
	}
	// Each cycle retains an immutable envelope generation and one complete
	// envelope/receipt selector. Keep a third inode in the bound for a failed
	// selector attempt. Their attempt/fixed hard links do not allocate more
	// inodes, but conservatively charge one full directory block for each of the
	// eight possible new directory entries and retained retry aliases.
	objectBytes, ok := checkedMultiply(envelopeBytes, 2)
	if !ok {
		return 0, 0, errors.New("native runtime envelope storage bound overflows")
	}
	objectBytes, ok = checkedAdd(objectBytes, selectionBytes)
	if !ok {
		return 0, 0, errors.New("native runtime receipt storage bound overflows")
	}
	authorityBytes, ok := checkedMultiply(authorityGenerationBytes, 3)
	if !ok {
		return 0, 0, errors.New("native runtime authority generation bound overflows")
	}
	objectBytes, ok = checkedAdd(objectBytes, authorityBytes)
	if !ok {
		return 0, 0, errors.New("native runtime generation storage bound overflows")
	}
	entryBytes, ok := checkedMultiply(block, 14)
	if !ok {
		return 0, 0, errors.New("native runtime directory-entry bound overflows")
	}
	total, ok := checkedAdd(objectBytes, entryBytes)
	if !ok {
		return 0, 0, errors.New("native runtime per-cycle byte bound overflows")
	}
	return total, 6, nil
}

func derivedRuntimeHorizonBounds(config Config) (uint64, uint64, error) {
	perCycleBytes, perCycleInodes, err := runtimePerCycleStorageBounds(config)
	if err != nil {
		return 0, 0, err
	}
	cyclesPerDay := uint64((86400 + config.RefreshIntervalSeconds - 1) / config.RefreshIntervalSeconds)
	cycles, ok := checkedMultiply(cyclesPerDay, uint64(config.RuntimeOperatingHorizonDays))
	if !ok {
		return 0, 0, errors.New("native runtime cycle horizon overflows")
	}
	horizonBytes, ok := checkedMultiply(perCycleBytes, cycles)
	if !ok {
		return 0, 0, errors.New("native runtime byte horizon overflows")
	}
	horizonInodes, ok := checkedMultiply(perCycleInodes, cycles)
	if !ok {
		return 0, 0, errors.New("native runtime inode horizon overflows")
	}
	trustBytes, err := runtimeAllocatedBytes(maximumConfigBytes, config.RuntimeFilesystemBlockBytes)
	if err != nil {
		return 0, 0, err
	}
	bootstrapObjects := uint64(2*boundary.MaximumLegacyRuntimeBootstrapGeneration + 2)
	bootstrapBytes, ok := checkedMultiply(trustBytes, bootstrapObjects)
	if !ok {
		return 0, 0, errors.New("native runtime bootstrap byte bound overflows")
	}
	// Fixed storage is the legacy cached-trust inode, one signed legacy
	// bootstrap, one separately retained legacy trust generation, the root attempt
	// directory, and the snapshot, snapshot-trust, acceptance-trust and
	// acceptance-envelope generation directories plus their private attempt
	// directories. Extra directory blocks conservatively cover their root
	// entries and the immutable successor namespace.
	fixedDirectoryEntries := uint64(20 + 2*boundary.MaximumLegacyRuntimeBootstrapGeneration)
	fixedDirectoryBytes, ok := checkedMultiply(config.RuntimeFilesystemBlockBytes, fixedDirectoryEntries)
	if !ok {
		return 0, 0, errors.New("native runtime fixed directory bound overflows")
	}
	fixedBytes, ok := checkedAdd(bootstrapBytes, fixedDirectoryBytes)
	if !ok {
		return 0, 0, errors.New("native runtime fixed byte bound overflows")
	}
	horizonBytes, ok = checkedAdd(horizonBytes, fixedBytes)
	fixedInodes := uint64(16 + 2*boundary.MaximumLegacyRuntimeBootstrapGeneration)
	if !ok || horizonInodes > ^uint64(0)-fixedInodes {
		return 0, 0, errors.New("native runtime fixed horizon overflows")
	}
	return horizonBytes, horizonInodes + fixedInodes, nil
}

func runtimeAllocatedBytes(size int, block uint64) (uint64, error) {
	if size < 1 || block == 0 {
		return 0, errors.New("native runtime storage size or block bound is invalid")
	}
	value := uint64(size)
	if value > ^uint64(0)-(block-1) {
		return 0, errors.New("native runtime allocation bound overflows")
	}
	return ((value + block - 1) / block) * block, nil
}

func maximumEvidenceManifestBytes(config Config) (uint64, error) {
	bound, ok := checkedAdd(4096, maximumCollectorCycleBytes)
	if !ok {
		return 0, errors.New("native evidence cycle framing bound overflows")
	}
	for _, source := range config.Sources {
		rawEnvelopeBytes, ok := checkedMultiply(uint64(source.MaximumPageBytes), 2)
		if !ok {
			return 0, errors.New("native inline envelope bound overflows")
		}
		rawEnvelopeBytes, ok = checkedAdd(rawEnvelopeBytes, 1024*1024)
		if !ok {
			return 0, errors.New("native inline envelope framing bound overflows")
		}
		inlineBytes, ok := base64EncodedBound(rawEnvelopeBytes)
		if !ok {
			return 0, errors.New("native inline envelope encoding bound overflows")
		}
		pageUnitBytes, ok := checkedAdd(inlineBytes, maximumEvidenceReferenceBytes)
		if !ok {
			return 0, errors.New("native inline page reference bound overflows")
		}
		pageBytes, ok := checkedMultiply(uint64(source.MaximumPages), pageUnitBytes)
		if !ok {
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
		"apiserver-backend-tls-policies",
		"apiserver-backend-traffic-policies",
		"apiserver-clusterrolebindings",
		"apiserver-clusterroles",
		"apiserver-client-traffic-policies",
		"apiserver-crds",
		"apiserver-csrs",
		"apiserver-daemonsets",
		"apiserver-deployments",
		"apiserver-endpoints",
		"apiserver-endpointslices",
		"apiserver-envoy-backends",
		"apiserver-envoy-extension-policies",
		"apiserver-gatewayclasses",
		"apiserver-gateways",
		"apiserver-grpcroutes",
		"apiserver-httproutes",
		"apiserver-ingresses",
		"apiserver-jobs",
		"apiserver-cronjobs",
		"apiserver-csidrivers",
		"apiserver-csinodes",
		"apiserver-nodes",
		"apiserver-namespaces",
		"apiserver-networkpolicies",
		"apiserver-persistentvolumeclaims",
		"apiserver-persistentvolumes",
		"apiserver-poddisruptionbudgets",
		"apiserver-pods",
		"apiserver-replicationcontrollers",
		"apiserver-replicasets",
		"apiserver-referencegrants",
		"apiserver-rolebindings",
		"apiserver-roles",
		"apiserver-secrets-metadata",
		"apiserver-security-policies",
		"apiserver-serviceaccounts",
		"apiserver-services",
		"apiserver-statefulsets",
		"apiserver-storageclasses",
		"apiserver-tcproutes",
		"apiserver-tlsroutes",
		"apiserver-udproutes",
		"apiserver-volumeattachments",
		"ca-issued-credentials",
		"ca-revocation-status",
		"provider-iam-bindings",
		"provider-iam-policies",
		"provider-load-balancer-backends",
		"provider-load-balancer-health-checks",
		"provider-load-balancer-listeners",
		"provider-load-balancers",
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
	cycleIssuedAt, issueErr := time.Parse(time.RFC3339, directive.CycleIssuedAt)
	cycleDeadlineAt, deadlineErr := time.Parse(time.RFC3339, directive.CycleDeadlineAt)
	if err != nil || collectedAt.Nanosecond() != 0 || !strings.HasSuffix(page.CollectedAt, "Z") ||
		issueErr != nil || deadlineErr != nil || collectedAt.Before(cycleIssuedAt) || !collectedAt.Before(cycleDeadlineAt) ||
		collectedAt.After(now.Add(30*time.Second)) {
		return errors.New("native response timestamp is absent or stale")
	}
	expectedContentType := "application/json"
	if source.Kind == "ca-revocation" {
		expectedContentType = "application/pkix-crl"
	}
	if page.Schema != NativePageSchema || page.SourceID != source.ID || page.SourceKind != source.Kind ||
		page.SemanticCollection != source.SemanticCollection || page.RequestID != providerRequest.RequestID ||
		page.CycleContractSHA256 != directive.CycleContractSHA256 || page.CycleID != directive.CycleID ||
		page.CycleIssuedAt != directive.CycleIssuedAt || page.CycleDeadlineAt != directive.CycleDeadlineAt ||
		!boundedProtocolText(page.ProviderRequestID, maximumProviderRequestIDBytes, false) || page.SessionID != directive.SessionID ||
		!boundedProtocolText(page.RequestedURL, maximumNativeURLBytes, false) ||
		!boundedProtocolText(page.NextURL, maximumNativeURLBytes, true) || !boundedProtocolText(page.NextToken, maximumPageTokenBytes, true) ||
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
		providerRequest.Headers["x-fs2-request-id"] != providerRequest.RequestID ||
		providerRequest.CredentialLaneID != source.ProviderCredentialLaneID ||
		providerRequest.CredentialIssuer != source.ProviderBearerIssuer ||
		providerRequest.CredentialAudience != source.ProviderBearerAudience ||
		providerRequest.CredentialSubject != source.ProviderBearerSubject ||
		providerRequest.CredentialAlgorithm != source.ProviderBearerAlgorithm ||
		providerRequest.CredentialKeyID != source.ProviderBearerKeyID ||
		providerRequest.CredentialJWKSHA256 != source.ProviderBearerJWKSHA256 {
		return errors.New("signed native provider request projection is incomplete or inconsistent")
	}
	if source.ProviderBearerIssuer == "" {
		if providerRequest.CredentialExpiresAt != "" || providerRequest.CredentialGeneration != "" {
			return errors.New("non-bearer native provider request carries an unenrolled credential identity")
		}
	} else {
		expiresAt, expiryErr := time.Parse(time.RFC3339, providerRequest.CredentialExpiresAt)
		if expiryErr != nil || expiresAt.Nanosecond() != 0 || !isDigest(providerRequest.CredentialGeneration) ||
			!collectedAt.Before(expiresAt) {
			return errors.New("signed native provider request carries an invalid or expired verified bearer generation")
		}
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

func validateRuntimeCapacity(config Config, upcomingBytes uint64, upcomingInodes uint64) error {
	rootInfo, err := os.Lstat(config.RuntimeRoot)
	if err != nil {
		return err
	}
	parentInfo, err := os.Lstat(filepath.Dir(config.RuntimeRoot))
	if err != nil {
		return err
	}
	rootStat, rootOK := rootInfo.Sys().(*syscall.Stat_t)
	parentStat, parentOK := parentInfo.Sys().(*syscall.Stat_t)
	if !rootOK || !parentOK || rootStat.Dev != config.RuntimeDeviceID || rootStat.Dev == parentStat.Dev ||
		rootStat.Dev == config.EvidenceDeviceID || rootStat.Uid != 0 || rootStat.Gid != config.RuntimeReaderGID ||
		rootInfo.Mode().Perm() != os.FileMode(config.RuntimeDirectoryMode) || rootStat.Mode&syscall.S_ISGID == 0 {
		return errors.New("native runtime root is not its independently accepted dedicated filesystem")
	}
	var status syscall.Statfs_t
	if err := syscall.Statfs(config.RuntimeRoot, &status); err != nil {
		return err
	}
	blockSize := uint64(status.Bsize)
	if blockSize != config.RuntimeFilesystemBlockBytes || status.Blocks > ^uint64(0)/blockSize ||
		status.Bavail > ^uint64(0)/blockSize || upcomingBytes > ^uint64(0)-config.RuntimeMinimumFreeBytes ||
		upcomingInodes > ^uint64(0)-config.RuntimeMinimumFreeInodes {
		return errors.New("native runtime filesystem identity or capacity cannot be represented safely")
	}
	totalBytes := status.Blocks * blockSize
	availableBytes := status.Bavail * blockSize
	if totalBytes < config.RuntimeCapacityBytes || uint64(status.Files) < config.RuntimeCapacityInodes ||
		availableBytes < config.RuntimeMinimumFreeBytes+upcomingBytes ||
		uint64(status.Ffree) < config.RuntimeMinimumFreeInodes+upcomingInodes {
		return errors.New("native runtime store cannot preserve its accepted byte and inode reserve")
	}
	return nil
}

func (r *Runner) appendRuntimeOrMatch(path string, raw []byte, mode os.FileMode) error {
	parent := filepath.Dir(path)
	allowedParents := map[string]struct{}{
		r.Config.RuntimeRoot: {},
		filepath.Join(r.Config.RuntimeRoot, "snapshot-generations"): {},
		filepath.Join(r.Config.RuntimeRoot, "snapshot-trust-generations"): {},
		filepath.Join(r.Config.RuntimeRoot, "acceptance-trust-generations"): {},
		filepath.Join(r.Config.RuntimeRoot, "acceptance-envelope-generations"): {},
		filepath.Join(r.Config.RuntimeRoot, "legacy-bootstrap-generations"): {},
	}
	if _, allowed := allowedParents[parent]; !allowed {
		return errors.New("runtime append target escaped the accepted runtime root")
	}
	if existing, err := readRootRegular(path, int64(len(raw))); err == nil {
		if !bytes.Equal(existing, raw) {
			return errors.New("runtime append target contains different retained bytes")
		}
		if err := validateRuntimeCapacity(r.Config, 0, 0); err != nil {
			return err
		}
		return fsyncDirectory(parent)
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	attemptRoot := filepath.Join(parent, ".attempts")
	attemptPath := filepath.Join(attemptRoot, filepath.Base(path)+"-"+digest(raw))
	upcomingBytes, upcomingInodes := uint64(0), uint64(0)
	if _, err := os.Lstat(attemptRoot); errors.Is(err, os.ErrNotExist) {
		upcomingBytes += r.Config.RuntimeFilesystemBlockBytes
		upcomingInodes++
	} else if err != nil {
		return err
	}
	if _, err := os.Lstat(attemptPath); errors.Is(err, os.ErrNotExist) {
		allocated, allocationErr := runtimeAllocatedBytes(len(raw), r.Config.RuntimeFilesystemBlockBytes)
		if allocationErr != nil {
			return allocationErr
		}
		upcomingBytes += allocated
		upcomingInodes++
	} else if err != nil {
		return err
	}
	// Reserve one full block for the new final directory entry even though its
	// inode is a hard link to the retained attempt.
	if upcomingBytes > ^uint64(0)-r.Config.RuntimeFilesystemBlockBytes {
		return errors.New("runtime publication directory bound overflows")
	}
	upcomingBytes += r.Config.RuntimeFilesystemBlockBytes
	if err := validateRuntimeCapacity(r.Config, upcomingBytes, upcomingInodes); err != nil {
		return err
	}
	return appendOrMatch(path, raw, mode)
}

func (r *Runner) ensureRuntimeChildDirectory(name string) error {
	if filepath.Base(name) != name || name == "." || name == "" {
		return errors.New("runtime child directory name is invalid")
	}
	path := filepath.Join(r.Config.RuntimeRoot, name)
	if _, err := os.Lstat(path); errors.Is(err, os.ErrNotExist) {
		if err := validateRuntimeCapacity(r.Config, r.Config.RuntimeFilesystemBlockBytes, 1); err != nil {
			return err
		}
		if err := os.Mkdir(path, os.FileMode(r.Config.RuntimeDirectoryMode)); err != nil && !errors.Is(err, os.ErrExist) {
			return err
		}
		if err := os.Chown(path, 0, int(r.Config.RuntimeReaderGID)); err != nil {
			return err
		}
		if err := os.Chmod(path, os.FileMode(r.Config.RuntimeDirectoryMode)|os.ModeSetgid); err != nil {
			return err
		}
	} else if err != nil {
		return err
	} else if err := validateRuntimeCapacity(r.Config, 0, 0); err != nil {
		return err
	}
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 ||
		info.Mode().Perm() != os.FileMode(r.Config.RuntimeDirectoryMode) {
		return errors.New("runtime reader directory mode differs from its accepted contract")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 || stat.Gid != r.Config.RuntimeReaderGID || stat.Mode&syscall.S_ISGID == 0 {
		return errors.New("runtime reader directory custody differs from its accepted root/setgid identity")
	}
	return fsyncDirectory(r.Config.RuntimeRoot)
}

func appendOrMatch(path string, raw []byte, mode os.FileMode) error {
	attemptPath, err := stageAppendOnlyAttempt(path, raw, mode)
	if err != nil {
		return err
	}
	return publishStagedAttempt(path, attemptPath, raw, mode)
}

func stageAppendOnlyAttempt(path string, raw []byte, mode os.FileMode) (string, error) {
	parent := filepath.Dir(path)
	if err := requireRootOwnedDirectory(parent); err != nil {
		return "", err
	}
	if _, err := os.Lstat(path); err == nil {
		existing, readErr := readRootRegular(path, int64(len(raw)))
		if readErr != nil || !bytes.Equal(existing, raw) {
			return "", errors.New("append-only evidence path already contains different bytes")
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	if err := ensureRootOwnedChildDirectory(parent, ".attempts"); err != nil {
		return "", err
	}
	attemptPath := filepath.Join(parent, ".attempts", filepath.Base(path)+"-"+digest(raw))
	fileDescriptor, err := syscall.Open(attemptPath, syscall.O_RDWR|syscall.O_CREAT|syscall.O_EXCL|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, uint32(mode.Perm()))
	if errors.Is(err, syscall.EEXIST) {
		fileDescriptor, err = syscall.Open(attemptPath, syscall.O_RDWR|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	}
	if err != nil {
		return "", err
	}
	file := os.NewFile(uintptr(fileDescriptor), attemptPath)
	info, statErr := file.Stat()
	if statErr != nil || info == nil {
		_ = file.Close()
		return "", errors.New("append-only evidence attempt cannot be inspected")
	}
	stat, statOK := info.Sys().(*syscall.Stat_t)
	if !statOK || stat.Uid != 0 || !info.Mode().IsRegular() || info.Mode().Perm() != mode.Perm() || info.Size() < 0 || info.Size() > int64(len(raw)) {
		_ = file.Close()
		return "", errors.New("append-only evidence attempt is not the exact resumable root-owned file")
	}
	written := int(info.Size())
	if written > 0 {
		prefix := make([]byte, written)
		count, readErr := file.ReadAt(prefix, 0)
		if readErr != nil || count != written || !bytes.Equal(prefix, raw[:written]) {
			_ = file.Close()
			return "", errors.New("append-only evidence attempt prefix differs from intended bytes")
		}
	}
	if _, err := file.Seek(int64(written), io.SeekStart); err != nil {
		_ = file.Close()
		return "", err
	}
	for written < len(raw) {
		count, writeErr := file.Write(raw[written:])
		if writeErr != nil || count < 1 {
			_ = file.Close()
			if writeErr != nil {
				return "", writeErr
			}
			return "", errors.New("append-only evidence attempt made no write progress")
		}
		written += count
	}
	if written != len(raw) {
		_ = file.Close()
		return "", errors.New("append-only evidence attempt was short-written")
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return "", err
	}
	if err := file.Close(); err != nil {
		return "", err
	}
	if err := fsyncDirectory(parent); err != nil {
		return "", err
	}
	return attemptPath, nil
}

func publishStagedAttempt(path string, attemptPath string, raw []byte, mode os.FileMode) error {
	attemptRaw, err := readRootRegular(attemptPath, int64(len(raw)))
	if err != nil || !bytes.Equal(attemptRaw, raw) {
		return errors.New("append-only evidence attempt differs from intended bytes")
	}
	info, err := os.Lstat(attemptPath)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != mode.Perm() {
		return errors.New("append-only evidence attempt has the wrong file mode")
	}
	if err := os.Link(attemptPath, path); err != nil {
		if !errors.Is(err, os.ErrExist) {
			return err
		}
		existing, readErr := readRootRegular(path, int64(len(raw)))
		if readErr != nil || !bytes.Equal(existing, raw) {
			return errors.New("append-only evidence publication raced with different bytes")
		}
		return fsyncDirectory(filepath.Dir(path))
	}
	return fsyncDirectory(filepath.Dir(path))
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
