package collector

import (
	"bytes"
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
)

type Authority struct {
	Config     AuthorityConfig
	Acceptance boundary.Acceptance
	privateKey ed25519.PrivateKey
	publicKey  ed25519.PublicKey
	serverCertificate tls.Certificate
	clientRoots       *x509.CertPool
	sources    map[string]AuthoritySourceSpec
	bearerVerifiers map[string]providerBearerVerifier
	slots      chan struct{}
	mu         sync.Mutex
	journalMu  sync.Mutex
	cycleMu    sync.Mutex
	pending    map[string]authorityPending
	journal    *authorityLedgerJournal
	cycleHighWaterIssuedAt time.Time
	cycleHighWaterID       string
	retainedSessions     int
	retainedSettlements  int
	retainedReservations int
	retainedChallenges   int
}

type authoritySession struct {
	sourceID                 string
	cycleContractSHA256      string
	cycleID                  string
	cycleIssuedAt            string
	cycleDeadlineAt          string
	nextPage                 int
	nextToken                string
	terminal                 bool
	previousSettlementSHA256 string
	responseBytes            int64
	headSHA256               string
	settlements              map[int]AuthoritySettlement
}

type authorityPending struct {
	sourceID                 string
	cycleContractSHA256      string
	cycleID                  string
	cycleIssuedAt            string
	cycleDeadlineAt          string
	pageIndex                int
	pageToken                string
	challenge                string
	directiveSHA256          string
	previousSettlementSHA256 string
}

type authorityLedgerCounts struct {
	sessions     int
	settlements  int
	reservations int
	challenges   int
}

const (
	ledgerEpochRetirementPayloadSchema = "fs2-serve.nebius.ai/public-edge-authority-ledger-epoch-retirement/v1"
	ledgerEpochRetirementEnvelopeSchema = "fs2-serve.nebius.ai/public-edge-authority-ledger-epoch-retirement-envelope/v1"
	authorityCollectionOutcomeSchema = "fs2-serve.nebius.ai/public-edge-authority-collection-outcome/v1"
	linuxStatfsReadOnly = 1
)

type authorityCollectionOutcomePayload struct {
	Schema                   string `json:"schema"`
	ClusterID                string `json:"cluster_id"`
	DeploymentID             string `json:"deployment_id"`
	CycleContractSHA256      string `json:"cycle_contract_sha256"`
	CycleID                  string `json:"cycle_id"`
	CycleIssuedAt            string `json:"cycle_issued_at"`
	CycleDeadlineAt          string `json:"cycle_deadline_at"`
	SourceID                 string `json:"source_id"`
	SessionID                string `json:"session_id"`
	PageIndex                int    `json:"page_index"`
	Challenge                string `json:"challenge"`
	DirectiveSHA256          string `json:"directive_sha256"`
	SettlementSHA256         string `json:"settlement_sha256"`
	ChallengeRecordSHA256    string `json:"challenge_record_sha256"`
	SessionHeadSHA256        string `json:"session_head_sha256"`
	Status                   string `json:"status"`
	Reason                   string `json:"reason"`
	OutcomeAt                string `json:"outcome_at"`
}

type authorityCollectionOutcome struct {
	Payload   authorityCollectionOutcomePayload `json:"payload"`
	Issuer    string                            `json:"issuer"`
	KeyID     string                            `json:"key_id"`
	Algorithm string                            `json:"algorithm"`
	Signature string                            `json:"signature"`
}

type ledgerEpochRetirementPayload struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	LedgerEpochID string `json:"ledger_epoch_id"`
	LedgerDeviceID uint64 `json:"ledger_device_id"`
	FinalSegment string `json:"final_segment"`
	FinalHeadSHA256 string `json:"final_head_sha256"`
	WriterState string `json:"writer_state"`
	RetiredAt string `json:"retired_at"`
}

type ledgerEpochRetirementEnvelope struct {
	Schema string `json:"schema"`
	Issuer string `json:"issuer"`
	KeyID string `json:"key_id"`
	Algorithm string `json:"algorithm"`
	PayloadBase64 string `json:"payload_base64"`
	PayloadSHA256 string `json:"payload_sha256"`
	Signature string `json:"signature"`
}

const (
	maximumAuthorityLedgerRecordBytes = 128 * 1024 * 1024
	maximumAuthorityReservationBytes  = 16 * 1024
	maximumAuthorityOutcomeBytes      = 32 * 1024
	maximumAuthoritySessionPages      = 10000
	maximumPendingCollections         = 1024
	maximumRetainedSessions           = 65536
	maximumRetainedRecords            = 1000000
)

func LoadAuthority(configPath string, acceptance boundary.Acceptance) (*Authority, error) {
	if os.Geteuid() != 0 {
		return nil, errors.New("native authority requires separate root custody for its signing key and append-only settlement ledger")
	}
	raw, err := readRootRegular(configPath, maximumConfigBytes)
	if err != nil {
		return nil, fmt.Errorf("read native authority config: %w", err)
	}
	if digest(raw) != acceptance.NativeAuthorityConfigSHA256 {
		return nil, errors.New("native authority config differs from independently accepted bytes")
	}
	if err := rejectClosedIntegrationGate(raw, "native-authority"); err != nil {
		return nil, err
	}
	var configDiscriminator struct {
		Schema string `json:"schema"`
	}
	if err := json.Unmarshal(raw, &configDiscriminator); err == nil && configDiscriminator.Schema == LegacyAuthorityConfigSchema {
		return nil, errors.New("legacy native-authority config v1 is retained evidence but cannot be reinterpreted as the epoch/cadence v2 contract; enroll a new additive v2 epoch")
	}
	var config AuthorityConfig
	if err := canonicalJSON(raw, &config); err != nil {
		return nil, err
	}
	if config.Schema != AuthorityConfigSchema || config.ClusterID != acceptance.ClusterID || config.DeploymentID != acceptance.DeploymentID ||
		!boundedProtocolText(config.ClusterID, maximumClusterIDBytes, false) || !boundedProtocolText(config.DeploymentID, maximumDeploymentIDBytes, false) ||
		!isDigest(config.LedgerEpochID) ||
		config.ListenAddress != ":8444" || len(config.Sources) == 0 || !filepath.IsAbs(config.SettlementRoot) ||
		filepath.Clean(config.SettlementRoot) != config.SettlementRoot || config.SettlementRoot == "/" ||
		config.LedgerOperatingHorizonDays < 365 || config.LedgerOperatingHorizonDays > 3660 ||
		config.CollectorRefreshIntervalSeconds < 240 || config.CollectorRefreshIntervalSeconds > 270 ||
		config.CollectorCollectionDeadlineSeconds < 1 ||
		config.CollectorCollectionDeadlineSeconds >= config.CollectorRefreshIntervalSeconds ||
		config.CollectorConfigSHA256 != acceptance.NativeCollectorConfigSHA256 || config.LedgerDeviceID == 0 ||
		config.LedgerMinimumFreeBytes < uint64(maximumAuthorityLedgerRecordBytes) || config.LedgerMinimumFreeInodes < 1024 ||
		config.LedgerCapacityInodes == 0 || config.LedgerExpectedMaximumRecordsPerDay != 0 {
		return nil, errors.New("native authority config is incomplete or has the wrong identity")
	}
	previousEpochFields := []bool{
		config.PreviousLedgerEpochID != "",
		config.PreviousLedgerRoot != "",
		config.PreviousLedgerDeviceID != 0,
		config.PreviousLedgerConfigPath != "",
		config.PreviousLedgerConfigSHA256 != "",
		config.PreviousLedgerFinalSegment != "",
		config.PreviousLedgerFinalHeadSHA256 != "",
		config.PreviousLedgerSigningIssuer != "",
		config.PreviousLedgerSigningKeyID != "",
		config.PreviousLedgerSigningPublicKey != "",
		config.PreviousLedgerRetirementReceiptPath != "",
		config.PreviousLedgerRetirementReceiptSHA256 != "",
		config.PreviousLedgerRetirementIssuer != "",
		config.PreviousLedgerRetirementKeyID != "",
		config.PreviousLedgerRetirementPublicKey != "",
	}
	previousEpochPresent := previousEpochFields[0]
	for _, present := range previousEpochFields {
		if present != previousEpochPresent {
			return nil, errors.New("native authority ledger epoch predecessor is only partially enrolled")
		}
	}
	if previousEpochPresent && (!isDigest(config.PreviousLedgerEpochID) || config.PreviousLedgerEpochID == config.LedgerEpochID ||
		!filepath.IsAbs(config.PreviousLedgerRoot) || filepath.Clean(config.PreviousLedgerRoot) != config.PreviousLedgerRoot ||
		config.PreviousLedgerRoot == "/" || config.PreviousLedgerRoot == config.SettlementRoot ||
		config.PreviousLedgerDeviceID == config.LedgerDeviceID || !canonicalSegmentID(config.PreviousLedgerFinalSegment) ||
			!filepath.IsAbs(config.PreviousLedgerConfigPath) || filepath.Clean(config.PreviousLedgerConfigPath) != config.PreviousLedgerConfigPath ||
			!isDigest(config.PreviousLedgerConfigSHA256) ||
			!isDigest(config.PreviousLedgerFinalHeadSHA256) ||
			!boundedProtocolText(config.PreviousLedgerSigningIssuer, maximumSigningIssuerBytes, false) ||
			!boundedProtocolText(config.PreviousLedgerSigningKeyID, maximumSigningKeyIDBytes, false) ||
			!filepath.IsAbs(config.PreviousLedgerRetirementReceiptPath) ||
			filepath.Clean(config.PreviousLedgerRetirementReceiptPath) != config.PreviousLedgerRetirementReceiptPath ||
			!isDigest(config.PreviousLedgerRetirementReceiptSHA256) ||
			!boundedProtocolText(config.PreviousLedgerRetirementIssuer, maximumSigningIssuerBytes, false) ||
			!boundedProtocolText(config.PreviousLedgerRetirementKeyID, maximumSigningKeyIDBytes, false) ||
			(config.PreviousLedgerPredecessorEpochID == "") != (config.PreviousLedgerPredecessorHeadSHA256 == "") ||
			(config.PreviousLedgerPredecessorEpochID != "" && (!isDigest(config.PreviousLedgerPredecessorEpochID) ||
				!isDigest(config.PreviousLedgerPredecessorHeadSHA256)))) {
			return nil, errors.New("native authority ledger epoch rollover contract is invalid")
		}
	keyRaw, err := readRootPrivateKey(config.SigningKeyPath, 1024)
	if err != nil {
		return nil, fmt.Errorf("read native authority signing key: %w", err)
	}
	if !isDigest(config.SigningKeySHA256) || digest(keyRaw) != config.SigningKeySHA256 {
		return nil, errors.New("native authority signing key differs from its accepted digest")
	}
	keyBytes, err := base64.RawURLEncoding.DecodeString(strings.TrimSpace(string(keyRaw)))
	if err != nil || len(keyBytes) != ed25519.PrivateKeySize || base64.RawURLEncoding.EncodeToString(keyBytes) != strings.TrimSpace(string(keyRaw)) {
		return nil, errors.New("native authority signing key is not canonical Ed25519 private-key bytes")
	}
	privateKey := ed25519.PrivateKey(keyBytes)
	publicKey := privateKey.Public().(ed25519.PublicKey)
	if config.SigningKeyID != "sha256:"+digest(publicKey) || !boundedProtocolText(config.SigningKeyID, maximumSigningKeyIDBytes, false) ||
		!boundedProtocolText(config.SigningIssuer, maximumSigningIssuerBytes, false) {
		return nil, errors.New("native authority signing identity does not match its key")
	}
	if previousEpochPresent && (config.PreviousLedgerSigningKeyID == config.SigningKeyID ||
		config.PreviousLedgerRetirementKeyID == config.SigningKeyID ||
		config.PreviousLedgerRetirementKeyID == config.PreviousLedgerSigningKeyID ||
		config.PreviousLedgerRetirementPublicKey == config.PreviousLedgerSigningPublicKey) {
		return nil, errors.New("native authority writer and retirement identities are not independently separated")
	}
	if !isDigest(config.ClientCASHA256) || !isDigest(config.TLSCertificateSHA256) || !isDigest(config.TLSPrivateKeySHA256) ||
		!isDigest(config.TLSSPKISHA256) ||
		!isDigest(config.CollectorSPKISHA256) {
		return nil, errors.New("native authority config contains an invalid trust digest")
	}
	if previousEpochPresent {
		if err := verifyPreviousLedgerEpochAnchor(config); err != nil {
			return nil, fmt.Errorf("verify prior native authority ledger epoch: %w", err)
		}
	}
	clientCARaw, err := readRootRegular(config.ClientCAPath, 4*1024*1024)
	if err != nil {
		return nil, fmt.Errorf("read native authority client CA: %w", err)
	}
	if digest(clientCARaw) != config.ClientCASHA256 {
		return nil, errors.New("native authority client CA differs from its accepted digest")
	}
	clientRoots := x509.NewCertPool()
	if !clientRoots.AppendCertsFromPEM(clientCARaw) {
		return nil, errors.New("native authority client CA contains no parseable certificate")
	}
	serverCertificateRaw, err := readRootRegular(config.TLSCertificatePath, 4*1024*1024)
	if err != nil {
		return nil, fmt.Errorf("read native authority TLS certificate: %w", err)
	}
	if digest(serverCertificateRaw) != config.TLSCertificateSHA256 {
		return nil, errors.New("native authority server certificate differs from its accepted digest")
	}
	serverPrivateKeyRaw, err := readRootPrivateKey(config.TLSPrivateKeyPath, 4*1024*1024)
	if err != nil {
		return nil, fmt.Errorf("read native authority TLS private key: %w", err)
	}
	if digest(serverPrivateKeyRaw) != config.TLSPrivateKeySHA256 {
		return nil, errors.New("native authority TLS private key differs from its accepted digest")
	}
	serverCertificate, err := tls.X509KeyPair(serverCertificateRaw, serverPrivateKeyRaw)
	if err != nil || len(serverCertificate.Certificate) < 1 {
		return nil, errors.New("native authority TLS certificate and private key do not form an exact pair")
	}
	serverLeaf, err := x509.ParseCertificate(serverCertificate.Certificate[0])
	if err != nil || digest(serverLeaf.RawSubjectPublicKeyInfo) != config.TLSSPKISHA256 ||
		!certificateHasUsage(serverLeaf, x509.ExtKeyUsageServerAuth) || time.Now().UTC().Before(serverLeaf.NotBefore) ||
		!time.Now().UTC().Before(serverLeaf.NotAfter) {
		return nil, errors.New("native authority TLS leaf identity, purpose or validity differs from acceptance")
	}
	serverCertificate.Leaf = serverLeaf
	sources := map[string]AuthoritySourceSpec{}
	bearerVerifiers := map[string]providerBearerVerifier{}
	if config.SigningKeyPath == config.TLSPrivateKeyPath {
		return nil, errors.New("native authority signing and server TLS identities reuse a private-key path")
	}
	type laneBinding struct {
		caSHA256 string
		certificateSHA256 string
		keySHA256 string
		spkiSHA256 string
		spiffeURI string
		serverName string
	}
	credentialLanes := map[string]laneBinding{}
	credentialDigests := map[string]string{}
	for _, source := range config.Sources {
		if !boundedProtocolText(source.ID, maximumSourceIDBytes, false) || !boundedProtocolText(source.Kind, maximumSourceKindBytes, false) ||
			!boundedProtocolText(source.SemanticCollection, maximumSemanticCollectionBytes, false) ||
			!boundedProtocolText(source.InitialURL, maximumNativeURLBytes, false) || !boundedProtocolText(source.ServerName, maximumServerNameBytes, false) ||
			!boundedProtocolText(source.ProviderRequestIDHeader, maximumProviderRequestHeaderBytes, false) ||
			!boundedProtocolText(source.ClientSPIFFEURI, maximumSPIFFEURIBytes, false) ||
			!boundedProtocolText(source.CredentialLaneID, maximumCredentialLaneIDBytes, false) || !isDigest(source.CABundleSHA256) ||
			!isDigest(source.ClientCertificateSHA256) || !isDigest(source.ClientKeySHA256) || !isDigest(source.ClientSPKISHA256) ||
			source.ClientSPIFFEURI == "" || source.CredentialLaneID == "" || strings.ContainsAny(source.CredentialLaneID, "\x00\r\n") ||
			source.MaximumPages < 1 || source.MaximumPages > 1024 || source.MaximumPageBytes < 1024 || source.MaximumPageBytes > 32*1024*1024 ||
			source.MaximumTotalBytes < source.MaximumPageBytes || source.MaximumTotalBytes > 128*1024*1024 ||
			(source.Pagination != "none" && source.Pagination != "kubernetes-continue" && source.Pagination != "provider-page-token") {
			return nil, errors.New("native authority source is incomplete")
		}
		if source.Kind == "ca-revocation" && (source.Pagination != "none" || source.RevocationIssuerBundlePath == "" ||
			!isDigest(source.RevocationIssuerBundleSHA256) || source.MaximumRevocationAgeSeconds < 1 || source.MaximumRevocationAgeSeconds > 86400) {
			return nil, errors.New("native CRL source lacks its exact non-paginated issuer and freshness contract")
		}
		if acceptance.Schema == boundary.AcceptancePayloadSchema && source.Pagination != mandatoryPaginationForSemanticCollection(source.SemanticCollection) {
			return nil, errors.New("native authority source pagination differs from the additive v5 semantic collection contract")
		}
		if source.BearerTokenPath == "" {
			if source.BearerTokenIssuer != "" || source.BearerTokenAudience != "" || source.BearerTokenSubject != "" ||
				source.BearerTokenAlgorithm != "" || source.BearerTokenKeyID != "" || source.BearerTokenJWKSPath != "" ||
				source.BearerTokenJWKSSHA256 != "" ||
				source.BearerTokenMaximumLifetimeSeconds != 0 || source.BearerTokenMinimumRemainingSeconds != 0 {
				return nil, errors.New("native authority source has bearer semantics without a brokered credential path")
			}
		} else {
			if !boundedProtocolText(source.BearerTokenIssuer, maximumBearerClaimBytes, false) ||
				!boundedProtocolText(source.BearerTokenAudience, maximumBearerClaimBytes, false) ||
				!boundedProtocolText(source.BearerTokenSubject, maximumBearerClaimBytes, false) ||
				!boundedProtocolText(source.BearerTokenAlgorithm, maximumBearerAlgorithmBytes, false) ||
				!boundedProtocolText(source.BearerTokenKeyID, maximumBearerKeyIDBytes, false) ||
				!filepath.IsAbs(source.BearerTokenJWKSPath) || filepath.Clean(source.BearerTokenJWKSPath) != source.BearerTokenJWKSPath ||
				!isDigest(source.BearerTokenJWKSSHA256) ||
				source.BearerTokenMaximumLifetimeSeconds < 60 || source.BearerTokenMaximumLifetimeSeconds > 900 ||
				source.BearerTokenMinimumRemainingSeconds < 30 ||
				source.BearerTokenMinimumRemainingSeconds >= source.BearerTokenMaximumLifetimeSeconds {
				return nil, errors.New("native authority bearer credential semantics are incomplete or not short lived")
			}
			verifier, err := loadProviderBearerVerifier(source)
			if err != nil {
				return nil, fmt.Errorf("load accepted provider bearer verifier for %s: %w", source.ID, err)
			}
			bearerVerifiers[source.ID] = verifier
			if _, _, err := loadProviderBearerCredential(source, verifier, time.Now().UTC()); err != nil {
				return nil, fmt.Errorf("validate root-private provider credential for %s: %w", source.ID, err)
			}
		}
		settlementMaximum, preparedSettlementMaximum, boundsOK := maximumAuthorityPageRecordBounds(uint64(source.MaximumPageBytes))
		if !boundsOK || settlementMaximum > uint64(maximumAuthorityLedgerRecordBytes) ||
			preparedSettlementMaximum > uint64(maximumPreparedSlotBytes) {
			return nil, errors.New("native authority source page bound exceeds the enrolled settlement or prepared-slot record extent")
		}
		if _, exists := sources[source.ID]; exists {
			return nil, errors.New("native authority repeats a source ID")
		}
		binding := laneBinding{
			caSHA256: source.CABundleSHA256,
			certificateSHA256: source.ClientCertificateSHA256,
			keySHA256: source.ClientKeySHA256,
			spkiSHA256: source.ClientSPKISHA256,
			spiffeURI: source.ClientSPIFFEURI,
			serverName: source.ServerName,
		}
		if enrolled, exists := credentialLanes[source.CredentialLaneID]; exists && enrolled != binding {
			return nil, errors.New("native authority credential lane contains inconsistent provider trust or client identity bytes")
		}
		credentialLanes[source.CredentialLaneID] = binding
		fingerprint := source.ClientKeySHA256 + ":" + source.ClientSPKISHA256
		if source.ClientKeySHA256 == config.TLSPrivateKeySHA256 || source.ClientKeySHA256 == config.SigningKeySHA256 {
			return nil, errors.New("native provider client key is reused by a server or signing trust lane")
		}
		if priorLane, reused := credentialDigests[fingerprint]; reused && priorLane != source.CredentialLaneID {
			return nil, errors.New("native authority credentials are reused across separately accepted provider lanes")
		}
		credentialDigests[fingerprint] = source.CredentialLaneID
		if err := requireHTTPS(source.InitialURL, source.ServerName); err != nil {
			return nil, err
		}
		caRaw, err := readRootRegular(source.CABundlePath, 4*1024*1024)
		if err != nil {
			return nil, fmt.Errorf("read native provider CA bundle for %s: %w", source.ID, err)
		}
		if digest(caRaw) != source.CABundleSHA256 {
			return nil, errors.New("native provider CA bundle differs from its accepted digest")
		}
		if source.RevocationIssuerBundlePath != "" {
			if !isDigest(source.RevocationIssuerBundleSHA256) {
				return nil, errors.New("CRL issuer bundle digest is not canonical")
			}
			issuerRaw, err := readRootRegular(source.RevocationIssuerBundlePath, 4*1024*1024)
			if err != nil {
				return nil, fmt.Errorf("read CRL issuer bundle for %s: %w", source.ID, err)
			}
			if digest(issuerRaw) != source.RevocationIssuerBundleSHA256 {
				return nil, errors.New("CRL issuer bundle differs from its accepted digest")
			}
		}
		sources[source.ID] = source
	}
	cycle, cycleErr := collectorCycleFor(
		acceptance,
		config.CollectorRefreshIntervalSeconds,
		config.CollectorCollectionDeadlineSeconds,
		authoritySourceIDs(config.Sources),
		time.Unix(0, 0).UTC(),
	)
	cycleRaw, cycleMarshalErr := json.Marshal(cycle)
	if cycleErr != nil || cycleMarshalErr != nil || len(cycleRaw) > maximumCollectorCycleBytes {
		return nil, errors.New("native authority source inventory cannot fit its accepted canonical cycle bound")
	}
	dailyBounds, err := derivedAuthorityLedgerDailyBounds(config)
	if err != nil || dailyBounds.bytes != config.LedgerExpectedMaximumBytesPerDay ||
		dailyBounds.events != config.LedgerExpectedMaximumEventsPerDay ||
		dailyBounds.inodes != config.LedgerExpectedMaximumInodesPerDay ||
		dailyBounds.bytes > (^uint64(0)-config.LedgerMinimumFreeBytes)/uint64(config.LedgerOperatingHorizonDays) ||
		dailyBounds.inodes > (^uint64(0)-config.LedgerMinimumFreeInodes)/uint64(config.LedgerOperatingHorizonDays) ||
		config.LedgerCapacityBytes < dailyBounds.bytes*uint64(config.LedgerOperatingHorizonDays)+config.LedgerMinimumFreeBytes ||
		config.LedgerCapacityInodes < dailyBounds.inodes*uint64(config.LedgerOperatingHorizonDays)+config.LedgerMinimumFreeInodes {
		return nil, errors.New("native authority ledger capacity is not the exact source-derived event, byte and physical-inode horizon")
	}
	if err := prepareAuthorityLedger(config.SettlementRoot); err != nil {
		return nil, fmt.Errorf("prepare native authority settlement ledger: %w", err)
	}
	if err := ensureAuthorityLedgerCapacity(config, uint64(maximumAuthorityLedgerRecordBytes), 8); err != nil {
		return nil, fmt.Errorf("validate native authority ledger capacity horizon: %w", err)
	}
	if err := verifyAnonymousPublicationSupport(config.SettlementRoot); err != nil {
		return nil, fmt.Errorf("validate native authority atomic publication support: %w", err)
	}
	journal, counts, err := loadAuthorityLedgerJournal(config, acceptance, privateKey, publicKey, time.Now().UTC())
	if err != nil {
		return nil, fmt.Errorf("recover native authority settlement ledger: %w", err)
	}
	authority := &Authority{
		Config:     config,
		Acceptance: acceptance,
		privateKey: privateKey,
		publicKey:  publicKey,
		serverCertificate: serverCertificate,
		clientRoots:       clientRoots,
		sources:    sources,
		bearerVerifiers: bearerVerifiers,
		slots:      make(chan struct{}, 8),
		pending:    map[string]authorityPending{},
		journal:    journal,
		cycleHighWaterIssuedAt: journal.recoveredCycleIssuedAt,
		cycleHighWaterID:       journal.recoveredCycleID,
		retainedSessions:     counts.sessions,
		retainedSettlements:  counts.settlements,
		retainedReservations: counts.reservations,
		retainedChallenges:   counts.challenges,
	}
	if err := authority.recoverCollectionOutcomes(); err != nil {
		return nil, fmt.Errorf("recover native authority collection outcomes: %w", err)
	}
	return authority, nil
}

func (a *Authority) recoverCollectionOutcomes() error {
	if a.journal == nil {
		return errors.New("authority outcome recovery lacks its signed ledger")
	}
	seen := map[string]struct{}{}
	for _, event := range a.journal.recoveredSessionHeads {
		if event.Payload.Kind != "session-head" || event.Payload.Phase != "terminal" {
			continue
		}
		if _, duplicate := seen[event.Payload.Challenge]; duplicate {
			continue
		}
		seen[event.Payload.Challenge] = struct{}{}
		headPath := filepath.Join(a.Config.SettlementRoot, filepath.FromSlash(event.Payload.ObjectPath))
		headRaw, err := readRootRegular(headPath, maximumSessionHeadBytes)
		if err != nil || digest(headRaw) != event.Payload.ObjectSHA256 {
			return errors.New("recovered session-head event lacks its exact signed object")
		}
		var head authoritySessionHead
		if err := canonicalJSON(headRaw, &head); err != nil || head.Payload.SessionID != event.Payload.SessionID ||
			head.Payload.LastChallenge != event.Payload.Challenge || head.Payload.NextPage != event.Payload.PageIndex+1 {
			return errors.New("recovered session-head event differs from its signed page identity")
		}
		settlementPath := filepath.Join(sessionLedgerRoot(a.Config, head.Payload.SessionID), fmt.Sprintf("page-%08d.json", event.Payload.PageIndex))
		settlementRaw, err := readRootRegular(settlementPath, maximumAuthorityLedgerRecordBytes)
		if err != nil || digest(settlementRaw) != head.Payload.LastSettlementSHA256 {
			return errors.New("recovered session head lacks its exact settlement")
		}
		var settlement AuthoritySettlement
		if err := canonicalJSON(settlementRaw, &settlement); err != nil {
			return err
		}
		if err := a.advanceCycleHighWater(settlement.CycleID, settlement.CycleIssuedAt); err != nil {
			return err
		}
		challengeRaw, err := readRootRegular(challengeLedgerPath(a.Config, settlement.Challenge), maximumAuthorityReservationBytes)
		if err != nil || validateAuthorityChallengeCrossLink(a.Config, a.Acceptance, settlement) != nil {
			return errors.New("recovered terminal prefix lacks its exact challenge and reservation cross-link")
		}
		if _, err := a.settleCollectionOutcome(settlement, challengeRaw, headRaw); err != nil {
			return err
		}
	}
	return nil
}

func (a *Authority) Serve(ctx context.Context) error {
	if a.clientRoots == nil || len(a.serverCertificate.Certificate) == 0 || a.serverCertificate.PrivateKey == nil {
		return errors.New("native authority TLS identity is not loaded from accepted bytes")
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/collect", a.handleCollect)
	mux.HandleFunc("/readyz", a.handleReady)
	server := &http.Server{
		Addr:              a.Config.ListenAddress,
		Handler:           mux,
		ReadHeaderTimeout: 3 * time.Second,
		ReadTimeout:       35 * time.Second,
		WriteTimeout:      35 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    32 * 1024,
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS13,
			ClientAuth: tls.RequireAndVerifyClientCert,
			ClientCAs:  a.clientRoots,
			Certificates: []tls.Certificate{a.serverCertificate},
		},
	}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	return server.ListenAndServeTLS("", "")
}

func (a *Authority) handleReady(response http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet || !a.authorizedCollector(request.TLS) {
		response.WriteHeader(http.StatusForbidden)
		return
	}
	if !a.Acceptance.ProductionTrustCurrent(time.Now().UTC()) {
		response.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	response.WriteHeader(http.StatusOK)
}

func (a *Authority) handleCollect(response http.ResponseWriter, request *http.Request) {
	response.Header().Set("Content-Type", "application/fs2-native-response-envelope+json")
	if request.Method != http.MethodPost || !a.authorizedCollector(request.TLS) {
		response.WriteHeader(http.StatusForbidden)
		return
	}
	if !a.Acceptance.ProductionTrustCurrent(time.Now().UTC()) {
		response.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	select {
	case a.slots <- struct{}{}:
		defer func() { <-a.slots }()
	default:
		response.WriteHeader(http.StatusTooManyRequests)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(request.Body, 64*1024+1))
	if err != nil || len(raw) > 64*1024 {
		response.WriteHeader(http.StatusBadRequest)
		return
	}
	var collectionRequest CollectionRequest
	if err := canonicalJSON(raw, &collectionRequest); err != nil || collectionRequest.Schema != CollectionRequestSchema ||
			collectionRequest.ClusterID != a.Acceptance.ClusterID || collectionRequest.DeploymentID != a.Acceptance.DeploymentID ||
			collectionRequest.PageIndex < 0 || collectionRequest.PageIndex >= maximumAuthoritySessionPages ||
			!boundedProtocolText(collectionRequest.ClusterID, maximumClusterIDBytes, false) ||
			!boundedProtocolText(collectionRequest.DeploymentID, maximumDeploymentIDBytes, false) ||
			!boundedProtocolText(collectionRequest.SourceID, maximumSourceIDBytes, false) ||
			!boundedProtocolText(collectionRequest.PageToken, maximumPageTokenBytes, true) ||
			!isDigest(collectionRequest.CycleContractSHA256) || !isDigest(collectionRequest.CycleID) ||
		!canonicalNonce(collectionRequest.SessionID) || !canonicalNonce(collectionRequest.Challenge) {
		response.WriteHeader(http.StatusBadRequest)
		return
	}
	source, exists := a.sources[collectionRequest.SourceID]
	if !exists || collectionRequest.PageIndex >= source.MaximumPages {
		response.WriteHeader(http.StatusForbidden)
		return
	}
	_, cycleDeadline, err := a.validateCollectionCycle(collectionRequest, time.Now().UTC())
	if err != nil {
		response.WriteHeader(http.StatusConflict)
		return
	}
	collectionContext, cancel := context.WithDeadline(request.Context(), cycleDeadline)
	defer cancel()
	directiveSHA256 := digest(raw)
	cachedEnvelope, cached, err := a.beginCollection(collectionRequest, source, directiveSHA256)
	if err != nil {
		response.WriteHeader(http.StatusConflict)
		return
	}
	if cached {
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write(cachedEnvelope)
		return
	}
	page, err := a.collect(collectionContext, source, collectionRequest)
	if err != nil {
		a.abortCollection(collectionRequest.SessionID, collectionRequest.Challenge)
		response.WriteHeader(http.StatusBadGateway)
		return
	}
	if !time.Now().UTC().Before(cycleDeadline) {
		a.abortCollection(collectionRequest.SessionID, collectionRequest.Challenge)
		response.WriteHeader(http.StatusConflict)
		return
	}
	envelopeRaw, err := a.signPage(page)
	if err != nil {
		a.abortCollection(collectionRequest.SessionID, collectionRequest.Challenge)
		response.WriteHeader(http.StatusInternalServerError)
		return
	}
	if err := a.settleCollection(collectionRequest, directiveSHA256, page, envelopeRaw); err != nil {
		a.abortCollection(collectionRequest.SessionID, collectionRequest.Challenge)
		response.WriteHeader(http.StatusInternalServerError)
		return
	}
	response.WriteHeader(http.StatusOK)
	_, _ = response.Write(envelopeRaw)
}

func (a *Authority) validateCollectionCycle(request CollectionRequest, now time.Time) (CollectorCycle, time.Time, error) {
	cycle, err := collectorCycleFor(
		a.Acceptance,
		a.Config.CollectorRefreshIntervalSeconds,
		a.Config.CollectorCollectionDeadlineSeconds,
		authoritySourceIDs(a.Config.Sources),
		now,
	)
	if err != nil {
		return CollectorCycle{}, time.Time{}, err
	}
	deadlineAt, deadlineErr := time.Parse(time.RFC3339, cycle.DeadlineAt)
	issuedAt, issueErr := time.Parse(time.RFC3339, cycle.IssuedAt)
	if deadlineErr != nil || issueErr != nil || !now.UTC().Truncate(time.Second).Before(deadlineAt) {
		return CollectorCycle{}, time.Time{}, errors.New("native authority collection cycle is outside its accepted deadline")
	}
	a.journalMu.Lock()
	journal := a.journal
	lastRecordedAt := time.Time{}
	if journal != nil {
		lastRecordedAt = journal.lastRecordedAt
	}
	a.journalMu.Unlock()
	if journal == nil || (!lastRecordedAt.IsZero() && (!lastRecordedAt.Before(deadlineAt) || issuedAt.After(now.UTC().Add(time.Second)))) {
		return CollectorCycle{}, time.Time{}, errors.New("native authority cycle conflicts with its durable signed cadence high-water")
	}
	sessionID, sessionErr := cycleSessionID(cycle, request.SourceID)
	if sessionErr != nil || request.CycleContractSHA256 != cycle.ContractSHA256 || request.CycleID != cycle.CycleID ||
		request.CycleIssuedAt != cycle.IssuedAt || request.CycleDeadlineAt != cycle.DeadlineAt || request.SessionID != sessionID ||
		request.Challenge != cycleChallenge(cycle, request.SourceID, sessionID, request.PageIndex, request.PageToken) {
		return CollectorCycle{}, time.Time{}, errors.New("native authority directive differs from the independently derived accepted cycle")
	}
	a.cycleMu.Lock()
	highWaterIssuedAt := a.cycleHighWaterIssuedAt
	highWaterID := a.cycleHighWaterID
	a.cycleMu.Unlock()
	if !highWaterIssuedAt.IsZero() && (issuedAt.Before(highWaterIssuedAt) || (issuedAt.Equal(highWaterIssuedAt) && highWaterID != cycle.CycleID)) {
		return CollectorCycle{}, time.Time{}, errors.New("native authority refuses a collection cycle below or conflicting with its recovered signed high-water")
	}
	return cycle, deadlineAt, nil
}

func (a *Authority) advanceCycleHighWater(cycleID string, issuedAtRaw string) error {
	issuedAt, err := time.Parse(time.RFC3339, issuedAtRaw)
	if err != nil || issuedAt.Nanosecond() != 0 || !isDigest(cycleID) {
		return errors.New("authority cycle high-water candidate is not canonical")
	}
	a.cycleMu.Lock()
	defer a.cycleMu.Unlock()
	if !a.cycleHighWaterIssuedAt.IsZero() {
		if issuedAt.Before(a.cycleHighWaterIssuedAt) || (issuedAt.Equal(a.cycleHighWaterIssuedAt) && a.cycleHighWaterID != cycleID) {
			return errors.New("authority cycle high-water cannot move backwards or fork")
		}
	}
	if issuedAt.After(a.cycleHighWaterIssuedAt) {
		a.cycleHighWaterIssuedAt = issuedAt
		a.cycleHighWaterID = cycleID
	}
	return nil
}

func (a *Authority) authorizedCollector(state *tls.ConnectionState) bool {
	if state == nil || len(state.VerifiedChains) < 1 || len(state.PeerCertificates) < 1 {
		return false
	}
	leaf := state.PeerCertificates[0]
	if digest(leaf.RawSubjectPublicKeyInfo) != a.Config.CollectorSPKISHA256 || len(leaf.URIs) != 1 ||
		leaf.URIs[0].String() != a.Config.CollectorSPIFFEURI {
		return false
	}
	hasClientAuth := false
	for _, usage := range leaf.ExtKeyUsage {
		if usage == x509.ExtKeyUsageClientAuth {
			hasClientAuth = true
		}
	}
	if !hasClientAuth {
		return false
	}
	for _, chain := range state.VerifiedChains {
		if len(chain) < 1 || !bytes.Equal(chain[0].Raw, leaf.Raw) {
			return false
		}
	}
	return true
}

func (a *Authority) beginCollection(request CollectionRequest, source AuthoritySourceSpec, directiveSHA256 string) ([]byte, bool, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	// The request may have waited behind another provider mutation after the
	// handler's preliminary validation. Re-derive the accepted cycle and check
	// its deadline while holding the same mutation lock, before advancing the
	// high-water or publishing any reservation/event bytes.
	cycle, _, err := a.validateCollectionCycle(request, time.Now().UTC())
	if err != nil {
		return nil, false, err
	}
	if err := a.advanceCycleHighWater(cycle.CycleID, cycle.IssuedAt); err != nil {
		return nil, false, err
	}
	if len(a.pending) >= maximumPendingCollections {
		return nil, false, errors.New("native authority has reached its bounded in-flight collection limit")
	}
	if _, exists := a.pending[request.SessionID]; exists {
		return nil, false, errors.New("collection session already has an in-flight provider request")
	}
	session, cachedSettlement, err := a.loadSessionForCollection(request, source)
	if err != nil {
		return nil, false, err
	}
	if cachedSettlement != nil {
		settlement := *cachedSettlement
		if settlement.DirectiveSHA256 == directiveSHA256 && settlement.CycleContractSHA256 == request.CycleContractSHA256 &&
			settlement.CycleID == request.CycleID && settlement.CycleIssuedAt == request.CycleIssuedAt &&
			settlement.CycleDeadlineAt == request.CycleDeadlineAt && settlement.Challenge == request.Challenge &&
			settlement.PageToken == request.PageToken {
			envelopeRaw, err := decodeSettlementEnvelope(settlement)
			if err != nil {
				return nil, false, err
			}
			challengeRaw, err := authorityChallengeBytes(a.Config, settlement, digestSettlement(settlement))
			if err != nil {
				return nil, false, err
			}
			if err := a.publishLedgerObject("challenge", settlement.SessionID, settlement.Challenge, settlement.PageIndex, challengeLedgerPath(a.Config, settlement.Challenge), challengeRaw); err != nil {
				return nil, false, err
			}
			if err := validateAuthorityChallengeCrossLink(a.Config, a.Acceptance, settlement); err != nil {
				return nil, false, err
			}
			_, headRaw, err := a.ensureSessionHead(settlement)
			if err != nil {
				return nil, false, err
			}
			if err := a.ensureCollectionAcknowledged(settlement, challengeRaw, headRaw); err != nil {
				return nil, false, err
			}
			return envelopeRaw, true, nil
		}
		return nil, false, errors.New("collection page is already settled under different directive bytes")
	}
	if session.sourceID != "" && session.sourceID != source.ID {
		return nil, false, errors.New("collection session changed its source identity")
	}
	if session.nextPage > 0 && (session.cycleContractSHA256 != request.CycleContractSHA256 || session.cycleID != request.CycleID ||
		session.cycleIssuedAt != request.CycleIssuedAt || session.cycleDeadlineAt != request.CycleDeadlineAt) {
		return nil, false, errors.New("collection session changed its accepted cadence identity")
	}
	if session.terminal || request.PageIndex != session.nextPage || request.PageToken != session.nextToken {
		return nil, false, errors.New("collection session, page or token is out of sequence")
	}
	reservationPath := reservationLedgerPath(a.Config, request.Challenge)
	_ = reservationPath
	reservationRaw, err := authorityReservationBytes(a.Config, request, directiveSHA256)
	if err != nil {
		return nil, false, err
	}
	if err := a.publishLedgerObject("reservation", request.SessionID, request.Challenge, request.PageIndex, reservationLedgerPath(a.Config, request.Challenge), reservationRaw); err != nil {
		return nil, false, err
	}
	a.pending[request.SessionID] = authorityPending{
		sourceID:                 source.ID,
		cycleContractSHA256:      request.CycleContractSHA256,
		cycleID:                  request.CycleID,
		cycleIssuedAt:            request.CycleIssuedAt,
		cycleDeadlineAt:          request.CycleDeadlineAt,
		pageIndex:                request.PageIndex,
		pageToken:                request.PageToken,
		challenge:                request.Challenge,
		directiveSHA256:          directiveSHA256,
		previousSettlementSHA256: session.previousSettlementSHA256,
	}
	return nil, false, nil
}

func (a *Authority) abortCollection(sessionID string, challenge string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	pending, exists := a.pending[sessionID]
	if exists && pending.challenge == challenge {
		delete(a.pending, sessionID)
	}
}

func (a *Authority) publishLedgerObject(kind string, sessionID string, challenge string, pageIndex int, objectPath string, objectRaw []byte) error {
	a.journalMu.Lock()
	defer a.journalMu.Unlock()
	if a.journal == nil {
		return errors.New("authority ledger journal is unavailable")
	}
	return a.journal.publish(kind, sessionID, challenge, pageIndex, objectPath, objectRaw, time.Now().UTC())
}

func (a *Authority) settleCollection(request CollectionRequest, directiveSHA256 string, page NativePage, envelopeRaw []byte) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	now := time.Now().UTC().Truncate(time.Second)
	if !a.Acceptance.ProductionTrustCurrent(now) { return errors.New("native authority production trust provenance expired before settlement") }
	_, cycleDeadline, cycleErr := a.validateCollectionCycle(request, now)
	if cycleErr != nil || !now.Before(cycleDeadline) {
		return errors.New("native authority refused settlement after the accepted collection deadline")
	}
	pending, exists := a.pending[request.SessionID]
	if !exists || pending.sourceID != request.SourceID || pending.pageIndex != request.PageIndex || pending.pageToken != request.PageToken ||
		pending.cycleContractSHA256 != request.CycleContractSHA256 || pending.cycleID != request.CycleID ||
		pending.cycleIssuedAt != request.CycleIssuedAt || pending.cycleDeadlineAt != request.CycleDeadlineAt ||
		pending.challenge != request.Challenge || pending.directiveSHA256 != directiveSHA256 || page.SessionID != request.SessionID ||
		page.CycleContractSHA256 != request.CycleContractSHA256 || page.CycleID != request.CycleID ||
		page.CycleIssuedAt != request.CycleIssuedAt || page.CycleDeadlineAt != request.CycleDeadlineAt ||
		page.Challenge != request.Challenge || page.PageIndex != request.PageIndex || page.SourceID != request.SourceID {
		return errors.New("native authority refused to settle a page outside its exact in-flight directive")
	}
	session, err := loadAuthoritySessionHead(a.Config, request.SessionID, request.SourceID, request.PageIndex, a.publicKey)
	if err != nil {
		return err
	}
	if session.terminal || session.nextPage != request.PageIndex || session.nextToken != request.PageToken ||
		session.previousSettlementSHA256 != pending.previousSettlementSHA256 {
		return errors.New("native authority session changed before durable settlement")
	}
	responseRaw, decodeErr := base64.StdEncoding.DecodeString(page.RawResponseBase64)
	source := a.sources[request.SourceID]
	if decodeErr != nil || base64.StdEncoding.EncodeToString(responseRaw) != page.RawResponseBase64 ||
		session.responseBytes > source.MaximumTotalBytes-int64(len(responseRaw)) {
		return errors.New("native authority refused to settle a response beyond its source total bound")
	}
	settlement := AuthoritySettlement{
		Schema:                   AuthoritySettlementSchema,
		ClusterID:                a.Acceptance.ClusterID,
		DeploymentID:             a.Acceptance.DeploymentID,
		SourceID:                 request.SourceID,
		CycleContractSHA256:      request.CycleContractSHA256,
		CycleID:                  request.CycleID,
		CycleIssuedAt:            request.CycleIssuedAt,
		CycleDeadlineAt:          request.CycleDeadlineAt,
		SessionID:                request.SessionID,
		PageIndex:                request.PageIndex,
		PageToken:                request.PageToken,
		Challenge:                request.Challenge,
		DirectiveSHA256:          directiveSHA256,
		PreviousSettlementSHA256: session.previousSettlementSHA256,
		EnvelopeBase64:           base64.StdEncoding.EncodeToString(envelopeRaw),
		EnvelopeSHA256:           digest(envelopeRaw),
		NextToken:                page.NextToken,
		Terminal:                 page.Complete,
		SettledAt:                now.Format(time.RFC3339),
	}
	settlementRaw, err := json.Marshal(settlement)
	if err != nil {
		return err
	}
	settlementPath := filepath.Join(sessionLedgerRoot(a.Config, settlement.SessionID), fmt.Sprintf("page-%08d.json", settlement.PageIndex))
	if err := a.publishLedgerObject("settlement", settlement.SessionID, settlement.Challenge, settlement.PageIndex, settlementPath, settlementRaw); err != nil {
		return err
	}
	challengeRaw, err := authorityChallengeBytes(a.Config, settlement, digest(settlementRaw))
	if err != nil {
		return err
	}
	if err := a.publishLedgerObject("challenge", settlement.SessionID, settlement.Challenge, settlement.PageIndex, challengeLedgerPath(a.Config, settlement.Challenge), challengeRaw); err != nil {
		return err
	}
	if err := validateAuthorityChallengeCrossLink(a.Config, a.Acceptance, settlement); err != nil {
		return err
	}
	_, headRaw, err := a.ensureSessionHead(settlement)
	if err != nil {
		return err
	}
	if err := a.ensureCollectionAcknowledged(settlement, challengeRaw, headRaw); err != nil {
		return err
	}
	delete(a.pending, request.SessionID)
	return nil
}

func (a *Authority) loadSessionForCollection(request CollectionRequest, source AuthoritySourceSpec) (authoritySession, *AuthoritySettlement, error) {
	settlementPath := filepath.Join(sessionLedgerRoot(a.Config, request.SessionID), fmt.Sprintf("page-%08d.json", request.PageIndex))
	settlementRaw, err := readRootRegular(settlementPath, maximumAuthorityLedgerRecordBytes)
	if err == nil {
		var settlement AuthoritySettlement
		if canonicalJSON(settlementRaw, &settlement) != nil || settlement.SourceID != source.ID || settlement.SessionID != request.SessionID ||
			settlement.CycleContractSHA256 != request.CycleContractSHA256 || settlement.CycleID != request.CycleID ||
			settlement.CycleIssuedAt != request.CycleIssuedAt || settlement.CycleDeadlineAt != request.CycleDeadlineAt ||
			settlement.PageIndex != request.PageIndex || settlement.Challenge != request.Challenge || settlement.PageToken != request.PageToken {
			return authoritySession{}, nil, errors.New("retained collection settlement conflicts with the exact retry directive")
		}
		head, _, headErr := a.ensureSessionHead(settlement)
		if headErr != nil {
			return authoritySession{}, nil, headErr
		}
		return head, &settlement, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return authoritySession{}, nil, err
	}
	head, err := loadAuthoritySessionHead(a.Config, request.SessionID, source.ID, request.PageIndex, a.publicKey)
	return head, nil, err
}

func prepareAuthorityLedger(root string) error {
	if err := requireRootOwnedDirectory(root); err != nil {
		return err
	}
	for _, name := range []string{"sessions", "reservations", "challenges", "outcomes", "event-index", "segments"} {
		if err := ensureRootOwnedChildDirectory(root, name); err != nil {
			return err
		}
	}
	return fsyncDirectory(root)
}

func prepareAuthorityShard(config AuthorityConfig, class string, identifier string) error {
	if !canonicalNonce(identifier) || (class != "sessions" && class != "reservations" && class != "challenges" && class != "outcomes" && class != "event-index") {
		return errors.New("authority ledger shard identity is invalid")
	}
	first := filepath.Join(config.SettlementRoot, class, identifier[:2])
	second := filepath.Join(first, identifier[2:4])
	if err := ensureRootOwnedChildDirectory(filepath.Join(config.SettlementRoot, class), identifier[:2]); err != nil {
		return err
	}
	return ensureRootOwnedChildDirectory(first, identifier[2:4])
}

func ensureRootOwnedChildDirectory(parent string, name string) error {
	if filepath.Base(name) != name || name == "." || name == "" {
		return errors.New("authority ledger child directory name is unsafe")
	}
	if err := requireRootOwnedDirectory(parent); err != nil {
		return err
	}
	path := filepath.Join(parent, name)
	if err := os.Mkdir(path, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return err
	}
	if err := requireRootOwnedDirectory(path); err != nil {
		return err
	}
	return fsyncDirectory(parent)
}

func sessionLedgerRoot(config AuthorityConfig, sessionID string) string {
	return filepath.Join(config.SettlementRoot, "sessions", sessionID[:2], sessionID[2:4], sessionID)
}

func reservationLedgerPath(config AuthorityConfig, challenge string) string {
	return filepath.Join(config.SettlementRoot, "reservations", challenge[:2], challenge[2:4], challenge+".json")
}

func challengeLedgerPath(config AuthorityConfig, challenge string) string {
	return filepath.Join(config.SettlementRoot, "challenges", challenge[:2], challenge[2:4], challenge+".json")
}

func collectionOutcomePath(config AuthorityConfig, challenge string) string {
	return filepath.Join(config.SettlementRoot, "outcomes", challenge[:2], challenge[2:4], challenge+".json")
}

func ensureAuthorityLedgerCapacity(config AuthorityConfig, upcomingBytes uint64, upcomingInodes uint64) error {
	rootInfo, err := os.Lstat(config.SettlementRoot)
	if err != nil {
		return err
	}
	parentInfo, err := os.Lstat(filepath.Dir(config.SettlementRoot))
	if err != nil {
		return err
	}
	rootStat, rootOK := rootInfo.Sys().(*syscall.Stat_t)
	parentStat, parentOK := parentInfo.Sys().(*syscall.Stat_t)
	if !rootOK || !parentOK || rootStat.Dev != config.LedgerDeviceID || rootStat.Dev == parentStat.Dev {
		return errors.New("authority ledger root is not its independently accepted dedicated filesystem")
	}
	var status syscall.Statfs_t
	if err := syscall.Statfs(config.SettlementRoot, &status); err != nil {
		return err
	}
	blockSize := uint64(status.Bsize)
	if status.Blocks > ^uint64(0)/blockSize || status.Bavail > ^uint64(0)/blockSize || upcomingInodes > ^uint64(0)-config.LedgerMinimumFreeInodes {
		return errors.New("authority ledger filesystem capacity cannot be represented safely")
	}
	total := status.Blocks * blockSize
	available := status.Bavail * blockSize
	if total < config.LedgerCapacityBytes || uint64(status.Files) < config.LedgerCapacityInodes ||
		upcomingBytes > ^uint64(0)-config.LedgerMinimumFreeBytes || available < config.LedgerMinimumFreeBytes+upcomingBytes ||
		uint64(status.Ffree) < config.LedgerMinimumFreeInodes+upcomingInodes {
		return errors.New("authority ledger cannot preserve its accepted operating horizon and free-space reserve")
	}
	return nil
}

func verifyPreviousLedgerEpochAnchor(config AuthorityConfig) error {
	if err := requireRootOwnedDirectory(config.PreviousLedgerRoot); err != nil {
		return err
	}
	rootInfo, err := os.Lstat(config.PreviousLedgerRoot)
	if err != nil {
		return err
	}
	parentInfo, parentErr := os.Lstat(filepath.Dir(config.PreviousLedgerRoot))
	rootStat, rootOK := rootInfo.Sys().(*syscall.Stat_t)
	if parentErr != nil || parentInfo == nil {
		return errors.New("prior ledger parent custody cannot be inspected")
	}
	parentStat, parentOK := parentInfo.Sys().(*syscall.Stat_t)
	var filesystem syscall.Statfs_t
	statfsErr := syscall.Statfs(config.PreviousLedgerRoot, &filesystem)
	if !rootOK || !parentOK || rootStat.Dev != config.PreviousLedgerDeviceID ||
		rootStat.Dev == parentStat.Dev || statfsErr != nil || uint64(filesystem.Flags)&linuxStatfsReadOnly == 0 {
		return errors.New("prior ledger epoch is not a separately mounted, retired read-only accepted device")
	}
	priorPublicKey, err := acceptedLedgerPublicKey(config.PreviousLedgerSigningPublicKey, config.PreviousLedgerSigningKeyID)
	if err != nil {
		return fmt.Errorf("decode prior ledger signing key: %w", err)
	}
	retirementPublicKey, err := acceptedLedgerPublicKey(config.PreviousLedgerRetirementPublicKey, config.PreviousLedgerRetirementKeyID)
	if err != nil {
		return fmt.Errorf("decode ledger retirement authority key: %w", err)
	}
	retirement, err := verifyLedgerRetirementReceipt(config, retirementPublicKey)
	if err != nil {
		return err
	}
	priorConfigRaw, err := readRootRegular(config.PreviousLedgerConfigPath, maximumConfigBytes)
	if err != nil || digest(priorConfigRaw) != config.PreviousLedgerConfigSHA256 {
		return errors.New("prior ledger authority config differs from its independently accepted digest")
	}
	var priorDiscriminator struct {
		Schema string `json:"schema"`
	}
	if err := json.Unmarshal(priorConfigRaw, &priorDiscriminator); err == nil && priorDiscriminator.Schema == LegacyAuthorityConfigSchema {
		return errors.New("legacy prior ledger v1 requires a separately signed migration manifest and cannot be verified under the v2 epoch contract")
	}
	var priorConfig AuthorityConfig
	if err := canonicalJSON(priorConfigRaw, &priorConfig); err != nil {
		return errors.New("prior ledger authority config is not canonical")
	}
	if priorConfig.Schema != AuthorityConfigSchema ||
		priorConfig.ClusterID != config.ClusterID || priorConfig.DeploymentID != config.DeploymentID ||
		priorConfig.SettlementRoot != config.PreviousLedgerRoot || priorConfig.LedgerDeviceID != config.PreviousLedgerDeviceID ||
		priorConfig.LedgerEpochID != config.PreviousLedgerEpochID || priorConfig.SigningIssuer != config.PreviousLedgerSigningIssuer ||
		priorConfig.SigningKeyID != config.PreviousLedgerSigningKeyID ||
		priorConfig.PreviousLedgerEpochID != config.PreviousLedgerPredecessorEpochID ||
		priorConfig.PreviousLedgerFinalHeadSHA256 != config.PreviousLedgerPredecessorHeadSHA256 ||
		priorConfig.LedgerOperatingHorizonDays < 365 || priorConfig.LedgerOperatingHorizonDays > 3660 ||
		priorConfig.LedgerExpectedMaximumEventsPerDay == 0 || priorConfig.LedgerExpectedMaximumBytesPerDay == 0 ||
		priorConfig.LedgerExpectedMaximumInodesPerDay == 0 || len(priorConfig.Sources) == 0 {
		return errors.New("prior ledger authority config does not bind its exact historical schema, limits, sources and predecessor")
	}
	journal := &authorityLedgerJournal{
		config: priorConfig,
		publicKey: priorPublicKey,
		pendingIntents: map[string]authorityLedgerEvent{},
	}
	segments, err := journal.retainedSegmentIDs()
	if err != nil || len(segments) == 0 || segments[len(segments)-1] != config.PreviousLedgerFinalSegment {
		return errors.New("prior ledger segment inventory lacks its unique accepted terminal segment")
	}
	headSegments, err := journal.retainedCheckpointHeadSegments()
	if err != nil || len(headSegments) != len(segments) {
		return errors.New("prior ledger head inventory does not exactly cover its segment inventory")
	}
	for index := range segments {
		if headSegments[index] != segments[index] {
			return errors.New("prior ledger contains an orphan, missing or forked global head")
		}
	}
	previousCheckpointSegment := ""
	previousCheckpointSHA256 := ""
	previousHeadSegment := ""
	previousHeadSHA256 := ""
	var finalCheckpoint authorityLedgerCheckpoint
	for _, segmentID := range segments {
		journal.resetLoadedTail(segmentID)
		journal.previousCheckpointSegment = previousCheckpointSegment
		journal.previousCheckpointSHA256 = previousCheckpointSHA256
		journal.previousHeadSegment = previousHeadSegment
		journal.previousHeadSHA256 = previousHeadSHA256
		if err := journal.loadTail(segmentID, true, false); err != nil {
			return fmt.Errorf("verify prior ledger segment %s: %w", segmentID, err)
		}
		if !journal.segmentFinalized || len(journal.pendingIntents) != 0 {
			return errors.New("prior ledger terminal epoch contains an unfinalized segment or unresolved intent")
		}
		checkpointRaw, checkpoint, err := journal.readCheckpoint(segmentID)
		if err != nil || checkpoint.Payload.PreviousCheckpointSegment != previousCheckpointSegment ||
			checkpoint.Payload.PreviousCheckpointSHA256 != previousCheckpointSHA256 {
			return errors.New("prior ledger checkpoint chain skips or forks its exact predecessor")
		}
		headRaw, head, err := journal.readCheckpointHead(segmentID)
		if err != nil || head.Payload.PreviousHeadSegment != previousHeadSegment ||
			head.Payload.PreviousHeadSHA256 != previousHeadSHA256 ||
			head.Payload.CheckpointSHA256 != digest(checkpointRaw) {
			return errors.New("prior ledger signed global-head chain skips or forks its exact predecessor")
		}
		previousCheckpointSegment = segmentID
		previousCheckpointSHA256 = digest(checkpointRaw)
		previousHeadSegment = segmentID
		previousHeadSHA256 = digest(headRaw)
		finalCheckpoint = checkpoint
	}
	if previousHeadSHA256 != config.PreviousLedgerFinalHeadSHA256 ||
		retirement.FinalSegment != previousHeadSegment || retirement.FinalHeadSHA256 != previousHeadSHA256 {
		return errors.New("prior ledger configured and retired terminal heads do not identify the unique verified terminal")
	}
	finalizedAt, finalErr := time.Parse(time.RFC3339, finalCheckpoint.Payload.FinalizedAt)
	retiredAt, retireErr := time.Parse(time.RFC3339, retirement.RetiredAt)
	if finalErr != nil || retireErr != nil || retiredAt.Before(finalizedAt) {
		return errors.New("prior ledger retirement predates its verified terminal checkpoint")
	}
	return verifyPriorLedgerNamespace(journal, segments)
}

func acceptedLedgerPublicKey(encoded string, keyID string) (ed25519.PublicKey, error) {
	decoded, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil || len(decoded) != ed25519.PublicKeySize || base64.RawURLEncoding.EncodeToString(decoded) != encoded ||
		keyID != "sha256:"+digest(decoded) {
		return nil, errors.New("accepted Ed25519 public key or key ID is invalid")
	}
	return ed25519.PublicKey(decoded), nil
}

func verifyLedgerRetirementReceipt(config AuthorityConfig, publicKey ed25519.PublicKey) (ledgerEpochRetirementPayload, error) {
	if strings.HasPrefix(config.PreviousLedgerRetirementReceiptPath, config.PreviousLedgerRoot+string(filepath.Separator)) {
		return ledgerEpochRetirementPayload{}, errors.New("ledger retirement receipt is not separately custodied from the retired device")
	}
	receiptParent, err := os.Lstat(filepath.Dir(config.PreviousLedgerRetirementReceiptPath))
	if err != nil {
		return ledgerEpochRetirementPayload{}, err
	}
	parentStat, ok := receiptParent.Sys().(*syscall.Stat_t)
	if !ok || parentStat.Dev == config.PreviousLedgerDeviceID {
		return ledgerEpochRetirementPayload{}, errors.New("ledger retirement receipt shares the retired ledger device")
	}
	raw, err := readRootRegular(config.PreviousLedgerRetirementReceiptPath, maximumLedgerCheckpointBytes)
	if err != nil || digest(raw) != config.PreviousLedgerRetirementReceiptSHA256 {
		return ledgerEpochRetirementPayload{}, errors.New("ledger retirement receipt differs from independently accepted bytes")
	}
	var envelope ledgerEpochRetirementEnvelope
	if err := canonicalJSON(raw, &envelope); err != nil || envelope.Schema != ledgerEpochRetirementEnvelopeSchema ||
		envelope.Issuer != config.PreviousLedgerRetirementIssuer || envelope.KeyID != config.PreviousLedgerRetirementKeyID ||
		envelope.Algorithm != "ed25519" || !isDigest(envelope.PayloadSHA256) {
		return ledgerEpochRetirementPayload{}, errors.New("ledger retirement envelope identity is invalid")
	}
	payloadRaw, err := base64.StdEncoding.DecodeString(envelope.PayloadBase64)
	if err != nil || base64.StdEncoding.EncodeToString(payloadRaw) != envelope.PayloadBase64 || digest(payloadRaw) != envelope.PayloadSHA256 {
		return ledgerEpochRetirementPayload{}, errors.New("ledger retirement payload is not canonically content addressed")
	}
	signature, err := base64.RawURLEncoding.DecodeString(envelope.Signature)
	message := bytes.Join([][]byte{
		[]byte(ledgerEpochRetirementEnvelopeSchema), []byte(envelope.Issuer), []byte(envelope.KeyID),
		[]byte(envelope.PayloadSHA256), payloadRaw,
	}, []byte("\n"))
	if err != nil || len(signature) != ed25519.SignatureSize || base64.RawURLEncoding.EncodeToString(signature) != envelope.Signature ||
		!ed25519.Verify(publicKey, message, signature) {
		return ledgerEpochRetirementPayload{}, errors.New("ledger retirement envelope signature is invalid")
	}
	var payload ledgerEpochRetirementPayload
	if err := canonicalJSON(payloadRaw, &payload); err != nil || payload.Schema != ledgerEpochRetirementPayloadSchema ||
		payload.ClusterID != config.ClusterID || payload.DeploymentID != config.DeploymentID ||
		payload.LedgerEpochID != config.PreviousLedgerEpochID || payload.LedgerDeviceID != config.PreviousLedgerDeviceID ||
		payload.FinalSegment != config.PreviousLedgerFinalSegment || payload.FinalHeadSHA256 != config.PreviousLedgerFinalHeadSHA256 ||
		payload.WriterState != "retired-read-only" {
		return ledgerEpochRetirementPayload{}, errors.New("ledger retirement payload does not bind the exact retired epoch")
	}
	retiredAt, err := time.Parse(time.RFC3339, payload.RetiredAt)
	if err != nil || retiredAt.Nanosecond() != 0 || !strings.HasSuffix(payload.RetiredAt, "Z") {
		return ledgerEpochRetirementPayload{}, errors.New("ledger retirement timestamp is not canonical")
	}
	return payload, nil
}

func verifyPriorLedgerNamespace(journal *authorityLedgerJournal, segments []string) error {
	expectedFiles := map[string]struct{}{}
	expectedDirectories := map[string]struct{}{ ".": {} }
	addFile := func(path string) error {
		relative, err := safeRelativeLedgerPath(journal.config.SettlementRoot, path)
		if err != nil {
			return err
		}
		expectedFiles[filepath.FromSlash(relative)] = struct{}{}
		current := filepath.Dir(filepath.FromSlash(relative))
		for current != "." && current != string(filepath.Separator) {
			expectedDirectories[current] = struct{}{}
			current = filepath.Dir(current)
		}
		return nil
	}
	for _, name := range []string{"sessions", "reservations", "challenges", "outcomes", "event-index", "segments", "checkpoint-heads"} {
		expectedDirectories[name] = struct{}{}
	}
	probePath := filepath.Join(journal.config.SettlementRoot, "anonymous-publication-probe-v1")
	probeExpected := []byte("fs2-public-edge-authority-anonymous-publication/v1\n")
	probeRaw, err := readRootRegular(probePath, int64(len(probeExpected)))
	if err != nil || !bytes.Equal(probeRaw, probeExpected) {
		return errors.New("prior ledger anonymous-publication capability evidence is absent or changed")
	}
	if err := addFile(probePath); err != nil {
		return err
	}
	for _, segmentID := range segments {
		segmentRoot := journal.segmentRoot(segmentID)
		for _, directory := range []string{"", "events", "prepared", filepath.Join("prepared", "archive"), "checkpoints"} {
			relative, err := filepath.Rel(journal.config.SettlementRoot, filepath.Join(segmentRoot, directory))
			if err != nil {
				return err
			}
			expectedDirectories[relative] = struct{}{}
		}
		if err := addFile(journal.checkpointPath(segmentID)); err != nil {
			return err
		}
		if err := addFile(journal.checkpointHeadPath(segmentID)); err != nil {
			return err
		}
		entries, err := os.ReadDir(journal.eventsRoot(segmentID))
		if err != nil {
			return err
		}
		for _, entry := range entries {
			sequence, ok := parseEventSequence(entry.Name())
			if !ok || entry.IsDir() {
				return errors.New("prior ledger segment contains an unexpected event namespace entry")
			}
			eventPath := filepath.Join(journal.eventsRoot(segmentID), entry.Name())
			_, event, err := journal.readEvent(eventPath)
			if err != nil || event.Payload.Sequence != sequence {
				return errors.New("prior ledger event inventory cannot be reconstructed")
			}
			if err := addFile(eventPath); err != nil {
				return err
			}
			indexPath, err := journal.eventIndexPath(event.Payload.Challenge, event.Payload.Kind, event.Payload.Phase)
			if err != nil || journal.verifyEventIndex(eventPath, indexPath) != nil {
				return errors.New("prior ledger event lacks its exact direct index")
			}
			if err := addFile(indexPath); err != nil {
				return err
			}
			if event.Payload.Phase == "terminal" {
				objectPath := filepath.Join(journal.config.SettlementRoot, filepath.FromSlash(event.Payload.ObjectPath))
				if err := addFile(objectPath); err != nil {
					return err
				}
				identity := authorityPreparedIdentity{
					kind: event.Payload.Kind, sessionID: event.Payload.SessionID, challenge: event.Payload.Challenge,
					pageIndex: event.Payload.PageIndex, sha256: event.Payload.ObjectSHA256, objectBytes: event.Payload.ObjectBytes,
				}
				archivePath, err := journal.preparedArchivePath(segmentID, identity)
				if err != nil {
					return err
				}
				if err := addFile(archivePath); err != nil {
					return err
				}
			}
		}
	}
	seenFiles := map[string]struct{}{}
	err = filepath.WalkDir(journal.config.SettlementRoot, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(journal.config.SettlementRoot, path)
		if err != nil || filepath.IsAbs(relative) || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
			return errors.New("prior ledger namespace escapes its accepted root")
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return errors.New("prior ledger namespace contains a symbolic link")
		}
		if entry.IsDir() {
			if _, expected := expectedDirectories[relative]; !expected {
				return errors.New("prior ledger namespace contains an orphan directory")
			}
			return requireRootOwnedDirectory(path)
		}
		if _, expected := expectedFiles[relative]; !expected {
			return errors.New("prior ledger namespace contains an orphan or unanchored file")
		}
		info, err := entry.Info()
		if err != nil || info == nil {
			return errors.New("prior ledger retained file metadata cannot be read")
		}
		stat, statOK := info.Sys().(*syscall.Stat_t)
		if !statOK || stat.Uid != 0 || !info.Mode().IsRegular() || info.Mode().Perm()&0o222 != 0 {
			return errors.New("prior ledger retained file is not immutable root custody")
		}
		seenFiles[relative] = struct{}{}
		return nil
	})
	if err != nil || len(seenFiles) != len(expectedFiles) {
		return errors.New("prior ledger namespace is incomplete or contains unverified retained history")
	}
	return nil
}

type authorityLedgerDailyBounds struct {
	bytes  uint64
	events uint64
	inodes uint64
}

func derivedAuthorityLedgerDailyBounds(config AuthorityConfig) (authorityLedgerDailyBounds, error) {
	cycles := uint64((86400 + config.CollectorRefreshIntervalSeconds - 1) / config.CollectorRefreshIntervalSeconds)
	var perCycleBytes uint64
	var perCycleEvents uint64
	for _, source := range config.Sources {
		total := uint64(source.MaximumTotalBytes)
		pages := uint64(source.MaximumPages)
		perSourceBytes, ok := maximumRetainedAuthorityBytes(total, pages)
		if !ok {
			return authorityLedgerDailyBounds{}, errors.New("authority ledger source-derived byte horizon overflows")
		}
		perCycleBytes, ok = checkedAdd(perCycleBytes, perSourceBytes)
		if !ok {
			return authorityLedgerDailyBounds{}, errors.New("authority ledger source-derived byte horizon overflows")
		}
		// Each page durably publishes reservation, settlement, challenge and
		// session-head objects, then exactly one immutable terminal outcome.
		// Every publication has one started and one terminal semantic event.
		events, ok := checkedMultiply(pages, 10)
		if !ok {
			return authorityLedgerDailyBounds{}, errors.New("authority ledger source-derived event horizon overflows")
		}
		perCycleEvents, ok = checkedAdd(perCycleEvents, events)
		if !ok {
			return authorityLedgerDailyBounds{}, errors.New("authority ledger source-derived event horizon overflows")
		}
	}
	dailyBytes, ok := checkedMultiply(perCycleBytes, cycles)
	if !ok {
		return authorityLedgerDailyBounds{}, errors.New("authority ledger daily byte horizon overflows")
	}
	dailyEvents, ok := checkedMultiply(perCycleEvents, cycles)
	if !ok {
		return authorityLedgerDailyBounds{}, errors.New("authority ledger daily event horizon overflows")
	}
	// Derive physical storage from the semantic-event ceiling, not from an
	// assumed successful four-object page. The most expensive complete two-event
	// publication is a fresh-session settlement: one prepared/archive inode, two
	// archive shards, two session shards, the session directory, one final-object
	// inode, two event inodes and two event-index shards = 11. A crash-left single
	// started event can retain at most the prepared inode, archive and destination
	// directories, final object, one event and two event-index shards = 10. The journal
	// permits only one unresolved intent, then deterministically settles it at
	// restart, so no other incomplete-prefix ratio is possible.
	pairs := dailyEvents / 2
	dailyInodes, ok := checkedMultiply(pairs, 11)
	if !ok {
		return authorityLedgerDailyBounds{}, errors.New("authority ledger daily inode horizon overflows")
	}
	if dailyEvents%2 != 0 {
		dailyInodes, ok = checkedAdd(dailyInodes, 10)
		if !ok {
			return authorityLedgerDailyBounds{}, errors.New("authority ledger daily incomplete-prefix inode horizon overflows")
		}
	}
	// One UTC segment directory plus events, prepared, prepared/archive and
	// checkpoints directories, one checkpoint inode, one independently signed
	// global-head inode, and the global-head directory's first-day cost. The
	// directory cost is deliberately charged every day so the accepted horizon
	// remains conservative without depending on which day creates it.
	dailyFixedBytes, ok := checkedAdd(maximumLedgerCheckpointBytes, maximumLedgerHeadBytes)
	if !ok {
		return authorityLedgerDailyBounds{}, errors.New("authority ledger fixed daily byte horizon overflows")
	}
	dailyBytes, ok = checkedAdd(dailyBytes, dailyFixedBytes)
	if !ok {
		return authorityLedgerDailyBounds{}, errors.New("authority ledger checkpoint/head byte horizon overflows")
	}
	dailyInodes, ok = checkedAdd(dailyInodes, 9)
	if !ok {
		return authorityLedgerDailyBounds{}, errors.New("authority ledger checkpoint/head inode horizon overflows")
	}
	return authorityLedgerDailyBounds{bytes: dailyBytes, events: dailyEvents, inodes: dailyInodes}, nil
}

func maximumRetainedAuthorityBytes(totalResponseBytes uint64, pages uint64) (uint64, bool) {
	if pages == 0 {
		return 0, false
	}
	// All string-bearing protocol fields have exact byte caps. Six is the
	// encoding/json worst-case expansion for an accepted UTF-8 byte appearing as
	// a JSON escape. Partitioned base64 bounds add two bytes of rounding per page
	// at each nesting layer.
	requestTextBytes := uint64(maximumSourceIDBytes + maximumNativeURLBytes + maximumCredentialLaneIDBytes +
		3*maximumBearerClaimBytes + maximumBearerAlgorithmBytes + maximumBearerKeyIDBytes +
		maximumBearerGenerationBytes + 4*64 + len(time.RFC3339))
	requestEscaped, ok := checkedMultiply(requestTextBytes, 6)
	if !ok {
		return 0, false
	}
	maximumNativeRequestBytes, ok := checkedAdd(requestEscaped, 2048)
	if !ok {
		return 0, false
	}
	requestTotal, ok := checkedMultiply(maximumNativeRequestBytes, pages)
	if !ok {
		return 0, false
	}
	requestBase64, ok := base64PartitionBound(requestTotal, pages)
	if !ok {
		return 0, false
	}
	pageTextBytes := uint64(maximumSourceIDBytes + maximumSourceKindBytes + maximumSemanticCollectionBytes +
		maximumProviderRequestIDBytes + 2*maximumNativeURLBytes + maximumPageTokenBytes + 6*64 + 2*len(time.RFC3339))
	pageEscaped, ok := checkedMultiply(pageTextBytes, 6)
	if !ok {
		return 0, false
	}
	pageFraming, ok := checkedAdd(pageEscaped, 4096)
	if !ok {
		return 0, false
	}
	pageFramingTotal, ok := checkedMultiply(pageFraming, pages)
	if !ok {
		return 0, false
	}
	pageFramingTotal, ok = checkedAdd(pageFramingTotal, requestBase64)
	if !ok {
		return 0, false
	}
	responseBase64, ok := base64PartitionBound(totalResponseBytes, pages)
	if !ok {
		return 0, false
	}
	pagePayloadTotal, ok := checkedAdd(pageFramingTotal, responseBase64)
	if !ok {
		return 0, false
	}
	payloadBase64, ok := base64PartitionBound(pagePayloadTotal, pages)
	if !ok {
		return 0, false
	}
	envelopeFramingPerPage := uint64(2048 + 6*(maximumSigningIssuerBytes+maximumSigningKeyIDBytes))
	envelopeFraming, ok := checkedMultiply(envelopeFramingPerPage, pages)
	if !ok {
		return 0, false
	}
	envelopeTotal, ok := checkedAdd(payloadBase64, envelopeFraming)
	if !ok {
		return 0, false
	}
	envelopeBase64, ok := base64PartitionBound(envelopeTotal, pages)
	if !ok {
		return 0, false
	}
	settlementTextBytes := uint64(maximumClusterIDBytes + maximumDeploymentIDBytes + maximumSourceIDBytes +
		2*maximumPageTokenBytes + 10*64 + 2*len(time.RFC3339))
	settlementEscaped, ok := checkedMultiply(settlementTextBytes, 6)
	if !ok {
		return 0, false
	}
	settlementFramingPerPage, ok := checkedAdd(settlementEscaped, 4096)
	if !ok {
		return 0, false
	}
	settlementFraming, ok := checkedMultiply(settlementFramingPerPage, pages)
	if !ok {
		return 0, false
	}
	settlementTotal, ok := checkedAdd(envelopeBase64, settlementFraming)
	if !ok {
		return 0, false
	}
	preparedSettlementBase64, ok := base64PartitionBound(settlementTotal, pages)
	if !ok {
		return 0, false
	}
	preparedSettlementFraming, ok := checkedMultiply(pages, 4096)
	if !ok {
		return 0, false
	}
	preparedSettlementTotal, ok := checkedAdd(preparedSettlementBase64, preparedSettlementFraming)
	if !ok {
		return 0, false
	}
	otherRawPerPage := uint64(2*maximumAuthorityReservationBytes + maximumSessionHeadBytes + maximumAuthorityOutcomeBytes)
	otherPreparedPerPage, ok := base64PartitionBound(otherRawPerPage, 4)
	if !ok {
		return 0, false
	}
	otherPreparedPerPage, ok = checkedAdd(otherPreparedPerPage, 4*4096)
	if !ok {
		return 0, false
	}
	otherPerPage, ok := checkedAdd(otherRawPerPage, otherPreparedPerPage)
	if !ok {
		return 0, false
	}
	otherPerPage, ok = checkedAdd(otherPerPage, 10*maximumLedgerEventBytes)
	if !ok {
		return 0, false
	}
	otherTotal, ok := checkedMultiply(otherPerPage, pages)
	if !ok {
		return 0, false
	}
	retained, ok := checkedAdd(settlementTotal, preparedSettlementTotal)
	if !ok {
		return 0, false
	}
	return checkedAdd(retained, otherTotal)
}

// maximumAuthorityPageRecordBounds derives the two largest individual retained
// records for one maximally sized native response. This is deliberately separate
// from the aggregate horizon calculation: the settlement and its prepared copy
// have different hard readers and independently rounded base64 encodings.
func maximumAuthorityPageRecordBounds(responseBytes uint64) (uint64, uint64, bool) {
	requestTextBytes := uint64(maximumSourceIDBytes + maximumNativeURLBytes + maximumCredentialLaneIDBytes +
		3*maximumBearerClaimBytes + maximumBearerAlgorithmBytes + maximumBearerKeyIDBytes +
		maximumBearerGenerationBytes + 4*64 + len(time.RFC3339))
	requestEscaped, ok := checkedMultiply(requestTextBytes, 6)
	if !ok {
		return 0, 0, false
	}
	requestBytes, ok := checkedAdd(requestEscaped, 2048)
	if !ok {
		return 0, 0, false
	}
	requestBase64, ok := base64EncodedBound(requestBytes)
	if !ok {
		return 0, 0, false
	}
	pageTextBytes := uint64(maximumSourceIDBytes + maximumSourceKindBytes + maximumSemanticCollectionBytes +
		maximumProviderRequestIDBytes + 2*maximumNativeURLBytes + maximumPageTokenBytes + 6*64 + 2*len(time.RFC3339))
	pageEscaped, ok := checkedMultiply(pageTextBytes, 6)
	if !ok {
		return 0, 0, false
	}
	pageBytes, ok := checkedAdd(pageEscaped, 4096)
	if !ok {
		return 0, 0, false
	}
	pageBytes, ok = checkedAdd(pageBytes, requestBase64)
	if !ok {
		return 0, 0, false
	}
	responseBase64, ok := base64EncodedBound(responseBytes)
	if !ok {
		return 0, 0, false
	}
	pageBytes, ok = checkedAdd(pageBytes, responseBase64)
	if !ok {
		return 0, 0, false
	}
	payloadBase64, ok := base64EncodedBound(pageBytes)
	if !ok {
		return 0, 0, false
	}
	envelopeFraming := uint64(2048 + 6*(maximumSigningIssuerBytes+maximumSigningKeyIDBytes))
	envelopeBytes, ok := checkedAdd(payloadBase64, envelopeFraming)
	if !ok {
		return 0, 0, false
	}
	envelopeBase64, ok := base64EncodedBound(envelopeBytes)
	if !ok {
		return 0, 0, false
	}
	settlementTextBytes := uint64(maximumClusterIDBytes + maximumDeploymentIDBytes + maximumSourceIDBytes +
		2*maximumPageTokenBytes + 10*64 + 2*len(time.RFC3339))
	settlementEscaped, ok := checkedMultiply(settlementTextBytes, 6)
	if !ok {
		return 0, 0, false
	}
	settlementFraming, ok := checkedAdd(settlementEscaped, 4096)
	if !ok {
		return 0, 0, false
	}
	settlementBytes, ok := checkedAdd(envelopeBase64, settlementFraming)
	if !ok {
		return 0, 0, false
	}
	preparedBase64, ok := base64EncodedBound(settlementBytes)
	if !ok {
		return 0, 0, false
	}
	preparedBytes, ok := checkedAdd(preparedBase64, 4096)
	if !ok {
		return 0, 0, false
	}
	return settlementBytes, preparedBytes, true
}

func base64PartitionBound(total uint64, parts uint64) (uint64, bool) {
	rounding, ok := checkedMultiply(parts, 2)
	if !ok {
		return 0, false
	}
	adjusted, ok := checkedAdd(total, rounding)
	if !ok {
		return 0, false
	}
	groups := adjusted / 3
	return checkedMultiply(groups, 4)
}

func base64EncodedBound(size uint64) (uint64, bool) {
	return base64PartitionBound(size, 1)
}

func checkedAdd(left uint64, right uint64) (uint64, bool) {
	if left > ^uint64(0)-right {
		return 0, false
	}
	return left + right, true
}

func checkedMultiply(left uint64, right uint64) (uint64, bool) {
	if left != 0 && right > ^uint64(0)/left {
		return 0, false
	}
	return left * right, true
}

func requireRootOwnedDirectory(path string) error {
	clean := filepath.Clean(path)
	if !filepath.IsAbs(clean) || clean == "/" {
		return errors.New("authority ledger path is not a dedicated absolute directory")
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
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || stat.Uid != 0 || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0o022 != 0 {
			return errors.New("authority ledger directory ancestry is not root-owned and non-writable")
		}
	}
	return nil
}

func recoverAuthorityLedger(config AuthorityConfig, acceptance boundary.Acceptance, publicKey ed25519.PublicKey) (authorityLedgerCounts, error) {
	if acceptance.ClusterID != config.ClusterID || acceptance.DeploymentID != config.DeploymentID || len(publicKey) != ed25519.PublicKeySize {
		return authorityLedgerCounts{}, errors.New("authority ledger recovery identity differs from acceptance")
	}
	// Historical roots are immutable evidence and are not enumerated at startup.
	// A retry addresses its sharded session and challenge records directly, then
	// reopens the complete signed chain before replay. This keeps recovery cost
	// bounded by maximumAuthoritySessionPages rather than retained history.
	for _, directory := range []string{"sessions", "reservations", "challenges"} {
		if err := requireRootOwnedDirectory(filepath.Join(config.SettlementRoot, directory)); err != nil {
			return authorityLedgerCounts{}, err
		}
	}
	return authorityLedgerCounts{}, nil
}

func loadAuthoritySession(config AuthorityConfig, sessionID string, expectedSource string, publicKey ed25519.PublicKey) (authoritySession, error) {
	state := authoritySession{sourceID: expectedSource, settlements: map[int]AuthoritySettlement{}}
	if !canonicalNonce(sessionID) {
		return state, errors.New("authority session ID is not canonical")
	}
	sessionRoot := sessionLedgerRoot(config, sessionID)
	entries, err := os.ReadDir(sessionRoot)
	if errors.Is(err, os.ErrNotExist) {
		return state, nil
	}
	if err != nil {
		return state, err
	}
	if err := requireRootOwnedDirectory(sessionRoot); err != nil {
		return state, err
	}
	pageEntries := make([]os.DirEntry, 0, len(entries))
	for _, entry := range entries {
		if entry.Name() == ".attempts" {
			if !entry.IsDir() {
				return state, errors.New("authority session attempt namespace is not a directory")
			}
			continue
		}
		pageEntries = append(pageEntries, entry)
	}
	if len(pageEntries) > maximumAuthoritySessionPages {
		return state, errors.New("authority session exceeds its bounded page count")
	}
	if err := validateSessionAttemptDirectory(sessionRoot); err != nil {
		return state, err
	}
	for index, entry := range pageEntries {
		expectedName := fmt.Sprintf("page-%08d.json", index)
		if entry.IsDir() || entry.Name() != expectedName {
			return state, errors.New("authority session ledger is non-contiguous or contains an unexpected entry")
		}
		raw, err := readRootRegular(filepath.Join(sessionRoot, entry.Name()), maximumAuthorityLedgerRecordBytes)
		if err != nil {
			return state, err
		}
		var settlement AuthoritySettlement
		if err := canonicalJSON(raw, &settlement); err != nil {
			return state, err
		}
		responseBytes, err := validateAuthoritySettlement(config, settlement, index, state, publicKey)
		if err != nil {
			return state, err
		}
		source, exists := authoritySource(config, settlement.SourceID)
		if !exists || responseBytes < 0 || state.responseBytes > source.MaximumTotalBytes-responseBytes {
			return state, errors.New("authority session exceeds its enrolled total provider response bound")
		}
		state.responseBytes += responseBytes
		if state.sourceID == "" {
			state.sourceID = settlement.SourceID
			state.cycleContractSHA256 = settlement.CycleContractSHA256
			state.cycleID = settlement.CycleID
			state.cycleIssuedAt = settlement.CycleIssuedAt
			state.cycleDeadlineAt = settlement.CycleDeadlineAt
		}
		state.settlements[index] = settlement
		state.nextPage = index + 1
		state.nextToken = settlement.NextToken
		state.terminal = settlement.Terminal
		state.previousSettlementSHA256 = digest(raw)
	}
	return state, nil
}

func validateSessionAttemptDirectory(sessionRoot string) error {
	attemptRoot := filepath.Join(sessionRoot, ".attempts")
	entries, err := os.ReadDir(attemptRoot)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if err := requireRootOwnedDirectory(attemptRoot); err != nil {
		return err
	}
	if len(entries) > maximumAuthoritySessionPages {
		return errors.New("authority session has too many retained publication attempts")
	}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || len(name) <= 65 || name[len(name)-65] != '-' || !isDigest(name[len(name)-64:]) {
			return errors.New("authority session contains a malformed retained publication attempt")
		}
		finalName := name[:len(name)-65]
		if !strings.HasPrefix(finalName, "page-") || !strings.HasSuffix(finalName, ".json") || len(finalName) != len("page-00000000.json") {
			return errors.New("authority session attempt does not target a page record")
		}
		pageIndex, parseErr := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(finalName, "page-"), ".json"))
		if parseErr != nil || pageIndex < 0 || pageIndex >= maximumAuthoritySessionPages || finalName != fmt.Sprintf("page-%08d.json", pageIndex) {
			return errors.New("authority session attempt page identity is invalid")
		}
		attemptInfo, statErr := os.Lstat(filepath.Join(attemptRoot, name))
		if statErr != nil || attemptInfo == nil {
			return errors.New("authority session attempt cannot be inspected")
		}
		attemptStat, statOK := attemptInfo.Sys().(*syscall.Stat_t)
		if !statOK || attemptStat.Uid != 0 || !attemptInfo.Mode().IsRegular() || attemptInfo.Mode().Perm() != 0o400 ||
			attemptInfo.Size() < 0 || attemptInfo.Size() > maximumAuthorityLedgerRecordBytes {
			return errors.New("authority session attempt is not a bounded root-owned retained record")
		}
		finalInfo, finalErr := os.Lstat(filepath.Join(sessionRoot, finalName))
		if finalErr == nil {
			if !finalInfo.Mode().IsRegular() || !os.SameFile(attemptInfo, finalInfo) {
				return errors.New("authority session published page is not linked to its exact retained attempt")
			}
			finalRaw, readErr := readRootRegular(filepath.Join(sessionRoot, finalName), maximumAuthorityLedgerRecordBytes)
			if readErr != nil || digest(finalRaw) != name[len(name)-64:] {
				return errors.New("authority session published page differs from its retained attempt digest")
			}
		}
		if finalErr != nil && !errors.Is(finalErr, os.ErrNotExist) {
			return finalErr
		}
	}
	return nil
}

func validateAuthoritySettlement(config AuthorityConfig, settlement AuthoritySettlement, expectedPage int, previous authoritySession, publicKey ed25519.PublicKey) (int64, error) {
	cycleIssuedAt, issueErr := time.Parse(time.RFC3339, settlement.CycleIssuedAt)
	cycleDeadlineAt, deadlineErr := time.Parse(time.RFC3339, settlement.CycleDeadlineAt)
	settledAt, err := time.Parse(time.RFC3339, settlement.SettledAt)
	if issueErr != nil || deadlineErr != nil || err != nil || cycleIssuedAt.Nanosecond() != 0 || cycleDeadlineAt.Nanosecond() != 0 ||
		settledAt.Nanosecond() != 0 || !strings.HasSuffix(settlement.CycleIssuedAt, "Z") ||
		!strings.HasSuffix(settlement.CycleDeadlineAt, "Z") || !strings.HasSuffix(settlement.SettledAt, "Z") ||
		cycleDeadlineAt.Sub(cycleIssuedAt) != time.Duration(config.CollectorCollectionDeadlineSeconds)*time.Second ||
		settledAt.Before(cycleIssuedAt) || !settledAt.Before(cycleDeadlineAt) {
		return 0, errors.New("authority settlement timestamp is not canonical")
	}
	if settlement.Schema != AuthoritySettlementSchema || settlement.ClusterID != config.ClusterID || settlement.DeploymentID != config.DeploymentID ||
		settlement.SourceID == "" || settlement.SessionID == "" || settlement.PageIndex != expectedPage || settlement.PageToken != previous.nextToken ||
		!isDigest(settlement.CycleContractSHA256) || !isDigest(settlement.CycleID) ||
		!canonicalNonce(settlement.SessionID) || !canonicalNonce(settlement.Challenge) || !isDigest(settlement.DirectiveSHA256) ||
		!isDigest(settlement.EnvelopeSHA256) || settlement.PreviousSettlementSHA256 != previous.previousSettlementSHA256 ||
		(previous.sourceID != "" && settlement.SourceID != previous.sourceID) || settlement.Terminal == (settlement.NextToken != "") || previous.terminal {
		return 0, errors.New("authority settlement does not extend its exact append-only session chain")
	}
	expectedSessionID := digest([]byte(strings.Join([]string{
		"fs2-serve.nebius.ai/public-edge-native-collection-session/v2",
		settlement.CycleContractSHA256, settlement.SourceID,
	}, "\n")))
	expectedChallenge := cycleChallenge(CollectorCycle{
		ContractSHA256: settlement.CycleContractSHA256,
		CycleID: settlement.CycleID,
		IssuedAt: settlement.CycleIssuedAt,
		DeadlineAt: settlement.CycleDeadlineAt,
	}, settlement.SourceID, expectedSessionID, settlement.PageIndex, settlement.PageToken)
	if settlement.SessionID != expectedSessionID || settlement.Challenge != expectedChallenge {
		return 0, errors.New("authority settlement session or challenge is not derived from its signed cycle")
	}
	if expectedPage > 0 && (settlement.CycleContractSHA256 != previous.cycleContractSHA256 || settlement.CycleID != previous.cycleID ||
		settlement.CycleIssuedAt != previous.cycleIssuedAt || settlement.CycleDeadlineAt != previous.cycleDeadlineAt) {
		return 0, errors.New("authority settlement changes its append-only collection cycle")
	}
	envelopeRaw, err := decodeSettlementEnvelope(settlement)
	if err != nil {
		return 0, err
	}
	return validateRetainedEnvelope(config, settlement, envelopeRaw, publicKey)
}

func decodeSettlementEnvelope(settlement AuthoritySettlement) ([]byte, error) {
	raw, err := base64.StdEncoding.DecodeString(settlement.EnvelopeBase64)
	if err != nil || base64.StdEncoding.EncodeToString(raw) != settlement.EnvelopeBase64 || digest(raw) != settlement.EnvelopeSHA256 {
		return nil, errors.New("authority settlement envelope is not canonically content addressed")
	}
	return raw, nil
}

func validateRetainedEnvelope(config AuthorityConfig, settlement AuthoritySettlement, envelopeRaw []byte, publicKey ed25519.PublicKey) (int64, error) {
	directiveRaw, err := json.Marshal(CollectionRequest{
		Schema:       CollectionRequestSchema,
		ClusterID:    config.ClusterID,
		DeploymentID: config.DeploymentID,
		SourceID:     settlement.SourceID,
		CycleContractSHA256: settlement.CycleContractSHA256,
		CycleID:      settlement.CycleID,
		CycleIssuedAt: settlement.CycleIssuedAt,
		CycleDeadlineAt: settlement.CycleDeadlineAt,
		SessionID:    settlement.SessionID,
		PageIndex:    settlement.PageIndex,
		PageToken:    settlement.PageToken,
		Challenge:    settlement.Challenge,
	})
	if err != nil || digest(directiveRaw) != settlement.DirectiveSHA256 {
		return 0, errors.New("retained settlement does not bind the canonical collection directive")
	}
	var envelope boundary.SignedEnvelope
	if err := canonicalJSON(envelopeRaw, &envelope); err != nil || envelope.Schema != NativeEnvelopeSchema || envelope.Algorithm != "ed25519" ||
		envelope.Issuer != config.SigningIssuer || envelope.KeyID != config.SigningKeyID || !isDigest(envelope.PayloadSHA256) {
		return 0, errors.New("retained native envelope has an invalid canonical signing identity")
	}
	payloadRaw, err := base64.StdEncoding.DecodeString(envelope.PayloadBase64)
	if err != nil || base64.StdEncoding.EncodeToString(payloadRaw) != envelope.PayloadBase64 || digest(payloadRaw) != envelope.PayloadSHA256 {
		return 0, errors.New("retained native envelope payload is not canonically content addressed")
	}
	signature, err := base64.RawURLEncoding.DecodeString(envelope.Signature)
	if err != nil || len(signature) != ed25519.SignatureSize || base64.RawURLEncoding.EncodeToString(signature) != envelope.Signature {
		return 0, errors.New("retained native envelope signature is not canonical Ed25519 bytes")
	}
	message := bytes.Join([][]byte{
		[]byte(NativeEnvelopeSchema),
		[]byte(config.SigningIssuer),
		[]byte(config.SigningKeyID),
		[]byte(envelope.PayloadSHA256),
		payloadRaw,
	}, []byte("\n"))
	if len(publicKey) != ed25519.PublicKeySize || !ed25519.Verify(publicKey, message, signature) {
		return 0, errors.New("retained native envelope Ed25519 signature is invalid")
	}
	var page NativePage
	if err := canonicalJSON(payloadRaw, &page); err != nil {
		return 0, err
	}
	source, exists := authoritySource(config, settlement.SourceID)
	if !exists {
		return 0, errors.New("retained native page refers to an unenrolled source")
	}
	expectedURL, err := authorityPageURL(source, settlement.PageToken)
	if err != nil {
		return 0, err
	}
	requestRaw, err := base64.StdEncoding.DecodeString(page.RequestBase64)
	if err != nil || base64.StdEncoding.EncodeToString(requestRaw) != page.RequestBase64 || digest(requestRaw) != page.RequestSHA256 {
		return 0, errors.New("retained native provider request is not canonically content addressed")
	}
	var providerRequest NativeRequest
	if err := canonicalJSON(requestRaw, &providerRequest); err != nil {
		return 0, err
	}
	expectedContentType := "application/json"
	if source.Kind == "ca-revocation" {
		expectedContentType = "application/pkix-crl"
	}
	if providerRequest.Schema != NativeRequestSchema || providerRequest.SourceID != source.ID || !canonicalNonce(providerRequest.RequestID) ||
		providerRequest.RequestID != page.RequestID || providerRequest.Method != http.MethodGet || providerRequest.URL != expectedURL ||
		providerRequest.BodySHA256 != digest(nil) || len(providerRequest.Headers) != 2 ||
		providerRequest.Headers["accept"] != expectedContentType || providerRequest.Headers["x-fs2-request-id"] != providerRequest.RequestID ||
		providerRequest.CredentialLaneID != source.CredentialLaneID ||
		providerRequest.CredentialIssuer != source.BearerTokenIssuer ||
		providerRequest.CredentialAudience != source.BearerTokenAudience ||
		providerRequest.CredentialSubject != source.BearerTokenSubject ||
		providerRequest.CredentialAlgorithm != source.BearerTokenAlgorithm ||
		providerRequest.CredentialKeyID != source.BearerTokenKeyID ||
		providerRequest.CredentialJWKSHA256 != source.BearerTokenJWKSSHA256 {
		return 0, errors.New("retained native provider request differs from its exact source contract")
	}
	responseRaw, err := base64.StdEncoding.DecodeString(page.RawResponseBase64)
	if err != nil || base64.StdEncoding.EncodeToString(responseRaw) != page.RawResponseBase64 || digest(responseRaw) != page.RawResponseSHA256 {
		return 0, errors.New("retained native provider response is not canonically content addressed")
	}
	collectedAt, err := time.Parse(time.RFC3339, page.CollectedAt)
	cycleIssuedAt, cycleIssueErr := time.Parse(time.RFC3339, settlement.CycleIssuedAt)
	cycleDeadlineAt, cycleDeadlineErr := time.Parse(time.RFC3339, settlement.CycleDeadlineAt)
	if err != nil || cycleIssueErr != nil || cycleDeadlineErr != nil || collectedAt.Nanosecond() != 0 ||
		cycleIssuedAt.Nanosecond() != 0 || cycleDeadlineAt.Nanosecond() != 0 ||
		!strings.HasSuffix(page.CollectedAt, "Z") || collectedAt.Before(cycleIssuedAt) || !collectedAt.Before(cycleDeadlineAt) {
		return 0, errors.New("retained native page collection time is not canonical")
	}
	if source.BearerTokenPath == "" {
		if providerRequest.CredentialExpiresAt != "" || providerRequest.CredentialGeneration != "" {
			return 0, errors.New("retained non-bearer provider request carries an unenrolled credential identity")
		}
	} else {
		credentialExpiresAt, credentialExpiryErr := time.Parse(time.RFC3339, providerRequest.CredentialExpiresAt)
		if credentialExpiryErr != nil || credentialExpiresAt.Nanosecond() != 0 ||
			!isDigest(providerRequest.CredentialGeneration) || !collectedAt.Before(credentialExpiresAt) {
			return 0, errors.New("retained provider request is not bound to a current verified bearer generation")
		}
	}
	settledAt, _ := time.Parse(time.RFC3339, settlement.SettledAt)
	if settledAt.Before(collectedAt) || settledAt.After(collectedAt.Add(30*time.Second)) {
		return 0, errors.New("retained native settlement time is inconsistent with collection")
	}
	complete, nextToken, nextURL, err := derivePaginationForContent(source.Pagination, expectedURL, responseRaw)
	if err != nil {
		return 0, err
	}
	if page.Schema != NativePageSchema || page.SourceID != source.ID || page.SourceKind != source.Kind ||
		page.SemanticCollection != source.SemanticCollection || !boundedProtocolText(page.ProviderRequestID, maximumProviderRequestIDBytes, false) ||
		!boundedProtocolText(page.RequestedURL, maximumNativeURLBytes, false) || !boundedProtocolText(page.NextURL, maximumNativeURLBytes, true) ||
		!boundedProtocolText(page.NextToken, maximumPageTokenBytes, true) ||
		page.CycleContractSHA256 != settlement.CycleContractSHA256 || page.CycleID != settlement.CycleID ||
		page.CycleIssuedAt != settlement.CycleIssuedAt || page.CycleDeadlineAt != settlement.CycleDeadlineAt ||
		page.SessionID != settlement.SessionID || page.Challenge != settlement.Challenge || page.PageIndex != settlement.PageIndex ||
		page.RequestedURL != expectedURL || !isDigest(page.TLSPeerCertificateSHA256) || page.HTTPStatus != http.StatusOK ||
		page.ResponseContentType != expectedContentType || page.Complete != complete || page.NextToken != nextToken || page.NextURL != nextURL ||
		page.Complete != settlement.Terminal || page.NextToken != settlement.NextToken {
		return 0, errors.New("retained native page is not bound to its exact settled session semantics")
	}
	if source.Kind == "ca-revocation" {
		if err := verifyCRL(responseRaw, source, collectedAt); err != nil {
			return 0, err
		}
	}
	return int64(len(responseRaw)), nil
}

func authoritySource(config AuthorityConfig, sourceID string) (AuthoritySourceSpec, bool) {
	for _, source := range config.Sources {
		if source.ID == sourceID {
			return source, true
		}
	}
	return AuthoritySourceSpec{}, false
}

func authorityReservationBytes(config AuthorityConfig, request CollectionRequest, directiveSHA256 string) ([]byte, error) {
	terminalPath := challengeLedgerPath(config, request.Challenge)
	if _, err := readRootRegular(terminalPath, 4096); err == nil {
		return nil, errors.New("collection challenge already has a terminal settlement")
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	reservation := AuthorityReservation{
		Schema:          AuthorityReservationSchema,
		ClusterID:       config.ClusterID,
		DeploymentID:    config.DeploymentID,
		SourceID:        request.SourceID,
		CycleContractSHA256: request.CycleContractSHA256,
		CycleID:         request.CycleID,
		CycleIssuedAt:   request.CycleIssuedAt,
		CycleDeadlineAt: request.CycleDeadlineAt,
		SessionID:       request.SessionID,
		PageIndex:       request.PageIndex,
		PageToken:       request.PageToken,
		Challenge:       request.Challenge,
		DirectiveSHA256: directiveSHA256,
	}
	raw, err := json.Marshal(reservation)
	if err != nil {
		return nil, err
	}
	if len(raw) > maximumAuthorityReservationBytes {
		return nil, errors.New("authority reservation exceeds its fixed record bound")
	}
	return raw, nil
}

func authorityChallengeBytes(config AuthorityConfig, settlement AuthoritySettlement, settlementSHA256 string) ([]byte, error) {
	challenge := AuthorityChallenge{
		Schema:           AuthorityChallengeSchema,
		ClusterID:        config.ClusterID,
		DeploymentID:     config.DeploymentID,
		CycleContractSHA256: settlement.CycleContractSHA256,
		CycleID:          settlement.CycleID,
		CycleIssuedAt:    settlement.CycleIssuedAt,
		CycleDeadlineAt:  settlement.CycleDeadlineAt,
		DirectiveSHA256:  settlement.DirectiveSHA256,
		Challenge:        settlement.Challenge,
		SessionID:        settlement.SessionID,
		PageIndex:        settlement.PageIndex,
		SettlementSHA256: settlementSHA256,
	}
	raw, err := json.Marshal(challenge)
	if err != nil {
		return nil, err
	}
	return raw, nil
}

func validateAuthorityChallengeCrossLink(config AuthorityConfig, acceptance boundary.Acceptance, settlement AuthoritySettlement) error {
	terminalRaw, err := readRootRegular(challengeLedgerPath(config, settlement.Challenge), maximumAuthorityReservationBytes)
	var terminal AuthorityChallenge
	if err != nil || canonicalJSON(terminalRaw, &terminal) != nil || terminal.Schema != AuthorityChallengeSchema ||
		terminal.ClusterID != acceptance.ClusterID || terminal.DeploymentID != acceptance.DeploymentID ||
		terminal.CycleContractSHA256 != settlement.CycleContractSHA256 || terminal.CycleID != settlement.CycleID ||
		terminal.CycleIssuedAt != settlement.CycleIssuedAt || terminal.CycleDeadlineAt != settlement.CycleDeadlineAt ||
		terminal.DirectiveSHA256 != settlement.DirectiveSHA256 ||
		terminal.Challenge != settlement.Challenge || terminal.SessionID != settlement.SessionID || terminal.PageIndex != settlement.PageIndex ||
		terminal.SettlementSHA256 != digestSettlement(settlement) {
		return errors.New("authority terminal challenge is not bound to its exact sharded settlement")
	}
	settlementRaw, err := readRootRegular(
		filepath.Join(sessionLedgerRoot(config, terminal.SessionID), fmt.Sprintf("page-%08d.json", terminal.PageIndex)),
		maximumAuthorityLedgerRecordBytes,
	)
	if err != nil || digest(settlementRaw) != terminal.SettlementSHA256 {
		return errors.New("authority terminal challenge settlement bytes are missing or different")
	}
	reservationRaw, err := readRootRegular(reservationLedgerPath(config, terminal.Challenge), maximumAuthorityReservationBytes)
	var reservation AuthorityReservation
	if err != nil || canonicalJSON(reservationRaw, &reservation) != nil || reservation.Schema != AuthorityReservationSchema ||
		reservation.ClusterID != acceptance.ClusterID || reservation.DeploymentID != acceptance.DeploymentID ||
		reservation.CycleContractSHA256 != settlement.CycleContractSHA256 || reservation.CycleID != settlement.CycleID ||
		reservation.CycleIssuedAt != settlement.CycleIssuedAt || reservation.CycleDeadlineAt != settlement.CycleDeadlineAt ||
		reservation.SourceID != settlement.SourceID || reservation.SessionID != settlement.SessionID ||
		reservation.PageIndex != settlement.PageIndex || reservation.PageToken != settlement.PageToken ||
		reservation.Challenge != settlement.Challenge || reservation.DirectiveSHA256 != settlement.DirectiveSHA256 {
		return errors.New("authority terminal settlement is not bound to its exact sharded reservation")
	}
	return nil
}

func (a *Authority) ensureCollectionAcknowledged(settlement AuthoritySettlement, challengeRaw []byte, headRaw []byte) error {
	outcome, err := a.settleCollectionOutcome(settlement, challengeRaw, headRaw)
	if err != nil {
		return err
	}
	if outcome.Payload.Status != "acknowledged" {
		return fmt.Errorf("native authority durably refused this collection ACK: %s", outcome.Payload.Reason)
	}
	return nil
}

func (a *Authority) settleCollectionOutcome(
	settlement AuthoritySettlement,
	challengeRaw []byte,
	headRaw []byte,
) (authorityCollectionOutcome, error) {
	if err := a.advanceCycleHighWater(settlement.CycleID, settlement.CycleIssuedAt); err != nil {
		return authorityCollectionOutcome{}, err
	}
	if retained, exists, err := a.loadCollectionOutcome(settlement, challengeRaw, headRaw); err != nil || exists {
		return retained, err
	}
	deadlineAt, err := time.Parse(time.RFC3339, settlement.CycleDeadlineAt)
	if err != nil || deadlineAt.Nanosecond() != 0 {
		return authorityCollectionOutcome{}, errors.New("authority collection outcome has a non-canonical deadline")
	}
	headRecordedAt, err := a.collectionOutcomeLedgerTime("session-head", settlement)
	if err != nil {
		return authorityCollectionOutcome{}, fmt.Errorf("derive collection outcome from signed session-head event: %w", err)
	}
	status := "acknowledged"
	reason := "session-head-durably-complete-before-cycle-deadline"
	if !headRecordedAt.Before(deadlineAt) {
		status = "refused-deadline"
		reason = "session-head-durable-event-reached-or-crossed-cycle-deadline"
	}
	payload := authorityCollectionOutcomePayload{
		Schema: authorityCollectionOutcomeSchema,
		ClusterID: a.Config.ClusterID,
		DeploymentID: a.Config.DeploymentID,
		CycleContractSHA256: settlement.CycleContractSHA256,
		CycleID: settlement.CycleID,
		CycleIssuedAt: settlement.CycleIssuedAt,
		CycleDeadlineAt: settlement.CycleDeadlineAt,
		SourceID: settlement.SourceID,
		SessionID: settlement.SessionID,
		PageIndex: settlement.PageIndex,
		Challenge: settlement.Challenge,
		DirectiveSHA256: settlement.DirectiveSHA256,
		SettlementSHA256: digestSettlement(settlement),
		ChallengeRecordSHA256: digest(challengeRaw),
		SessionHeadSHA256: digest(headRaw),
		Status: status,
		Reason: reason,
		OutcomeAt: headRecordedAt.Format(time.RFC3339),
	}
	payloadRaw, err := json.Marshal(payload)
	if err != nil {
		return authorityCollectionOutcome{}, err
	}
	outcome := authorityCollectionOutcome{
		Payload: payload,
		Issuer: a.Config.SigningIssuer,
		KeyID: a.Config.SigningKeyID,
		Algorithm: "ed25519",
		Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(
			a.privateKey,
			ledgerSignatureMessage(authorityCollectionOutcomeSchema, a.Config.SigningIssuer, a.Config.SigningKeyID, payloadRaw),
		)),
	}
	raw, err := json.Marshal(outcome)
	if err != nil || len(raw) > maximumAuthorityOutcomeBytes {
		return authorityCollectionOutcome{}, errors.New("authority collection outcome exceeds its signed bound")
	}
	if err := a.publishLedgerObject(
		"collection-outcome",
		settlement.SessionID,
		settlement.Challenge,
		settlement.PageIndex,
		collectionOutcomePath(a.Config, settlement.Challenge),
		raw,
	); err != nil {
		return authorityCollectionOutcome{}, err
	}
	retained, exists, err := a.loadCollectionOutcome(settlement, challengeRaw, headRaw)
	if err != nil || !exists {
		return authorityCollectionOutcome{}, errors.New("authority collection outcome was not durably reopened after publication")
	}
	return retained, nil
}

func (a *Authority) loadCollectionOutcome(
	settlement AuthoritySettlement,
	challengeRaw []byte,
	headRaw []byte,
) (authorityCollectionOutcome, bool, error) {
	path := collectionOutcomePath(a.Config, settlement.Challenge)
	raw, err := readRootRegular(path, maximumAuthorityOutcomeBytes)
	if errors.Is(err, os.ErrNotExist) {
		return authorityCollectionOutcome{}, false, nil
	}
	if err != nil {
		return authorityCollectionOutcome{}, false, err
	}
	var outcome authorityCollectionOutcome
	if err := canonicalJSON(raw, &outcome); err != nil {
		return authorityCollectionOutcome{}, false, err
	}
	payloadRaw, err := json.Marshal(outcome.Payload)
	issuedAt, issuedErr := time.Parse(time.RFC3339, outcome.Payload.CycleIssuedAt)
	deadlineAt, deadlineErr := time.Parse(time.RFC3339, outcome.Payload.CycleDeadlineAt)
	outcomeAt, outcomeErr := time.Parse(time.RFC3339, outcome.Payload.OutcomeAt)
	if err != nil || issuedErr != nil || deadlineErr != nil || outcomeErr != nil ||
		issuedAt.Nanosecond() != 0 || deadlineAt.Nanosecond() != 0 || outcomeAt.Nanosecond() != 0 ||
		outcome.Payload.Schema != authorityCollectionOutcomeSchema || outcome.Payload.ClusterID != a.Config.ClusterID ||
		outcome.Payload.DeploymentID != a.Config.DeploymentID || outcome.Payload.CycleContractSHA256 != settlement.CycleContractSHA256 ||
		outcome.Payload.CycleID != settlement.CycleID || outcome.Payload.CycleIssuedAt != settlement.CycleIssuedAt ||
		outcome.Payload.CycleDeadlineAt != settlement.CycleDeadlineAt || outcome.Payload.SourceID != settlement.SourceID ||
		outcome.Payload.SessionID != settlement.SessionID || outcome.Payload.PageIndex != settlement.PageIndex ||
		outcome.Payload.Challenge != settlement.Challenge || outcome.Payload.DirectiveSHA256 != settlement.DirectiveSHA256 ||
		outcome.Payload.SettlementSHA256 != digestSettlement(settlement) || outcome.Payload.ChallengeRecordSHA256 != digest(challengeRaw) ||
		outcome.Payload.SessionHeadSHA256 != digest(headRaw) ||
		(outcome.Payload.Status != "acknowledged" && outcome.Payload.Status != "refused-deadline") ||
		outcomeAt.Before(issuedAt) || (outcome.Payload.Status == "acknowledged" && !outcomeAt.Before(deadlineAt)) ||
		(outcome.Payload.Status == "refused-deadline" && outcomeAt.Before(deadlineAt)) || outcome.Issuer != a.Config.SigningIssuer ||
		outcome.KeyID != a.Config.SigningKeyID || outcome.Algorithm != "ed25519" {
		return authorityCollectionOutcome{}, false, errors.New("authority collection outcome is not bound to its exact terminal sequence")
	}
	signature, err := base64.RawURLEncoding.DecodeString(outcome.Signature)
	if err != nil || len(signature) != ed25519.SignatureSize || base64.RawURLEncoding.EncodeToString(signature) != outcome.Signature ||
		!ed25519.Verify(a.publicKey, ledgerSignatureMessage(authorityCollectionOutcomeSchema, outcome.Issuer, outcome.KeyID, payloadRaw), signature) {
		return authorityCollectionOutcome{}, false, errors.New("authority collection outcome signature is invalid")
	}
	committed, err := a.collectionOutcomeCommitted(settlement, raw)
	if err != nil {
		return authorityCollectionOutcome{}, false, err
	}
	return outcome, committed, nil
}

func (a *Authority) collectionOutcomeCommitted(settlement AuthoritySettlement, outcomeRaw []byte) (bool, error) {
	a.journalMu.Lock()
	defer a.journalMu.Unlock()
	if a.journal == nil {
		return false, errors.New("authority collection outcome lacks its signed ledger")
	}
	indexPath, err := a.journal.eventIndexPath(settlement.Challenge, "collection-outcome", "terminal")
	if err != nil {
		return false, err
	}
	indexedRaw, event, err := a.journal.readIndexedEvent(indexPath)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	relativePath, err := safeRelativeLedgerPath(a.Config.SettlementRoot, collectionOutcomePath(a.Config, settlement.Challenge))
	if err != nil || !eventMatchesObject(
		event,
		"collection-outcome",
		"terminal",
		settlement.SessionID,
		settlement.Challenge,
		settlement.PageIndex,
		relativePath,
		outcomeRaw,
	) {
		return false, errors.New("authority collection outcome terminal index conflicts with its exact signed object")
	}
	if err := a.journal.validateEventObject(event, indexedRaw, false); err != nil {
		return false, err
	}
	return true, nil
}

func (a *Authority) collectionOutcomeLedgerTime(kind string, settlement AuthoritySettlement) (time.Time, error) {
	a.journalMu.Lock()
	defer a.journalMu.Unlock()
	if a.journal == nil {
		return time.Time{}, errors.New("authority outcome ledger is unavailable")
	}
	indexPath, err := a.journal.eventIndexPath(settlement.Challenge, kind, "terminal")
	if err != nil {
		return time.Time{}, err
	}
	raw, event, err := a.journal.readIndexedEvent(indexPath)
	if err != nil {
		return time.Time{}, err
	}
	if err := a.journal.validateEventObject(event, raw, false); err != nil {
		return time.Time{}, err
	}
	recordedAt, err := time.Parse(time.RFC3339, event.Payload.RecordedAt)
	if err != nil || recordedAt.Nanosecond() != 0 {
		return time.Time{}, errors.New("authority outcome terminal event time is not canonical")
	}
	return recordedAt, nil
}

func digestSettlement(settlement AuthoritySettlement) string {
	raw, _ := json.Marshal(settlement)
	return digest(raw)
}

func authorityLedgerEntryExists(path string) bool {
	info, err := os.Lstat(path)
	return err == nil && info.Mode().IsRegular() && info.Mode()&os.ModeSymlink == 0
}

type providerBearerIdentity struct {
	issuer     string
	audience   string
	subject    string
	expiresAt  string
	generation string
	algorithm  string
	keyID      string
	jwksSHA256 string
}

type providerBearerVerifier struct {
	algorithm  string
	keyID      string
	jwksSHA256 string
	publicKey  crypto.PublicKey
}

type providerJWKS struct {
	Keys []providerJWK `json:"keys"`
}

type providerJWK struct {
	KeyType   string `json:"kty"`
	KeyID     string `json:"kid"`
	Algorithm string `json:"alg"`
	Use       string `json:"use,omitempty"`
	Curve     string `json:"crv,omitempty"`
	X         string `json:"x,omitempty"`
	Y         string `json:"y,omitempty"`
	Modulus   string `json:"n,omitempty"`
	Exponent  string `json:"e,omitempty"`
}

func loadProviderBearerVerifier(source AuthoritySourceSpec) (providerBearerVerifier, error) {
	raw, err := readRootRegular(source.BearerTokenJWKSPath, 1024*1024)
	if err != nil {
		return providerBearerVerifier{}, err
	}
	if digest(raw) != source.BearerTokenJWKSSHA256 {
		return providerBearerVerifier{}, errors.New("provider bearer JWKS differs from its accepted digest")
	}
	var set providerJWKS
	if err := canonicalJSON(raw, &set); err != nil || len(set.Keys) == 0 || len(set.Keys) > 64 {
		return providerBearerVerifier{}, errors.New("provider bearer JWKS is not one bounded canonical key set")
	}
	seen := map[string]struct{}{}
	var selected *providerJWK
	for index := range set.Keys {
		key := &set.Keys[index]
		if !boundedProtocolText(key.KeyID, maximumBearerKeyIDBytes, false) || !boundedProtocolText(key.Algorithm, maximumBearerAlgorithmBytes, false) ||
			(key.Use != "" && key.Use != "sig") {
			return providerBearerVerifier{}, errors.New("provider bearer JWKS contains an invalid key identity or purpose")
		}
		if _, duplicate := seen[key.KeyID]; duplicate {
			return providerBearerVerifier{}, errors.New("provider bearer JWKS repeats a key ID")
		}
		seen[key.KeyID] = struct{}{}
		if key.KeyID == source.BearerTokenKeyID && key.Algorithm == source.BearerTokenAlgorithm {
			selected = key
		}
	}
	if selected == nil {
		return providerBearerVerifier{}, errors.New("provider bearer JWKS omits the exactly accepted algorithm and key ID")
	}
	publicKey, err := providerJWKPublicKey(*selected)
	if err != nil {
		return providerBearerVerifier{}, err
	}
	return providerBearerVerifier{
		algorithm: source.BearerTokenAlgorithm,
		keyID: source.BearerTokenKeyID,
		jwksSHA256: source.BearerTokenJWKSSHA256,
		publicKey: publicKey,
	}, nil
}

func providerJWKPublicKey(key providerJWK) (crypto.PublicKey, error) {
	decode := func(value string) ([]byte, error) {
		decoded, err := base64.RawURLEncoding.DecodeString(value)
		if err != nil || value == "" || base64.RawURLEncoding.EncodeToString(decoded) != value {
			return nil, errors.New("provider bearer JWK contains non-canonical base64url")
		}
		return decoded, nil
	}
	switch key.Algorithm {
	case "RS256":
		if key.KeyType != "RSA" || key.Curve != "" || key.X != "" || key.Y != "" {
			return nil, errors.New("provider bearer RSA key shape is invalid")
		}
		modulus, modulusErr := decode(key.Modulus)
		exponentRaw, exponentErr := decode(key.Exponent)
		if modulusErr != nil || exponentErr != nil || len(exponentRaw) == 0 || len(exponentRaw) > 4 {
			return nil, errors.New("provider bearer RSA key is malformed")
		}
		exponent := 0
		for _, octet := range exponentRaw {
			exponent = exponent<<8 | int(octet)
		}
		publicKey := &rsa.PublicKey{N: new(big.Int).SetBytes(modulus), E: exponent}
		if publicKey.N.BitLen() < 2048 || publicKey.N.BitLen() > 4096 || exponent < 65537 || exponent%2 == 0 {
			return nil, errors.New("provider bearer RSA key strength or exponent is outside policy")
		}
		return publicKey, nil
	case "ES256":
		if key.KeyType != "EC" || key.Curve != "P-256" || key.Modulus != "" || key.Exponent != "" {
			return nil, errors.New("provider bearer ECDSA key shape is invalid")
		}
		xRaw, xErr := decode(key.X)
		yRaw, yErr := decode(key.Y)
		if xErr != nil || yErr != nil || len(xRaw) != 32 || len(yRaw) != 32 {
			return nil, errors.New("provider bearer ECDSA coordinates are invalid")
		}
		publicKey := &ecdsa.PublicKey{Curve: elliptic.P256(), X: new(big.Int).SetBytes(xRaw), Y: new(big.Int).SetBytes(yRaw)}
		if !publicKey.Curve.IsOnCurve(publicKey.X, publicKey.Y) {
			return nil, errors.New("provider bearer ECDSA key is not on the accepted curve")
		}
		return publicKey, nil
	case "EdDSA":
		if key.KeyType != "OKP" || key.Curve != "Ed25519" || key.Y != "" || key.Modulus != "" || key.Exponent != "" {
			return nil, errors.New("provider bearer Ed25519 key shape is invalid")
		}
		xRaw, err := decode(key.X)
		if err != nil || len(xRaw) != ed25519.PublicKeySize {
			return nil, errors.New("provider bearer Ed25519 key is invalid")
		}
		return ed25519.PublicKey(xRaw), nil
	default:
		return nil, errors.New("provider bearer algorithm is not in the exact accepted set")
	}
}

func verifyProviderBearerSignature(verifier providerBearerVerifier, signingInput string, encodedSignature string) error {
	signature, err := base64.RawURLEncoding.DecodeString(encodedSignature)
	if err != nil || base64.RawURLEncoding.EncodeToString(signature) != encodedSignature {
		return errors.New("provider bearer signature is not canonical base64url")
	}
	hashed := sha256.Sum256([]byte(signingInput))
	switch publicKey := verifier.publicKey.(type) {
	case *rsa.PublicKey:
		if verifier.algorithm != "RS256" || rsa.VerifyPKCS1v15(publicKey, crypto.SHA256, hashed[:], signature) != nil {
			return errors.New("provider bearer RS256 signature is invalid")
		}
	case *ecdsa.PublicKey:
		if verifier.algorithm != "ES256" || len(signature) != 64 ||
			!ecdsa.Verify(publicKey, hashed[:], new(big.Int).SetBytes(signature[:32]), new(big.Int).SetBytes(signature[32:])) {
			return errors.New("provider bearer ES256 signature is invalid")
		}
	case ed25519.PublicKey:
		if verifier.algorithm != "EdDSA" || !ed25519.Verify(publicKey, []byte(signingInput), signature) {
			return errors.New("provider bearer EdDSA signature is invalid")
		}
	default:
		return errors.New("provider bearer verifier key type is unsupported")
	}
	return nil
}

func loadProviderBearerCredential(source AuthoritySourceSpec, verifier providerBearerVerifier, now time.Time) (string, providerBearerIdentity, error) {
	raw, err := readRootPrivateKey(source.BearerTokenPath, 64*1024)
	if err != nil {
		return "", providerBearerIdentity{}, err
	}
	token := strings.TrimSpace(string(raw))
	parts := strings.Split(token, ".")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" || parts[2] == "" || strings.ContainsAny(token, "\r\n") {
		return "", providerBearerIdentity{}, errors.New("provider bearer credential is not a compact JWS")
	}
	headerRaw, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil || base64.RawURLEncoding.EncodeToString(headerRaw) != parts[0] {
		return "", providerBearerIdentity{}, errors.New("provider bearer header is not canonical base64url")
	}
	var header struct {
		Algorithm string `json:"alg"`
		KeyID string `json:"kid"`
		Type string `json:"typ"`
	}
	if err := canonicalJSON(headerRaw, &header); err != nil || header.Algorithm != verifier.algorithm ||
		header.KeyID != verifier.keyID || header.Type != "JWT" {
		return "", providerBearerIdentity{}, errors.New("provider bearer header differs from its exact accepted algorithm, key and type")
	}
	payloadRaw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil || base64.RawURLEncoding.EncodeToString(payloadRaw) != parts[1] {
		return "", providerBearerIdentity{}, errors.New("provider bearer payload is not canonical base64url")
	}
	var claims struct {
		Issuer    string          `json:"iss"`
		Subject   string          `json:"sub"`
		Audience  json.RawMessage `json:"aud"`
		ExpiresAt json.Number     `json:"exp"`
		IssuedAt  json.Number     `json:"iat"`
		ID        string          `json:"jti"`
	}
	if err := canonicalJSON(payloadRaw, &claims); err != nil || claims.Issuer != source.BearerTokenIssuer ||
		claims.Subject != source.BearerTokenSubject || !boundedProtocolText(claims.ID, maximumBearerKeyIDBytes, false) {
		return "", providerBearerIdentity{}, errors.New("provider bearer identity differs from its accepted issuer, subject or generation")
	}
	if err := verifyProviderBearerSignature(verifier, parts[0]+"."+parts[1], parts[2]); err != nil {
		return "", providerBearerIdentity{}, err
	}
	audience, err := exactBearerAudience(claims.Audience, source.BearerTokenAudience)
	if err != nil {
		return "", providerBearerIdentity{}, err
	}
	expiresUnix, expiryErr := claims.ExpiresAt.Int64()
	issuedUnix, issueErr := claims.IssuedAt.Int64()
	expiresAt := time.Unix(expiresUnix, 0).UTC()
	issuedAt := time.Unix(issuedUnix, 0).UTC()
	now = now.UTC()
	if expiryErr != nil || issueErr != nil || !expiresAt.After(now.Add(time.Duration(source.BearerTokenMinimumRemainingSeconds)*time.Second)) ||
		issuedAt.After(now.Add(30*time.Second)) || !expiresAt.After(issuedAt) ||
		expiresAt.Sub(issuedAt) > time.Duration(source.BearerTokenMaximumLifetimeSeconds)*time.Second {
		return "", providerBearerIdentity{}, errors.New("provider bearer credential is expired, premature or exceeds its accepted short lifetime")
	}
	return token, providerBearerIdentity{
		issuer: source.BearerTokenIssuer,
		audience: audience,
		subject: source.BearerTokenSubject,
		expiresAt: expiresAt.Truncate(time.Second).Format(time.RFC3339),
		generation: digest([]byte(claims.ID)),
		algorithm: verifier.algorithm,
		keyID: verifier.keyID,
		jwksSHA256: verifier.jwksSHA256,
	}, nil
}

func exactBearerAudience(raw json.RawMessage, expected string) (string, error) {
	var single string
	if err := json.Unmarshal(raw, &single); err == nil {
		if single == expected {
			return single, nil
		}
		return "", errors.New("provider bearer audience differs from acceptance")
	}
	var multiple []string
	if err := json.Unmarshal(raw, &multiple); err != nil || len(multiple) != 1 || multiple[0] != expected {
		return "", errors.New("provider bearer audience is ambiguous or differs from acceptance")
	}
	return multiple[0], nil
}

func (a *Authority) collect(ctx context.Context, source AuthoritySourceSpec, directive CollectionRequest) (NativePage, error) {
	cycleDeadline, deadlineErr := time.Parse(time.RFC3339, directive.CycleDeadlineAt)
	if deadlineErr != nil || !time.Now().UTC().Before(cycleDeadline) {
		return NativePage{}, errors.New("native provider request began outside its accepted collection deadline")
	}
	requestURL, err := authorityPageURL(source, directive.PageToken)
	if err != nil {
		return NativePage{}, err
	}
	requestID, err := randomID()
	if err != nil {
		return NativePage{}, err
	}
	accept := "application/json"
	if source.Kind == "ca-revocation" {
		accept = "application/pkix-crl"
	}
	token := ""
	credential := providerBearerIdentity{}
	if source.BearerTokenPath != "" {
		verifier, enrolled := a.bearerVerifiers[source.ID]
		if !enrolled {
			return NativePage{}, errors.New("native provider bearer verifier is not enrolled in immutable authority state")
		}
		token, credential, err = loadProviderBearerCredential(source, verifier, time.Now().UTC())
		if err != nil {
			return NativePage{}, err
		}
	}
	projection := NativeRequest{
		Schema:     NativeRequestSchema,
		SourceID:   source.ID,
		RequestID:  requestID,
		Method:     http.MethodGet,
		URL:        requestURL,
		BodySHA256: digest(nil),
		Headers: map[string]string{
			"accept":          accept,
			"x-fs2-request-id": requestID,
		},
		CredentialLaneID: source.CredentialLaneID,
		CredentialIssuer: credential.issuer,
		CredentialAudience: credential.audience,
		CredentialSubject: credential.subject,
		CredentialExpiresAt: credential.expiresAt,
		CredentialGeneration: credential.generation,
		CredentialAlgorithm: credential.algorithm,
		CredentialKeyID: credential.keyID,
		CredentialJWKSHA256: credential.jwksSHA256,
	}
	projectionRaw, _ := json.Marshal(projection)
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, requestURL, nil)
	if err != nil {
		return NativePage{}, err
	}
	request.Header.Set("Accept", accept)
	request.Header.Set("X-FS2-Request-ID", requestID)
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	client, err := mutualTLSClient(
		source.CABundlePath,
		source.CABundleSHA256,
		source.ServerName,
		source.ClientCertificatePath,
		source.ClientCertificateSHA256,
		source.ClientKeyPath,
		source.ClientKeySHA256,
		source.ClientSPKISHA256,
		source.ClientSPIFFEURI,
	)
	if err != nil {
		return NativePage{}, err
	}
	if !time.Now().UTC().Before(cycleDeadline) {
		return NativePage{}, errors.New("native provider request cannot start after its accepted collection deadline")
	}
	providerResponse, err := client.Do(request)
	if err != nil {
		return NativePage{}, err
	}
	responseRaw, readErr := io.ReadAll(io.LimitReader(providerResponse.Body, source.MaximumPageBytes+1))
	closeErr := providerResponse.Body.Close()
	collectedAt := time.Now().UTC().Truncate(time.Second)
	if readErr != nil || closeErr != nil || int64(len(responseRaw)) > source.MaximumPageBytes ||
		providerResponse.StatusCode != http.StatusOK || providerResponse.TLS == nil || len(providerResponse.TLS.PeerCertificates) == 0 {
		return NativePage{}, errors.New("native provider response is unauthenticated, unsuccessful or oversized")
	}
	if !collectedAt.Before(cycleDeadline) {
		return NativePage{}, errors.New("native provider response completed after its accepted collection deadline")
	}
	providerRequestID := strings.TrimSpace(providerResponse.Header.Get(source.ProviderRequestIDHeader))
	if !boundedProtocolText(providerRequestID, maximumProviderRequestIDBytes, false) {
		return NativePage{}, errors.New("native provider response lacks its audit/request identity")
	}
	if source.Kind == "ca-revocation" {
		if err := verifyCRL(responseRaw, source, time.Now()); err != nil {
			return NativePage{}, err
		}
	}
	complete, nextToken, nextURL, err := derivePaginationForContent(source.Pagination, requestURL, responseRaw)
	if err != nil {
		return NativePage{}, err
	}
	if source.Kind == "kubernetes-secret-metadata-projection" {
		responseRaw, err = projectSecretMetadataList(responseRaw)
		if err != nil {
			return NativePage{}, err
		}
	}
	if source.Kind == "kubernetes-tls-certificate-projection" {
		responseRaw, err = projectTLSCertificateList(responseRaw)
		if err != nil {
			return NativePage{}, err
		}
	}
	return NativePage{
		Schema:                   NativePageSchema,
		SourceID:                 source.ID,
		SourceKind:               source.Kind,
		SemanticCollection:       source.SemanticCollection,
		CycleContractSHA256:      directive.CycleContractSHA256,
		CycleID:                  directive.CycleID,
		CycleIssuedAt:            directive.CycleIssuedAt,
		CycleDeadlineAt:          directive.CycleDeadlineAt,
		RequestID:                requestID,
		ProviderRequestID:        providerRequestID,
		SessionID:                directive.SessionID,
		Challenge:                directive.Challenge,
		PageIndex:                directive.PageIndex,
		RequestBase64:            base64.StdEncoding.EncodeToString(projectionRaw),
		RequestSHA256:            digest(projectionRaw),
		RequestedURL:             requestURL,
		CollectedAt:              collectedAt.Format(time.RFC3339),
		TLSPeerCertificateSHA256: digest(providerResponse.TLS.PeerCertificates[0].Raw),
		HTTPStatus:               providerResponse.StatusCode,
		ResponseContentType:      normalizedContentType(providerResponse.Header.Get("Content-Type")),
		RawResponseBase64:        base64.StdEncoding.EncodeToString(responseRaw),
		RawResponseSHA256:        digest(responseRaw),
		Complete:                 complete,
		NextToken:                nextToken,
		NextURL:                  nextURL,
	}, nil
}

type secretMetadataProjection struct {
	Schema     string                   `json:"schema"`
	APIVersion string                   `json:"apiVersion"`
	Kind       string                   `json:"kind"`
	Metadata   map[string]json.RawMessage `json:"metadata"`
	Items      []secretMetadataObject   `json:"items"`
}

type secretMetadataObject struct {
	APIVersion string                     `json:"apiVersion"`
	Kind       string                     `json:"kind"`
	Metadata   map[string]json.RawMessage `json:"metadata"`
	Type       string                     `json:"type,omitempty"`
}

type tlsCertificateProjection struct {
	Schema     string                         `json:"schema"`
	APIVersion string                         `json:"apiVersion"`
	Kind       string                         `json:"kind"`
	Metadata   map[string]json.RawMessage     `json:"metadata"`
	Items      []tlsCertificateProjectionItem `json:"items"`
}

type tlsCertificateProjectionItem struct {
	APIVersion string                     `json:"apiVersion"`
	Kind       string                     `json:"kind"`
	Metadata   map[string]json.RawMessage `json:"metadata"`
	Type       string                     `json:"type"`
	Certificate map[string]string         `json:"certificate"`
}

func projectSecretMetadataList(raw []byte) ([]byte, error) {
	var fields map[string]json.RawMessage
	if err := boundary.DecodeExactJSON(raw, &fields); err != nil {
		return nil, errors.New("Kubernetes Secret response is not exact JSON")
	}
	var apiVersion string
	var kind string
	var metadata map[string]json.RawMessage
	var items []json.RawMessage
	if json.Unmarshal(fields["apiVersion"], &apiVersion) != nil || json.Unmarshal(fields["kind"], &kind) != nil ||
		json.Unmarshal(fields["metadata"], &metadata) != nil || json.Unmarshal(fields["items"], &items) != nil ||
		apiVersion != "v1" || kind != "SecretList" || metadata == nil || items == nil {
		return nil, errors.New("Kubernetes Secret response is not an exact v1 SecretList")
	}
	safeListMetadata, metadataErr := projectSafeSecretListMetadata(metadata)
	if metadataErr != nil {
		return nil, metadataErr
	}
	projection := secretMetadataProjection{
		Schema: "fs2-serve.nebius.ai/kubernetes-secret-metadata-projection/v1",
		APIVersion: apiVersion,
		Kind: kind,
		Metadata: safeListMetadata,
		Items: make([]secretMetadataObject, 0, len(items)),
	}
	for _, itemRaw := range items {
		var itemFields map[string]json.RawMessage
		if err := boundary.DecodeExactJSON(itemRaw, &itemFields); err != nil {
			return nil, errors.New("Kubernetes Secret item is not exact JSON")
		}
		var itemAPIVersion string
		var itemKind string
		var itemMetadata map[string]json.RawMessage
		var secretType string
		if json.Unmarshal(itemFields["apiVersion"], &itemAPIVersion) != nil || json.Unmarshal(itemFields["kind"], &itemKind) != nil ||
			json.Unmarshal(itemFields["metadata"], &itemMetadata) != nil || itemAPIVersion != "v1" || itemKind != "Secret" || itemMetadata == nil {
			return nil, errors.New("Kubernetes Secret item lacks exact type and metadata")
		}
		if typeRaw, exists := itemFields["type"]; exists && json.Unmarshal(typeRaw, &secretType) != nil {
			return nil, errors.New("Kubernetes Secret type is malformed")
		}
		safeMetadata, metadataErr := projectSafeSecretMetadata(itemMetadata)
		if metadataErr != nil {
			return nil, metadataErr
		}
		projection.Items = append(projection.Items, secretMetadataObject{APIVersion: itemAPIVersion, Kind: itemKind, Metadata: safeMetadata, Type: secretType})
	}
	projected, err := json.Marshal(projection)
	if err != nil {
		return nil, err
	}
	return projected, nil
}

// projectTLSCertificateList retains only public certificate material and the
// exact Secret identity. The private key is neither copied into the signed
// native page nor persisted in the collector evidence store.
func projectTLSCertificateList(raw []byte) ([]byte, error) {
	var fields map[string]json.RawMessage
	if err := boundary.DecodeExactJSON(raw, &fields); err != nil {
		return nil, errors.New("Kubernetes TLS Secret response is not exact JSON")
	}
	var apiVersion string
	var kind string
	var metadata map[string]json.RawMessage
	var items []json.RawMessage
	if json.Unmarshal(fields["apiVersion"], &apiVersion) != nil || json.Unmarshal(fields["kind"], &kind) != nil ||
		json.Unmarshal(fields["metadata"], &metadata) != nil || json.Unmarshal(fields["items"], &items) != nil ||
		apiVersion != "v1" || kind != "SecretList" || metadata == nil || items == nil {
		return nil, errors.New("Kubernetes TLS Secret response is not an exact v1 SecretList")
	}
	safeListMetadata, metadataErr := projectSafeSecretListMetadata(metadata)
	if metadataErr != nil {
		return nil, metadataErr
	}
	projection := tlsCertificateProjection{
		Schema:     "fs2-serve.nebius.ai/kubernetes-tls-certificate-projection/v1",
		APIVersion: apiVersion,
		Kind:       kind,
		Metadata:   safeListMetadata,
		Items:      []tlsCertificateProjectionItem{},
	}
	for _, itemRaw := range items {
		var itemFields map[string]json.RawMessage
		if err := boundary.DecodeExactJSON(itemRaw, &itemFields); err != nil {
			return nil, errors.New("Kubernetes TLS Secret item is not exact JSON")
		}
		var itemAPIVersion string
		var itemKind string
		var itemMetadata map[string]json.RawMessage
		var secretType string
		if json.Unmarshal(itemFields["apiVersion"], &itemAPIVersion) != nil || json.Unmarshal(itemFields["kind"], &itemKind) != nil ||
			json.Unmarshal(itemFields["metadata"], &itemMetadata) != nil || itemAPIVersion != "v1" || itemKind != "Secret" || itemMetadata == nil ||
			json.Unmarshal(itemFields["type"], &secretType) != nil {
			return nil, errors.New("Kubernetes TLS Secret item lacks exact type and metadata")
		}
		if secretType != "kubernetes.io/tls" {
			continue
		}
		var data map[string]string
		if json.Unmarshal(itemFields["data"], &data) != nil {
			return nil, errors.New("Kubernetes TLS Secret data is malformed")
		}
		caPEM := data["ca.crt"]
		leafPEM := data["tls.crt"]
		if caPEM == "" || leafPEM == "" {
			continue
		}
		safeMetadata, metadataErr := projectSafeSecretMetadata(itemMetadata)
		if metadataErr != nil {
			return nil, metadataErr
		}
		projection.Items = append(projection.Items, tlsCertificateProjectionItem{
			APIVersion: itemAPIVersion,
			Kind:       itemKind,
			Metadata:   safeMetadata,
			Type:       secretType,
			Certificate: map[string]string{
				"ca_pem_base64":   caPEM,
				"leaf_pem_base64": leafPEM,
			},
		})
	}
	projected, err := json.Marshal(projection)
	if err != nil {
		return nil, err
	}
	return projected, nil
}

func projectSafeSecretMetadata(metadata map[string]json.RawMessage) (map[string]json.RawMessage, error) {
	if metadata == nil {
		return nil, errors.New("Kubernetes Secret metadata is absent")
	}
	projected := make(map[string]json.RawMessage, 4)
	for _, field := range []string{"name", "namespace", "uid", "resourceVersion"} {
		raw, exists := metadata[field]
		if !exists {
			return nil, fmt.Errorf("Kubernetes Secret metadata lacks %s", field)
		}
		var value string
		if json.Unmarshal(raw, &value) != nil || value == "" || len(value) > 512 || strings.ContainsAny(value, "\x00\r\n") {
			return nil, fmt.Errorf("Kubernetes Secret metadata %s is invalid", field)
		}
		canonical, err := json.Marshal(value)
		if err != nil {
			return nil, err
		}
		projected[field] = canonical
	}
	return projected, nil
}

func projectSafeSecretListMetadata(metadata map[string]json.RawMessage) (map[string]json.RawMessage, error) {
	if metadata == nil {
		return nil, errors.New("Kubernetes SecretList metadata is absent")
	}
	projected := make(map[string]json.RawMessage, 3)
	var resourceVersion string
	if json.Unmarshal(metadata["resourceVersion"], &resourceVersion) != nil || resourceVersion == "" || len(resourceVersion) > 512 || strings.ContainsAny(resourceVersion, "\x00\r\n") {
		return nil, errors.New("Kubernetes SecretList resourceVersion is invalid")
	}
	projected["resourceVersion"], _ = json.Marshal(resourceVersion)
	if raw, exists := metadata["continue"]; exists {
		var value string
		if json.Unmarshal(raw, &value) != nil || len(value) > maximumPageTokenBytes || strings.ContainsAny(value, "\x00\r\n") {
			return nil, errors.New("Kubernetes SecretList continue token is invalid")
		}
		projected["continue"], _ = json.Marshal(value)
	}
	if raw, exists := metadata["remainingItemCount"]; exists {
		var value int64
		if json.Unmarshal(raw, &value) != nil || value < 0 {
			return nil, errors.New("Kubernetes SecretList remainingItemCount is invalid")
		}
		projected["remainingItemCount"], _ = json.Marshal(value)
	}
	return projected, nil
}

func (a *Authority) signPage(page NativePage) ([]byte, error) {
	if !a.Acceptance.ProductionTrustCurrent(time.Now().UTC()) { return nil, errors.New("native authority production trust provenance expired before signing") }
	payloadRaw, err := json.Marshal(page)
	if err != nil {
		return nil, err
	}
	payloadSHA256 := digest(payloadRaw)
	message := bytes.Join([][]byte{
		[]byte(NativeEnvelopeSchema),
		[]byte(a.Config.SigningIssuer),
		[]byte(a.Config.SigningKeyID),
		[]byte(payloadSHA256),
		payloadRaw,
	}, []byte("\n"))
	envelope := boundary.SignedEnvelope{
		Algorithm:     "ed25519",
		Issuer:        a.Config.SigningIssuer,
		KeyID:         a.Config.SigningKeyID,
		PayloadBase64: base64.StdEncoding.EncodeToString(payloadRaw),
		PayloadSHA256: payloadSHA256,
		Schema:        NativeEnvelopeSchema,
		Signature:     base64.RawURLEncoding.EncodeToString(ed25519.Sign(a.privateKey, message)),
	}
	return json.Marshal(envelope)
}

func authorityPageURL(source AuthoritySourceSpec, token string) (string, error) {
	parsed, err := url.Parse(source.InitialURL)
	if err != nil {
		return "", err
	}
	query := parsed.Query()
	query.Del("continue")
	query.Del("page_token")
	if token != "" {
		if len(token) > 4096 || strings.ContainsAny(token, "\x00\r\n") {
			return "", errors.New("collection page token is malformed")
		}
		if source.Pagination == "kubernetes-continue" {
			query.Set("continue", token)
		} else if source.Pagination == "provider-page-token" {
			query.Set("page_token", token)
		} else {
			return "", errors.New("non-paginated source received a page token")
		}
	}
	parsed.RawQuery = query.Encode()
	return parsed.String(), nil
}

func derivePaginationForContent(kind string, currentURL string, raw []byte) (bool, string, string, error) {
	if kind == "none" {
		return true, "", "", nil
	}
	temporary := SourceSpec{Pagination: kind}
	return derivePagination(temporary, currentURL, raw)
}

func verifyCRL(raw []byte, source AuthoritySourceSpec, now time.Time) error {
	if source.RevocationIssuerBundlePath == "" || source.MaximumRevocationAgeSeconds < 1 || source.MaximumRevocationAgeSeconds > 86400 {
		return errors.New("CRL authority lacks an enrolled issuer bundle or freshness bound")
	}
	list, err := x509.ParseRevocationList(raw)
	if err != nil {
		return errors.New("native revocation response is not canonical DER CRL")
	}
	issuerRaw, err := readRootRegular(source.RevocationIssuerBundlePath, 4*1024*1024)
	if err != nil {
		return err
	}
	if digest(issuerRaw) != source.RevocationIssuerBundleSHA256 {
		return errors.New("CRL issuer bundle changed after authority admission")
	}
	issuers := []*x509.Certificate{}
	for len(issuerRaw) > 0 {
		block, rest := pem.Decode(issuerRaw)
		if block == nil {
			return errors.New("CRL issuer bundle has trailing or malformed PEM")
		}
		issuerRaw = rest
		if block.Type != "CERTIFICATE" {
			return errors.New("CRL issuer bundle contains a non-certificate object")
		}
		certificate, err := x509.ParseCertificate(block.Bytes)
		if err != nil || !certificate.IsCA || certificate.KeyUsage&x509.KeyUsageCRLSign == 0 {
			return errors.New("CRL issuer is not an enrolled CRL-signing CA")
		}
		issuers = append(issuers, certificate)
	}
	verified := false
	for _, issuer := range issuers {
		if list.CheckSignatureFrom(issuer) == nil {
			verified = true
			break
		}
	}
	now = now.UTC()
	if !verified || list.ThisUpdate.After(now.Add(30*time.Second)) || !list.NextUpdate.After(now) ||
		now.Sub(list.ThisUpdate) > time.Duration(source.MaximumRevocationAgeSeconds)*time.Second {
		return errors.New("CRL signature, issuer or freshness validation failed")
	}
	return nil
}

func canonicalNonce(value string) bool {
	if len(value) != 64 || value == strings.Repeat("0", 64) {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && hex.EncodeToString(decoded) == value
}

func publicKeyID(publicKey ed25519.PublicKey) string {
	hash := sha256.Sum256(publicKey)
	return "sha256:" + hex.EncodeToString(hash[:])
}

func sortedAuthoritySourceIDs(sources map[string]AuthoritySourceSpec) []string {
	values := make([]string, 0, len(sources))
	for value := range sources {
		values = append(values, value)
	}
	sort.Strings(values)
	return values
}

var _ = os.ErrNotExist
