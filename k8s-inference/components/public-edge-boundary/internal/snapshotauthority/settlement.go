package snapshotauthority

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/collector"
)

const settlementLinuxOTmpfile = 0x410000
const settlementLinuxATSymlinkFollow = 0x400
const maximumSettlementControlBytes = 256 * 1024
const settlementPerCycleMetadataBytes = 8 * 4096
const settlementPerCycleInodes = 8
const settlementFixedBytes = 1024 * 1024
const settlementFixedInodes = 32

var errSettlementDailyCapacity = errors.New("snapshot settlement daily signed-response limit is exhausted")

type settlementStore struct {
	config Config
	privateKey ed25519.PrivateKey
	verificationKeys map[string]ed25519.PublicKey
	mu sync.Mutex
	healthMu sync.RWMutex
	reconciliationErr error
	writerLockPath string
}

func loadSettlementStore(config Config, privateKey ed25519.PrivateKey) (*settlementStore, error) {
	if err := requireSettlementRoot(config); err != nil { return nil, err }
	verificationKeys, err := loadSettlementVerificationKeys(config, privateKey)
	if err != nil { return nil, err }
	store := &settlementStore{config: config, privateKey: privateKey, verificationKeys: verificationKeys, writerLockPath: filepath.Join(config.SettlementRoot, "writer.lock")}
	for _, name := range []string{"prepared", "reservations", "by-bundle", "outcomes", "daily-slots"} { if err := store.ensureDirectory(filepath.Join(config.SettlementRoot, name), 0o700); err != nil { return nil, err } }
	if err := store.ensureWriterLock(); err != nil { return nil, err }
	if err := store.recoverRetained(); err != nil { return nil, err }
	return store, nil
}

func validateSettlementCapacityContract(config Config) error {
	if config.SettlementCapacityBytes <= config.SettlementMinimumFreeBytes || config.SettlementCapacityInodes <= config.SettlementMinimumFreeInodes { return errors.New("snapshot settlement reserve exceeds its dedicated volume capacity") }
	perSettlement := uint64(maximumSettlementBytes + 2*maximumSettlementControlBytes + settlementPerCycleMetadataBytes)
	dailyBytes, ok := checkedMultiply(config.MaximumSettlementsPerDay, perSettlement)
	if !ok { return errors.New("snapshot settlement daily byte bound overflows") }
	dailyInodes, ok := checkedMultiply(config.MaximumSettlementsPerDay, settlementPerCycleInodes)
	if !ok { return errors.New("snapshot settlement daily inode bound overflows") }
	horizonBytes, ok := checkedMultiply(dailyBytes, uint64(config.SettlementOperatingHorizonDays))
	if !ok || horizonBytes > ^uint64(0)-settlementFixedBytes || horizonBytes+settlementFixedBytes > config.SettlementCapacityBytes-config.SettlementMinimumFreeBytes { return errors.New("snapshot settlement volume cannot retain its accepted byte horizon") }
	horizonInodes, ok := checkedMultiply(dailyInodes, uint64(config.SettlementOperatingHorizonDays))
	if !ok || horizonInodes > ^uint64(0)-settlementFixedInodes || horizonInodes+settlementFixedInodes > config.SettlementCapacityInodes-config.SettlementMinimumFreeInodes { return errors.New("snapshot settlement volume cannot retain its accepted inode horizon") }
	return nil
}

func (store *settlementStore) settle(bundle collector.EvidenceBundle, bundleRaw []byte, snapshotEnvelopeRaw []byte, channel settlementChannel) ([]byte, error) {
	release, err := store.acquireWriter()
	if err != nil { return nil, err }
	defer release()
	return store.settleLocked(bundle, bundleRaw, snapshotEnvelopeRaw, channel, true)
}

func (store *settlementStore) settleLocked(bundle collector.EvidenceBundle, bundleRaw []byte, snapshotEnvelopeRaw []byte, channel settlementChannel, allowQuotaCreation bool) ([]byte, error) {
	bundleSHA256 := digestBytes(bundleRaw)
	if existing, err := store.lookup(bundleSHA256, bundleRaw, settlementChannel{}); err == nil { return existing, nil } else if !errors.Is(err, os.ErrNotExist) { return nil, err }
	if !isDigestText(bundle.CycleID) || !isDigestText(bundle.CycleContractSHA256) { return nil, errors.New("snapshot settlement cycle identity is invalid") }
	var quota SettlementQuota
	var quotaEnvelopeRaw []byte
	var err error
	if allowQuotaCreation {
		quota, quotaEnvelopeRaw, err = store.reserveDailyQuota(bundle, bundleSHA256, channel)
	} else {
		quota, quotaEnvelopeRaw, err = store.findQuotaForBundle(bundle, bundleSHA256)
	}
	if err != nil { return nil, err }
	preparedName := "cycle-"+bundle.CycleID+".json"
	preparedPath := filepath.Join(store.config.SettlementRoot, "prepared", preparedName)
	var envelopeRaw []byte
	if retained, err := store.readRegular(preparedPath, maximumSettlementBytes); err == nil {
		retainedSnapshot, validationErr := store.validateSettlement(retained, bundleSHA256, bundleRaw, quota, quotaEnvelopeRaw)
		if validationErr != nil || snapshotEnvelopeRaw != nil && !bytes.Equal(retainedSnapshot, snapshotEnvelopeRaw) {
			return nil, errors.New("snapshot cycle is already reserved for different evidence or response bytes")
		}
		snapshotEnvelopeRaw = retainedSnapshot
		envelopeRaw = retained
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	} else {
		if snapshotEnvelopeRaw == nil { return nil, os.ErrNotExist }
		preparedAt := time.Now().UTC().Truncate(time.Second)
		deadline, parseErr := time.Parse(time.RFC3339, bundle.CycleDeadlineAt)
		if parseErr != nil || !preparedAt.Before(deadline) { return nil, errors.New("snapshot settlement cannot prepare after its exact cycle deadline") }
		settlement := Settlement{
			Schema: SettlementSchema, ClusterID: store.config.ClusterID, DeploymentID: store.config.DeploymentID,
			CycleID: bundle.CycleID, CycleContractSHA256: bundle.CycleContractSHA256,
			CycleIssuedAt: bundle.CycleIssuedAt, CycleDeadlineAt: bundle.CycleDeadlineAt,
			EvidenceBundleSHA256: bundleSHA256, SnapshotEnvelopeSHA256: digestBytes(snapshotEnvelopeRaw),
			SnapshotEnvelopeBase64: base64.StdEncoding.EncodeToString(snapshotEnvelopeRaw),
			CollectorCredentialLaneID: store.configCollectorLane(), AuthenticatedCollectorChannel: quota.AuthenticatedCollectorChannel,
			QuotaEnvelopeSHA256: digestBytes(quotaEnvelopeRaw), QuotaUTCDate: quota.UTCDate, QuotaSlot: quota.Slot,
			Status: "prepared", PreparedAt: preparedAt.Format(time.RFC3339),
		}
		settlementRaw, marshalErr := json.Marshal(settlement)
		if marshalErr != nil { return nil, marshalErr }
		envelopeRaw, err = store.signSettlement(settlementRaw)
		if err != nil || len(envelopeRaw) > maximumSettlementBytes { return nil, errors.New("snapshot settlement exceeds its exact signed bound") }
		if err := store.publishRegular(preparedPath, envelopeRaw, 0o400); err != nil { return nil, err }
	}
	reservationRoot := filepath.Join(store.config.SettlementRoot, "reservations", bundleSHA256[:2])
	if err := store.ensureDirectory(reservationRoot, 0o700); err != nil { return nil, err }
	reservationPath := filepath.Join(reservationRoot, "bundle-"+bundleSHA256+".json")
	if err := store.ensurePublicationCapacity(4096, 0); err != nil { return nil, err }
	if err := os.Link(preparedPath, reservationPath); err != nil {
		if !errors.Is(err, os.ErrExist) { return nil, err }
		retained, readErr := store.readRegular(reservationPath, maximumSettlementBytes)
		if readErr != nil { return nil, readErr }
		snapshotRaw, validateErr := store.validateSettlement(retained, bundleSHA256, bundleRaw, quota, quotaEnvelopeRaw)
		if validateErr != nil || !bytes.Equal(snapshotRaw, snapshotEnvelopeRaw) { return nil, errors.New("snapshot bundle reservation conflicts with another signed response") }
		envelopeRaw = retained
	}
	if err := fsyncSettlementDirectory(reservationRoot); err != nil { return nil, err }
	if existing, outcomeErr := store.readOutcome(bundle, bundleSHA256, envelopeRaw, quotaEnvelopeRaw); outcomeErr == nil {
		if existing.Status != "acknowledged" { return nil, errors.New("snapshot settlement terminal outcome refused the cycle") }
		return snapshotEnvelopeRaw, nil
	} else if !errors.Is(outcomeErr, os.ErrNotExist) {
		return nil, outcomeErr
	}
	deadline, parseErr := time.Parse(time.RFC3339, bundle.CycleDeadlineAt)
	if parseErr != nil { return nil, parseErr }
	finalRoot := filepath.Join(store.config.SettlementRoot, "by-bundle", bundleSHA256[:2])
	finalPath := filepath.Join(finalRoot, "bundle-"+bundleSHA256+".json")
	if existingFinal, finalErr := store.readRegular(finalPath, maximumSettlementBytes); finalErr == nil {
		retainedSnapshot, validationErr := store.validateSettlement(existingFinal, bundleSHA256, bundleRaw, quota, quotaEnvelopeRaw)
		if validationErr != nil || !bytes.Equal(retainedSnapshot, snapshotEnvelopeRaw) { return nil, errors.New("recovered final snapshot settlement conflicts with its exact prepared response") }
		if syncErr := fsyncSettlementDirectory(finalRoot); syncErr != nil { return nil, syncErr }
		outcome, outcomeErr := store.settleOutcome(bundle, bundleSHA256, existingFinal, quotaEnvelopeRaw, true, quota.AuthenticatedCollectorChannel)
		if outcomeErr != nil { return nil, outcomeErr }
		if outcome.Status != "acknowledged" { return nil, errors.New("recovered final snapshot settlement was durably refused after its deadline") }
		return retainedSnapshot, nil
	} else if !errors.Is(finalErr, os.ErrNotExist) {
		return nil, finalErr
	}
	if err := store.ensureDirectory(finalRoot, 0o700); err != nil { return nil, err }
	if err := store.ensurePublicationCapacity(4096, 0); err != nil { return nil, err }
	preFinal := time.Now().UTC().Truncate(time.Second)
	if !preFinal.Before(deadline) {
		if _, outcomeErr := store.settleOutcome(bundle, bundleSHA256, envelopeRaw, quotaEnvelopeRaw, false, quota.AuthenticatedCollectorChannel); outcomeErr != nil { return nil, outcomeErr }
		return nil, errors.New("snapshot settlement terminal outcome refused the cycle deadline")
	}
	if err := os.Link(reservationPath, finalPath); err != nil {
		if !errors.Is(err, os.ErrExist) { return nil, err }
		existing, readErr := store.readRegular(finalPath, maximumSettlementBytes)
		if readErr != nil { return nil, readErr }
		retainedSnapshot, validateErr := store.validateSettlement(existing, bundleSHA256, bundleRaw, quota, quotaEnvelopeRaw)
		if validateErr != nil || !bytes.Equal(retainedSnapshot, snapshotEnvelopeRaw) { return nil, errors.New("final snapshot settlement conflicts with its exact prepared response") }
		envelopeRaw = existing
	}
	if err := fsyncSettlementDirectory(finalRoot); err != nil { return nil, err }
	retained, err := store.readRegular(finalPath, maximumSettlementBytes)
	if err != nil { return nil, err }
	retainedSnapshot, err := store.validateSettlement(retained, bundleSHA256, bundleRaw, quota, quotaEnvelopeRaw)
	if err != nil { return nil, err }
	outcome, err := store.settleOutcome(bundle, bundleSHA256, retained, quotaEnvelopeRaw, true, quota.AuthenticatedCollectorChannel)
	if err != nil { return nil, err }
	if outcome.Status != "acknowledged" { return nil, errors.New("snapshot settlement terminal outcome refused the cycle deadline") }
	return retainedSnapshot, nil
}

func (store *settlementStore) ensureWriterLock() error {
	file, err := os.OpenFile(store.writerLockPath, os.O_CREATE|os.O_EXCL|os.O_RDWR|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		if !errors.Is(err, os.ErrExist) { return err }
		file, err = os.OpenFile(store.writerLockPath, os.O_RDWR|syscall.O_NOFOLLOW, 0)
		if err != nil { return err }
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 { return errors.New("snapshot settlement writer lock is not an exact private regular file") }
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != store.config.SettlementRuntimeUID || uint64(stat.Dev) != store.config.SettlementDeviceID { return errors.New("snapshot settlement writer lock lacks accepted UID/device custody") }
	if err := file.Sync(); err != nil { return err }
	return fsyncSettlementDirectory(store.config.SettlementRoot)
}

func (store *settlementStore) acquireWriter() (func(), error) {
	store.mu.Lock()
	file, err := os.OpenFile(store.writerLockPath, os.O_RDWR|syscall.O_NOFOLLOW, 0)
	if err != nil { store.mu.Unlock(); return nil, err }
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 { file.Close(); store.mu.Unlock(); return nil, errors.New("snapshot settlement writer lock changed") }
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != store.config.SettlementRuntimeUID || uint64(stat.Dev) != store.config.SettlementDeviceID { file.Close(); store.mu.Unlock(); return nil, errors.New("snapshot settlement writer lock custody changed") }
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX); err != nil { file.Close(); store.mu.Unlock(); return nil, err }
	return func() { _ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN); _ = file.Close(); store.mu.Unlock() }, nil
}

func (store *settlementStore) ensurePublicationCapacity(upcomingBytes uint64, upcomingInodes uint64) error {
	var statistics syscall.Statfs_t
	if err := syscall.Statfs(store.config.SettlementRoot, &statistics); err != nil { return err }
	blockSize := uint64(statistics.Bsize)
	if blockSize == 0 || uint64(statistics.Blocks) > ^uint64(0)/blockSize || uint64(statistics.Bavail) > ^uint64(0)/blockSize || upcomingBytes > ^uint64(0)-store.config.SettlementMinimumFreeBytes || upcomingInodes > ^uint64(0)-store.config.SettlementMinimumFreeInodes { return errors.New("snapshot settlement capacity accounting overflows") }
	capacityBytes := uint64(statistics.Blocks)*blockSize
	availableBytes := uint64(statistics.Bavail)*blockSize
	if capacityBytes != store.config.SettlementCapacityBytes || uint64(statistics.Files) != store.config.SettlementCapacityInodes || availableBytes < store.config.SettlementMinimumFreeBytes+upcomingBytes || uint64(statistics.Ffree) < store.config.SettlementMinimumFreeInodes+upcomingInodes { return errors.New("snapshot settlement dedicated volume lacks its accepted capacity or reserve") }
	return nil
}

func (store *settlementStore) reserveDailyQuota(bundle collector.EvidenceBundle, bundleSHA256 string, channel settlementChannel) (SettlementQuota, []byte, error) {
	if !store.acceptedCollectorChannel(channel.Identity) || len(channel.AttestationEnvelopeRaw) == 0 { return SettlementQuota{}, nil, errors.New("snapshot settlement collector channel is not independently attested") }
	issuedAt, issueErr := time.Parse(time.RFC3339, bundle.CycleIssuedAt)
	deadline, deadlineErr := time.Parse(time.RFC3339, bundle.CycleDeadlineAt)
	if issueErr != nil || deadlineErr != nil || issuedAt.Nanosecond() != 0 || deadline.Nanosecond() != 0 || !issuedAt.Before(deadline) {
		return SettlementQuota{}, nil, errors.New("snapshot settlement cycle window is invalid")
	}
	dates := []string{issuedAt.UTC().Format("2006-01-02")}
	deadlineDate := deadline.Add(-time.Nanosecond).UTC().Format("2006-01-02")
	if deadlineDate != dates[0] { dates = append(dates, deadlineDate) }
	var retainedQuota SettlementQuota
	var retainedRaw []byte
	for _, date := range dates {
		quota, raw, found, err := store.findDailyQuota(date, bundle.CycleID, bundle.CycleContractSHA256, bundleSHA256)
		if err != nil { return SettlementQuota{}, nil, err }
		if found {
			if retainedRaw != nil { return SettlementQuota{}, nil, errors.New("snapshot settlement quota is duplicated across UTC dates") }
			retainedQuota, retainedRaw = quota, raw
		}
	}
	if retainedRaw != nil { return retainedQuota, retainedRaw, nil }
	now := time.Now().UTC().Truncate(time.Second)
	if now.Before(issuedAt) || !now.Before(deadline) { return SettlementQuota{}, nil, errors.New("snapshot settlement cannot reserve capacity outside its exact cycle window") }
	if err := store.ensurePublicationCapacity(uint64(maximumSettlementBytes+2*maximumSettlementControlBytes+settlementPerCycleMetadataBytes), settlementPerCycleInodes); err != nil { return SettlementQuota{}, nil, err }
	date := now.Format("2006-01-02")
	dayRoot := filepath.Join(store.config.SettlementRoot, "daily-slots", date)
	if err := store.ensureDirectory(dayRoot, 0o700); err != nil { return SettlementQuota{}, nil, err }
	_, _, found, err := store.findDailyQuota(date, bundle.CycleID, bundle.CycleContractSHA256, bundleSHA256)
	if err != nil { return SettlementQuota{}, nil, err }
	if found { return store.findDailyQuotaResult(date, bundle.CycleID, bundle.CycleContractSHA256, bundleSHA256) }
	entries, err := os.ReadDir(dayRoot)
	if err != nil { return SettlementQuota{}, nil, err }
	used := make(map[uint64]bool, len(entries))
	for _, entry := range entries {
		index, parseErr := settlementSlotIndex(entry)
		if parseErr != nil || index >= store.config.MaximumSettlementsPerDay { return SettlementQuota{}, nil, errors.New("snapshot settlement daily quota namespace is not canonical") }
		used[index] = true
	}
	if uint64(len(used)) >= store.config.MaximumSettlementsPerDay { return SettlementQuota{}, nil, errSettlementDailyCapacity }
	for slot := uint64(0); slot < store.config.MaximumSettlementsPerDay; slot++ {
		if used[slot] { continue }
		quota := SettlementQuota{
			Schema: SettlementQuotaSchema, ClusterID: store.config.ClusterID, DeploymentID: store.config.DeploymentID,
			CycleID: bundle.CycleID, CycleContractSHA256: bundle.CycleContractSHA256, EvidenceBundleSHA256: bundleSHA256,
			CycleIssuedAt: bundle.CycleIssuedAt, CycleDeadlineAt: bundle.CycleDeadlineAt,
			AuthenticatedCollectorChannel: channel.Identity, CollectorChannelAttestationSHA256: digestBytes(channel.AttestationEnvelopeRaw), UTCDate: date, Slot: slot, ReservedAt: now.Format(time.RFC3339),
		}
		payloadRaw, marshalErr := json.Marshal(quota)
		if marshalErr != nil { return SettlementQuota{}, nil, marshalErr }
		envelopeRaw, signErr := store.signQuota(payloadRaw)
		if signErr != nil || len(envelopeRaw) > maximumSettlementControlBytes { return SettlementQuota{}, nil, errors.New("snapshot settlement quota exceeds its exact signed bound") }
		path := filepath.Join(dayRoot, fmt.Sprintf("slot-%012d.json", slot))
		if publishErr := store.publishRegular(path, envelopeRaw, 0o400); publishErr != nil { return SettlementQuota{}, nil, publishErr }
		return store.readQuota(path, date, slot)
	}
	return SettlementQuota{}, nil, errSettlementDailyCapacity
}

func (store *settlementStore) findDailyQuotaResult(date string, cycleID string, cycleContractSHA256 string, bundleSHA256 string) (SettlementQuota, []byte, error) {
	quota, raw, found, err := store.findDailyQuota(date, cycleID, cycleContractSHA256, bundleSHA256)
	if err != nil { return SettlementQuota{}, nil, err }
	if !found { return SettlementQuota{}, nil, os.ErrNotExist }
	return quota, raw, nil
}

func (store *settlementStore) findDailyQuota(date string, cycleID string, cycleContractSHA256 string, bundleSHA256 string) (SettlementQuota, []byte, bool, error) {
	dayRoot := filepath.Join(store.config.SettlementRoot, "daily-slots", date)
	entries, err := os.ReadDir(dayRoot)
	if errors.Is(err, os.ErrNotExist) { return SettlementQuota{}, nil, false, nil }
	if err != nil { return SettlementQuota{}, nil, false, err }
	if uint64(len(entries)) > store.config.MaximumSettlementsPerDay { return SettlementQuota{}, nil, false, errors.New("snapshot settlement daily quota exceeds its accepted cadence cap") }
	var foundQuota SettlementQuota
	var foundRaw []byte
	for _, entry := range entries {
		index, parseErr := settlementSlotIndex(entry)
		if parseErr != nil || index >= store.config.MaximumSettlementsPerDay { return SettlementQuota{}, nil, false, errors.New("snapshot settlement daily quota namespace is not canonical") }
		quota, raw, readErr := store.readQuota(filepath.Join(dayRoot, entry.Name()), date, index)
		if readErr != nil { return SettlementQuota{}, nil, false, readErr }
		if quota.CycleID == cycleID && (quota.CycleContractSHA256 != cycleContractSHA256 || quota.EvidenceBundleSHA256 != bundleSHA256) {
			return SettlementQuota{}, nil, false, errors.New("snapshot settlement cycle quota conflicts with different evidence")
		}
		if quota.EvidenceBundleSHA256 == bundleSHA256 {
			if quota.CycleID != cycleID || quota.CycleContractSHA256 != cycleContractSHA256 || foundRaw != nil { return SettlementQuota{}, nil, false, errors.New("snapshot settlement bundle quota is ambiguous") }
			foundQuota, foundRaw = quota, raw
		}
	}
	return foundQuota, foundRaw, foundRaw != nil, nil
}

func settlementSlotIndex(entry os.DirEntry) (uint64, error) {
	if entry.IsDir() || !strings.HasPrefix(entry.Name(), "slot-") || !strings.HasSuffix(entry.Name(), ".json") { return 0, errors.New("daily quota entry is not a slot") }
	indexText := strings.TrimSuffix(strings.TrimPrefix(entry.Name(), "slot-"), ".json")
	if len(indexText) != 12 { return 0, errors.New("daily quota slot width is invalid") }
	return strconv.ParseUint(indexText, 10, 64)
}

func (store *settlementStore) readQuota(path string, date string, slot uint64) (SettlementQuota, []byte, error) {
	envelopeRaw, err := store.readRegular(path, maximumSettlementControlBytes)
	if err != nil { return SettlementQuota{}, nil, err }
	var envelope SettlementEnvelope
	if _, err := boundary.CanonicalJSON(envelopeRaw, &envelope); err != nil || envelope.Schema != SettlementQuotaEnvelopeSchema || envelope.Algorithm != "ed25519" { return SettlementQuota{}, nil, errors.New("snapshot settlement quota envelope is invalid") }
	payloadRaw, err := base64.StdEncoding.Strict().DecodeString(envelope.PayloadBase64)
	if err != nil || base64.StdEncoding.EncodeToString(payloadRaw) != envelope.PayloadBase64 || digestBytes(payloadRaw) != envelope.PayloadSHA256 { return SettlementQuota{}, nil, errors.New("snapshot settlement quota payload is not content addressed") }
	signature, err := base64.RawURLEncoding.Strict().DecodeString(envelope.Signature)
	message := bytes.Join([][]byte{[]byte(SettlementQuotaEnvelopeSchema), []byte(envelope.Issuer), []byte(envelope.KeyID), []byte(envelope.PayloadSHA256), payloadRaw}, []byte("\n"))
	if err != nil || len(signature) != ed25519.SignatureSize || !store.verifyRetainedSignature(envelope.Issuer, envelope.KeyID, message, signature) { return SettlementQuota{}, nil, errors.New("snapshot settlement quota signature is invalid") }
	var quota SettlementQuota
	if _, err := boundary.CanonicalJSON(payloadRaw, &quota); err != nil || quota.Schema != SettlementQuotaSchema || quota.ClusterID != store.config.ClusterID || quota.DeploymentID != store.config.DeploymentID || quota.UTCDate != date || quota.Slot != slot || !isDigestText(quota.CycleID) || !isDigestText(quota.CycleContractSHA256) || !isDigestText(quota.EvidenceBundleSHA256) || !isDigestText(quota.CollectorChannelAttestationSHA256) || !validCollectorChannelRecord(quota.AuthenticatedCollectorChannel) { return SettlementQuota{}, nil, errors.New("snapshot settlement quota differs from its accepted slot") }
	reservedAt, parseErr := time.Parse(time.RFC3339, quota.ReservedAt)
	issuedAt, issueErr := time.Parse(time.RFC3339, quota.CycleIssuedAt)
	deadline, deadlineErr := time.Parse(time.RFC3339, quota.CycleDeadlineAt)
	if parseErr != nil || issueErr != nil || deadlineErr != nil || reservedAt.Nanosecond() != 0 || issuedAt.Nanosecond() != 0 || deadline.Nanosecond() != 0 || reservedAt.UTC().Format("2006-01-02") != date || reservedAt.Before(issuedAt) || !reservedAt.Before(deadline) { return SettlementQuota{}, nil, errors.New("snapshot settlement quota time is invalid") }
	return quota, envelopeRaw, nil
}

func (store *settlementStore) recover(bundleRaw []byte, channel settlementChannel) ([]byte, error) {
	var bundle collector.EvidenceBundle
	if _, err := boundary.CanonicalJSON(bundleRaw, &bundle); err != nil { return nil, err }
	release, err := store.acquireWriter()
	if err != nil { return nil, err }
	defer release()
	return store.settleLocked(bundle, bundleRaw, nil, channel, false)
}

func (store *settlementStore) recoverRetained() error {
	release, err := store.acquireWriter()
	if err != nil { return err }
	defer release()
	root := filepath.Join(store.config.SettlementRoot, "daily-slots")
	days, err := os.ReadDir(root)
	if err != nil { return err }
	if len(days) > store.config.SettlementOperatingHorizonDays+2 { return errors.New("snapshot settlement daily quota namespace exceeds its accepted operating horizon") }
	now := time.Now().UTC().Truncate(time.Second)
	for _, day := range days {
		if !day.IsDir() || len(day.Name()) != len("2006-01-02") { return errors.New("snapshot settlement daily quota root contains an unexpected entry") }
		parsedDay, parseErr := time.Parse("2006-01-02", day.Name())
		if parseErr != nil || parsedDay.UTC().Format("2006-01-02") != day.Name() { return errors.New("snapshot settlement daily quota directory is not a canonical UTC date") }
		entries, readErr := os.ReadDir(filepath.Join(root, day.Name()))
		if readErr != nil || uint64(len(entries)) > store.config.MaximumSettlementsPerDay { return errors.New("snapshot settlement daily quota directory exceeds its accepted cadence") }
		for _, entry := range entries {
			slot, slotErr := settlementSlotIndex(entry)
			if slotErr != nil || slot >= store.config.MaximumSettlementsPerDay { return errors.New("snapshot settlement retained quota slot is invalid") }
			quota, quotaRaw, quotaErr := store.readQuota(filepath.Join(root, day.Name(), entry.Name()), day.Name(), slot)
			if quotaErr != nil { return quotaErr }
			if reconcileErr := store.reconcileQuota(quota, quotaRaw, now); reconcileErr != nil { return reconcileErr }
		}
	}
	return nil
}

func (store *settlementStore) reconcileRecentRetained(now time.Time) error {
	release, err := store.acquireWriter()
	if err != nil { return err }
	defer release()
	root := filepath.Join(store.config.SettlementRoot, "daily-slots")
	dates := []string{now.UTC().Format("2006-01-02"), now.UTC().Add(-24*time.Hour).Format("2006-01-02")}
	for _, date := range dates {
		dayRoot := filepath.Join(root, date)
		entries, readErr := os.ReadDir(dayRoot)
		if errors.Is(readErr, os.ErrNotExist) { continue }
		if readErr != nil || uint64(len(entries)) > store.config.MaximumSettlementsPerDay { return errors.New("snapshot settlement recent quota directory exceeds its accepted cadence") }
		for _, entry := range entries {
			slot, slotErr := settlementSlotIndex(entry)
			if slotErr != nil || slot >= store.config.MaximumSettlementsPerDay { return errors.New("snapshot settlement recent quota slot is invalid") }
			quota, quotaRaw, quotaErr := store.readQuota(filepath.Join(dayRoot, entry.Name()), date, slot)
			if quotaErr != nil { return quotaErr }
			if reconcileErr := store.reconcileQuota(quota, quotaRaw, now.UTC().Truncate(time.Second)); reconcileErr != nil { return reconcileErr }
		}
	}
	return nil
}

func (store *settlementStore) setReconciliationHealth(err error) {
	store.healthMu.Lock()
	store.reconciliationErr = err
	store.healthMu.Unlock()
}

func (store *settlementStore) reconciliationHealth() error {
	store.healthMu.RLock()
	defer store.healthMu.RUnlock()
	return store.reconciliationErr
}

func (store *settlementStore) reconcileQuota(quota SettlementQuota, quotaRaw []byte, now time.Time) error {
	bundle := collector.EvidenceBundle{CycleID: quota.CycleID, CycleContractSHA256: quota.CycleContractSHA256, CycleIssuedAt: quota.CycleIssuedAt, CycleDeadlineAt: quota.CycleDeadlineAt}
	deadline, err := time.Parse(time.RFC3339, quota.CycleDeadlineAt)
	if err != nil { return err }
	preparedPath := filepath.Join(store.config.SettlementRoot, "prepared", "cycle-"+quota.CycleID+".json")
	preparedRaw, preparedErr := store.readRegular(preparedPath, maximumSettlementBytes)
	if errors.Is(preparedErr, os.ErrNotExist) {
		if existing, outcomeErr := store.readOutcome(bundle, quota.EvidenceBundleSHA256, nil, quotaRaw); outcomeErr == nil {
			if existing.Status != "refused-deadline" { return errors.New("quota-only settlement has a non-refusal outcome") }
			return nil
		} else if !errors.Is(outcomeErr, os.ErrNotExist) {
			return outcomeErr
		}
		if !now.Before(deadline) { _, err = store.settleOutcome(bundle, quota.EvidenceBundleSHA256, nil, quotaRaw, false, quota.AuthenticatedCollectorChannel); return err }
		return nil
	}
	if preparedErr != nil { return preparedErr }
	_, snapshotRaw, err := store.decodeSettlement(preparedRaw, quota, quotaRaw)
	if err != nil { return err }
	reservationRoot := filepath.Join(store.config.SettlementRoot, "reservations", quota.EvidenceBundleSHA256[:2])
	if err := store.ensureDirectory(reservationRoot, 0o700); err != nil { return err }
	reservationPath := filepath.Join(reservationRoot, "bundle-"+quota.EvidenceBundleSHA256+".json")
	if err := os.Link(preparedPath, reservationPath); err != nil {
		if !errors.Is(err, os.ErrExist) { return err }
		retainedReservation, readErr := store.readRegular(reservationPath, maximumSettlementBytes)
		if readErr != nil || !bytes.Equal(retainedReservation, preparedRaw) { return errors.New("startup settlement reservation conflicts with its exact prepared record") }
		_, retainedSnapshot, validateErr := store.decodeSettlement(retainedReservation, quota, quotaRaw)
		if validateErr != nil || !bytes.Equal(retainedSnapshot, snapshotRaw) { return errors.New("startup settlement reservation is not its exact signed prepared record") }
	}
	if err := fsyncSettlementDirectory(reservationRoot); err != nil { return err }
	if existing, outcomeErr := store.readOutcome(bundle, quota.EvidenceBundleSHA256, preparedRaw, quotaRaw); outcomeErr == nil {
		if existing.Status == "acknowledged" && !existing.FinalSettlementPresent { return errors.New("acknowledged recovered settlement lacks its final record") }
		return nil
	} else if !errors.Is(outcomeErr, os.ErrNotExist) {
		return outcomeErr
	}
	finalRoot := filepath.Join(store.config.SettlementRoot, "by-bundle", quota.EvidenceBundleSHA256[:2])
	finalPath := filepath.Join(finalRoot, "bundle-"+quota.EvidenceBundleSHA256+".json")
	if finalRaw, finalErr := store.readRegular(finalPath, maximumSettlementBytes); finalErr == nil {
		_, retainedSnapshot, validationErr := store.decodeSettlement(finalRaw, quota, quotaRaw)
		if validationErr != nil || !bytes.Equal(retainedSnapshot, snapshotRaw) || !bytes.Equal(finalRaw, preparedRaw) { return errors.New("startup settlement recovery found a conflicting final record") }
		if syncErr := fsyncSettlementDirectory(finalRoot); syncErr != nil { return syncErr }
		_, outcomeErr := store.settleOutcome(bundle, quota.EvidenceBundleSHA256, finalRaw, quotaRaw, true, quota.AuthenticatedCollectorChannel)
		return outcomeErr
	} else if !errors.Is(finalErr, os.ErrNotExist) {
		return finalErr
	}
	if !now.Before(deadline) { _, err = store.settleOutcome(bundle, quota.EvidenceBundleSHA256, preparedRaw, quotaRaw, false, quota.AuthenticatedCollectorChannel); return err }
	if err := store.ensureDirectory(finalRoot, 0o700); err != nil { return err }
	preFinal := time.Now().UTC().Truncate(time.Second)
	if !preFinal.Before(deadline) { _, err = store.settleOutcome(bundle, quota.EvidenceBundleSHA256, preparedRaw, quotaRaw, false, quota.AuthenticatedCollectorChannel); return err }
	if err := os.Link(reservationPath, finalPath); err != nil && !errors.Is(err, os.ErrExist) { return err }
	if err := fsyncSettlementDirectory(finalRoot); err != nil { return err }
	_, err = store.settleOutcome(bundle, quota.EvidenceBundleSHA256, preparedRaw, quotaRaw, true, quota.AuthenticatedCollectorChannel)
	return err
}

func (store *settlementStore) lookup(bundleSHA256 string, bundleRaw []byte, _ settlementChannel) ([]byte, error) {
	if !isDigestText(bundleSHA256) { return nil, errors.New("snapshot settlement bundle digest is invalid") }
	path := filepath.Join(store.config.SettlementRoot, "by-bundle", bundleSHA256[:2], "bundle-"+bundleSHA256+".json")
	var bundle collector.EvidenceBundle
	if _, err := boundary.CanonicalJSON(bundleRaw, &bundle); err != nil { return nil, err }
	quota, quotaRaw, err := store.findQuotaForBundle(bundle, bundleSHA256)
	if err != nil { return nil, err }
	raw, err := store.readRegular(path, maximumSettlementBytes)
	if err != nil { return nil, err }
	snapshotRaw, err := store.validateSettlement(raw, bundleSHA256, bundleRaw, quota, quotaRaw)
	if err != nil { return nil, err }
	outcome, err := store.readOutcome(bundle, bundleSHA256, raw, quotaRaw)
	if err != nil { return nil, err }
	if outcome.Status != "acknowledged" { return nil, errors.New("snapshot settlement has a durable refused outcome") }
	return snapshotRaw, nil
}

func (store *settlementStore) findQuotaForBundle(bundle collector.EvidenceBundle, bundleSHA256 string) (SettlementQuota, []byte, error) {
	issuedAt, issueErr := time.Parse(time.RFC3339, bundle.CycleIssuedAt)
	deadline, deadlineErr := time.Parse(time.RFC3339, bundle.CycleDeadlineAt)
	if issueErr != nil || deadlineErr != nil || !issuedAt.Before(deadline) { return SettlementQuota{}, nil, errors.New("snapshot settlement bundle cycle window is invalid") }
	dates := []string{issuedAt.UTC().Format("2006-01-02")}
	deadlineDate := deadline.Add(-time.Nanosecond).UTC().Format("2006-01-02")
	if deadlineDate != dates[0] { dates = append(dates, deadlineDate) }
	var foundQuota SettlementQuota
	var foundRaw []byte
	for _, date := range dates {
		quota, raw, found, err := store.findDailyQuota(date, bundle.CycleID, bundle.CycleContractSHA256, bundleSHA256)
		if err != nil { return SettlementQuota{}, nil, err }
		if found {
			if foundRaw != nil { return SettlementQuota{}, nil, errors.New("snapshot settlement bundle has duplicate quota reservations") }
			foundQuota, foundRaw = quota, raw
		}
	}
	if foundRaw == nil { return SettlementQuota{}, nil, os.ErrNotExist }
	return foundQuota, foundRaw, nil
}

func (store *settlementStore) settleOutcome(bundle collector.EvidenceBundle, bundleSHA256 string, settlementEnvelopeRaw []byte, quotaEnvelopeRaw []byte, finalSettlementPresent bool, channel string) (SettlementOutcome, error) {
	if existing, err := store.readOutcome(bundle, bundleSHA256, settlementEnvelopeRaw, quotaEnvelopeRaw); err == nil { return existing, nil } else if !errors.Is(err, os.ErrNotExist) { return SettlementOutcome{}, err }
	completedAt := time.Now().UTC().Truncate(time.Second)
	deadline, err := time.Parse(time.RFC3339, bundle.CycleDeadlineAt)
	if err != nil { return SettlementOutcome{}, err }
	status := "refused-deadline"
	if finalSettlementPresent && completedAt.Before(deadline) { status = "acknowledged" }
	outcome := SettlementOutcome{
		Schema: SettlementOutcomeSchema, ClusterID: store.config.ClusterID, DeploymentID: store.config.DeploymentID,
		CycleID: bundle.CycleID, CycleContractSHA256: bundle.CycleContractSHA256, EvidenceBundleSHA256: bundleSHA256,
		SettlementEnvelopeSHA256: digestBytes(settlementEnvelopeRaw), QuotaEnvelopeSHA256: digestBytes(quotaEnvelopeRaw),
		FinalSettlementPresent: finalSettlementPresent, AuthenticatedCollectorChannel: channel,
		Status: status, CompletedAt: completedAt.Format(time.RFC3339),
	}
	payloadRaw, err := json.Marshal(outcome)
	if err != nil { return SettlementOutcome{}, err }
	envelopeRaw, err := store.signOutcome(payloadRaw)
	if err != nil || len(envelopeRaw) > maximumSettlementControlBytes { return SettlementOutcome{}, errors.New("snapshot settlement outcome exceeds its exact signed bound") }
	root := filepath.Join(store.config.SettlementRoot, "outcomes", bundleSHA256[:2])
	if err := store.ensureDirectory(root, 0o700); err != nil { return SettlementOutcome{}, err }
	path := filepath.Join(root, "bundle-"+bundleSHA256+".json")
	if err := store.publishRegular(path, envelopeRaw, 0o400); err != nil { return SettlementOutcome{}, err }
	return store.readOutcome(bundle, bundleSHA256, settlementEnvelopeRaw, quotaEnvelopeRaw)
}

func (store *settlementStore) readOutcome(bundle collector.EvidenceBundle, bundleSHA256 string, settlementEnvelopeRaw []byte, quotaEnvelopeRaw []byte) (SettlementOutcome, error) {
	path := filepath.Join(store.config.SettlementRoot, "outcomes", bundleSHA256[:2], "bundle-"+bundleSHA256+".json")
	envelopeRaw, err := store.readRegular(path, maximumSettlementControlBytes)
	if err != nil { return SettlementOutcome{}, err }
	var envelope SettlementEnvelope
	if _, err := boundary.CanonicalJSON(envelopeRaw, &envelope); err != nil || envelope.Schema != SettlementOutcomeEnvelopeSchema || envelope.Algorithm != "ed25519" { return SettlementOutcome{}, errors.New("snapshot settlement outcome envelope is invalid") }
	payloadRaw, err := base64.StdEncoding.Strict().DecodeString(envelope.PayloadBase64)
	if err != nil || base64.StdEncoding.EncodeToString(payloadRaw) != envelope.PayloadBase64 || digestBytes(payloadRaw) != envelope.PayloadSHA256 { return SettlementOutcome{}, errors.New("snapshot settlement outcome payload is not content addressed") }
	signature, err := base64.RawURLEncoding.Strict().DecodeString(envelope.Signature)
	message := bytes.Join([][]byte{[]byte(SettlementOutcomeEnvelopeSchema), []byte(envelope.Issuer), []byte(envelope.KeyID), []byte(envelope.PayloadSHA256), payloadRaw}, []byte("\n"))
	if err != nil || len(signature) != ed25519.SignatureSize || !store.verifyRetainedSignature(envelope.Issuer, envelope.KeyID, message, signature) { return SettlementOutcome{}, errors.New("snapshot settlement outcome signature is invalid") }
	var outcome SettlementOutcome
	if _, err := boundary.CanonicalJSON(payloadRaw, &outcome); err != nil || outcome.Schema != SettlementOutcomeSchema || outcome.ClusterID != store.config.ClusterID || outcome.DeploymentID != store.config.DeploymentID || outcome.CycleID != bundle.CycleID || outcome.CycleContractSHA256 != bundle.CycleContractSHA256 || outcome.EvidenceBundleSHA256 != bundleSHA256 || outcome.SettlementEnvelopeSHA256 != digestBytes(settlementEnvelopeRaw) || outcome.QuotaEnvelopeSHA256 != digestBytes(quotaEnvelopeRaw) || !validCollectorChannelRecord(outcome.AuthenticatedCollectorChannel) || (outcome.Status != "acknowledged" && outcome.Status != "refused-deadline") { return SettlementOutcome{}, errors.New("snapshot settlement outcome differs from the exact durable commit") }
	completedAt, completionErr := time.Parse(time.RFC3339, outcome.CompletedAt)
	deadline, deadlineErr := time.Parse(time.RFC3339, bundle.CycleDeadlineAt)
	if completionErr != nil || deadlineErr != nil || completedAt.Nanosecond() != 0 || outcome.Status == "acknowledged" && (!outcome.FinalSettlementPresent || !completedAt.Before(deadline)) || outcome.Status == "refused-deadline" && completedAt.Before(deadline) { return SettlementOutcome{}, errors.New("snapshot settlement outcome time and status disagree") }
	finalPath := filepath.Join(store.config.SettlementRoot, "by-bundle", bundleSHA256[:2], "bundle-"+bundleSHA256+".json")
	finalRaw, finalErr := store.readRegular(finalPath, maximumSettlementBytes)
	if outcome.FinalSettlementPresent {
		if finalErr != nil || !bytes.Equal(finalRaw, settlementEnvelopeRaw) { return SettlementOutcome{}, errors.New("snapshot settlement outcome lacks its exact durable final record") }
	} else if finalErr == nil || !errors.Is(finalErr, os.ErrNotExist) {
		return SettlementOutcome{}, errors.New("refused snapshot outcome conflicts with an unexpected final record")
	}
	return outcome, nil
}

func (store *settlementStore) validateSettlement(envelopeRaw []byte, bundleSHA256 string, bundleRaw []byte, quota SettlementQuota, quotaEnvelopeRaw []byte) ([]byte, error) {
	settlement, snapshotRaw, err := store.decodeSettlement(envelopeRaw, quota, quotaEnvelopeRaw)
	if err != nil { return nil, err }
	var bundle collector.EvidenceBundle
	if _, err := boundary.CanonicalJSON(bundleRaw, &bundle); err != nil || digestBytes(bundleRaw) != bundleSHA256 || settlement.EvidenceBundleSHA256 != bundleSHA256 || settlement.CycleID != bundle.CycleID || settlement.CycleContractSHA256 != bundle.CycleContractSHA256 || settlement.CycleIssuedAt != bundle.CycleIssuedAt || settlement.CycleDeadlineAt != bundle.CycleDeadlineAt { return nil, errors.New("retained settlement differs from the exact evidence bundle and cycle") }
	return snapshotRaw, nil
}

func (store *settlementStore) decodeSettlement(envelopeRaw []byte, quota SettlementQuota, quotaEnvelopeRaw []byte) (Settlement, []byte, error) {
	var envelope SettlementEnvelope
	if _, err := boundary.CanonicalJSON(envelopeRaw, &envelope); err != nil || envelope.Schema != SettlementEnvelopeSchema || envelope.Algorithm != "ed25519" { return Settlement{}, nil, errors.New("retained snapshot settlement envelope is invalid") }
	payloadRaw, err := base64.StdEncoding.Strict().DecodeString(envelope.PayloadBase64)
	if err != nil || base64.StdEncoding.EncodeToString(payloadRaw) != envelope.PayloadBase64 || digestBytes(payloadRaw) != envelope.PayloadSHA256 { return Settlement{}, nil, errors.New("retained snapshot settlement payload is not content addressed") }
	signature, err := base64.RawURLEncoding.Strict().DecodeString(envelope.Signature)
	message := bytes.Join([][]byte{[]byte(SettlementEnvelopeSchema), []byte(envelope.Issuer), []byte(envelope.KeyID), []byte(envelope.PayloadSHA256), payloadRaw}, []byte("\n"))
	if err != nil || len(signature) != ed25519.SignatureSize || !store.verifyRetainedSignature(envelope.Issuer, envelope.KeyID, message, signature) { return Settlement{}, nil, errors.New("retained snapshot settlement signature is invalid") }
	var settlement Settlement
	if _, err := boundary.CanonicalJSON(payloadRaw, &settlement); err != nil || settlement.Schema != SettlementSchema || settlement.ClusterID != store.config.ClusterID || settlement.DeploymentID != store.config.DeploymentID || settlement.EvidenceBundleSHA256 != quota.EvidenceBundleSHA256 || settlement.Status != "prepared" || settlement.CollectorCredentialLaneID != store.configCollectorLane() || settlement.AuthenticatedCollectorChannel != quota.AuthenticatedCollectorChannel || settlement.QuotaEnvelopeSHA256 != digestBytes(quotaEnvelopeRaw) || settlement.QuotaUTCDate != quota.UTCDate || settlement.QuotaSlot != quota.Slot || settlement.CycleID != quota.CycleID || settlement.CycleContractSHA256 != quota.CycleContractSHA256 || settlement.CycleIssuedAt != quota.CycleIssuedAt || settlement.CycleDeadlineAt != quota.CycleDeadlineAt { return Settlement{}, nil, errors.New("retained snapshot settlement differs from the exact quota or authenticated collector") }
	preparedAt, preparedErr := time.Parse(time.RFC3339, settlement.PreparedAt)
	reservedAt, reservedErr := time.Parse(time.RFC3339, quota.ReservedAt)
	deadline, deadlineErr := time.Parse(time.RFC3339, quota.CycleDeadlineAt)
	if preparedErr != nil || reservedErr != nil || deadlineErr != nil || preparedAt.Nanosecond() != 0 || reservedAt.Nanosecond() != 0 || deadline.Nanosecond() != 0 || preparedAt.Before(reservedAt) || !preparedAt.Before(deadline) {
		return Settlement{}, nil, errors.New("retained settlement was not prepared under its durable quota inside the cycle deadline")
	}
	snapshotRaw, err := base64.StdEncoding.Strict().DecodeString(settlement.SnapshotEnvelopeBase64)
	if err != nil || base64.StdEncoding.EncodeToString(snapshotRaw) != settlement.SnapshotEnvelopeBase64 || digestBytes(snapshotRaw) != settlement.SnapshotEnvelopeSHA256 { return Settlement{}, nil, errors.New("retained settlement snapshot bytes are invalid") }
	return settlement, snapshotRaw, nil
}

func (store *settlementStore) signSettlement(payloadRaw []byte) ([]byte, error) {
	payloadSHA256 := digestBytes(payloadRaw)
	message := bytes.Join([][]byte{[]byte(SettlementEnvelopeSchema), []byte(store.config.SettlementSigningIssuer), []byte(store.config.SettlementSigningKeyID), []byte(payloadSHA256), payloadRaw}, []byte("\n"))
	envelope := SettlementEnvelope{Schema: SettlementEnvelopeSchema, Algorithm: "ed25519", Issuer: store.config.SettlementSigningIssuer, KeyID: store.config.SettlementSigningKeyID, PayloadSHA256: payloadSHA256, PayloadBase64: base64.StdEncoding.EncodeToString(payloadRaw), Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(store.privateKey, message))}
	return json.Marshal(envelope)
}

func (store *settlementStore) signOutcome(payloadRaw []byte) ([]byte, error) {
	payloadSHA256 := digestBytes(payloadRaw)
	message := bytes.Join([][]byte{[]byte(SettlementOutcomeEnvelopeSchema), []byte(store.config.SettlementSigningIssuer), []byte(store.config.SettlementSigningKeyID), []byte(payloadSHA256), payloadRaw}, []byte("\n"))
	envelope := SettlementEnvelope{Schema: SettlementOutcomeEnvelopeSchema, Algorithm: "ed25519", Issuer: store.config.SettlementSigningIssuer, KeyID: store.config.SettlementSigningKeyID, PayloadSHA256: payloadSHA256, PayloadBase64: base64.StdEncoding.EncodeToString(payloadRaw), Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(store.privateKey, message))}
	return json.Marshal(envelope)
}

func (store *settlementStore) signQuota(payloadRaw []byte) ([]byte, error) {
	payloadSHA256 := digestBytes(payloadRaw)
	message := bytes.Join([][]byte{[]byte(SettlementQuotaEnvelopeSchema), []byte(store.config.SettlementSigningIssuer), []byte(store.config.SettlementSigningKeyID), []byte(payloadSHA256), payloadRaw}, []byte("\n"))
	envelope := SettlementEnvelope{Schema: SettlementQuotaEnvelopeSchema, Algorithm: "ed25519", Issuer: store.config.SettlementSigningIssuer, KeyID: store.config.SettlementSigningKeyID, PayloadSHA256: payloadSHA256, PayloadBase64: base64.StdEncoding.EncodeToString(payloadRaw), Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(store.privateKey, message))}
	return json.Marshal(envelope)
}

func (store *settlementStore) verifyRetainedSignature(issuer string, keyID string, message []byte, signature []byte) bool {
	key, exists := store.verificationKeys[issuer+"\x00"+keyID]
	return exists && ed25519.Verify(key, message, signature)
}

func (store *settlementStore) configCollectorLane() string { return store.config.CollectorCredentialLaneID }

func (store *settlementStore) acceptedCollectorChannel(channel string) bool {
	for _, identity := range store.config.CollectorIdentities {
		if channel == identity.Slot+":"+identity.SPKISHA256 { return true }
	}
	return false
}

func validCollectorChannelRecord(channel string) bool {
	parts := strings.Split(channel, ":")
	return len(parts) == 2 && parts[0] != "" && len(parts[0]) <= 64 && !strings.ContainsAny(parts[0], "\x00\r\n") && isDigestText(parts[1])
}

func (store *settlementStore) ready() error {
	if err := store.reconciliationHealth(); err != nil { return err }
	if err := requireSettlementRoot(store.config); err != nil { return err }
	var statistics syscall.Statfs_t
	if err := syscall.Statfs(store.config.SettlementRoot, &statistics); err != nil { return err }
	blockSize := uint64(statistics.Bsize)
	if blockSize == 0 || uint64(statistics.Bavail) > ^uint64(0)/blockSize { return errors.New("snapshot settlement free-space accounting overflows") }
	availableBytes := uint64(statistics.Bavail) * blockSize
	if availableBytes < store.config.SettlementMinimumFreeBytes || uint64(statistics.Ffree) < store.config.SettlementMinimumFreeInodes {
		return errors.New("snapshot settlement volume is below its accepted byte or inode reserve")
	}
	return nil
}

func (store *settlementStore) ensureDirectory(path string, mode os.FileMode) error {
	parent := filepath.Dir(path)
	if path != store.config.SettlementRoot {
		if info, err := os.Lstat(parent); err != nil || info == nil || !info.IsDir() { return errors.New("snapshot settlement parent directory is absent") }
	}
	if _, err := os.Lstat(path); errors.Is(err, os.ErrNotExist) {
		if capacityErr := store.ensurePublicationCapacity(4096, 1); capacityErr != nil { return capacityErr }
	} else if err != nil {
		return err
	}
	if err := os.Mkdir(path, mode); err != nil && !errors.Is(err, os.ErrExist) { return err }
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.IsDir() || info.Mode().Perm() != mode.Perm() { return errors.New("snapshot settlement directory is not exact") }
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != store.config.SettlementRuntimeUID || uint64(stat.Dev) != store.config.SettlementDeviceID { return errors.New("snapshot settlement directory lacks accepted UID/device custody") }
	return fsyncSettlementDirectory(parent)
}

func (store *settlementStore) readRegular(path string, maximum int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil { return nil, err }
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != store.config.SettlementRuntimeUID || uint64(stat.Dev) != store.config.SettlementDeviceID || !info.Mode().IsRegular() || info.Mode().Perm() != 0o400 || info.Size() < 1 || info.Size() > maximum { return nil, errors.New("snapshot settlement record lacks exact immutable custody") }
	raw, err := os.ReadFile(path)
	if err != nil || int64(len(raw)) != info.Size() { return nil, errors.New("snapshot settlement record cannot be reopened exactly") }
	after, err := os.Lstat(path)
	if err != nil || !os.SameFile(info, after) { return nil, errors.New("snapshot settlement record changed during read") }
	return raw, nil
}

func (store *settlementStore) publishRegular(path string, raw []byte, mode os.FileMode) error {
	if existing, err := store.readRegular(path, int64(len(raw))); err == nil { if bytes.Equal(existing, raw) { return fsyncSettlementDirectory(filepath.Dir(path)) }; return errors.New("snapshot settlement publication conflicts") } else if !errors.Is(err, os.ErrNotExist) { return err }
	if len(raw) == 0 || uint64(len(raw)) > ^uint64(0)-4096 { return errors.New("snapshot settlement publication size is invalid") }
	if err := store.ensurePublicationCapacity(uint64(len(raw))+4096, 1); err != nil { return err }
	parent := filepath.Dir(path)
	fd, err := syscall.Open(parent, syscall.O_RDWR|syscall.O_CLOEXEC|settlementLinuxOTmpfile, uint32(mode.Perm()))
	if err != nil { return err }
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	if err := syscall.Fchmod(fd, uint32(mode.Perm())); err != nil { return err }
	if _, err := io.Copy(file, bytes.NewReader(raw)); err != nil { return err }
	if err := file.Sync(); err != nil { return err }
	procPath := "/proc/self/fd/"+strconv.Itoa(fd)
	procInfo, procErr := os.Stat(procPath)
	fileInfo, fileErr := file.Stat()
	if procErr != nil || fileErr != nil || !os.SameFile(procInfo, fileInfo) { return errors.New("anonymous settlement inode identity cannot be proven") }
	oldPath, _ := syscall.BytePtrFromString(procPath)
	newPath, _ := syscall.BytePtrFromString(path)
	directory := -100
	_, _, errno := syscall.RawSyscall6(syscall.SYS_LINKAT, uintptr(directory), uintptr(unsafe.Pointer(oldPath)), uintptr(directory), uintptr(unsafe.Pointer(newPath)), uintptr(settlementLinuxATSymlinkFollow), 0)
	if errno != 0 && errno != syscall.EEXIST { return errno }
	if errno == syscall.EEXIST { existing, err := store.readRegular(path, int64(len(raw))); if err != nil || !bytes.Equal(existing, raw) { return errors.New("snapshot settlement no-replace race conflicts") } }
	return fsyncSettlementDirectory(parent)
}

func fsyncSettlementDirectory(path string) error { file, err := os.Open(path); if err != nil { return err }; defer file.Close(); return file.Sync() }

func checkedMultiply(left uint64, right uint64) (uint64, bool) { if left != 0 && right > ^uint64(0)/left { return 0, false }; return left*right, true }
