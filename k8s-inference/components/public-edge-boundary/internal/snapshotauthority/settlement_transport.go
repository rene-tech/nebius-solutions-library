package snapshotauthority

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"sync"
	"syscall"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/collector"
)

const settlementWireRequestSchema = "fs2-serve.nebius.ai/snapshot-settlement-request/v1"
const settlementWireResponseSchema = "fs2-serve.nebius.ai/snapshot-settlement-response/v1"
const maximumSettlementWireHeaderBytes = 64 * 1024

type settlementBackend interface {
	lookup(string, []byte, settlementChannel) ([]byte, error)
	recover([]byte, settlementChannel) ([]byte, error)
	settle(collector.EvidenceBundle, []byte, []byte, settlementChannel) ([]byte, error)
	ready() error
}

type settlementChannel struct {
	Identity string
	AttestationEnvelopeRaw []byte
}

type settlementWireRequest struct {
	Schema string `json:"schema"`
	Mode string `json:"mode"`
	AuthenticatedCollectorChannel string `json:"authenticated_collector_channel"`
	CollectorChannelAttestationBase64 string `json:"collector_channel_attestation_base64"`
	CollectorChannelAttestationSHA256 string `json:"collector_channel_attestation_sha256"`
	EvidenceBundleLength int64 `json:"evidence_bundle_length"`
	EvidenceBundleSHA256 string `json:"evidence_bundle_sha256"`
	SnapshotEnvelopeLength int64 `json:"snapshot_envelope_length"`
	SnapshotEnvelopeSHA256 string `json:"snapshot_envelope_sha256"`
}

type settlementWireResponse struct {
	Schema string `json:"schema"`
	Status string `json:"status"`
	SnapshotEnvelopeLength int64 `json:"snapshot_envelope_length"`
	SnapshotEnvelopeSHA256 string `json:"snapshot_envelope_sha256"`
}

type settlementClient struct { config Config }

func loadSettlementClient(config Config) (*settlementClient, error) {
	client := &settlementClient{config: config}
	if err := client.ready(); err != nil { return nil, err }
	return client, nil
}

func (client *settlementClient) lookup(_ string, bundleRaw []byte, channel settlementChannel) ([]byte, error) {
	return client.exchange("lookup", bundleRaw, nil, channel)
}

func (client *settlementClient) recover(bundleRaw []byte, channel settlementChannel) ([]byte, error) {
	return client.exchange("recover", bundleRaw, nil, channel)
}

func (client *settlementClient) settle(_ collector.EvidenceBundle, bundleRaw []byte, snapshotRaw []byte, channel settlementChannel) ([]byte, error) {
	return client.exchange("settle", bundleRaw, snapshotRaw, channel)
}

func (client *settlementClient) ready() error {
	_, err := client.exchange("ready", nil, nil, settlementChannel{})
	return err
}

func (client *settlementClient) exchange(mode string, bundleRaw []byte, snapshotRaw []byte, channel settlementChannel) ([]byte, error) {
	if int64(len(bundleRaw)) > maximumRequestBytes || len(snapshotRaw) > boundary.MaxSnapshotBytes { return nil, errors.New("snapshot settlement request exceeds its exact wire bounds") }
	request := settlementWireRequest{
		Schema: settlementWireRequestSchema, Mode: mode, AuthenticatedCollectorChannel: channel.Identity,
		CollectorChannelAttestationBase64: base64.StdEncoding.EncodeToString(channel.AttestationEnvelopeRaw),
		CollectorChannelAttestationSHA256: digestBytes(channel.AttestationEnvelopeRaw),
		EvidenceBundleLength: int64(len(bundleRaw)), EvidenceBundleSHA256: digestBytes(bundleRaw),
		SnapshotEnvelopeLength: int64(len(snapshotRaw)), SnapshotEnvelopeSHA256: digestBytes(snapshotRaw),
	}
	headerRaw, err := json.Marshal(request)
	if err != nil || len(headerRaw) > maximumSettlementWireHeaderBytes { return nil, errors.New("snapshot settlement request header is invalid") }
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	connection, err := (&net.Dialer{}).DialContext(ctx, "unix", abstractSettlementSocket(client.config.SettlementSocketName))
	if err != nil { return nil, err }
	defer connection.Close()
	unixConnection, ok := connection.(*net.UnixConn)
	if !ok { return nil, errors.New("snapshot settlement channel is not Unix") }
	credentials, err := unixPeerCredentials(unixConnection)
	if err != nil || credentials.Uid != client.config.SettlementRuntimeUID || credentials.Gid != client.config.SettlementRuntimeGID { return nil, errors.New("snapshot settlement custodian peer identity is invalid") }
	deadline, _ := ctx.Deadline()
	if err := connection.SetDeadline(deadline); err != nil { return nil, err }
	if err := writeSettlementFrame(connection, headerRaw, bundleRaw, snapshotRaw); err != nil { return nil, err }
	if err := unixConnection.CloseWrite(); err != nil { return nil, err }
	responseRaw, unexpectedFirst, responseBody, err := readSettlementFrame(connection, maximumSettlementWireHeaderBytes, 0, boundary.MaxSnapshotBytes)
	if err != nil { return nil, err }
	if len(unexpectedFirst) != 0 || settlementFrameHasTrailingBytes(connection) { return nil, errors.New("snapshot settlement custodian response has trailing bytes") }
	var response settlementWireResponse
	if _, err := boundary.CanonicalJSON(responseRaw, &response); err != nil || response.Schema != settlementWireResponseSchema || response.SnapshotEnvelopeLength != int64(len(responseBody)) || response.SnapshotEnvelopeSHA256 != digestBytes(responseBody) { return nil, errors.New("snapshot settlement custodian response is invalid") }
	switch response.Status {
	case "ok":
		return responseBody, nil
	case "not-found":
		return nil, os.ErrNotExist
	case "refused":
		return nil, errors.New("snapshot settlement custodian durably refused the request")
	default:
		return nil, errors.New("snapshot settlement custodian failed closed")
	}
}

type SettlementCustodian struct {
	config Config
	acceptance boundary.Acceptance
	collectorConfig collector.Config
	nativeTrust *boundary.ExternalTrust
	snapshotTrust *boundary.ExternalTrust
	collectorChannels *collectorChannelVerifier
	store *settlementStore
	slots chan struct{}
}

func LoadSettlementCustodian(configPath string, acceptance boundary.Acceptance) (*SettlementCustodian, error) {
	raw, err := readAcceptedRegular(configPath, maximumConfigBytes, acceptance.SnapshotAuthorityConfigSHA256, false)
	if err != nil { return nil, err }
	var config Config
	if _, err := boundary.CanonicalJSON(raw, &config); err != nil { return nil, err }
	if err := validateConfig(config, acceptance); err != nil { return nil, err }
	if uint32(os.Geteuid()) != config.SettlementRuntimeUID || uint32(os.Getegid()) != config.SettlementRuntimeGID { return nil, errors.New("snapshot settlement custodian is not running as its distinct accepted UID/GID") }
	if err := requireNoSupplementaryGroups(); err != nil { return nil, err }
	collectorConfig, nativeTrust, err := collector.LoadEvidenceVerificationContract(config.NativeCollectorConfigPath, config.NativeResponseTrustPath, acceptance)
	if err != nil { return nil, err }
	expectedDailySettlements := uint64((86400 + collectorConfig.RefreshIntervalSeconds - 1) / collectorConfig.RefreshIntervalSeconds)
	if config.MaximumSettlementsPerDay != expectedDailySettlements { return nil, errors.New("snapshot settlement quota differs from the independently accepted collector cadence") }
	snapshotTrust, err := boundary.LoadExternalTrust(config.SnapshotTrustPath, acceptance.SnapshotTrustSHA256, boundary.TrustSchema)
	if err != nil { return nil, err }
	collectorChannels, err := loadCollectorChannelVerifier(config)
	if err != nil { return nil, err }
	privateKey, err := loadSettlementSigningKey(config)
	if err != nil { return nil, err }
	store, err := loadSettlementStore(config, privateKey)
	if err != nil { return nil, err }
	return &SettlementCustodian{config: config, acceptance: acceptance, collectorConfig: collectorConfig, nativeTrust: nativeTrust, snapshotTrust: snapshotTrust, collectorChannels: collectorChannels, store: store, slots: make(chan struct{}, config.MaximumConcurrentRequests)}, nil
}

func (custodian *SettlementCustodian) Serve(ctx context.Context) error {
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: abstractSettlementSocket(custodian.config.SettlementSocketName), Net: "unix"})
	if err != nil { return err }
	defer listener.Close()
	var wait sync.WaitGroup
	go func() { <-ctx.Done(); _ = listener.Close() }()
	wait.Add(1)
	go func() {
		defer wait.Done()
		ticker := time.NewTicker(15*time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done(): return
			case now := <-ticker.C:
				custodian.store.setReconciliationHealth(custodian.store.reconcileRecentRetained(now.UTC()))
			}
		}
	}()
	for {
		connection, acceptErr := listener.AcceptUnix()
		if acceptErr != nil {
			if ctx.Err() != nil { wait.Wait(); return nil }
			return acceptErr
		}
		select {
		case custodian.slots <- struct{}{}:
			wait.Add(1)
			go func() { defer wait.Done(); defer func(){ <-custodian.slots }(); custodian.handleConnection(connection) }()
		default:
			_ = writeSettlementResponse(connection, "busy", nil)
			_ = connection.Close()
		}
	}
}

func (custodian *SettlementCustodian) handleConnection(connection *net.UnixConn) {
	defer connection.Close()
	_ = connection.SetDeadline(time.Now().Add(30*time.Second))
	if !custodian.acceptance.ProductionTrustCurrent(time.Now().UTC()) { _ = writeSettlementResponse(connection, "error", nil); return }
	credentials, err := unixPeerCredentials(connection)
	if err != nil || credentials.Uid != custodian.config.RuntimeUID || credentials.Gid != custodian.config.RuntimeGID { _ = writeSettlementResponse(connection, "unauthorized", nil); return }
	headerRaw, bundleRaw, snapshotRaw, err := readSettlementFrame(connection, maximumSettlementWireHeaderBytes, maximumRequestBytes, boundary.MaxSnapshotBytes)
	if err != nil { _ = writeSettlementResponse(connection, "invalid", nil); return }
	if settlementFrameHasTrailingBytes(connection) { _ = writeSettlementResponse(connection, "invalid", nil); return }
	var request settlementWireRequest
	if _, err := boundary.CanonicalJSON(headerRaw, &request); err != nil || request.Schema != settlementWireRequestSchema || request.EvidenceBundleLength != int64(len(bundleRaw)) || request.EvidenceBundleSHA256 != digestBytes(bundleRaw) || request.SnapshotEnvelopeLength != int64(len(snapshotRaw)) || request.SnapshotEnvelopeSHA256 != digestBytes(snapshotRaw) { _ = writeSettlementResponse(connection, "invalid", nil); return }
	if request.Mode == "ready" {
		if len(bundleRaw) != 0 || len(snapshotRaw) != 0 || request.AuthenticatedCollectorChannel != "" || request.CollectorChannelAttestationBase64 != "" || request.CollectorChannelAttestationSHA256 != digestBytes(nil) || custodian.store.ready() != nil { _ = writeSettlementResponse(connection, "error", nil); return }
		_ = writeSettlementResponse(connection, "ok", nil)
		return
	}
	attestationRaw, decodeErr := base64.StdEncoding.Strict().DecodeString(request.CollectorChannelAttestationBase64)
	if decodeErr != nil || base64.StdEncoding.EncodeToString(attestationRaw) != request.CollectorChannelAttestationBase64 || len(attestationRaw) < 1 || len(attestationRaw) > maximumSettlementWireHeaderBytes || digestBytes(attestationRaw) != request.CollectorChannelAttestationSHA256 { _ = writeSettlementResponse(connection, "unauthorized", nil); return }
	channel, channelErr := custodian.verifyCollectorChannel(attestationRaw, request.AuthenticatedCollectorChannel, request.EvidenceBundleSHA256, time.Now().UTC())
	if channelErr != nil { _ = writeSettlementResponse(connection, "unauthorized", nil); return }
	if custodian.store.ready() != nil { _ = writeSettlementResponse(connection, "error", nil); return }
	var result []byte
	switch request.Mode {
	case "lookup":
		result, err = custodian.store.lookup(digestBytes(bundleRaw), bundleRaw, channel)
	case "recover":
		result, err = custodian.store.recover(bundleRaw, channel)
	case "settle":
		now := time.Now().UTC().Truncate(time.Second)
		verified, verifyErr := collector.VerifyEvidenceBundle(bundleRaw, custodian.collectorConfig, custodian.acceptance, custodian.nativeTrust, now)
		if verifyErr != nil || validateSnapshotResponse(custodian.snapshotTrust, custodian.config, custodian.acceptance, snapshotRaw, bundleRaw, now) != nil { _ = writeSettlementResponse(connection, "refused", nil); return }
		if !custodian.acceptance.ProductionTrustCurrent(time.Now().UTC()) { _ = writeSettlementResponse(connection, "refused", nil); return }
		result, err = custodian.store.settle(verified.Bundle, bundleRaw, snapshotRaw, channel)
	default:
		_ = writeSettlementResponse(connection, "invalid", nil)
		return
	}
	if errors.Is(err, os.ErrNotExist) { _ = writeSettlementResponse(connection, "not-found", nil); return }
	if err != nil { _ = writeSettlementResponse(connection, "refused", nil); return }
	if !custodian.acceptance.ProductionTrustCurrent(time.Now().UTC()) { _ = writeSettlementResponse(connection, "refused", nil); return }
	_ = writeSettlementResponse(connection, "ok", result)
}

func (custodian *SettlementCustodian) verifyCollectorChannel(attestationRaw []byte, claimedIdentity string, bundleSHA256 string, now time.Time) (settlementChannel, error) {
	payloadRaw, err := custodian.snapshotTrust.VerifyEnvelope(boundary.EnvelopeSchema, collector.SnapshotIssuerRole, attestationRaw)
	if err != nil { return settlementChannel{}, errors.New("collector channel attestation signature is invalid") }
	var evidence CollectorChannelEvidence
	if _, err := boundary.CanonicalJSON(payloadRaw, &evidence); err != nil || evidence.Schema != CollectorChannelEvidenceSchema || evidence.ClusterID != custodian.config.ClusterID || evidence.DeploymentID != custodian.config.DeploymentID || evidence.EvidenceBundleSHA256 != bundleSHA256 || !isDigestText(bundleSHA256) {
		return settlementChannel{}, errors.New("collector channel attestation differs from the exact request")
	}
	identity := evidence.Slot+":"+evidence.SPKISHA256
	if claimedIdentity != identity { return settlementChannel{}, errors.New("collector channel identity differs from its signed transcript") }
	if err := custodian.collectorChannels.verifyEvidence(evidence, now.UTC()); err != nil { return settlementChannel{}, err }
	return settlementChannel{Identity: identity, AttestationEnvelopeRaw: append([]byte(nil), attestationRaw...)}, nil
}

func abstractSettlementSocket(name string) string {
	if len(name) > 0 && name[0] == '@' { return "\x00"+name[1:] }
	return name
}

func unixPeerCredentials(connection *net.UnixConn) (*syscall.Ucred, error) {
	raw, err := connection.SyscallConn()
	if err != nil { return nil, err }
	var credentials *syscall.Ucred
	var controlErr error
	if err := raw.Control(func(fd uintptr) { credentials, controlErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED) }); err != nil { return nil, err }
	if controlErr != nil || credentials == nil { return nil, errors.New("Unix peer credentials are unavailable") }
	return credentials, nil
}

func writeSettlementFrame(writer io.Writer, headerRaw []byte, first []byte, second []byte) error {
	if len(headerRaw) < 1 || len(headerRaw) > maximumSettlementWireHeaderBytes { return errors.New("settlement frame header is outside its exact bound") }
	prefix := make([]byte, 4)
	binary.BigEndian.PutUint32(prefix, uint32(len(headerRaw)))
	for _, part := range [][]byte{prefix, headerRaw, first, second} {
		if len(part) == 0 { continue }
		if _, err := io.Copy(writer, bytesReader(part)); err != nil { return err }
	}
	return nil
}

func readSettlementFrame(reader io.Reader, maximumHeader int, maximumFirst int64, maximumSecond int) ([]byte, []byte, []byte, error) {
	prefix := make([]byte, 4)
	if _, err := io.ReadFull(reader, prefix); err != nil { return nil, nil, nil, err }
	headerLength := int(binary.BigEndian.Uint32(prefix))
	if headerLength < 1 || headerLength > maximumHeader { return nil, nil, nil, errors.New("settlement frame header length is invalid") }
	headerRaw := make([]byte, headerLength)
	if _, err := io.ReadFull(reader, headerRaw); err != nil { return nil, nil, nil, err }
	var projection map[string]any
	if err := boundary.DecodeExactJSON(headerRaw, &projection); err != nil { return nil, nil, nil, errors.New("settlement frame header is invalid") }
	firstLength := int64(0)
	secondLength := int64(0)
	if value, exists := projection["evidence_bundle_length"]; exists { number, ok := value.(json.Number); if !ok { return nil, nil, nil, errors.New("settlement frame first length is not numeric") }; parsed, parseErr := number.Int64(); if parseErr != nil { return nil, nil, nil, parseErr }; firstLength = parsed }
	if value, exists := projection["snapshot_envelope_length"]; exists { number, ok := value.(json.Number); if !ok { return nil, nil, nil, errors.New("settlement frame second length is not numeric") }; parsed, parseErr := number.Int64(); if parseErr != nil { return nil, nil, nil, parseErr }; secondLength = parsed }
	if firstLength < 0 || firstLength > maximumFirst || secondLength < 0 || secondLength > int64(maximumSecond) { return nil, nil, nil, errors.New("settlement frame body lengths are invalid") }
	first := make([]byte, int(firstLength))
	second := make([]byte, int(secondLength))
	if _, err := io.ReadFull(reader, first); err != nil { return nil, nil, nil, err }
	if _, err := io.ReadFull(reader, second); err != nil { return nil, nil, nil, err }
	return headerRaw, first, second, nil
}

func writeSettlementResponse(writer io.Writer, status string, snapshotRaw []byte) error {
	header := settlementWireResponse{Schema: settlementWireResponseSchema, Status: status, SnapshotEnvelopeLength: int64(len(snapshotRaw)), SnapshotEnvelopeSHA256: digestBytes(snapshotRaw)}
	raw, err := json.Marshal(header)
	if err != nil { return err }
	return writeSettlementFrame(writer, raw, nil, snapshotRaw)
}

func settlementFrameHasTrailingBytes(reader io.Reader) bool {
	probe := make([]byte, 1)
	count, err := reader.Read(probe)
	return count != 0 || err != io.EOF
}

type byteSliceReader struct { value []byte; offset int }
func bytesReader(value []byte) *byteSliceReader { return &byteSliceReader{value: value} }
func (reader *byteSliceReader) Read(target []byte) (int, error) {
	if reader.offset == len(reader.value) { return 0, io.EOF }
	count := copy(target, reader.value[reader.offset:])
	reader.offset += count
	return count, nil
}
