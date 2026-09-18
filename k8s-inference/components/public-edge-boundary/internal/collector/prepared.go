package collector

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	linuxOTmpfile        = 0x410000
	linuxATSymlinkFollow = 0x400
	linuxATFDCWD         = -100
	linuxRenameNoReplace = 1
	authorityPreparedSchema = "fs2-serve.nebius.ai/public-edge-authority-prepared-object/v1"
	maximumPreparedSlotBytes = 192 * 1024 * 1024
)

type authorityPreparedIdentity struct {
	kind        string
	sessionID   string
	challenge   string
	pageIndex   int
	sha256      string
	objectBytes uint64
}

type authorityPreparedSlot struct {
	Schema       string `json:"schema"`
	ClusterID    string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	SegmentID    string `json:"segment_id"`
	Kind         string `json:"kind"`
	SessionID    string `json:"session_id"`
	Challenge    string `json:"challenge"`
	PageIndex    int    `json:"page_index"`
	ObjectSHA256 string `json:"object_sha256"`
	ObjectBytes  uint64 `json:"object_bytes"`
	ObjectBase64 string `json:"object_base64"`
}

func (j *authorityLedgerJournal) preparedRoot(segmentID string) string {
	return filepath.Join(j.segmentRoot(segmentID), "prepared")
}

func (j *authorityLedgerJournal) pendingPreparedPath(segmentID string) string {
	return filepath.Join(j.preparedRoot(segmentID), "pending-slot.json")
}

func (j *authorityLedgerJournal) preparedArchiveRoot(segmentID string) string {
	return filepath.Join(j.preparedRoot(segmentID), "archive")
}

func preparedObjectName(identity authorityPreparedIdentity) (string, error) {
	if !ledgerObjectKind(identity.kind) || !canonicalNonce(identity.sessionID) || !canonicalNonce(identity.challenge) ||
		identity.pageIndex < 0 || identity.pageIndex >= maximumAuthoritySessionPages || !isDigest(identity.sha256) || identity.objectBytes == 0 {
		return "", errors.New("authority prepared-object identity is invalid")
	}
	name := fmt.Sprintf("%s~%s~%s~%04d~%s~%d.raw", identity.challenge, identity.kind, identity.sessionID, identity.pageIndex, identity.sha256, identity.objectBytes)
	if len(name) > 255 {
		return "", errors.New("authority prepared-object filename exceeds its fixed bound")
	}
	return name, nil
}

func parsePreparedObjectName(name string) (authorityPreparedIdentity, error) {
	identity := authorityPreparedIdentity{}
	if !strings.HasSuffix(name, ".raw") {
		return identity, errors.New("authority prepared-object filename has an invalid suffix")
	}
	parts := strings.Split(strings.TrimSuffix(name, ".raw"), "~")
	if len(parts) != 6 {
		return identity, errors.New("authority prepared-object filename has an invalid field count")
	}
	pageIndex, err := strconv.Atoi(parts[3])
	if err != nil || parts[3] != fmt.Sprintf("%04d", pageIndex) {
		return identity, errors.New("authority prepared-object page index is not canonical")
	}
	objectBytes, err := strconv.ParseUint(parts[5], 10, 64)
	if err != nil || parts[5] != strconv.FormatUint(objectBytes, 10) {
		return identity, errors.New("authority prepared-object size is not canonical")
	}
	identity = authorityPreparedIdentity{
		challenge: parts[0], kind: parts[1], sessionID: parts[2], pageIndex: pageIndex,
		sha256: parts[4], objectBytes: objectBytes,
	}
	canonical, err := preparedObjectName(identity)
	if err != nil || canonical != name {
		return authorityPreparedIdentity{}, errors.New("authority prepared-object filename is not canonical")
	}
	return identity, nil
}

func (j *authorityLedgerJournal) preparedSlotBytes(identity authorityPreparedIdentity, raw []byte) ([]byte, error) {
	_, err := preparedObjectName(identity)
	if err != nil || uint64(len(raw)) != identity.objectBytes || digest(raw) != identity.sha256 {
		return nil, errors.New("authority prepared-object bytes differ from their exact identity")
	}
	slot := authorityPreparedSlot{
		Schema: authorityPreparedSchema, ClusterID: j.config.ClusterID, DeploymentID: j.config.DeploymentID,
		SegmentID: j.segmentID, Kind: identity.kind, SessionID: identity.sessionID, Challenge: identity.challenge,
		PageIndex: identity.pageIndex, ObjectSHA256: identity.sha256, ObjectBytes: identity.objectBytes,
		ObjectBase64: base64.StdEncoding.EncodeToString(raw),
	}
	encoded, err := json.Marshal(slot)
	if err != nil || len(encoded) > maximumPreparedSlotBytes {
		return nil, errors.New("authority prepared-object slot exceeds its exact encoded bound")
	}
	return encoded, nil
}

func (j *authorityLedgerJournal) stagePreparedObject(identity authorityPreparedIdentity, raw []byte) (string, uint64, bool, error) {
	slotRaw, err := j.preparedSlotBytes(identity, raw)
	if err != nil {
		return "", 0, false, err
	}
	path := j.pendingPreparedPath(j.segmentID)
	if existing, readErr := readRootRegular(path, maximumPreparedSlotBytes); readErr == nil {
		if !bytes.Equal(existing, slotRaw) {
			return "", 0, false, errors.New("authority ledger already has a different durable prepared object")
		}
		return path, uint64(len(slotRaw)), false, nil
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return "", 0, false, readErr
	}
	if err := publishAnonymousRegular(path, slotRaw, 0o400); err != nil {
		return "", 0, false, err
	}
	return path, uint64(len(slotRaw)), true, nil
}

func publishAnonymousRegular(path string, raw []byte, mode os.FileMode) error {
	parent := filepath.Dir(path)
	if err := requireRootOwnedDirectory(parent); err != nil {
		return err
	}
	if existing, err := readRootRegular(path, int64(len(raw))); err == nil {
		if !bytes.Equal(existing, raw) {
			return errors.New("anonymous no-replace publication conflicts with different retained bytes")
		}
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	fd, err := syscall.Open(parent, syscall.O_RDWR|syscall.O_CLOEXEC|linuxOTmpfile, uint32(mode.Perm()))
	if err != nil {
		return err
	}
	if err := syscall.Fchmod(fd, uint32(mode.Perm())); err != nil {
		_ = syscall.Close(fd)
		return err
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	info, err := file.Stat()
	var stat *syscall.Stat_t
	statOK := false
	if info != nil {
		stat, statOK = info.Sys().(*syscall.Stat_t)
	}
	if err != nil || info == nil || !statOK || stat.Uid != 0 || !info.Mode().IsRegular() || info.Mode().Perm() != mode.Perm() || info.Size() != 0 {
		return errors.New("anonymous publication inode has an invalid initial extent")
	}
	written := 0
	for written < len(raw) {
		count, writeErr := file.Write(raw[written:])
		if writeErr != nil || count < 1 {
			if writeErr != nil {
				return writeErr
			}
			return errors.New("anonymous publication made no write progress")
		}
		written += count
	}
	if err := file.Sync(); err != nil {
		return err
	}
	if err := linkAnonymousFile(fd, path); err != nil {
		if !errors.Is(err, syscall.EEXIST) {
			return err
		}
		existing, readErr := readRootRegular(path, int64(len(raw)))
		if readErr != nil || !bytes.Equal(existing, raw) {
			return errors.New("anonymous no-replace publication raced with different bytes")
		}
	}
	return fsyncDirectory(parent)
}

func linkAnonymousFile(descriptor int, path string) error {
	procPath := fmt.Sprintf("/proc/self/fd/%d", descriptor)
	var descriptorStat syscall.Stat_t
	if err := syscall.Fstat(descriptor, &descriptorStat); err != nil {
		return err
	}
	procInfo, err := os.Stat(procPath)
	var procStat *syscall.Stat_t
	procStatOK := false
	if procInfo != nil {
		procStat, procStatOK = procInfo.Sys().(*syscall.Stat_t)
	}
	if err != nil || procInfo == nil || !procStatOK || procStat.Dev != descriptorStat.Dev || procStat.Ino != descriptorStat.Ino {
		return errors.New("anonymous prepared-object proc descriptor does not identify its exact inode")
	}
	oldPath, err := syscall.BytePtrFromString(procPath)
	if err != nil {
		return err
	}
	target, err := syscall.BytePtrFromString(path)
	if err != nil {
		return err
	}
	targetDirectory := linuxATFDCWD
	_, _, errno := syscall.RawSyscall6(
		syscall.SYS_LINKAT,
		uintptr(targetDirectory),
		uintptr(unsafe.Pointer(oldPath)),
		uintptr(targetDirectory),
		uintptr(unsafe.Pointer(target)),
		uintptr(linuxATSymlinkFollow),
		0,
	)
	if errno != 0 {
		return errno
	}
	return nil
}

func verifyAnonymousPublicationSupport(root string) error {
	probe := []byte("fs2-public-edge-authority-anonymous-publication/v1\n")
	path := filepath.Join(root, "anonymous-publication-probe-v1")
	if existing, err := readRootRegular(path, int64(len(probe))); err == nil {
		if !bytes.Equal(existing, probe) {
			return errors.New("authority anonymous-publication probe conflicts with retained bytes")
		}
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	fd, err := syscall.Open(root, syscall.O_RDWR|syscall.O_CLOEXEC|linuxOTmpfile, 0o400)
	if err != nil {
		return fmt.Errorf("authority ledger filesystem does not support anonymous publication: %w", err)
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	if err := syscall.Fchmod(fd, 0o400); err != nil {
		return err
	}
	if _, err := file.Write(probe); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return err
	}
	if err := linkAnonymousFile(fd, path); err != nil {
		return fmt.Errorf("authority ledger filesystem cannot publish anonymous inodes without extra capabilities: %w", err)
	}
	return fsyncDirectory(root)
}

func (j *authorityLedgerJournal) expectedObjectPath(identity authorityPreparedIdentity) (string, error) {
	switch identity.kind {
	case "reservation":
		return reservationLedgerPath(j.config, identity.challenge), nil
	case "challenge":
		return challengeLedgerPath(j.config, identity.challenge), nil
	case "settlement":
		return filepath.Join(sessionLedgerRoot(j.config, identity.sessionID), fmt.Sprintf("page-%08d.json", identity.pageIndex)), nil
	case "session-head":
		return sessionHeadPath(j.config, identity.sessionID, identity.pageIndex+1), nil
	case "collection-outcome":
		return collectionOutcomePath(j.config, identity.challenge), nil
	default:
		return "", errors.New("authority prepared-object kind has no destination")
	}
}

func (j *authorityLedgerJournal) prepareObjectDestination(identity authorityPreparedIdentity) error {
	switch identity.kind {
	case "reservation":
		return prepareAuthorityShard(j.config, "reservations", identity.challenge)
	case "challenge":
		return prepareAuthorityShard(j.config, "challenges", identity.challenge)
	case "settlement":
		if err := prepareAuthorityShard(j.config, "sessions", identity.sessionID); err != nil {
			return err
		}
		return ensureRootOwnedChildDirectory(filepath.Dir(sessionLedgerRoot(j.config, identity.sessionID)), identity.sessionID)
	case "session-head":
		return ensureRootOwnedChildDirectory(sessionLedgerRoot(j.config, identity.sessionID), "heads")
	case "collection-outcome":
		return prepareAuthorityShard(j.config, "outcomes", identity.challenge)
	default:
		return errors.New("authority prepared-object kind has no destination preparation")
	}
}

func (j *authorityLedgerJournal) decodePreparedSlot(raw []byte, expectedSegment string) (authorityPreparedIdentity, []byte, error) {
	var slot authorityPreparedSlot
	if err := canonicalJSON(raw, &slot); err != nil {
		return authorityPreparedIdentity{}, nil, err
	}
	canonical, err := json.Marshal(slot)
	if err != nil || !bytes.Equal(canonical, raw) || slot.Schema != authorityPreparedSchema ||
		slot.ClusterID != j.config.ClusterID || slot.DeploymentID != j.config.DeploymentID || slot.SegmentID != expectedSegment {
		return authorityPreparedIdentity{}, nil, errors.New("authority prepared-object slot identity is invalid")
	}
	objectRaw, err := base64.StdEncoding.DecodeString(slot.ObjectBase64)
	if err != nil || base64.StdEncoding.EncodeToString(objectRaw) != slot.ObjectBase64 {
		return authorityPreparedIdentity{}, nil, errors.New("authority prepared-object bytes are not canonical base64")
	}
	identity := authorityPreparedIdentity{
		kind: slot.Kind, sessionID: slot.SessionID, challenge: slot.Challenge, pageIndex: slot.PageIndex,
		sha256: slot.ObjectSHA256, objectBytes: slot.ObjectBytes,
	}
	if _, err := preparedObjectName(identity); err != nil || uint64(len(objectRaw)) != identity.objectBytes || digest(objectRaw) != identity.sha256 {
		return authorityPreparedIdentity{}, nil, errors.New("authority prepared-object slot differs from its content address")
	}
	return identity, objectRaw, nil
}

func (j *authorityLedgerJournal) preparedArchivePath(segmentID string, identity authorityPreparedIdentity) (string, error) {
	name, err := preparedObjectName(identity)
	if err != nil {
		return "", err
	}
	return filepath.Join(j.preparedArchiveRoot(segmentID), identity.challenge[:2], identity.challenge[2:4], name), nil
}

func (j *authorityLedgerJournal) prepareArchiveDestination(segmentID string, identity authorityPreparedIdentity) (string, error) {
	root := j.preparedArchiveRoot(segmentID)
	if err := ensureRootOwnedChildDirectory(root, identity.challenge[:2]); err != nil {
		return "", err
	}
	first := filepath.Join(root, identity.challenge[:2])
	if err := ensureRootOwnedChildDirectory(first, identity.challenge[2:4]); err != nil {
		return "", err
	}
	return j.preparedArchivePath(segmentID, identity)
}

func (j *authorityLedgerJournal) archivePreparedSlot(segmentID string, identity authorityPreparedIdentity) error {
	pendingPath := j.pendingPreparedPath(segmentID)
	archivePath, err := j.prepareArchiveDestination(segmentID, identity)
	if err != nil {
		return err
	}
	if err := renameNoReplace(pendingPath, archivePath); err != nil {
		if !errors.Is(err, syscall.EEXIST) {
			return err
		}
		pendingRaw, pendingErr := readRootRegular(pendingPath, maximumPreparedSlotBytes)
		archiveRaw, archiveErr := readRootRegular(archivePath, maximumPreparedSlotBytes)
		if pendingErr != nil || archiveErr != nil || !bytes.Equal(pendingRaw, archiveRaw) {
			return errors.New("authority prepared archive conflicts with different retained bytes")
		}
		// renameat2 cannot create both names. This branch only recovers an exact
		// duplicate retained by an earlier implementation or interrupted repair.
		// Move that inode to a deterministic evidence alias so the single pending
		// CAS slot is released without deleting or replacing either copy.
		alias := archivePath + ".recovered-duplicate-" + digest(pendingRaw)
		if err := renameNoReplace(pendingPath, alias); err != nil {
			return fmt.Errorf("preserve duplicate prepared archive: %w", err)
		}
		if err := fsyncDirectory(filepath.Dir(alias)); err != nil {
			return err
		}
	}
	if err := fsyncDirectory(filepath.Dir(archivePath)); err != nil {
		return err
	}
	return fsyncDirectory(filepath.Dir(pendingPath))
}

func publishPreparedObject(destinationPath string, raw []byte) error {
	if err := publishAnonymousRegular(destinationPath, raw, 0o400); err != nil {
		return err
	}
	retained, err := readRootRegular(destinationPath, maximumAuthorityLedgerRecordBytes)
	if err != nil || !bytes.Equal(retained, raw) {
		return errors.New("authority prepared-object destination differs from its exact retained bytes")
	}
	return nil
}

func (j *authorityLedgerJournal) recoverPreparedObjects(now time.Time) error {
	pendingPath := j.pendingPreparedPath(j.segmentID)
	slotRaw, err := readRootRegular(pendingPath, maximumPreparedSlotBytes)
	if errors.Is(err, os.ErrNotExist) {
		if len(j.pendingIntents) != 0 {
			return errors.New("authority ledger pending intent lacks its durable prepared slot")
		}
		return nil
	}
	if err != nil {
		return err
	}
	identity, objectRaw, err := j.decodePreparedSlot(slotRaw, j.segmentID)
	if err != nil {
		return err
	}
	terminalIndex, err := j.eventIndexPath(identity.challenge, identity.kind, "terminal")
	if err != nil {
		return err
	}
	if terminalRaw, terminalErr := readRootRegular(terminalIndex, maximumLedgerEventBytes); terminalErr == nil {
		_, terminal, decodeErr := j.decodeEvent(terminalRaw)
		destination, pathErr := j.expectedObjectPath(identity)
		relative, relativeErr := safeRelativeLedgerPath(j.config.SettlementRoot, destination)
		if decodeErr != nil || pathErr != nil || relativeErr != nil ||
			!eventMatchesObject(terminal, identity.kind, "terminal", identity.sessionID, identity.challenge, identity.pageIndex, relative, objectRaw) {
			return errors.New("authority prepared-object terminal event differs from the retained slot")
		}
		if err := j.validateEventObject(terminal, terminalRaw, false); err != nil {
			return err
		}
		return j.ensurePreparedArchived(terminal)
	} else if !errors.Is(terminalErr, os.ErrNotExist) {
		return terminalErr
	}
	if j.config.LedgerExpectedMaximumBytesPerDay < uint64(len(slotRaw)) ||
		j.objectBytes > j.config.LedgerExpectedMaximumBytesPerDay-uint64(len(slotRaw)) {
		return errors.New("authority prepared-object slot exceeds its signed daily byte bound")
	}
	j.objectBytes += uint64(len(slotRaw))
	intentKey := ledgerIntentKey(identity.kind, identity.challenge)
	started, startedAlready := j.pendingIntents[intentKey]
	if len(j.pendingIntents) != 0 && !startedAlready {
		return errors.New("authority prepared-object differs from the unresolved signed intent")
	}
	return j.completePreparedObject(identity, objectRaw, recoveryLedgerTime(j.segmentID, j.lastRecordedAt, now), started, startedAlready)
}

func (j *authorityLedgerJournal) retainedPreparedBytes(event authorityLedgerEvent) (uint64, error) {
	identity := authorityPreparedIdentity{
		kind: event.Payload.Kind, sessionID: event.Payload.SessionID, challenge: event.Payload.Challenge,
		pageIndex: event.Payload.PageIndex, sha256: event.Payload.ObjectSHA256, objectBytes: event.Payload.ObjectBytes,
	}
	archivePath, err := j.preparedArchivePath(event.Payload.SegmentID, identity)
	if err != nil {
		return 0, err
	}
	for _, candidate := range []string{archivePath, j.pendingPreparedPath(event.Payload.SegmentID)} {
		slotRaw, readErr := readRootRegular(candidate, maximumPreparedSlotBytes)
		if errors.Is(readErr, os.ErrNotExist) {
			continue
		}
		if readErr != nil {
			return 0, readErr
		}
		retainedIdentity, objectRaw, decodeErr := j.decodePreparedSlot(slotRaw, event.Payload.SegmentID)
		if decodeErr != nil || retainedIdentity != identity || digest(objectRaw) != event.Payload.ObjectSHA256 ||
			digest(slotRaw) != event.Payload.PreparedSHA256 || uint64(len(slotRaw)) != event.Payload.PreparedBytes {
			return 0, errors.New("authority prepared archive differs from its terminal event")
		}
		return uint64(len(slotRaw)), nil
	}
	return 0, errors.New("authority terminal event lacks its retained prepared archive")
}

func (j *authorityLedgerJournal) ensurePreparedArchived(event authorityLedgerEvent) error {
	identity := authorityPreparedIdentity{
		kind: event.Payload.Kind, sessionID: event.Payload.SessionID, challenge: event.Payload.Challenge,
		pageIndex: event.Payload.PageIndex, sha256: event.Payload.ObjectSHA256, objectBytes: event.Payload.ObjectBytes,
	}
	intentKey := ledgerIntentKey(event.Payload.Kind, event.Payload.Challenge)
	if started, pending := j.pendingIntents[intentKey]; pending && event.Payload.StartedEventSHA256 != digestEvent(started) {
		return errors.New("authority terminal event does not settle the in-memory pending intent")
	}
	pendingPath := j.pendingPreparedPath(event.Payload.SegmentID)
	if pendingRaw, err := readRootRegular(pendingPath, maximumPreparedSlotBytes); err == nil {
		retainedIdentity, _, decodeErr := j.decodePreparedSlot(pendingRaw, event.Payload.SegmentID)
		if decodeErr != nil || retainedIdentity != identity || digest(pendingRaw) != event.Payload.PreparedSHA256 ||
			uint64(len(pendingRaw)) != event.Payload.PreparedBytes {
			return errors.New("authority terminal event conflicts with its pending prepared slot")
		}
		if err := j.archivePreparedSlot(event.Payload.SegmentID, identity); err != nil {
			return err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	archivePath, err := j.preparedArchivePath(event.Payload.SegmentID, identity)
	if err != nil {
		return err
	}
	archiveRaw, err := readRootRegular(archivePath, maximumPreparedSlotBytes)
	if err != nil || digest(archiveRaw) != event.Payload.PreparedSHA256 || uint64(len(archiveRaw)) != event.Payload.PreparedBytes {
		return errors.New("authority terminal event lacks its exact durable prepared archive")
	}
	if _, _, err := j.decodePreparedSlot(archiveRaw, event.Payload.SegmentID); err != nil {
		return err
	}
	if _, err := os.Lstat(pendingPath); !errors.Is(err, os.ErrNotExist) {
		return errors.New("authority prepared pending slot remains visible after terminal archival")
	}
	delete(j.pendingIntents, intentKey)
	return nil
}

func (j *authorityLedgerJournal) completePreparedObject(
	identity authorityPreparedIdentity,
	objectRaw []byte,
	recordedAt time.Time,
	started authorityLedgerEvent,
	startedAlready bool,
) error {
	pendingPath := j.pendingPreparedPath(j.segmentID)
	preparedRaw, err := readRootRegular(pendingPath, maximumPreparedSlotBytes)
	if err != nil {
		return err
	}
	preparedIdentity, retainedObjectRaw, err := j.decodePreparedSlot(preparedRaw, j.segmentID)
	if err != nil || preparedIdentity != identity || !bytes.Equal(retainedObjectRaw, objectRaw) {
		return errors.New("authority prepared-object completion differs from its fixed pending slot")
	}
	preparedSHA256 := digest(preparedRaw)
	preparedBytes := uint64(len(preparedRaw))
	destination, err := j.expectedObjectPath(identity)
	if err != nil {
		return err
	}
	relative, err := safeRelativeLedgerPath(j.config.SettlementRoot, destination)
	if err != nil {
		return err
	}
	additionalEvents := uint64(1)
	if !startedAlready {
		additionalEvents = 2
	}
	additionalBytes := uint64(len(objectRaw)) + additionalEvents*maximumLedgerEventBytes
	if j.config.LedgerExpectedMaximumEventsPerDay < additionalEvents ||
		j.nextSequence > j.config.LedgerExpectedMaximumEventsPerDay-additionalEvents ||
		j.config.LedgerExpectedMaximumBytesPerDay < additionalBytes ||
		j.objectBytes > j.config.LedgerExpectedMaximumBytesPerDay-additionalBytes {
		return errors.New("authority prepared-object completion exceeds its signed daily budgets")
	}
	var startedRaw []byte
	if !startedAlready {
		started, startedRaw, err = j.appendEvent(authorityLedgerEventPayload{
			Kind: identity.kind, Phase: "started", SessionID: identity.sessionID, Challenge: identity.challenge,
			PageIndex: identity.pageIndex, ObjectPath: relative, ObjectSHA256: identity.sha256,
			ObjectBytes: identity.objectBytes, PreparedSHA256: preparedSHA256, PreparedBytes: preparedBytes,
			RecordedAt: canonicalLedgerTime(recordedAt),
		})
		if err != nil {
			return err
		}
		j.pendingIntents[ledgerIntentKey(identity.kind, identity.challenge)] = started
	} else {
		if started.Payload.PreparedSHA256 != preparedSHA256 || started.Payload.PreparedBytes != preparedBytes {
			return errors.New("authority signed started event differs from its durable prepared slot")
		}
		startedRaw, err = json.Marshal(started)
		if err != nil {
			return err
		}
	}
	if err := j.prepareObjectDestination(identity); err != nil {
		return err
	}
	if err := publishPreparedObject(destination, objectRaw); err != nil {
		return err
	}
	terminalRecordedAt := time.Now().UTC().Truncate(time.Second)
	if terminalRecordedAt.Before(recordedAt.UTC().Truncate(time.Second)) {
		return errors.New("authority ledger clock moved backwards before terminal publication")
	}
	_, _, err = j.appendEvent(authorityLedgerEventPayload{
		Kind: identity.kind, Phase: "terminal", SessionID: identity.sessionID, Challenge: identity.challenge,
		PageIndex: identity.pageIndex, ObjectPath: relative, ObjectSHA256: identity.sha256,
		ObjectBytes: identity.objectBytes, PreparedSHA256: preparedSHA256, PreparedBytes: preparedBytes,
		StartedEventSHA256: digest(startedRaw),
		RecordedAt: canonicalLedgerTime(terminalRecordedAt),
	})
	if err != nil {
		return err
	}
	j.objectBytes += uint64(len(objectRaw))
	if err := j.archivePreparedSlot(j.segmentID, identity); err != nil {
		return err
	}
	delete(j.pendingIntents, ledgerIntentKey(identity.kind, identity.challenge))
	return nil
}

func recoveryLedgerTime(segmentID string, previous time.Time, now time.Time) time.Time {
	value := now.UTC().Truncate(time.Second)
	if ledgerSegmentID(value) != segmentID {
		segmentStart, _ := time.Parse("2006-01-02", segmentID)
		value = segmentStart.Add(24*time.Hour - time.Second)
	}
	if value.Before(previous) {
		return previous
	}
	return value
}

func safeRelativeLedgerPath(root string, path string) (string, error) {
	relative, err := filepath.Rel(root, path)
	if err != nil || relative == "." || filepath.IsAbs(relative) || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", errors.New("authority ledger path escapes its dedicated root")
	}
	return filepath.ToSlash(relative), nil
}
