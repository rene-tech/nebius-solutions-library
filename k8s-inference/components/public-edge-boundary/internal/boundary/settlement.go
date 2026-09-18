package boundary

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
	"unsafe"
)

const (
	TransitionSettlementConfigSchema = "fs2-serve.nebius.ai/public-edge-transition-settlement-config/v1"
	previousTransitionSettlementSchema = "fs2-serve.nebius.ai/public-edge-transition-settlement/v1"
	transitionSettlementSchema       = "fs2-serve.nebius.ai/public-edge-transition-settlement/v2"
	transitionDailyPolicySchema      = "fs2-serve.nebius.ai/public-edge-transition-daily-policy/v1"
	transitionDailyReservationSchema = "fs2-serve.nebius.ai/public-edge-transition-daily-reservation/v2"
	maximumTransitionConfigBytes     = 64 * 1024
	maximumTransitionReceiptBytes    = 64 * 1024
	maximumTransitionDailyPolicyBytes = 4 * 1024
	maximumTransitionReservationBytes = 96 * 1024
	maximumTransitionReceiptsPerDay  = 1_000_000
	transitionOTmpfile               = 0x410000
	transitionATSymlinkFollow        = 0x400
	transitionATFDCWD                = -100
)

type TransitionSettlementConfig struct {
	Schema             string `json:"schema"`
	ClusterID          string `json:"cluster_id"`
	DeploymentID       string `json:"deployment_id"`
	Root               string `json:"root"`
	DeviceID           uint64 `json:"device_id"`
	CapacityBytes      uint64 `json:"capacity_bytes"`
	CapacityInodes     uint64 `json:"capacity_inodes"`
	MinimumFreeBytes   uint64 `json:"minimum_free_bytes"`
	MinimumFreeInodes  uint64 `json:"minimum_free_inodes"`
	OperatingHorizonDays int  `json:"operating_horizon_days"`
	MaximumReceiptsPerDay uint64 `json:"maximum_receipts_per_day"`
}

type TransitionSettlement struct {
	Schema           string `json:"schema"`
	ClusterID        string `json:"cluster_id"`
	DeploymentID     string `json:"deployment_id"`
	SnapshotID       string `json:"snapshot_id"`
	TransitionID     string `json:"transition_id"`
	TransitionSHA256 string `json:"transition_sha256"`
	AdmissionUID     string `json:"admission_uid"`
	RequestSHA256    string `json:"request_sha256"`
	AdmissionChannel AdmissionChannelBinding `json:"admission_channel"`
	SettledAt        string `json:"settled_at"`
	SettlementDay    string `json:"settlement_day,omitempty"`
}

// transitionDailyPolicy pins the one global receipt ceiling for a UTC day.
// A config rotation may choose a different ceiling only in a later UTC day;
// it cannot make higher retained slots from an earlier policy generation
// invisible to a smaller scan.
type transitionDailyPolicy struct {
	Schema          string `json:"schema"`
	Day             string `json:"day"`
	MaximumReceipts uint64 `json:"maximum_receipts"`
	ConfigSHA256    string `json:"config_sha256"`
}

// transitionDailyReservation is the append-only cross-process daily CAS. The
// exact receipt bytes are retained in the slot so a crash after reservation
// but before final receipt publication can be completed without inventing a
// second settlement timestamp or consuming another day's capacity.
type transitionDailyReservation struct {
	Schema           string `json:"schema"`
	Day              string `json:"day"`
	Slot             uint64 `json:"slot"`
	PolicySHA256     string `json:"policy_sha256"`
	TransitionID     string `json:"transition_id"`
	SettlementSHA256 string `json:"settlement_sha256"`
	SettlementBase64 string `json:"settlement_base64"`
}

type admissionChannelConsumer struct {
	ledger *TransitionLedger
	channel AdmissionChannelBinding
}

type TransitionLedger struct {
	config       TransitionSettlementConfig
	configSHA256 string
}

func LoadTransitionLedger(path string, expectedSHA256 string, clusterID string, deploymentID string) (*TransitionLedger, error) {
	raw, err := readProtectedRegular(path, maximumTransitionConfigBytes)
	if err != nil {
		return nil, fmt.Errorf("read transition settlement config: %w", err)
	}
	if digestHex(raw) != expectedSHA256 {
		return nil, errors.New("transition settlement config differs from independently accepted bytes")
	}
	var config TransitionSettlementConfig
	if err := decodeExactJSON(raw, &config); err != nil {
		return nil, err
	}
	canonical, err := json.Marshal(config)
	if err != nil || !bytes.Equal(canonical, raw) || config.Schema != TransitionSettlementConfigSchema ||
		config.ClusterID != clusterID || config.DeploymentID != deploymentID || !filepath.IsAbs(config.Root) ||
		filepath.Clean(config.Root) != config.Root || config.Root == "/" || config.DeviceID == 0 ||
		config.CapacityBytes < 1024*1024 || config.CapacityInodes < 4096 ||
		config.MinimumFreeBytes < maximumTransitionReceiptBytes+maximumTransitionReservationBytes || config.MinimumFreeInodes < 1024 ||
		config.CapacityBytes <= config.MinimumFreeBytes || config.CapacityInodes <= config.MinimumFreeInodes ||
		config.OperatingHorizonDays < 365 || config.OperatingHorizonDays > 3660 || config.MaximumReceiptsPerDay < 1 ||
		config.MaximumReceiptsPerDay > maximumTransitionReceiptsPerDay {
		return nil, errors.New("transition settlement config is incomplete or cannot preserve its append-only horizon")
	}
	days := uint64(config.OperatingHorizonDays)
	requiredInodes := (config.MaximumReceiptsPerDay*10 + 16) * days + 16
	requiredBytes := ((maximumTransitionReceiptBytes+maximumTransitionReservationBytes)*config.MaximumReceiptsPerDay + maximumTransitionDailyPolicyBytes) * days
	if requiredInodes > config.CapacityInodes-config.MinimumFreeInodes ||
		requiredBytes > config.CapacityBytes-config.MinimumFreeBytes {
		return nil, errors.New("transition settlement config cannot preserve its policy-pinned append-only horizon")
	}
	if err := requireTransitionDirectory(config.Root); err != nil {
		return nil, err
	}
	ledger := &TransitionLedger{config: config, configSHA256: expectedSHA256}
	if err := ledger.ensureCapacity(maximumTransitionReceiptBytes+maximumTransitionReservationBytes, 10); err != nil {
		return nil, err
	}
	probePath := filepath.Join(config.Root, "atomic-publication-probe-v1")
	probe := []byte("fs2-public-edge-transition-atomic-publication/v1\n")
	if err := publishTransitionRegular(probePath, probe); err != nil {
		return nil, fmt.Errorf("transition ledger lacks atomic no-replace publication: %w", err)
	}
	return ledger, nil
}

func (l *TransitionLedger) Consume(snapshot Snapshot, transition Transition, admissionUID string, requestSHA256 string, now time.Time) error {
	return errors.New("transition settlement requires an authenticated API-server admission channel")
}

func BindAdmissionChannel(l *TransitionLedger, channel AdmissionChannelBinding) (TransitionConsumer, error) {
	if l == nil || channel.Schema != AdmissionChannelBindingSchema ||
		(channel.IdentitySlot != "current" && channel.IdentitySlot != "next") ||
		!isSHA256(channel.PeerCertificateSHA256) || !isSHA256(channel.PeerSPKISHA256) ||
		!safeText(channel.PeerSPIFFEURI, false) || !safeText(channel.WebhookServerName, false) ||
		!isSHA256(channel.ControlPlaneAdmissionConfigurationSHA256) {
		return nil, errors.New("admission channel binding is invalid")
	}
	return admissionChannelConsumer{ledger: l, channel: channel}, nil
}

func (c admissionChannelConsumer) Consume(snapshot Snapshot, transition Transition, admissionUID string, requestSHA256 string, now time.Time) error {
	return c.ledger.consumeBound(snapshot, transition, admissionUID, requestSHA256, c.channel, now)
}

func (l *TransitionLedger) consumeBound(snapshot Snapshot, transition Transition, admissionUID string, requestSHA256 string, channel AdmissionChannelBinding, now time.Time) error {
	if snapshot.ClusterID != l.config.ClusterID || snapshot.DeploymentID != l.config.DeploymentID || !isSHA256(snapshot.SnapshotID) ||
		!isSHA256(transition.TransitionID) || !safeText(admissionUID, false) || len(admissionUID) > 128 || !isSHA256(requestSHA256) {
		return errors.New("transition settlement identity is invalid")
	}
	writerLock, err := l.acquireWriterLock()
	if err != nil {
		return err
	}
	defer func() {
		_ = syscall.Flock(int(writerLock.Fd()), syscall.LOCK_UN)
		_ = writerLock.Close()
	}()
	// Recheck the security deadline only after winning the shared writer CAS.
	// Waiting admission requests cannot settle against a time captured before a
	// competing replica consumed capacity or the snapshot expired.
	now = time.Now().UTC().Truncate(time.Second)
	if !snapshotCurrent(snapshot, now) {
		return errors.New("transition settlement refuses an expired snapshot")
	}
	transitionRaw, err := json.Marshal(transition)
	if err != nil {
		return err
	}
	settledAt := now
	receipt := TransitionSettlement{
		Schema: transitionSettlementSchema, ClusterID: l.config.ClusterID, DeploymentID: l.config.DeploymentID,
		SnapshotID: snapshot.SnapshotID, TransitionID: transition.TransitionID, TransitionSHA256: digestHex(transitionRaw),
		AdmissionUID: admissionUID, RequestSHA256: requestSHA256, AdmissionChannel: channel,
		SettledAt: settledAt.Format(time.RFC3339), SettlementDay: settledAt.Format("2006-01-02"),
	}
	receiptRaw, err := json.Marshal(receipt)
	if err != nil || len(receiptRaw) > maximumTransitionReceiptBytes {
		return errors.New("transition settlement receipt exceeds its exact bound")
	}
	path, err := l.receiptPath(transition.TransitionID)
	if err != nil {
		return err
	}
	if retained, readErr := readTransitionRegular(path, maximumTransitionReceiptBytes); readErr == nil {
		if err := validateTransitionRetry(retained, receipt); err != nil {
			return err
		}
		if _, err := l.reserveDailySettlement(retained, receipt); err != nil {
			return fmt.Errorf("confirm retained transition daily reservation: %w", err)
		}
		if err := confirmTransitionReceiptDurable(path); err != nil {
			return fmt.Errorf("confirm retained transition receipt durability: %w", err)
		}
		if !snapshotCurrent(snapshot, time.Now()) {
			return errors.New("transition idempotent retry crossed snapshot expiry")
		}
		return nil
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return readErr
	}
	reservedReceiptRaw, err := l.reserveDailySettlement(receiptRaw, receipt)
	if err != nil {
		return err
	}
	receiptRaw = reservedReceiptRaw
	if err := l.ensureCapacity(uint64(len(receiptRaw)), 5); err != nil {
		return err
	}
	if err := ensureTransitionDirectory(filepath.Join(l.config.Root, "receipts")); err != nil {
		return err
	}
	if err := ensureTransitionDirectory(filepath.Join(l.config.Root, "receipts", transition.TransitionID[:2])); err != nil {
		return err
	}
	if err := ensureTransitionDirectory(filepath.Dir(path)); err != nil {
		return err
	}
	if err := publishTransitionRegular(path, receiptRaw); err != nil {
		if retained, readErr := readTransitionRegular(path, maximumTransitionReceiptBytes); readErr == nil {
			if retryErr := validateTransitionRetry(retained, receipt); retryErr != nil {
				return retryErr
			}
			if syncErr := confirmTransitionReceiptDurable(path); syncErr != nil {
				return fmt.Errorf("repair transition receipt directory durability: %w", syncErr)
			}
			if !snapshotCurrent(snapshot, time.Now()) {
				return errors.New("transition settlement crossed snapshot expiry")
			}
			return nil
		}
		return err
	}
	if !snapshotCurrent(snapshot, time.Now()) {
		return errors.New("transition settlement crossed snapshot expiry")
	}
	return nil
}

func (l *TransitionLedger) reserveDailySettlement(
	receiptRaw []byte,
	expected TransitionSettlement,
) ([]byte, error) {
	if len(receiptRaw) < 1 || len(receiptRaw) > maximumTransitionReceiptBytes {
		return nil, errors.New("transition daily reservation has an invalid receipt")
	}
	var retainedReceipt TransitionSettlement
	if err := decodeExactJSON(receiptRaw, &retainedReceipt); err != nil || validateTransitionRetry(receiptRaw, expected) != nil {
		return nil, errors.New("transition daily reservation receipt is not the exact admission settlement")
	}
	settledAt, err := parseWholeUTC(retainedReceipt.SettledAt)
	if err != nil {
		return nil, errors.New("transition daily reservation lacks its actual UTC settlement time")
	}
	day := settledAt.UTC().Format("2006-01-02")
	if retainedReceipt.SettlementDay != "" && retainedReceipt.SettlementDay != day {
		return nil, errors.New("transition receipt settlement day differs from its UTC timestamp")
	}
	previousDay := settledAt.Add(-24 * time.Hour).UTC().Format("2006-01-02")
	if previousRaw, found, err := l.findDailySettlement(previousDay, expected); err != nil {
		return nil, err
	} else if found {
		return previousRaw, nil
	}
	dayRoot := filepath.Join(l.config.Root, "days", day)
	if err := l.ensureCapacity(maximumTransitionReceiptBytes+maximumTransitionReservationBytes, 10); err != nil {
		return nil, err
	}
	for _, directory := range []string{filepath.Join(l.config.Root, "days"), dayRoot} {
		if err := ensureTransitionDirectory(directory); err != nil {
			return nil, err
		}
	}
	policy, policySHA256, err := l.ensureDailyPolicy(day)
	if err != nil {
		return nil, err
	}
	if err := ensureTransitionDirectory(filepath.Join(dayRoot, "slots")); err != nil {
		return nil, err
	}
	seed, err := strconv.ParseUint(expected.TransitionID[:16], 16, 64)
	if err != nil {
		return nil, errors.New("transition ID cannot derive its bounded daily slot")
	}
	seed %= policy.MaximumReceipts
	for offset := uint64(0); offset < policy.MaximumReceipts; offset++ {
		slot := seed + offset
		if slot >= policy.MaximumReceipts {
			slot -= policy.MaximumReceipts
		}
		slotName := fmt.Sprintf("%08d", slot)
		shardRoot := filepath.Join(dayRoot, "slots", slotName[:2])
		leafRoot := filepath.Join(shardRoot, slotName[2:4])
		if err := l.ensureCapacity(maximumTransitionReceiptBytes+maximumTransitionReservationBytes, 10); err != nil {
			return nil, err
		}
		for _, directory := range []string{shardRoot, leafRoot} {
			if err := ensureTransitionDirectory(directory); err != nil {
				return nil, err
			}
		}
		path := filepath.Join(leafRoot, "slot-"+slotName+".json")
		retained, readErr := readTransitionRegular(path, maximumTransitionReservationBytes)
		if readErr == nil {
			reservedRaw, match, matchErr := validateDailyReservation(retained, day, slot, policySHA256, expected)
			if matchErr != nil {
				return nil, matchErr
			}
			if match {
				if err := confirmTransitionRegularDurable(path, maximumTransitionReservationBytes); err != nil {
					return nil, err
				}
				return reservedRaw, nil
			}
			continue
		}
		if !errors.Is(readErr, os.ErrNotExist) {
			return nil, readErr
		}
		record := transitionDailyReservation{
			Schema: transitionDailyReservationSchema, Day: day, Slot: slot,
			PolicySHA256: policySHA256,
			TransitionID: expected.TransitionID, SettlementSHA256: digestHex(receiptRaw),
			SettlementBase64: base64.StdEncoding.EncodeToString(receiptRaw),
		}
		recordRaw, marshalErr := json.Marshal(record)
		if marshalErr != nil || len(recordRaw) > maximumTransitionReservationBytes {
			return nil, errors.New("transition daily reservation exceeds its exact bound")
		}
		if err := l.ensureCapacity(uint64(len(recordRaw)+len(receiptRaw)), 10); err != nil {
			return nil, err
		}
		if publishErr := publishTransitionRegular(path, recordRaw); publishErr != nil {
			// A competing replica may have won this exact no-replace slot. Reopen
			// it before moving to another slot rather than treating a collision as
			// authority to exceed the configured daily ceiling.
			if retained, readErr := readTransitionRegular(path, maximumTransitionReservationBytes); readErr == nil {
				reservedRaw, match, matchErr := validateDailyReservation(retained, day, slot, policySHA256, expected)
				if matchErr != nil {
					return nil, matchErr
				}
				if match {
					if err := confirmTransitionRegularDurable(path, maximumTransitionReservationBytes); err != nil {
						return nil, err
					}
					return reservedRaw, nil
				}
				continue
			}
			return nil, publishErr
		}
		return receiptRaw, nil
	}
	return nil, errors.New("transition settlement daily receipt limit is exhausted")
}

func (l *TransitionLedger) findDailySettlement(
	day string,
	expected TransitionSettlement,
) ([]byte, bool, error) {
	policy, policyRaw, err := l.loadDailyPolicy(day)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	policySHA256 := digestHex(policyRaw)
	seed, err := strconv.ParseUint(expected.TransitionID[:16], 16, 64)
	if err != nil {
		return nil, false, errors.New("transition ID cannot derive its bounded daily lookup")
	}
	seed %= policy.MaximumReceipts
	dayRoot := filepath.Join(l.config.Root, "days", day, "slots")
	for offset := uint64(0); offset < policy.MaximumReceipts; offset++ {
		slot := seed + offset
		if slot >= policy.MaximumReceipts {
			slot -= policy.MaximumReceipts
		}
		slotName := fmt.Sprintf("%08d", slot)
		path := filepath.Join(dayRoot, slotName[:2], slotName[2:4], "slot-"+slotName+".json")
		retained, readErr := readTransitionRegular(path, maximumTransitionReservationBytes)
		if errors.Is(readErr, os.ErrNotExist) {
			return nil, false, nil
		}
		if readErr != nil {
			return nil, false, readErr
		}
		reservedRaw, match, matchErr := validateDailyReservation(retained, day, slot, policySHA256, expected)
		if matchErr != nil {
			return nil, false, matchErr
		}
		if match {
			if err := confirmTransitionRegularDurable(path, maximumTransitionReservationBytes); err != nil {
				return nil, false, err
			}
			return reservedRaw, true, nil
		}
	}
	return nil, false, nil
}

func (l *TransitionLedger) ensureDailyPolicy(day string) (transitionDailyPolicy, string, error) {
	policy := transitionDailyPolicy{
		Schema: transitionDailyPolicySchema, Day: day,
		MaximumReceipts: l.config.MaximumReceiptsPerDay, ConfigSHA256: l.configSHA256,
	}
	policyRaw, err := json.Marshal(policy)
	if err != nil || len(policyRaw) > maximumTransitionDailyPolicyBytes {
		return transitionDailyPolicy{}, "", errors.New("transition daily policy exceeds its exact bound")
	}
	path := filepath.Join(l.config.Root, "days", day, "policy.json")
	if retained, readErr := readTransitionRegular(path, maximumTransitionDailyPolicyBytes); readErr == nil {
		retainedPolicy, retainedRaw, validateErr := l.loadDailyPolicy(day)
		if validateErr != nil {
			return transitionDailyPolicy{}, "", validateErr
		}
		if !bytes.Equal(retained, retainedRaw) || retainedPolicy.MaximumReceipts != policy.MaximumReceipts {
			return transitionDailyPolicy{}, "", errors.New("transition daily policy cannot change within its UTC day")
		}
		if err := confirmTransitionRegularDurable(path, maximumTransitionDailyPolicyBytes); err != nil {
			return transitionDailyPolicy{}, "", err
		}
		return retainedPolicy, digestHex(retainedRaw), nil
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return transitionDailyPolicy{}, "", readErr
	}
	// An older implementation could have created slots without recording its
	// cap. Refuse to guess that cap: doing so could hide retained high slots.
	if _, statErr := os.Lstat(filepath.Join(l.config.Root, "days", day, "slots")); statErr == nil {
		return transitionDailyPolicy{}, "", errors.New("transition day contains unenrolled legacy slots without a pinned policy")
	} else if !errors.Is(statErr, os.ErrNotExist) {
		return transitionDailyPolicy{}, "", statErr
	}
	if err := l.ensureCapacity(uint64(len(policyRaw)), 2); err != nil {
		return transitionDailyPolicy{}, "", err
	}
	if err := publishTransitionRegular(path, policyRaw); err != nil {
		return transitionDailyPolicy{}, "", err
	}
	return policy, digestHex(policyRaw), nil
}

func (l *TransitionLedger) loadDailyPolicy(day string) (transitionDailyPolicy, []byte, error) {
	if len(day) != len("2006-01-02") {
		return transitionDailyPolicy{}, nil, errors.New("transition daily policy day is invalid")
	}
	path := filepath.Join(l.config.Root, "days", day, "policy.json")
	raw, err := readTransitionRegular(path, maximumTransitionDailyPolicyBytes)
	if err != nil {
		return transitionDailyPolicy{}, nil, err
	}
	var policy transitionDailyPolicy
	if err := decodeExactJSON(raw, &policy); err != nil {
		return transitionDailyPolicy{}, nil, err
	}
	canonical, err := json.Marshal(policy)
	parsedDay, dayErr := time.Parse("2006-01-02", policy.Day)
	if err != nil || !bytes.Equal(canonical, raw) || policy.Schema != transitionDailyPolicySchema ||
		policy.Day != day || dayErr != nil || parsedDay.Format("2006-01-02") != day ||
		policy.MaximumReceipts < 1 || policy.MaximumReceipts > maximumTransitionReceiptsPerDay ||
		!isSHA256(policy.ConfigSHA256) {
		return transitionDailyPolicy{}, nil, errors.New("transition daily policy is non-canonical or invalid")
	}
	return policy, raw, nil
}

func validateDailyReservation(
	raw []byte,
	day string,
	slot uint64,
	policySHA256 string,
	expected TransitionSettlement,
) ([]byte, bool, error) {
	var record transitionDailyReservation
	if err := decodeExactJSON(raw, &record); err != nil {
		return nil, false, err
	}
	canonical, err := json.Marshal(record)
	if err != nil || !bytes.Equal(canonical, raw) || record.Schema != transitionDailyReservationSchema ||
		record.Day != day || record.Slot != slot || record.PolicySHA256 != policySHA256 || !isSHA256(record.PolicySHA256) ||
		!isSHA256(record.TransitionID) ||
		!isSHA256(record.SettlementSHA256) {
		return nil, false, errors.New("transition daily reservation is non-canonical or misplaced")
	}
	if record.TransitionID != expected.TransitionID {
		return nil, false, nil
	}
	settlementRaw, err := base64.StdEncoding.Strict().DecodeString(record.SettlementBase64)
	if err != nil || base64.StdEncoding.EncodeToString(settlementRaw) != record.SettlementBase64 ||
		digestHex(settlementRaw) != record.SettlementSHA256 || len(settlementRaw) > maximumTransitionReceiptBytes {
		return nil, false, errors.New("transition daily reservation settlement bytes are invalid")
	}
	if err := validateTransitionRetry(settlementRaw, expected); err != nil {
		return nil, false, err
	}
	return settlementRaw, true, nil
}

func confirmTransitionReceiptDurable(path string) error {
	return confirmTransitionRegularDurable(path, maximumTransitionReceiptBytes)
}

func confirmTransitionRegularDurable(path string, maximum int64) error {
	if filepath.Base(path) == "." || filepath.Base(path) == "" {
		return errors.New("transition receipt path is invalid")
	}
	if _, err := readTransitionRegular(path, maximum); err != nil {
		return err
	}
	return fsyncTransitionDirectory(filepath.Dir(path))
}

func snapshotCurrent(snapshot Snapshot, now time.Time) bool {
	issuedAt, issuedErr := parseWholeUTC(snapshot.IssuedAt)
	expiresAt, expiresErr := parseWholeUTC(snapshot.ExpiresAt)
	now = now.UTC().Truncate(time.Second)
	return issuedErr == nil && expiresErr == nil && !issuedAt.After(now) && now.Before(expiresAt)
}

func validateTransitionRetry(raw []byte, expected TransitionSettlement) error {
	var retained TransitionSettlement
	if err := decodeExactJSON(raw, &retained); err != nil {
		return err
	}
	canonical, err := json.Marshal(retained)
	settledAt, settledAtErr := parseWholeUTC(retained.SettledAt)
	settlementDay := ""
	if settledAtErr == nil {
		settlementDay = settledAt.UTC().Format("2006-01-02")
	}
	if err != nil || !bytes.Equal(canonical, raw) ||
		(retained.Schema != transitionSettlementSchema && retained.Schema != previousTransitionSettlementSchema) ||
		retained.ClusterID != expected.ClusterID || retained.DeploymentID != expected.DeploymentID ||
		retained.SnapshotID != expected.SnapshotID || retained.TransitionID != expected.TransitionID ||
		retained.TransitionSHA256 != expected.TransitionSHA256 || retained.AdmissionUID != expected.AdmissionUID ||
		retained.RequestSHA256 != expected.RequestSHA256 || retained.AdmissionChannel != expected.AdmissionChannel ||
		(retained.Schema == transitionSettlementSchema && retained.SettlementDay != settlementDay) ||
		(retained.Schema == previousTransitionSettlementSchema && retained.SettlementDay != "") {
		return errors.New("transition was already consumed by a different admission request")
	}
	if settledAtErr != nil || settledAt.IsZero() {
		return errors.New("transition settlement timestamp is invalid")
	}
	return nil
}

func (l *TransitionLedger) receiptPath(transitionID string) (string, error) {
	if !isSHA256(transitionID) {
		return "", errors.New("transition ID is not canonical")
	}
	return filepath.Join(l.config.Root, "receipts", transitionID[:2], transitionID[2:4], transitionID+".json"), nil
}

func requireTransitionDirectory(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.IsDir() || info.Mode().Perm() != 0o700 || stat.Uid != uint32(os.Geteuid()) || stat.Gid != uint32(os.Getegid()) {
		return errors.New("transition ledger directory lacks exact private writer custody")
	}
	return nil
}

func ensureTransitionDirectory(path string) error {
	if err := os.Mkdir(path, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return err
	}
	if err := requireTransitionDirectory(path); err != nil {
		return err
	}
	return fsyncTransitionDirectory(filepath.Dir(path))
}

// acquireWriterLock serializes statfs reservation, daily slot allocation and
// receipt publication for every process and replica sharing the accepted
// ledger filesystem. It deliberately uses non-blocking flock: admission fails
// closed under contention instead of waiting beyond snapshot authorization.
// The deployment must still place this filesystem under independent shared
// writer custody; a process-local volume cannot satisfy that integration gate.
func (l *TransitionLedger) acquireWriterLock() (*os.File, error) {
	path := filepath.Join(l.config.Root, "transition-writer.lock")
	fd, err := syscall.Open(path, syscall.O_RDWR|syscall.O_CREAT|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	fail := func(message string) (*os.File, error) {
		_ = file.Close()
		return nil, errors.New(message)
	}
	info, err := file.Stat()
	if err != nil || info == nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 {
		return fail("transition writer lock has invalid type or mode")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Geteuid()) || stat.Gid != uint32(os.Getegid()) ||
		stat.Dev != l.config.DeviceID || stat.Nlink != 1 {
		return fail("transition writer lock has invalid custody or filesystem identity")
	}
	if err := fsyncTransitionDirectory(l.config.Root); err != nil {
		_ = file.Close()
		return nil, err
	}
	if err := syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return nil, errors.New("transition writer CAS is busy")
		}
		return nil, err
	}
	return file, nil
}

func readTransitionRegular(path string, maximum int64) ([]byte, error) {
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	before, err := file.Stat()
	if err != nil || !before.Mode().IsRegular() || before.Mode().Perm() != 0o400 || before.Size() < 1 || before.Size() > maximum {
		return nil, errors.New("transition ledger record has invalid custody or extent")
	}
	stat, ok := before.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Geteuid()) || stat.Gid != uint32(os.Getegid()) {
		return nil, errors.New("transition ledger record has the wrong owner")
	}
	raw := make([]byte, before.Size())
	if _, err := io.ReadFull(file, raw); err != nil {
		return nil, err
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(before, after) || before.Size() != after.Size() {
		return nil, errors.New("transition ledger record changed while read")
	}
	return raw, nil
}

func publishTransitionRegular(path string, raw []byte) error {
	parent := filepath.Dir(path)
	if err := requireTransitionDirectory(parent); err != nil {
		return err
	}
	if retained, err := readTransitionRegular(path, int64(len(raw))); err == nil {
		if !bytes.Equal(retained, raw) {
			return errors.New("transition no-replace publication conflicts with retained bytes")
		}
		return fsyncTransitionDirectory(parent)
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	fd, err := syscall.Open(parent, syscall.O_RDWR|syscall.O_CLOEXEC|transitionOTmpfile, 0o400)
	if err != nil {
		return err
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	if err := syscall.Fchmod(fd, 0o400); err != nil {
		return err
	}
	written := 0
	for written < len(raw) {
		count, writeErr := file.Write(raw[written:])
		if writeErr != nil || count < 1 {
			if writeErr != nil {
				return writeErr
			}
			return errors.New("transition publication made no write progress")
		}
		written += count
	}
	if err := file.Sync(); err != nil {
		return err
	}
	procPath := fmt.Sprintf("/proc/self/fd/%d", fd)
	var descriptorStat syscall.Stat_t
	if err := syscall.Fstat(fd, &descriptorStat); err != nil {
		return err
	}
	procInfo, err := os.Stat(procPath)
	if err != nil || procInfo == nil {
		return errors.New("transition publication descriptor identity is unavailable")
	}
	procStat, ok := procInfo.Sys().(*syscall.Stat_t)
	if !ok || procStat.Dev != descriptorStat.Dev || procStat.Ino != descriptorStat.Ino {
		return errors.New("transition publication descriptor identity is invalid")
	}
	oldPath, _ := syscall.BytePtrFromString(procPath)
	targetPath, _ := syscall.BytePtrFromString(path)
	directoryFD := transitionATFDCWD
	_, _, errno := syscall.RawSyscall6(syscall.SYS_LINKAT, uintptr(directoryFD), uintptr(unsafe.Pointer(oldPath)),
		uintptr(directoryFD), uintptr(unsafe.Pointer(targetPath)), uintptr(transitionATSymlinkFollow), 0)
	if errno != 0 && errno != syscall.EEXIST {
		return errno
	}
	retained, readErr := readTransitionRegular(path, int64(len(raw)))
	if readErr != nil || !bytes.Equal(retained, raw) {
		return errors.New("transition publication did not retain exact bytes")
	}
	return fsyncTransitionDirectory(parent)
}

func fsyncTransitionDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func (l *TransitionLedger) ensureCapacity(upcomingBytes uint64, upcomingInodes uint64) error {
	rootInfo, err := os.Lstat(l.config.Root)
	parentInfo, parentErr := os.Lstat(filepath.Dir(l.config.Root))
	if err != nil || parentErr != nil || rootInfo == nil || parentInfo == nil {
		return errors.New("transition ledger device ancestry is unavailable")
	}
	rootStat, rootOK := rootInfo.Sys().(*syscall.Stat_t)
	parentStat, parentOK := parentInfo.Sys().(*syscall.Stat_t)
	if !rootOK || !parentOK || rootStat.Dev != l.config.DeviceID || rootStat.Dev == parentStat.Dev {
		return errors.New("transition ledger is not on its independently accepted dedicated device")
	}
	var status syscall.Statfs_t
	if err := syscall.Statfs(l.config.Root, &status); err != nil {
		return err
	}
	blockSize := uint64(status.Bsize)
	if uint64(status.Blocks) > ^uint64(0)/blockSize || uint64(status.Bavail) > ^uint64(0)/blockSize {
		return errors.New("transition ledger capacity arithmetic overflows")
	}
	total := uint64(status.Blocks) * blockSize
	available := uint64(status.Bavail) * blockSize
	if upcomingBytes > ^uint64(0)-l.config.MinimumFreeBytes || upcomingInodes > ^uint64(0)-l.config.MinimumFreeInodes ||
		total < l.config.CapacityBytes || uint64(status.Files) < l.config.CapacityInodes ||
		available < l.config.MinimumFreeBytes+upcomingBytes || uint64(status.Ffree) < l.config.MinimumFreeInodes+upcomingInodes {
		return errors.New("transition ledger cannot preserve its accepted free-space reserve")
	}
	return nil
}
