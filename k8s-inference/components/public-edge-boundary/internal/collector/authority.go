package collector

import (
	"bytes"
	"context"
	"crypto/ed25519"
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
	slots      chan struct{}
	mu         sync.Mutex
	pending    map[string]authorityPending
	retainedSessions     int
	retainedSettlements  int
	retainedReservations int
	retainedChallenges   int
}

type authoritySession struct {
	sourceID                 string
	nextPage                 int
	nextToken                string
	terminal                 bool
	previousSettlementSHA256 string
	responseBytes            int64
	settlements              map[int]AuthoritySettlement
}

type authorityPending struct {
	sourceID                 string
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
	maximumAuthorityLedgerRecordBytes = 128 * 1024 * 1024
	maximumAuthorityReservationBytes  = 16 * 1024
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
	var config AuthorityConfig
	if err := canonicalJSON(raw, &config); err != nil {
		return nil, err
	}
	if config.Schema != AuthorityConfigSchema || config.ClusterID != acceptance.ClusterID || config.DeploymentID != acceptance.DeploymentID ||
		config.ListenAddress != ":8444" || len(config.Sources) == 0 || !filepath.IsAbs(config.SettlementRoot) ||
		filepath.Clean(config.SettlementRoot) != config.SettlementRoot || config.SettlementRoot == "/" ||
		config.LedgerOperatingHorizonDays < 365 || config.LedgerOperatingHorizonDays > 3660 ||
		config.CollectorRefreshIntervalSeconds < 240 || config.CollectorRefreshIntervalSeconds > 270 ||
		config.CollectorConfigSHA256 != acceptance.NativeCollectorConfigSHA256 || config.LedgerDeviceID == 0 ||
		config.LedgerMinimumFreeBytes < uint64(maximumAuthorityLedgerRecordBytes) || config.LedgerMinimumFreeInodes < 1024 ||
		config.LedgerCapacityInodes == 0 {
		return nil, errors.New("native authority config is incomplete or has the wrong identity")
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
	if config.SigningKeyID != "sha256:"+digest(publicKey) || config.SigningIssuer == "" {
		return nil, errors.New("native authority signing identity does not match its key")
	}
	if !isDigest(config.ClientCASHA256) || !isDigest(config.TLSCertificateSHA256) || !isDigest(config.TLSPrivateKeySHA256) ||
		!isDigest(config.TLSSPKISHA256) ||
		!isDigest(config.CollectorSPKISHA256) {
		return nil, errors.New("native authority config contains an invalid trust digest")
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
		if source.ID == "" || source.Kind == "" || source.SemanticCollection == "" || !isDigest(source.CABundleSHA256) ||
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
	dailyBytes, dailyRecords, err := derivedAuthorityLedgerDailyBounds(config)
	if err != nil || dailyBytes != config.LedgerExpectedMaximumBytesPerDay || dailyRecords != config.LedgerExpectedMaximumRecordsPerDay ||
		dailyBytes > (^uint64(0)-config.LedgerMinimumFreeBytes)/uint64(config.LedgerOperatingHorizonDays) ||
		dailyRecords > (^uint64(0)-config.LedgerMinimumFreeInodes)/uint64(config.LedgerOperatingHorizonDays) ||
		config.LedgerCapacityBytes < dailyBytes*uint64(config.LedgerOperatingHorizonDays)+config.LedgerMinimumFreeBytes ||
		config.LedgerCapacityInodes < dailyRecords*uint64(config.LedgerOperatingHorizonDays)+config.LedgerMinimumFreeInodes {
		return nil, errors.New("native authority ledger capacity is not the exact source-derived byte and inode horizon")
	}
	if err := prepareAuthorityLedger(config.SettlementRoot); err != nil {
		return nil, fmt.Errorf("prepare native authority settlement ledger: %w", err)
	}
	if err := ensureAuthorityLedgerCapacity(config, uint64(maximumAuthorityLedgerRecordBytes), 8); err != nil {
		return nil, fmt.Errorf("validate native authority ledger capacity horizon: %w", err)
	}
	counts, err := recoverAuthorityLedger(config, acceptance, publicKey)
	if err != nil {
		return nil, fmt.Errorf("recover native authority settlement ledger: %w", err)
	}
	return &Authority{
		Config:     config,
		Acceptance: acceptance,
		privateKey: privateKey,
		publicKey:  publicKey,
		serverCertificate: serverCertificate,
		clientRoots:       clientRoots,
		sources:    sources,
		slots:      make(chan struct{}, 8),
		pending:    map[string]authorityPending{},
		retainedSessions:     counts.sessions,
		retainedSettlements:  counts.settlements,
		retainedReservations: counts.reservations,
		retainedChallenges:   counts.challenges,
	}, nil
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
	if request.Method != http.MethodGet || request.TLS == nil || len(request.TLS.VerifiedChains) == 0 {
		response.WriteHeader(http.StatusForbidden)
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
		len(collectionRequest.SourceID) < 1 || len(collectionRequest.SourceID) > 256 || strings.ContainsAny(collectionRequest.SourceID, "\x00\r\n") ||
		len(collectionRequest.PageToken) > 4096 || strings.ContainsAny(collectionRequest.PageToken, "\x00\r\n") ||
		!canonicalNonce(collectionRequest.SessionID) || !canonicalNonce(collectionRequest.Challenge) {
		response.WriteHeader(http.StatusBadRequest)
		return
	}
	source, exists := a.sources[collectionRequest.SourceID]
	if !exists || collectionRequest.PageIndex >= source.MaximumPages {
		response.WriteHeader(http.StatusForbidden)
		return
	}
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
	page, err := a.collect(request.Context(), source, collectionRequest)
	if err != nil {
		a.abortCollection(collectionRequest.SessionID, collectionRequest.Challenge)
		response.WriteHeader(http.StatusBadGateway)
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
	if len(a.pending) >= maximumPendingCollections {
		return nil, false, errors.New("native authority has reached its bounded in-flight collection limit")
	}
	if _, exists := a.pending[request.SessionID]; exists {
		return nil, false, errors.New("collection session already has an in-flight provider request")
	}
	session, err := loadAuthoritySession(a.Config, request.SessionID, source.ID, a.publicKey)
	if err != nil {
		return nil, false, err
	}
	if settlement, settled := session.settlements[request.PageIndex]; settled {
		if settlement.DirectiveSHA256 == directiveSHA256 && settlement.Challenge == request.Challenge && settlement.PageToken == request.PageToken {
			envelopeRaw, err := decodeSettlementEnvelope(settlement)
			if err != nil {
				return nil, false, err
			}
			created, err := persistChallengeIndex(a.Config, settlement, digestSettlement(settlement))
			if err != nil {
				return nil, false, err
			}
			if created {
				a.retainedChallenges++
			}
			if err := validateAuthorityChallengeCrossLink(a.Config, a.Acceptance, settlement); err != nil {
				return nil, false, err
			}
			return envelopeRaw, true, nil
		}
		return nil, false, errors.New("collection page is already settled under different directive bytes")
	}
	if session.sourceID != "" && session.sourceID != source.ID {
		return nil, false, errors.New("collection session changed its source identity")
	}
	if session.terminal || request.PageIndex != session.nextPage || request.PageToken != session.nextToken {
		return nil, false, errors.New("collection session, page or token is out of sequence")
	}
	reservationPath := reservationLedgerPath(a.Config, request.Challenge)
	_ = reservationPath
	created, err := reserveAuthorityChallenge(a.Config, request, directiveSHA256)
	if err != nil {
		return nil, false, err
	}
	if created {
		a.retainedReservations++
	}
	a.pending[request.SessionID] = authorityPending{
		sourceID:                 source.ID,
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

func (a *Authority) settleCollection(request CollectionRequest, directiveSHA256 string, page NativePage, envelopeRaw []byte) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	pending, exists := a.pending[request.SessionID]
	if !exists || pending.sourceID != request.SourceID || pending.pageIndex != request.PageIndex || pending.pageToken != request.PageToken ||
		pending.challenge != request.Challenge || pending.directiveSHA256 != directiveSHA256 || page.SessionID != request.SessionID ||
		page.Challenge != request.Challenge || page.PageIndex != request.PageIndex || page.SourceID != request.SourceID {
		return errors.New("native authority refused to settle a page outside its exact in-flight directive")
	}
	session, err := loadAuthoritySession(a.Config, request.SessionID, request.SourceID, a.publicKey)
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
		SettledAt:                time.Now().UTC().Truncate(time.Second).Format(time.RFC3339),
	}
	settlementRaw, err := json.Marshal(settlement)
	if err != nil {
		return err
	}
	recordCreated, sessionCreated, err := persistSettlement(a.Config, settlement, settlementRaw)
	if err != nil {
		return err
	}
	if recordCreated {
		a.retainedSettlements++
	}
	if sessionCreated {
		a.retainedSessions++
	}
	challengeCreated, err := persistChallengeIndex(a.Config, settlement, digest(settlementRaw))
	if err != nil {
		return err
	}
	if challengeCreated {
		a.retainedChallenges++
	}
	if err := validateAuthorityChallengeCrossLink(a.Config, a.Acceptance, settlement); err != nil {
		return err
	}
	delete(a.pending, request.SessionID)
	return nil
}

func prepareAuthorityLedger(root string) error {
	if err := requireRootOwnedDirectory(root); err != nil {
		return err
	}
	for _, name := range []string{"sessions", "reservations", "challenges"} {
		if err := ensureRootOwnedChildDirectory(root, name); err != nil {
			return err
		}
	}
	return fsyncDirectory(root)
}

func prepareAuthorityShard(config AuthorityConfig, class string, identifier string) error {
	if !canonicalNonce(identifier) || (class != "sessions" && class != "reservations" && class != "challenges") {
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

func derivedAuthorityLedgerDailyBounds(config AuthorityConfig) (uint64, uint64, error) {
	cycles := uint64((86400 + config.CollectorRefreshIntervalSeconds - 1) / config.CollectorRefreshIntervalSeconds)
	var perCycleBytes uint64
	var perCycleRecords uint64
	for _, source := range config.Sources {
		total := uint64(source.MaximumTotalBytes)
		pages := uint64(source.MaximumPages)
		pageOverhead := pages * 1024 * 1024
		if total > (^uint64(0)-pageOverhead)/4 || perCycleBytes > ^uint64(0)-(total*4+pageOverhead) {
			return 0, 0, errors.New("authority ledger source-derived byte horizon overflows")
		}
		perCycleBytes += total*4 + pageOverhead
		// Reservation and challenge publication can each create two shard
		// directories, one attempts directory and one retained attempt inode.
		// Settlement creates one attempt inode per page plus two session shards,
		// the session directory and its attempts directory once per source cycle.
		if perCycleRecords > ^uint64(0)-(pages*9+4) {
			return 0, 0, errors.New("authority ledger source-derived inode horizon overflows")
		}
		perCycleRecords += pages*9 + 4
	}
	if perCycleBytes > ^uint64(0)/cycles || perCycleRecords > ^uint64(0)/cycles {
		return 0, 0, errors.New("authority ledger daily horizon overflows")
	}
	return perCycleBytes * cycles, perCycleRecords * cycles, nil
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
	settledAt, err := time.Parse(time.RFC3339, settlement.SettledAt)
	if err != nil || settledAt.Nanosecond() != 0 || !strings.HasSuffix(settlement.SettledAt, "Z") {
		return 0, errors.New("authority settlement timestamp is not canonical")
	}
	if settlement.Schema != AuthoritySettlementSchema || settlement.ClusterID != config.ClusterID || settlement.DeploymentID != config.DeploymentID ||
		settlement.SourceID == "" || settlement.SessionID == "" || settlement.PageIndex != expectedPage || settlement.PageToken != previous.nextToken ||
		!canonicalNonce(settlement.SessionID) || !canonicalNonce(settlement.Challenge) || !isDigest(settlement.DirectiveSHA256) ||
		!isDigest(settlement.EnvelopeSHA256) || settlement.PreviousSettlementSHA256 != previous.previousSettlementSHA256 ||
		(previous.sourceID != "" && settlement.SourceID != previous.sourceID) || settlement.Terminal == (settlement.NextToken != "") || previous.terminal {
		return 0, errors.New("authority settlement does not extend its exact append-only session chain")
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
		providerRequest.Headers["accept"] != expectedContentType || providerRequest.Headers["x-fs2-request-id"] != providerRequest.RequestID {
		return 0, errors.New("retained native provider request differs from its exact source contract")
	}
	responseRaw, err := base64.StdEncoding.DecodeString(page.RawResponseBase64)
	if err != nil || base64.StdEncoding.EncodeToString(responseRaw) != page.RawResponseBase64 || digest(responseRaw) != page.RawResponseSHA256 {
		return 0, errors.New("retained native provider response is not canonically content addressed")
	}
	collectedAt, err := time.Parse(time.RFC3339, page.CollectedAt)
	if err != nil || collectedAt.Nanosecond() != 0 || !strings.HasSuffix(page.CollectedAt, "Z") {
		return 0, errors.New("retained native page collection time is not canonical")
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
		page.SemanticCollection != source.SemanticCollection || page.ProviderRequestID == "" || strings.ContainsAny(page.ProviderRequestID, "\x00\r\n") ||
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

func persistSettlement(config AuthorityConfig, settlement AuthoritySettlement, raw []byte) (bool, bool, error) {
	if len(raw) > maximumAuthorityLedgerRecordBytes {
		return false, false, errors.New("authority settlement exceeds its fixed record bound")
	}
	if err := ensureAuthorityLedgerCapacity(config, uint64(len(raw))+maximumAuthorityReservationBytes, 5); err != nil {
		return false, false, err
	}
	if err := prepareAuthorityShard(config, "sessions", settlement.SessionID); err != nil {
		return false, false, err
	}
	sessionRoot := sessionLedgerRoot(config, settlement.SessionID)
	_, beforeErr := os.Lstat(sessionRoot)
	sessionCreated := errors.Is(beforeErr, os.ErrNotExist)
	if beforeErr != nil && !sessionCreated {
		return false, false, beforeErr
	}
	if err := ensureRootOwnedChildDirectory(filepath.Dir(sessionRoot), settlement.SessionID); err != nil {
		return false, false, err
	}
	path := filepath.Join(sessionRoot, fmt.Sprintf("page-%08d.json", settlement.PageIndex))
	_, beforeErr = os.Lstat(path)
	recordCreated := errors.Is(beforeErr, os.ErrNotExist)
	if beforeErr != nil && !recordCreated {
		return false, false, beforeErr
	}
	if err := appendOrMatch(path, raw, 0o400); err != nil {
		return false, false, err
	}
	return recordCreated, sessionCreated, nil
}

func reserveAuthorityChallenge(config AuthorityConfig, request CollectionRequest, directiveSHA256 string) (bool, error) {
	terminalPath := challengeLedgerPath(config, request.Challenge)
	if _, err := readRootRegular(terminalPath, 4096); err == nil {
		return false, errors.New("collection challenge already has a terminal settlement")
	} else if !errors.Is(err, os.ErrNotExist) {
		return false, err
	}
	reservation := AuthorityReservation{
		Schema:          AuthorityReservationSchema,
		ClusterID:       config.ClusterID,
		DeploymentID:    config.DeploymentID,
		SourceID:        request.SourceID,
		SessionID:       request.SessionID,
		PageIndex:       request.PageIndex,
		PageToken:       request.PageToken,
		Challenge:       request.Challenge,
		DirectiveSHA256: directiveSHA256,
	}
	raw, err := json.Marshal(reservation)
	if err != nil {
		return false, err
	}
	if len(raw) > maximumAuthorityReservationBytes {
		return false, errors.New("authority reservation exceeds its fixed record bound")
	}
	if err := ensureAuthorityLedgerCapacity(config, uint64(len(raw)), 4); err != nil {
		return false, err
	}
	if err := prepareAuthorityShard(config, "reservations", request.Challenge); err != nil {
		return false, err
	}
	path := reservationLedgerPath(config, request.Challenge)
	_, beforeErr := os.Lstat(path)
	created := errors.Is(beforeErr, os.ErrNotExist)
	if beforeErr != nil && !created {
		return false, beforeErr
	}
	if err := appendOrMatch(path, raw, 0o400); err != nil {
		return false, err
	}
	return created, nil
}

func persistChallengeIndex(config AuthorityConfig, settlement AuthoritySettlement, settlementSHA256 string) (bool, error) {
	challenge := AuthorityChallenge{
		Schema:           AuthorityChallengeSchema,
		ClusterID:        config.ClusterID,
		DeploymentID:     config.DeploymentID,
		Challenge:        settlement.Challenge,
		SessionID:        settlement.SessionID,
		PageIndex:        settlement.PageIndex,
		SettlementSHA256: settlementSHA256,
	}
	raw, err := json.Marshal(challenge)
	if err != nil {
		return false, err
	}
	if err := ensureAuthorityLedgerCapacity(config, uint64(len(raw)), 4); err != nil {
		return false, err
	}
	if err := prepareAuthorityShard(config, "challenges", settlement.Challenge); err != nil {
		return false, err
	}
	path := challengeLedgerPath(config, settlement.Challenge)
	_, beforeErr := os.Lstat(path)
	created := errors.Is(beforeErr, os.ErrNotExist)
	if beforeErr != nil && !created {
		return false, beforeErr
	}
	if err := appendOrMatch(path, raw, 0o400); err != nil {
		return false, err
	}
	return created, nil
}

func validateAuthorityChallengeCrossLink(config AuthorityConfig, acceptance boundary.Acceptance, settlement AuthoritySettlement) error {
	terminalRaw, err := readRootRegular(challengeLedgerPath(config, settlement.Challenge), maximumAuthorityReservationBytes)
	var terminal AuthorityChallenge
	if err != nil || canonicalJSON(terminalRaw, &terminal) != nil || terminal.Schema != AuthorityChallengeSchema ||
		terminal.ClusterID != acceptance.ClusterID || terminal.DeploymentID != acceptance.DeploymentID ||
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
		reservation.SourceID != settlement.SourceID || reservation.SessionID != settlement.SessionID ||
		reservation.PageIndex != settlement.PageIndex || reservation.PageToken != settlement.PageToken ||
		reservation.Challenge != settlement.Challenge || reservation.DirectiveSHA256 != settlement.DirectiveSHA256 {
		return errors.New("authority terminal settlement is not bound to its exact sharded reservation")
	}
	return nil
}

func digestSettlement(settlement AuthoritySettlement) string {
	raw, _ := json.Marshal(settlement)
	return digest(raw)
}

func authorityLedgerEntryExists(path string) bool {
	info, err := os.Lstat(path)
	return err == nil && info.Mode().IsRegular() && info.Mode()&os.ModeSymlink == 0
}

func (a *Authority) collect(ctx context.Context, source AuthoritySourceSpec, directive CollectionRequest) (NativePage, error) {
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
	}
	projectionRaw, _ := json.Marshal(projection)
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, requestURL, nil)
	if err != nil {
		return NativePage{}, err
	}
	request.Header.Set("Accept", accept)
	request.Header.Set("X-FS2-Request-ID", requestID)
	if source.BearerTokenPath != "" {
		tokenRaw, err := readRootRegular(source.BearerTokenPath, 64*1024)
		if err != nil {
			return NativePage{}, err
		}
		token := strings.TrimSpace(string(tokenRaw))
		if token == "" || strings.ContainsAny(token, "\r\n") {
			return NativePage{}, errors.New("authority credential is malformed")
		}
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
	providerResponse, err := client.Do(request)
	if err != nil {
		return NativePage{}, err
	}
	responseRaw, readErr := io.ReadAll(io.LimitReader(providerResponse.Body, source.MaximumPageBytes+1))
	closeErr := providerResponse.Body.Close()
	if readErr != nil || closeErr != nil || int64(len(responseRaw)) > source.MaximumPageBytes ||
		providerResponse.StatusCode != http.StatusOK || providerResponse.TLS == nil || len(providerResponse.TLS.PeerCertificates) == 0 {
		return NativePage{}, errors.New("native provider response is unauthenticated, unsuccessful or oversized")
	}
	providerRequestID := strings.TrimSpace(providerResponse.Header.Get(source.ProviderRequestIDHeader))
	if providerRequestID == "" || strings.ContainsAny(providerRequestID, "\x00\r\n") {
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
	return NativePage{
		Schema:                   NativePageSchema,
		SourceID:                 source.ID,
		SourceKind:               source.Kind,
		SemanticCollection:       source.SemanticCollection,
		RequestID:                requestID,
		ProviderRequestID:        providerRequestID,
		SessionID:                directive.SessionID,
		Challenge:                directive.Challenge,
		PageIndex:                directive.PageIndex,
		RequestBase64:            base64.StdEncoding.EncodeToString(projectionRaw),
		RequestSHA256:            digest(projectionRaw),
		RequestedURL:             requestURL,
		CollectedAt:              time.Now().UTC().Truncate(time.Second).Format(time.RFC3339),
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

func (a *Authority) signPage(page NativePage) ([]byte, error) {
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
