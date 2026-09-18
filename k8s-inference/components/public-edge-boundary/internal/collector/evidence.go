package collector

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
)

// LoadEvidenceVerificationContract loads only the immutable source plan and
// native-response trust needed by a separately custodied snapshot authority.
// It intentionally does not open the collector's writable evidence/runtime
// stores or its client credentials.
func LoadEvidenceVerificationContract(
	configPath string,
	nativeTrustPath string,
	acceptance boundary.Acceptance,
) (Config, *boundary.ExternalTrust, error) {
	configRaw, err := readRootRegular(configPath, maximumConfigBytes)
	if err != nil {
		return Config{}, nil, fmt.Errorf("read native collector verification config: %w", err)
	}
	if digest(configRaw) != acceptance.NativeCollectorConfigSHA256 {
		return Config{}, nil, errors.New("native collector verification config differs from independently accepted bytes")
	}
	if err := rejectClosedIntegrationGate(configRaw, "native-collector"); err != nil {
		return Config{}, nil, err
	}
	var discriminator struct {
		Schema string `json:"schema"`
	}
	if err := json.Unmarshal(configRaw, &discriminator); err == nil && discriminator.Schema != ConfigSchema {
		return Config{}, nil, errors.New("only additive native collector config v3 can authorize semantic snapshot evidence verification")
	}
	var config Config
	if err := canonicalJSON(configRaw, &config); err != nil {
		return Config{}, nil, err
	}
	if err := validateConfig(config, acceptance); err != nil {
		return Config{}, nil, err
	}
	trust, err := boundary.LoadExternalTrust(nativeTrustPath, acceptance.NativeResponseTrustSHA256, NativeTrustSchema)
	if err != nil {
		return Config{}, nil, err
	}
	return config, trust, nil
}

// VerifiedEvidenceBundle contains only pages reconstructed from exact signed
// native envelopes. The collector and the separately custodied snapshot
// authority share this verifier so a manifest digest can never replace
// reopening the provider request and response bytes.
type VerifiedEvidenceBundle struct {
	Bundle       EvidenceBundle
	Pages        map[string][]NativePage
	RawResponses map[string][][]byte
}

// VerifyEvidenceBundle reopens every bounded inline native envelope and proves
// the complete request, response, pagination and collection-cycle chain. The
// caller must load config and nativeTrust from the exact acceptance-bound bytes;
// this function independently rederives the cycle from those inputs.
func VerifyEvidenceBundle(
	bundleRaw []byte,
	config Config,
	acceptance boundary.Acceptance,
	nativeTrust *boundary.ExternalTrust,
	now time.Time,
) (*VerifiedEvidenceBundle, error) {
	if len(bundleRaw) < 1 || int64(len(bundleRaw)) > config.MaximumBundleBytes || nativeTrust == nil ||
		(config.Schema != ConfigSchema && config.Schema != PreviousConfigSchema) || config.ClusterID != acceptance.ClusterID ||
		config.DeploymentID != acceptance.DeploymentID || config.EvidenceStoreID == "" ||
		digestJSON(config.MandatoryCollections) != acceptance.MandatoryCollectionsSHA256 {
		return nil, errors.New("native evidence verifier inputs differ from the accepted collector contract")
	}
	if err := validateConfig(config, acceptance); err != nil {
		return nil, fmt.Errorf("validate exact versioned native collector contract: %w", err)
	}
	var bundle EvidenceBundle
	if err := canonicalJSON(bundleRaw, &bundle); err != nil || bundle.Schema != EvidenceBundleSchema ||
		bundle.ClusterID != config.ClusterID || bundle.DeploymentID != config.DeploymentID ||
		bundle.EvidenceStoreID != config.EvidenceStoreID ||
		bundle.CycleContractSHA256 != bundle.Cycle.ContractSHA256 || bundle.CycleID != bundle.Cycle.CycleID ||
		bundle.CycleIssuedAt != bundle.Cycle.IssuedAt || bundle.CycleDeadlineAt != bundle.Cycle.DeadlineAt ||
		bundle.RefreshIntervalSeconds != config.RefreshIntervalSeconds ||
		bundle.CollectionDeadlineSeconds != config.CollectionDeadlineSeconds ||
		(bundle.ActivationPredecessorSelectionSHA256 != "" && !isDigest(bundle.ActivationPredecessorSelectionSHA256)) {
		return nil, errors.New("native evidence bundle identity or collection cycle is invalid")
	}
	cycleIssuedAt, issueErr := time.Parse(time.RFC3339, bundle.CycleIssuedAt)
	cycleDeadlineAt, deadlineErr := time.Parse(time.RFC3339, bundle.CycleDeadlineAt)
	expectedCycle, cycleErr := collectorCycleFor(
		acceptance,
		config.RefreshIntervalSeconds,
		config.CollectionDeadlineSeconds,
		collectorSourceIDs(config.Sources),
		cycleIssuedAt,
	)
	expectedCycleRaw, expectedMarshalErr := json.Marshal(expectedCycle)
	actualCycleRaw, actualMarshalErr := json.Marshal(bundle.Cycle)
	now = now.UTC().Truncate(time.Second)
	if issueErr != nil || deadlineErr != nil || cycleErr != nil || expectedMarshalErr != nil || actualMarshalErr != nil ||
		cycleIssuedAt.Nanosecond() != 0 || cycleDeadlineAt.Nanosecond() != 0 ||
		!cycleDeadlineAt.After(cycleIssuedAt) || !now.Before(cycleDeadlineAt) ||
		!bytes.Equal(expectedCycleRaw, actualCycleRaw) || len(bundle.Sources) != len(config.Sources) {
		return nil, errors.New("native evidence bundle differs from the exact accepted source plan or active deadline")
	}
	verified := &VerifiedEvidenceBundle{
		Bundle: bundle, Pages: map[string][]NativePage{}, RawResponses: map[string][][]byte{},
	}
	for sourceIndex, source := range config.Sources {
		evidence := bundle.Sources[sourceIndex]
		if evidence.SourceID != source.ID || evidence.Kind != source.Kind ||
			evidence.SemanticCollection != source.SemanticCollection ||
			len(evidence.Pages) < 1 || len(evidence.Pages) > source.MaximumPages {
			return nil, fmt.Errorf("native evidence source %s is absent, reordered or outside its page bound", source.ID)
		}
		sessionID, err := cycleSessionID(bundle.Cycle, source.ID)
		if err != nil {
			return nil, err
		}
		pageToken := ""
		totalResponseBytes := int64(0)
		providerRequestIDs := map[string]struct{}{}
		for pageIndex, captured := range evidence.Pages {
			challenge := cycleChallenge(bundle.Cycle, source.ID, sessionID, pageIndex, pageToken)
			directive := CollectionRequest{
				Schema: CollectionRequestSchema, ClusterID: config.ClusterID, DeploymentID: config.DeploymentID,
				SourceID: source.ID, CycleContractSHA256: bundle.Cycle.ContractSHA256,
				CycleID: bundle.Cycle.CycleID, CycleIssuedAt: bundle.Cycle.IssuedAt,
				CycleDeadlineAt: bundle.Cycle.DeadlineAt, SessionID: sessionID,
				PageIndex: pageIndex, PageToken: pageToken, Challenge: challenge,
			}
			directiveRaw, err := json.Marshal(directive)
			if err != nil || captured.CycleContractSHA256 != bundle.Cycle.ContractSHA256 ||
				captured.CycleID != bundle.Cycle.CycleID || captured.CycleIssuedAt != bundle.Cycle.IssuedAt ||
				captured.CycleDeadlineAt != bundle.Cycle.DeadlineAt || captured.DirectiveSHA256 != digest(directiveRaw) ||
				captured.Page != nil || captured.EnvelopeBase64 == "" || !isDigest(captured.EnvelopeSHA256) ||
				!isDigest(captured.PayloadSHA256) ||
				captured.EnvelopeObject != "native-page-"+captured.EnvelopeSHA256+".json" {
				return nil, fmt.Errorf("native evidence source %s page %d has an ambiguous or invalid manifest binding", source.ID, pageIndex)
			}
			envelopeRaw, err := base64.StdEncoding.Strict().DecodeString(captured.EnvelopeBase64)
			maximumNativeEnvelopeBytes := source.MaximumPageBytes*2 + 1024*1024
			if err != nil || base64.StdEncoding.EncodeToString(envelopeRaw) != captured.EnvelopeBase64 ||
				int64(len(envelopeRaw)) > maximumNativeEnvelopeBytes || digest(envelopeRaw) != captured.EnvelopeSHA256 {
				return nil, fmt.Errorf("native evidence source %s page %d envelope bytes are invalid", source.ID, pageIndex)
			}
			payloadRaw, err := nativeTrust.VerifyEnvelope(NativeEnvelopeSchema, NativeIssuerRole, envelopeRaw)
			if err != nil || digest(payloadRaw) != captured.PayloadSHA256 {
				return nil, fmt.Errorf("native evidence source %s page %d signature or payload digest is invalid", source.ID, pageIndex)
			}
			var page NativePage
			if _, err := boundary.CanonicalJSON(payloadRaw, &page); err != nil ||
				page.Schema != NativePageSchema || page.SourceID != source.ID || page.SourceKind != source.Kind ||
				page.SemanticCollection != source.SemanticCollection || page.CycleContractSHA256 != bundle.Cycle.ContractSHA256 ||
				page.CycleID != bundle.Cycle.CycleID || page.CycleIssuedAt != bundle.Cycle.IssuedAt ||
				page.CycleDeadlineAt != bundle.Cycle.DeadlineAt || page.SessionID != sessionID ||
				page.Challenge != challenge || page.PageIndex != pageIndex || page.CollectedAt != captured.CollectedAt {
				return nil, fmt.Errorf("native evidence source %s page %d does not bind its signed cycle directive", source.ID, pageIndex)
			}
			if _, duplicate := providerRequestIDs[page.ProviderRequestID]; duplicate {
				return nil, fmt.Errorf("native evidence source %s repeats a provider request identifier", source.ID)
			}
			providerRequestIDs[page.ProviderRequestID] = struct{}{}
			requestRaw, err := base64.StdEncoding.Strict().DecodeString(page.RequestBase64)
			if err != nil || base64.StdEncoding.EncodeToString(requestRaw) != page.RequestBase64 ||
				digest(requestRaw) != page.RequestSHA256 || len(requestRaw) > maximumConfigBytes {
				return nil, fmt.Errorf("native evidence source %s page %d request bytes are invalid", source.ID, pageIndex)
			}
			var providerRequest NativeRequest
			if _, err := boundary.CanonicalJSON(requestRaw, &providerRequest); err != nil {
				return nil, fmt.Errorf("native evidence source %s page %d request projection is non-canonical", source.ID, pageIndex)
			}
			expectedURL, err := collectorPageURL(source.InitialURL, source.Pagination, pageToken)
			if err != nil || page.RequestedURL != expectedURL {
				return nil, fmt.Errorf("native evidence source %s page %d requested an unexpected URL", source.ID, pageIndex)
			}
			responseRaw, err := base64.StdEncoding.Strict().DecodeString(page.RawResponseBase64)
			if err != nil || base64.StdEncoding.EncodeToString(responseRaw) != page.RawResponseBase64 ||
				digest(responseRaw) != page.RawResponseSHA256 || int64(len(responseRaw)) > source.MaximumPageBytes {
				return nil, fmt.Errorf("native evidence source %s page %d response bytes are invalid", source.ID, pageIndex)
			}
			totalResponseBytes += int64(len(responseRaw))
			if totalResponseBytes > source.MaximumTotalBytes {
				return nil, fmt.Errorf("native evidence source %s exceeds its accepted total response bound", source.ID)
			}
			if err := validatePage(page, source, directive, providerRequest, requestRaw, expectedURL, responseRaw, now); err != nil {
				return nil, fmt.Errorf("verify native evidence source %s page %d: %w", source.ID, pageIndex, err)
			}
			complete, nextToken, nextURL, err := derivePagination(source, page.RequestedURL, responseRaw)
			if err != nil || page.Complete != complete || page.NextToken != nextToken || page.NextURL != nextURL ||
				(pageIndex == len(evidence.Pages)-1) != complete {
				return nil, fmt.Errorf("native evidence source %s page %d pagination or terminal state is invalid", source.ID, pageIndex)
			}
			verified.Pages[source.ID] = append(verified.Pages[source.ID], page)
			verified.RawResponses[source.ID] = append(verified.RawResponses[source.ID], bytes.Clone(responseRaw))
			pageToken = nextToken
		}
	}
	expectedCollectedAt, err := deterministicBundleCollectedAt(bundle.Cycle, bundle.Sources)
	if err != nil || bundle.CollectedAt != expectedCollectedAt {
		return nil, errors.New("native evidence bundle terminal collection time is not derived from its exact pages")
	}
	return verified, nil
}
