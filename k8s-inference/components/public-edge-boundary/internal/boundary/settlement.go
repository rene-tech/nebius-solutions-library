package boundary

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"
	"time"
	"unsafe"
)

const (
	TransitionSettlementConfigSchema = "fs2-serve.nebius.ai/public-edge-transition-settlement-config/v1"
	transitionSettlementSchema       = "fs2-serve.nebius.ai/public-edge-transition-settlement/v1"
	maximumTransitionConfigBytes     = 64 * 1024
	maximumTransitionReceiptBytes    = 64 * 1024
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
}

type admissionChannelConsumer struct {
	ledger *TransitionLedger
	channel AdmissionChannelBinding
}

type TransitionLedger struct {
	config TransitionSettlementConfig
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
		config.MinimumFreeBytes < maximumTransitionReceiptBytes || config.MinimumFreeInodes < 1024 ||
		config.CapacityBytes <= config.MinimumFreeBytes || config.CapacityInodes <= config.MinimumFreeInodes ||
		config.OperatingHorizonDays < 365 || config.OperatingHorizonDays > 3660 || config.MaximumReceiptsPerDay < 1 ||
		config.MaximumReceiptsPerDay > (config.CapacityInodes-config.MinimumFreeInodes)/(3*uint64(config.OperatingHorizonDays)) ||
		config.MaximumReceiptsPerDay > (config.CapacityBytes-config.MinimumFreeBytes)/(maximumTransitionReceiptBytes*uint64(config.OperatingHorizonDays)) {
		return nil, errors.New("transition settlement config is incomplete or cannot preserve its append-only horizon")
	}
	if err := requireTransitionDirectory(config.Root); err != nil {
		return nil, err
	}
	ledger := &TransitionLedger{config: config}
	if err := ledger.ensureCapacity(maximumTransitionReceiptBytes, 5); err != nil {
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
	if !snapshotCurrent(snapshot, now) {
		return errors.New("transition settlement refuses an expired snapshot")
	}
	transitionRaw, err := json.Marshal(transition)
	if err != nil {
		return err
	}
	receipt := TransitionSettlement{
		Schema: transitionSettlementSchema, ClusterID: l.config.ClusterID, DeploymentID: l.config.DeploymentID,
		SnapshotID: snapshot.SnapshotID, TransitionID: transition.TransitionID, TransitionSHA256: digestHex(transitionRaw),
		AdmissionUID: admissionUID, RequestSHA256: requestSHA256, AdmissionChannel: channel,
		SettledAt: now.UTC().Truncate(time.Second).Format(time.RFC3339),
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
		if !snapshotCurrent(snapshot, time.Now()) {
			return errors.New("transition idempotent retry crossed snapshot expiry")
		}
		return nil
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return readErr
	}
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
	if err != nil || !bytes.Equal(canonical, raw) || retained.Schema != transitionSettlementSchema ||
		retained.ClusterID != expected.ClusterID || retained.DeploymentID != expected.DeploymentID ||
		retained.SnapshotID != expected.SnapshotID || retained.TransitionID != expected.TransitionID ||
		retained.TransitionSHA256 != expected.TransitionSHA256 || retained.AdmissionUID != expected.AdmissionUID ||
		retained.RequestSHA256 != expected.RequestSHA256 || retained.AdmissionChannel != expected.AdmissionChannel {
		return errors.New("transition was already consumed by a different admission request")
	}
	settledAt, err := parseWholeUTC(retained.SettledAt)
	if err != nil || settledAt.IsZero() {
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

func readTransitionRegular(path string, maximum int64) ([]byte, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
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
	_, _, errno := syscall.RawSyscall6(syscall.SYS_LINKAT, uintptr(transitionATFDCWD), uintptr(unsafe.Pointer(oldPath)),
		uintptr(transitionATFDCWD), uintptr(unsafe.Pointer(targetPath)), uintptr(transitionATSymlinkFollow), 0)
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
	rootStat, rootOK := rootInfo.Sys().(*syscall.Stat_t)
	parentStat, parentOK := parentInfo.Sys().(*syscall.Stat_t)
	if err != nil || parentErr != nil || !rootOK || !parentOK || rootStat.Dev != l.config.DeviceID || rootStat.Dev == parentStat.Dev {
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
