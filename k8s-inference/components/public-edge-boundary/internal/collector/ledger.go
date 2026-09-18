package collector

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
)

const (
	authorityLedgerEventSchema      = "fs2-serve.nebius.ai/public-edge-authority-ledger-event/v1"
	authorityLedgerCheckpointSchema = "fs2-serve.nebius.ai/public-edge-authority-ledger-checkpoint/v1"
	authorityLedgerHeadSchema       = "fs2-serve.nebius.ai/public-edge-authority-ledger-global-head/v1"
	maximumLedgerEventBytes         = 32 * 1024
	maximumLedgerCheckpointBytes    = 32 * 1024
	maximumLedgerHeadBytes          = 32 * 1024
	maximumAutomaticLedgerGapDays   = 7
)

type authorityLedgerEventPayload struct {
	Schema                   string `json:"schema"`
	ClusterID                string `json:"cluster_id"`
	DeploymentID             string `json:"deployment_id"`
	LedgerEpochID            string `json:"ledger_epoch_id"`
	SegmentID                string `json:"segment_id"`
	Sequence                 uint64 `json:"sequence"`
	Kind                     string `json:"kind"`
	Phase                    string `json:"phase"`
	SessionID                string `json:"session_id"`
	Challenge                string `json:"challenge"`
	PageIndex                int    `json:"page_index"`
	ObjectPath               string `json:"object_path"`
	ObjectSHA256             string `json:"object_sha256"`
	ObjectBytes              uint64 `json:"object_bytes"`
	PreparedSHA256           string `json:"prepared_sha256"`
	PreparedBytes            uint64 `json:"prepared_bytes"`
	PreviousEventSHA256      string `json:"previous_event_sha256"`
	StartedEventSHA256       string `json:"started_event_sha256"`
	RecordedAt               string `json:"recorded_at"`
}

type authorityLedgerEvent struct {
	Payload   authorityLedgerEventPayload `json:"payload"`
	Issuer    string                      `json:"issuer"`
	KeyID     string                      `json:"key_id"`
	Algorithm string                      `json:"algorithm"`
	Signature string                      `json:"signature"`
}

type authorityLedgerCheckpointPayload struct {
	Schema                     string `json:"schema"`
	ClusterID                  string `json:"cluster_id"`
	DeploymentID               string `json:"deployment_id"`
	LedgerEpochID              string `json:"ledger_epoch_id"`
	SegmentID                  string `json:"segment_id"`
	EventCount                 uint64 `json:"event_count"`
	ObjectBytes                uint64 `json:"object_bytes"`
	FinalEventSHA256           string `json:"final_event_sha256"`
	PreviousCheckpointSegment  string `json:"previous_checkpoint_segment"`
	PreviousCheckpointSHA256   string `json:"previous_checkpoint_sha256"`
	FinalizedAt                string `json:"finalized_at"`
}

type authorityLedgerCheckpoint struct {
	Payload   authorityLedgerCheckpointPayload `json:"payload"`
	Issuer    string                           `json:"issuer"`
	KeyID     string                           `json:"key_id"`
	Algorithm string                           `json:"algorithm"`
	Signature string                           `json:"signature"`
}

type authorityLedgerHeadPayload struct {
	Schema                        string `json:"schema"`
	ClusterID                     string `json:"cluster_id"`
	DeploymentID                  string `json:"deployment_id"`
	LedgerEpochID                 string `json:"ledger_epoch_id"`
	SegmentID                     string `json:"segment_id"`
	CheckpointSHA256              string `json:"checkpoint_sha256"`
	PreviousHeadSegment           string `json:"previous_head_segment"`
	PreviousHeadSHA256            string `json:"previous_head_sha256"`
	PreviousEpochID               string `json:"previous_epoch_id"`
	PreviousEpochHeadSHA256       string `json:"previous_epoch_head_sha256"`
	CheckpointFinalizedAt         string `json:"checkpoint_finalized_at"`
}

type authorityLedgerHead struct {
	Payload   authorityLedgerHeadPayload `json:"payload"`
	Issuer    string                     `json:"issuer"`
	KeyID     string                     `json:"key_id"`
	Algorithm string                     `json:"algorithm"`
	Signature string                     `json:"signature"`
}

type authorityLedgerJournal struct {
	config                   AuthorityConfig
	acceptance               boundary.Acceptance
	privateKey               ed25519.PrivateKey
	publicKey                ed25519.PublicKey
	segmentID                string
	nextSequence             uint64
	previousEventSHA256      string
	objectBytes              uint64
	previousCheckpointSegment string
	previousCheckpointSHA256 string
	previousHeadSegment       string
	previousHeadSHA256        string
	pendingIntents           map[string]authorityLedgerEvent
	recoveredSessionHeads    []authorityLedgerEvent
	recoveredCycleIssuedAt   time.Time
	recoveredCycleID         string
	segmentFinalized         bool
	lastRecordedAt           time.Time
}

func loadAuthorityLedgerJournal(config AuthorityConfig, acceptance boundary.Acceptance, privateKey ed25519.PrivateKey, publicKey ed25519.PublicKey, now time.Time) (*authorityLedgerJournal, authorityLedgerCounts, error) {
	if len(privateKey) != ed25519.PrivateKeySize || len(publicKey) != ed25519.PublicKeySize {
		return nil, authorityLedgerCounts{}, errors.New("authority ledger journal lacks its exact signing identity")
	}
	journal := &authorityLedgerJournal{
		config:     config,
		acceptance: acceptance,
		privateKey: privateKey,
		publicKey:  publicKey,
		segmentID:  ledgerSegmentID(now),
		pendingIntents: map[string]authorityLedgerEvent{},
	}
	priorSegment, priorDigest, resumedHistoricalTail, err := journal.latestHistoricalCheckpoint(now)
	if err != nil {
		return nil, authorityLedgerCounts{}, err
	}
	todaySegment := ledgerSegmentID(now)
	if priorSegment != "" && todaySegment <= priorSegment {
		return nil, authorityLedgerCounts{}, errors.New("authority ledger clock is not later than its signed checkpoint chain")
	}
	journal.previousCheckpointSegment = priorSegment
	journal.previousCheckpointSHA256 = priorDigest
	bridgedGap := false
	priorSegment, priorDigest, bridgedGap, err = journal.bridgeEmptyLedgerSegments(priorSegment, priorDigest, todaySegment, now)
	if err != nil {
		return nil, authorityLedgerCounts{}, err
	}
	journal.previousCheckpointSegment = priorSegment
	journal.previousCheckpointSHA256 = priorDigest
	if resumedHistoricalTail || bridgedGap {
		// loadTail deliberately loaded the crash-left historical tail so it could
		// be recovered and checkpointed. That in-memory tail is finalized and
		// must never be mistaken for today's appendable segment.
		journal.resetLoadedTail(todaySegment)
	}
	if err := journal.loadTail(todaySegment, false, true); err != nil {
		return nil, authorityLedgerCounts{}, err
	}
	if err := journal.recoverPreparedObjects(now); err != nil {
		return nil, authorityLedgerCounts{}, err
	}
	return journal, authorityLedgerCounts{}, nil
}

func (j *authorityLedgerJournal) bridgeEmptyLedgerSegments(priorSegment string, priorDigest string, todaySegment string, now time.Time) (string, string, bool, error) {
	if priorSegment == "" {
		return priorSegment, priorDigest, false, nil
	}
	priorTime, priorErr := time.Parse("2006-01-02", priorSegment)
	todayTime, todayErr := time.Parse("2006-01-02", todaySegment)
	if priorErr != nil || todayErr != nil || !todayTime.After(priorTime) {
		return "", "", false, errors.New("authority ledger gap chronology is invalid")
	}
	missingDays := int(todayTime.Sub(priorTime)/(24*time.Hour)) - 1
	if missingDays < 1 {
		return priorSegment, priorDigest, false, nil
	}
	if missingDays > maximumAutomaticLedgerGapDays {
		return "", "", false, errors.New("authority ledger gap exceeds bounded automatic recovery and requires an independently signed recovery transition")
	}
	for offset := 1; offset <= missingDays; offset++ {
		segmentID := ledgerSegmentID(priorTime.Add(time.Duration(offset) * 24 * time.Hour))
		j.resetLoadedTail(segmentID)
		j.previousCheckpointSegment = priorSegment
		j.previousCheckpointSHA256 = priorDigest
		if err := j.prepareSegment(segmentID); err != nil {
			return "", "", false, err
		}
		if err := j.finalizeLoadedSegment(segmentID, now); err != nil {
			return "", "", false, err
		}
		checkpointRaw, checkpoint, err := j.readCheckpoint(segmentID)
		if err != nil {
			return "", "", false, err
		}
		headRaw, err := j.ensureCheckpointHead(segmentID, digest(checkpointRaw), checkpoint.Payload.FinalizedAt, j.previousHeadSegment, j.previousHeadSHA256)
		if err != nil {
			return "", "", false, err
		}
		priorSegment = segmentID
		priorDigest = digest(checkpointRaw)
		j.previousHeadSegment = segmentID
		j.previousHeadSHA256 = digest(headRaw)
	}
	return priorSegment, priorDigest, true, nil
}

func (j *authorityLedgerJournal) resetLoadedTail(segmentID string) {
	j.segmentID = segmentID
	j.nextSequence = 0
	j.previousEventSHA256 = ""
	j.objectBytes = 0
	j.pendingIntents = map[string]authorityLedgerEvent{}
	j.segmentFinalized = false
	j.lastRecordedAt = time.Time{}
}

func ledgerSegmentID(now time.Time) string {
	return now.UTC().Format("2006-01-02")
}

func (j *authorityLedgerJournal) latestHistoricalCheckpoint(now time.Time) (string, string, bool, error) {
	// Enumerate the bounded segment namespace rather than deriving a date
	// window from the possibly corrected wall clock. This makes a retained
	// future segment visible and fail-closed instead of allowing a competing
	// branch to be created after a forward clock jump.
	todaySegment := ledgerSegmentID(now)
	allSegments, err := j.retainedSegmentIDs()
	if err != nil {
		return "", "", false, err
	}
	segments := make([]string, 0, len(allSegments))
	for _, segmentID := range allSegments {
		if segmentID > todaySegment {
			return "", "", false, errors.New("authority ledger contains a retained future segment")
		}
		if segmentID < todaySegment {
			segments = append(segments, segmentID)
		}
	}
	priorSegment := ""
	priorDigest := ""
	priorHeadSegment := ""
	priorHeadDigest := ""
	resumedHistoricalTail := false
	for index, segmentID := range segments {
		raw, checkpoint, err := j.readCheckpoint(segmentID)
		if errors.Is(err, os.ErrNotExist) {
			if index != len(segments)-1 {
				return "", "", false, errors.New("authority ledger checkpoint chain has an unfinalized historical gap")
			}
			j.previousCheckpointSegment = priorSegment
			j.previousCheckpointSHA256 = priorDigest
			j.previousHeadSegment = priorHeadSegment
			j.previousHeadSHA256 = priorHeadDigest
			if err := j.loadTail(segmentID, true, true); err != nil {
				return "", "", false, err
			}
			if err := j.recoverPreparedObjects(now); err != nil {
				return "", "", false, err
			}
			if err := j.finalizeLoadedSegment(segmentID, now); err != nil {
				return "", "", false, err
			}
			resumedHistoricalTail = true
			raw, checkpoint, err = j.readCheckpoint(segmentID)
		}
		if err != nil {
			return "", "", false, err
		}
		if priorSegment != "" && (checkpoint.Payload.PreviousCheckpointSegment != priorSegment ||
			checkpoint.Payload.PreviousCheckpointSHA256 != priorDigest) {
			return "", "", false, errors.New("authority ledger checkpoint chain forks or skips a retained predecessor")
		}
		if priorSegment == "" && checkpoint.Payload.PreviousCheckpointSegment != "" &&
			checkpoint.Payload.PreviousCheckpointSegment >= segmentID {
			return "", "", false, errors.New("authority ledger checkpoint predecessor chronology is invalid")
		}
		headRaw, err := j.ensureCheckpointHead(segmentID, digest(raw), checkpoint.Payload.FinalizedAt, priorHeadSegment, priorHeadDigest)
		if err != nil {
			return "", "", false, err
		}
		priorSegment = segmentID
		priorDigest = digest(raw)
		priorHeadSegment = segmentID
		priorHeadDigest = digest(headRaw)
	}
	headSegments, err := j.retainedCheckpointHeadSegments()
	if err != nil {
		return "", "", false, err
	}
	if len(headSegments) != len(segments) {
		return "", "", false, errors.New("authority ledger global-head inventory does not exactly cover every historical segment")
	}
	for index := range segments {
		if headSegments[index] != segments[index] {
			return "", "", false, errors.New("authority ledger global-head inventory diverges from its segment inventory")
		}
	}
	j.previousHeadSegment = priorHeadSegment
	j.previousHeadSHA256 = priorHeadDigest
	return priorSegment, priorDigest, resumedHistoricalTail, nil
}

func (j *authorityLedgerJournal) retainedSegmentIDs() ([]string, error) {
	segmentsRoot := filepath.Join(j.config.SettlementRoot, "segments")
	entries, err := os.ReadDir(segmentsRoot)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if err := requireRootOwnedDirectory(segmentsRoot); err != nil {
		return nil, err
	}
	maximumSegments := j.config.LedgerOperatingHorizonDays + 1
	if maximumSegments < 1 || len(entries) > maximumSegments {
		return nil, errors.New("authority ledger segment namespace exceeds its accepted operating horizon")
	}
	segments := make([]string, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() || !canonicalSegmentID(entry.Name()) {
			return nil, errors.New("authority ledger segment namespace contains an unexpected entry")
		}
		segments = append(segments, entry.Name())
	}
	sort.Strings(segments)
	headSegments, err := j.retainedCheckpointHeadSegments()
	if err != nil {
		return nil, err
	}
	segmentSet := map[string]struct{}{}
	for _, segmentID := range segments {
		segmentSet[segmentID] = struct{}{}
	}
	for _, segmentID := range headSegments {
		if _, exists := segmentSet[segmentID]; !exists {
			return nil, errors.New("authority ledger global head lacks its retained segment")
		}
	}
	return segments, nil
}

func (j *authorityLedgerJournal) retainedCheckpointHeadSegments() ([]string, error) {
	headRoot := filepath.Join(j.config.SettlementRoot, "checkpoint-heads")
	entries, err := os.ReadDir(headRoot)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if err := requireRootOwnedDirectory(headRoot); err != nil {
		return nil, err
	}
	if len(entries) > j.config.LedgerOperatingHorizonDays {
		return nil, errors.New("authority ledger global-head namespace exceeds its accepted operating horizon")
	}
	segments := make([]string, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasPrefix(entry.Name(), "head-") || !strings.HasSuffix(entry.Name(), ".json") {
			return nil, errors.New("authority ledger global-head namespace contains an unexpected entry")
		}
		segmentID := strings.TrimSuffix(strings.TrimPrefix(entry.Name(), "head-"), ".json")
		if !canonicalSegmentID(segmentID) || entry.Name() != "head-"+segmentID+".json" {
			return nil, errors.New("authority ledger global-head filename is non-canonical")
		}
		segments = append(segments, segmentID)
	}
	sort.Strings(segments)
	return segments, nil
}

func (j *authorityLedgerJournal) loadTail(segmentID string, historical bool, repairIndexes bool) error {
	eventsRoot := j.eventsRoot(segmentID)
	entries, err := os.ReadDir(eventsRoot)
	if errors.Is(err, os.ErrNotExist) {
		if historical {
			return errors.New("historical authority ledger segment has no event namespace")
		}
		return j.prepareSegment(segmentID)
	}
	if err != nil {
		return err
	}
	if err := requireRootOwnedDirectory(eventsRoot); err != nil {
		return err
	}
	maximumEvents := j.config.LedgerExpectedMaximumEventsPerDay
	if maximumEvents == 0 || uint64(len(entries)) > maximumEvents+1 {
		return errors.New("authority ledger tail exceeds its source-derived daily bound")
	}
	var sequence uint64
	previousDigest := ""
	var objectBytes uint64
	var previousRecordedAt time.Time
	pending := map[string]authorityLedgerEvent{}
	for _, entry := range entries {
		if entry.Name() == ".attempts" {
			if !entry.IsDir() {
				return errors.New("authority ledger event attempts namespace is invalid")
			}
			continue
		}
		expectedName := fmt.Sprintf("event-%012d.json", sequence)
		if entry.IsDir() || entry.Name() != expectedName {
			return errors.New("authority ledger tail is non-contiguous or contains an unexpected event")
		}
		raw, event, err := j.readEvent(filepath.Join(eventsRoot, entry.Name()))
		if err != nil {
			return err
		}
		if event.Payload.SegmentID != segmentID || event.Payload.Sequence != sequence || event.Payload.PreviousEventSHA256 != previousDigest {
			return errors.New("authority ledger event does not extend its exact segment chain")
		}
		recordedAt, err := time.Parse(time.RFC3339, event.Payload.RecordedAt)
		if err != nil || recordedAt.Nanosecond() != 0 || !strings.HasSuffix(event.Payload.RecordedAt, "Z") ||
			(!previousRecordedAt.IsZero() && recordedAt.Before(previousRecordedAt)) {
			return errors.New("authority ledger event chronology is non-canonical or moves backwards")
		}
		if event.Payload.Phase == "started" && ledgerSegmentID(recordedAt) != segmentID {
			return errors.New("authority ledger started event lies outside its UTC segment")
		}
		if objectBytes > ^uint64(0)-uint64(len(raw)) {
			return errors.New("authority ledger retained-byte accumulator overflows")
		}
		objectBytes += uint64(len(raw))
		intentKey := ledgerIntentKey(event.Payload.Kind, event.Payload.Challenge)
		switch event.Payload.Phase {
		case "started":
			if event.Payload.StartedEventSHA256 != "" {
				return errors.New("authority ledger started event contains a terminal predecessor")
			}
			if _, exists := pending[intentKey]; exists {
				return errors.New("authority ledger segment repeats an unresolved started event")
			}
			indexPath, indexErr := j.eventIndexPath(event.Payload.Challenge, event.Payload.Kind, event.Payload.Phase)
			if indexErr != nil {
				return indexErr
			}
			if repairIndexes {
				if err := j.linkEventIndex(filepath.Join(eventsRoot, entry.Name()), indexPath); err != nil {
					return err
				}
			} else if err := j.verifyEventIndex(filepath.Join(eventsRoot, entry.Name()), indexPath); err != nil {
				return err
			}
			pending[intentKey] = event
		case "terminal":
			started, exists := pending[intentKey]
			if !exists || event.Payload.StartedEventSHA256 != digestEvent(started) {
				return errors.New("authority ledger terminal event does not settle its exact started event")
			}
			if err := j.validateEventObject(event, raw, repairIndexes); err != nil {
				return err
			}
			if event.Payload.Kind == "session-head" {
				j.recoveredSessionHeads = append(j.recoveredSessionHeads, event)
			}
			if event.Payload.Kind == "reservation" {
				objectPath := filepath.Join(j.config.SettlementRoot, filepath.FromSlash(event.Payload.ObjectPath))
				reservationRaw, readErr := readRootRegular(objectPath, maximumAuthorityReservationBytes)
				var reservation AuthorityReservation
				if readErr != nil || canonicalJSON(reservationRaw, &reservation) != nil ||
					reservation.SessionID != event.Payload.SessionID || reservation.Challenge != event.Payload.Challenge ||
					reservation.PageIndex != event.Payload.PageIndex || !isDigest(reservation.CycleID) {
					return errors.New("authority ledger reservation event lacks its exact cycle-bearing object")
				}
				issuedAt, issueErr := time.Parse(time.RFC3339, reservation.CycleIssuedAt)
				if issueErr != nil || issuedAt.Nanosecond() != 0 ||
					(!j.recoveredCycleIssuedAt.IsZero() && (issuedAt.Before(j.recoveredCycleIssuedAt) ||
						(issuedAt.Equal(j.recoveredCycleIssuedAt) && j.recoveredCycleID != reservation.CycleID))) {
					return errors.New("authority ledger reservation cycle high-water moves backwards or forks")
				}
				if issuedAt.After(j.recoveredCycleIssuedAt) {
					j.recoveredCycleIssuedAt = issuedAt
					j.recoveredCycleID = reservation.CycleID
				}
			}
			if objectBytes > ^uint64(0)-event.Payload.ObjectBytes {
				return errors.New("authority ledger segment byte accumulator overflows")
			}
			objectBytes += event.Payload.ObjectBytes
			preparedBytes, preparedErr := j.retainedPreparedBytes(event)
			if preparedErr != nil || objectBytes > ^uint64(0)-preparedBytes {
				return errors.New("authority ledger terminal event lacks its exact retained prepared bytes")
			}
			objectBytes += preparedBytes
			if objectBytes > j.config.LedgerExpectedMaximumBytesPerDay {
				return errors.New("authority ledger tail exceeds its source-derived daily byte bound")
			}
			delete(pending, intentKey)
		default:
			return errors.New("authority ledger event phase is unsupported")
		}
		previousDigest = digest(raw)
		previousRecordedAt = recordedAt
		sequence++
	}
	if historical {
		j.segmentID = segmentID
	}
	j.nextSequence = sequence
	j.previousEventSHA256 = previousDigest
	j.objectBytes = objectBytes
	j.pendingIntents = pending
	j.segmentFinalized = false
	j.lastRecordedAt = previousRecordedAt
	if checkpointRaw, checkpoint, checkpointErr := j.readCheckpoint(segmentID); checkpointErr == nil {
		if len(pending) != 0 || checkpoint.Payload.EventCount != sequence || checkpoint.Payload.ObjectBytes != objectBytes ||
			checkpoint.Payload.FinalEventSHA256 != previousDigest || digest(checkpointRaw) == "" ||
			checkpoint.Payload.PreviousCheckpointSegment != j.previousCheckpointSegment ||
			checkpoint.Payload.PreviousCheckpointSHA256 != j.previousCheckpointSHA256 {
			return errors.New("authority ledger finalized checkpoint differs from its exact segment tail")
		}
		j.segmentFinalized = true
	} else if !errors.Is(checkpointErr, os.ErrNotExist) {
		return checkpointErr
	}
	return nil
}

func (j *authorityLedgerJournal) verifyEventIndex(eventPath string, indexPath string) error {
	eventInfo, eventErr := os.Lstat(eventPath)
	indexInfo, indexErr := os.Lstat(indexPath)
	if eventErr != nil || indexErr != nil || !eventInfo.Mode().IsRegular() || !indexInfo.Mode().IsRegular() ||
		!os.SameFile(eventInfo, indexInfo) {
		return errors.New("authority ledger read-only event index is absent or does not hard-link its exact signed event")
	}
	return nil
}

func (j *authorityLedgerJournal) publish(kind string, sessionID string, challenge string, pageIndex int, objectPath string, objectRaw []byte, now time.Time) error {
	if !ledgerObjectKind(kind) || !canonicalNonce(sessionID) || !canonicalNonce(challenge) ||
		pageIndex < 0 || pageIndex >= maximumAuthoritySessionPages || len(objectRaw) == 0 {
		return errors.New("authority ledger publication identity is invalid")
	}
	if _, err := os.Lstat(j.pendingPreparedPath(j.segmentID)); err == nil {
		if err := j.recoverPreparedObjects(now); err != nil {
			return err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	relativePath, err := filepath.Rel(j.config.SettlementRoot, objectPath)
	if err != nil || relativePath == "." || filepath.IsAbs(relativePath) || relativePath == ".." || strings.HasPrefix(relativePath, ".."+string(filepath.Separator)) {
		return errors.New("authority ledger object path escapes its dedicated root")
	}
	relativePath = filepath.ToSlash(relativePath)
	identity := authorityPreparedIdentity{
		kind: kind, sessionID: sessionID, challenge: challenge, pageIndex: pageIndex,
		sha256: digest(objectRaw), objectBytes: uint64(len(objectRaw)),
	}
	expectedPath, err := j.expectedObjectPath(identity)
	if err != nil || expectedPath != objectPath {
		return errors.New("authority ledger object path differs from its derived identity")
	}
	terminalIndex, err := j.eventIndexPath(challenge, kind, "terminal")
	if err != nil {
		return err
	}
	if indexedRaw, readErr := readRootRegular(terminalIndex, maximumLedgerEventBytes); readErr == nil {
		_, event, eventErr := j.decodeEvent(indexedRaw)
		if eventErr != nil || !eventMatchesObject(event, kind, "terminal", sessionID, challenge, pageIndex, relativePath, objectRaw) {
			return errors.New("authority ledger terminal index conflicts with the exact retained object")
		}
		if err := j.validateEventObject(event, indexedRaw, false); err != nil {
			return err
		}
		return j.ensurePreparedArchived(event)
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return readErr
	}
	intentKey := ledgerIntentKey(kind, challenge)
	startedIndex, err := j.eventIndexPath(challenge, kind, "started")
	if err != nil {
		return err
	}
	startedRaw, started, startedErr := j.readIndexedEvent(startedIndex)
	if startedErr == nil {
		if !eventMatchesObject(started, kind, "started", sessionID, challenge, pageIndex, relativePath, objectRaw) {
			return errors.New("authority ledger started index conflicts with the exact intended object")
		}
		if started.Payload.SegmentID != j.segmentID {
			return errors.New("authority ledger started intent is outside the loaded resumable tail")
		}
		pending, exists := j.pendingIntents[intentKey]
		if !exists || digestEvent(pending) != digest(startedRaw) {
			return errors.New("authority ledger started index is not present in the loaded pending tail")
		}
	} else if !errors.Is(startedErr, os.ErrNotExist) {
		return startedErr
	} else {
		if len(j.pendingIntents) != 0 {
			return errors.New("authority ledger must settle its existing pending intent before starting another publication")
		}
		if err := j.advanceSegment(now); err != nil {
			return err
		}
	}
	additionalEvents := uint64(1)
	if startedErr != nil {
		additionalEvents = 2
	}
	slotRaw, err := j.preparedSlotBytes(identity, objectRaw)
	if err != nil {
		return err
	}
	maximumAdditionalBytes := uint64(len(objectRaw))+uint64(len(slotRaw)) + additionalEvents*maximumLedgerEventBytes
	if j.config.LedgerExpectedMaximumEventsPerDay < additionalEvents ||
		j.nextSequence > j.config.LedgerExpectedMaximumEventsPerDay-additionalEvents ||
		j.config.LedgerExpectedMaximumBytesPerDay < maximumAdditionalBytes ||
		j.objectBytes > j.config.LedgerExpectedMaximumBytesPerDay-maximumAdditionalBytes {
		return errors.New("authority ledger publication exceeds its source-derived daily semantic-event or byte bound")
	}
	if err := ensureAuthorityLedgerCapacity(
		j.config,
		maximumAdditionalBytes,
		maximumPublicationInodes(kind, startedErr != nil),
	); err != nil {
		return err
	}
	_, preparedBytes, preparedCreated, err := j.stagePreparedObject(identity, objectRaw)
	if err != nil {
		return err
	}
	if preparedCreated {
		if j.config.LedgerExpectedMaximumBytesPerDay < preparedBytes ||
			j.objectBytes > j.config.LedgerExpectedMaximumBytesPerDay-preparedBytes {
			return errors.New("authority ledger prepared slot exceeds its signed daily byte bound")
		}
		j.objectBytes += preparedBytes
	}
	return j.completePreparedObject(identity, objectRaw, time.Now().UTC(), started, startedErr == nil)
}

func (j *authorityLedgerJournal) recoverPendingIntents(now time.Time) error {
	return j.recoverPreparedObjects(now)
}

func (j *authorityLedgerJournal) advanceSegment(now time.Time) error {
	desired := ledgerSegmentID(now)
	if desired < j.segmentID {
		return errors.New("authority ledger refuses a UTC clock rollback")
	}
	if desired == j.segmentID {
		if j.segmentFinalized {
			return errors.New("authority ledger refuses to append to a finalized segment")
		}
		return nil
	}
	currentTime, currentErr := time.Parse("2006-01-02", j.segmentID)
	desiredTime, desiredErr := time.Parse("2006-01-02", desired)
	if currentErr != nil || desiredErr != nil || desiredTime.Sub(currentTime) != 24*time.Hour {
		return errors.New("authority ledger refuses an automatic multi-segment forward clock jump")
	}
	if len(j.pendingIntents) != 0 {
		return errors.New("authority ledger cannot cross a UTC segment with an unresolved intent")
	}
	if err := j.finalizeLoadedSegment(j.segmentID, now); err != nil {
		return err
	}
	checkpointRaw, checkpoint, err := j.readCheckpoint(j.segmentID)
	if err != nil {
		return err
	}
	headRaw, err := j.ensureCheckpointHead(
		j.segmentID,
		digest(checkpointRaw),
		checkpoint.Payload.FinalizedAt,
		j.previousHeadSegment,
		j.previousHeadSHA256,
	)
	if err != nil {
		return err
	}
	j.previousCheckpointSegment = j.segmentID
	j.previousCheckpointSHA256 = digest(checkpointRaw)
	j.previousHeadSegment = j.segmentID
	j.previousHeadSHA256 = digest(headRaw)
	j.segmentID = desired
	j.nextSequence = 0
	j.previousEventSHA256 = ""
	j.objectBytes = 0
	j.pendingIntents = map[string]authorityLedgerEvent{}
	j.segmentFinalized = false
	return j.prepareSegment(desired)
}

func (j *authorityLedgerJournal) appendEvent(payload authorityLedgerEventPayload) (authorityLedgerEvent, []byte, error) {
	if j.nextSequence >= j.config.LedgerExpectedMaximumEventsPerDay {
		return authorityLedgerEvent{}, nil, errors.New("authority ledger segment reached its source-derived semantic-event bound")
	}
	payload.Schema = authorityLedgerEventSchema
	payload.ClusterID = j.config.ClusterID
	payload.DeploymentID = j.config.DeploymentID
	payload.LedgerEpochID = j.config.LedgerEpochID
	payload.SegmentID = j.segmentID
	payload.Sequence = j.nextSequence
	payload.PreviousEventSHA256 = j.previousEventSHA256
	eventPath := filepath.Join(j.eventsRoot(j.segmentID), fmt.Sprintf("event-%012d.json", j.nextSequence))
	if _, err := os.Lstat(eventPath); err == nil {
		return j.adoptPublishedEvent(eventPath, payload)
	} else if !errors.Is(err, os.ErrNotExist) {
		return authorityLedgerEvent{}, nil, err
	}
	recordedAt, err := time.Parse(time.RFC3339, payload.RecordedAt)
	if err != nil || recordedAt.Nanosecond() != 0 || !strings.HasSuffix(payload.RecordedAt, "Z") ||
		(!j.lastRecordedAt.IsZero() && recordedAt.Before(j.lastRecordedAt)) ||
		(payload.Phase == "started" && ledgerSegmentID(recordedAt) != j.segmentID) {
		return authorityLedgerEvent{}, nil, errors.New("authority ledger refuses a non-canonical or backwards event timestamp")
	}
	eventRaw, err := j.signEvent(payload)
	if err != nil {
		return authorityLedgerEvent{}, nil, err
	}
	if j.config.LedgerExpectedMaximumBytesPerDay < uint64(len(eventRaw)) ||
		j.objectBytes > j.config.LedgerExpectedMaximumBytesPerDay-uint64(len(eventRaw)) {
		return authorityLedgerEvent{}, nil, errors.New("authority ledger event exceeds its source-derived retained-byte bound")
	}
	_, event, err := j.decodeEvent(eventRaw)
	if err != nil {
		return authorityLedgerEvent{}, nil, err
	}
	if err := publishAnonymousRegular(eventPath, eventRaw, 0o400); err != nil {
		if adopted, adoptedRaw, adoptErr := j.adoptPublishedEvent(eventPath, payload); adoptErr == nil {
			return adopted, adoptedRaw, nil
		}
		return authorityLedgerEvent{}, nil, err
	}
	indexPath, err := j.eventIndexPath(payload.Challenge, payload.Kind, payload.Phase)
	if err != nil {
		return authorityLedgerEvent{}, nil, err
	}
	if err := j.linkEventIndex(eventPath, indexPath); err != nil {
		if adopted, adoptedRaw, adoptErr := j.adoptPublishedEvent(eventPath, payload); adoptErr == nil {
			return adopted, adoptedRaw, nil
		}
		return authorityLedgerEvent{}, nil, err
	}
	j.objectBytes += uint64(len(eventRaw))
	j.previousEventSHA256 = digest(eventRaw)
	j.lastRecordedAt = recordedAt
	j.nextSequence++
	return event, eventRaw, nil
}

func (j *authorityLedgerJournal) adoptPublishedEvent(path string, expected authorityLedgerEventPayload) (authorityLedgerEvent, []byte, error) {
	raw, event, err := j.readEvent(path)
	if err != nil {
		return authorityLedgerEvent{}, nil, err
	}
	actual := event.Payload
	expected.RecordedAt = actual.RecordedAt
	if actual != expected || actual.SegmentID != j.segmentID || actual.Sequence != j.nextSequence ||
		actual.PreviousEventSHA256 != j.previousEventSHA256 {
		return authorityLedgerEvent{}, nil, errors.New("authority ledger retained event conflicts with the exact pending append")
	}
	recordedAt, err := time.Parse(time.RFC3339, actual.RecordedAt)
	if err != nil || recordedAt.Nanosecond() != 0 || !strings.HasSuffix(actual.RecordedAt, "Z") ||
		(!j.lastRecordedAt.IsZero() && recordedAt.Before(j.lastRecordedAt)) ||
		(actual.Phase == "started" && ledgerSegmentID(recordedAt) != j.segmentID) {
		return authorityLedgerEvent{}, nil, errors.New("authority ledger retained event has invalid chronology")
	}
	if j.config.LedgerExpectedMaximumBytesPerDay < uint64(len(raw)) ||
		j.objectBytes > j.config.LedgerExpectedMaximumBytesPerDay-uint64(len(raw)) {
		return authorityLedgerEvent{}, nil, errors.New("authority ledger retained event exceeds its daily byte bound")
	}
	indexPath, err := j.eventIndexPath(actual.Challenge, actual.Kind, actual.Phase)
	if err != nil {
		return authorityLedgerEvent{}, nil, err
	}
	if err := j.linkEventIndex(path, indexPath); err != nil {
		return authorityLedgerEvent{}, nil, err
	}
	if err := fsyncDirectory(filepath.Dir(path)); err != nil {
		return authorityLedgerEvent{}, nil, err
	}
	if err := fsyncDirectory(filepath.Dir(indexPath)); err != nil {
		return authorityLedgerEvent{}, nil, err
	}
	j.objectBytes += uint64(len(raw))
	j.previousEventSHA256 = digest(raw)
	j.lastRecordedAt = recordedAt
	j.nextSequence++
	return event, raw, nil
}

func (j *authorityLedgerJournal) signEvent(payload authorityLedgerEventPayload) ([]byte, error) {
	payloadRaw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	signature := ed25519.Sign(j.privateKey, ledgerSignatureMessage(authorityLedgerEventSchema, j.config.SigningIssuer, j.config.SigningKeyID, payloadRaw))
	event := authorityLedgerEvent{
		Payload:   payload,
		Issuer:    j.config.SigningIssuer,
		KeyID:     j.config.SigningKeyID,
		Algorithm: "ed25519",
		Signature: base64.RawURLEncoding.EncodeToString(signature),
	}
	return json.Marshal(event)
}

func (j *authorityLedgerJournal) readEvent(path string) ([]byte, authorityLedgerEvent, error) {
	raw, err := readRootRegular(path, maximumLedgerEventBytes)
	if err != nil {
		return nil, authorityLedgerEvent{}, err
	}
	return j.decodeEvent(raw)
}

func (j *authorityLedgerJournal) decodeEvent(raw []byte) ([]byte, authorityLedgerEvent, error) {
	var event authorityLedgerEvent
	if err := canonicalJSON(raw, &event); err != nil {
		return nil, event, err
	}
	payloadRaw, err := json.Marshal(event.Payload)
		if err != nil || event.Payload.Schema != authorityLedgerEventSchema || event.Payload.ClusterID != j.config.ClusterID ||
			event.Payload.DeploymentID != j.config.DeploymentID || event.Payload.LedgerEpochID != j.config.LedgerEpochID || event.Issuer != j.config.SigningIssuer ||
		event.KeyID != j.config.SigningKeyID || event.Algorithm != "ed25519" || !ledgerObjectKind(event.Payload.Kind) ||
		(event.Payload.Phase != "started" && event.Payload.Phase != "terminal") || !canonicalNonce(event.Payload.SessionID) ||
		!canonicalNonce(event.Payload.Challenge) || event.Payload.PageIndex < 0 || event.Payload.PageIndex >= maximumAuthoritySessionPages ||
		!isDigest(event.Payload.ObjectSHA256) || event.Payload.ObjectBytes == 0 ||
		!isDigest(event.Payload.PreparedSHA256) || event.Payload.PreparedBytes == 0 ||
		event.Payload.PreparedBytes > maximumPreparedSlotBytes || event.Payload.ObjectPath == "" ||
		filepath.IsAbs(filepath.FromSlash(event.Payload.ObjectPath)) || event.Payload.ObjectPath == ".." || strings.HasPrefix(event.Payload.ObjectPath, "../") {
		return nil, event, errors.New("authority ledger event has an invalid signing identity")
	}
	signature, err := base64.RawURLEncoding.DecodeString(event.Signature)
	if err != nil || len(signature) != ed25519.SignatureSize || base64.RawURLEncoding.EncodeToString(signature) != event.Signature ||
		!ed25519.Verify(j.publicKey, ledgerSignatureMessage(authorityLedgerEventSchema, event.Issuer, event.KeyID, payloadRaw), signature) {
		return nil, event, errors.New("authority ledger event signature is invalid")
	}
	return raw, event, nil
}

func ledgerSignatureMessage(schema string, issuer string, keyID string, payloadRaw []byte) []byte {
	return bytes.Join([][]byte{[]byte(schema), []byte(issuer), []byte(keyID), []byte(digest(payloadRaw)), payloadRaw}, []byte("\n"))
}

func digestEvent(event authorityLedgerEvent) string {
	raw, err := json.Marshal(event)
	if err != nil {
		return ""
	}
	return digest(raw)
}

func ledgerIntentKey(kind string, challenge string) string {
	return kind + ":" + challenge
}

func ledgerObjectKind(kind string) bool {
	return kind == "reservation" || kind == "settlement" || kind == "challenge" || kind == "session-head" ||
		kind == "collection-outcome"
}

func canonicalLedgerTime(value time.Time) string {
	return value.UTC().Truncate(time.Second).Format(time.RFC3339)
}

func eventMatchesObject(event authorityLedgerEvent, kind string, phase string, sessionID string, challenge string, pageIndex int, relativePath string, objectRaw []byte) bool {
	return event.Payload.Kind == kind && event.Payload.Phase == phase && event.Payload.SessionID == sessionID &&
		event.Payload.Challenge == challenge && event.Payload.PageIndex == pageIndex && event.Payload.ObjectPath == relativePath &&
		event.Payload.ObjectSHA256 == digest(objectRaw) && event.Payload.ObjectBytes == uint64(len(objectRaw))
}

func (j *authorityLedgerJournal) readIndexedEvent(path string) ([]byte, authorityLedgerEvent, error) {
	raw, err := readRootRegular(path, maximumLedgerEventBytes)
	if err != nil {
		return nil, authorityLedgerEvent{}, err
	}
	return j.decodeEvent(raw)
}

func maximumPublicationInodes(kind string, includesStarted bool) uint64 {
	// Worst-case physical inodes for one publication. The pending prepared slot
	// and final object are distinct retained inodes; after terminal settlement the
	// prepared slot moves into two content-addressed archive shards. Direct event
	// indexes are hard links and allocate no additional inode.
	objectInodes := uint64(0)
	switch kind {
	case "reservation", "challenge":
		objectInodes = 6 // two destination shards, final object, two archive shards, prepared inode
	case "settlement":
		objectInodes = 7 // two shards, session directory, final object, two archive shards, prepared inode
	case "session-head":
		objectInodes = 5 // heads directory, final object, two archive shards, prepared inode
	case "collection-outcome":
		objectInodes = 6 // two outcome shards, final object, two archive shards, prepared inode
	}
	eventInodes := uint64(1) // terminal event
	if includesStarted {
		eventInodes++
	}
	return objectInodes + eventInodes + 2 // two event-index shards
}

func (j *authorityLedgerJournal) validateEventObject(event authorityLedgerEvent, eventRaw []byte, repairIndex bool) error {
	if !ledgerObjectKind(event.Payload.Kind) || event.Payload.Phase != "terminal" {
		return errors.New("authority ledger event contains an unsupported kind")
	}
	if !canonicalNonce(event.Payload.SessionID) || !canonicalNonce(event.Payload.Challenge) || event.Payload.PageIndex < 0 ||
		event.Payload.PageIndex >= maximumAuthoritySessionPages || !isDigest(event.Payload.ObjectSHA256) || event.Payload.ObjectBytes == 0 {
		return errors.New("authority ledger event object identity is invalid")
	}
	objectPath := filepath.Join(j.config.SettlementRoot, filepath.FromSlash(event.Payload.ObjectPath))
	relativePath, err := filepath.Rel(j.config.SettlementRoot, objectPath)
	if err != nil || filepath.IsAbs(relativePath) || relativePath == ".." || strings.HasPrefix(relativePath, ".."+string(filepath.Separator)) {
		return errors.New("authority ledger event object escapes its dedicated root")
	}
	objectRaw, err := readRootRegular(objectPath, maximumAuthorityLedgerRecordBytes)
	if err != nil || uint64(len(objectRaw)) != event.Payload.ObjectBytes || digest(objectRaw) != event.Payload.ObjectSHA256 {
		return errors.New("authority ledger event object is missing or differs from its signed digest")
	}
	indexPath, err := j.eventIndexPath(event.Payload.Challenge, event.Payload.Kind, event.Payload.Phase)
	if err != nil {
		return err
	}
	if repairIndex {
		eventPath := filepath.Join(j.eventsRoot(event.Payload.SegmentID), fmt.Sprintf("event-%012d.json", event.Payload.Sequence))
		if digest(eventRaw) == "" {
			return errors.New("authority ledger event cannot repair an empty digest")
		}
		return j.linkEventIndex(eventPath, indexPath)
	}
	indexedInfo, err := os.Lstat(indexPath)
	eventInfo, eventErr := os.Lstat(filepath.Join(j.eventsRoot(event.Payload.SegmentID), fmt.Sprintf("event-%012d.json", event.Payload.Sequence)))
	if err != nil || eventErr != nil || !indexedInfo.Mode().IsRegular() || !eventInfo.Mode().IsRegular() || !os.SameFile(indexedInfo, eventInfo) {
		return errors.New("authority ledger direct event index is missing or not linked to the signed event")
	}
	return nil
}

func (j *authorityLedgerJournal) linkEventIndex(eventPath string, indexPath string) error {
	baseName := filepath.Base(indexPath)
	if len(baseName) < 65 || !canonicalNonce(baseName[:64]) {
		return errors.New("authority ledger event index filename is invalid")
	}
	if err := prepareAuthorityShard(j.config, "event-index", baseName[:64]); err != nil {
		return err
	}
	if err := os.Link(eventPath, indexPath); err != nil {
		if !errors.Is(err, os.ErrExist) {
			return err
		}
		eventInfo, eventErr := os.Lstat(eventPath)
		indexInfo, indexErr := os.Lstat(indexPath)
			if eventErr != nil || indexErr != nil || !os.SameFile(eventInfo, indexInfo) {
				return errors.New("authority ledger event index conflicts with another retained event")
			}
			return fsyncDirectory(filepath.Dir(indexPath))
	}
	return fsyncDirectory(filepath.Dir(indexPath))
}

func (j *authorityLedgerJournal) eventIndexPath(challenge string, kind string, phase string) (string, error) {
	if !canonicalNonce(challenge) || !ledgerObjectKind(kind) || (phase != "started" && phase != "terminal") {
		return "", errors.New("authority ledger event index identity is invalid")
	}
	return filepath.Join(j.config.SettlementRoot, "event-index", challenge[:2], challenge[2:4], challenge+"-"+kind+"-"+phase+".json"), nil
}

func (j *authorityLedgerJournal) prepareSegment(segmentID string) error {
	if !canonicalSegmentID(segmentID) {
		return errors.New("authority ledger segment ID is not canonical")
	}
	segmentsRoot := filepath.Join(j.config.SettlementRoot, "segments")
	if err := ensureRootOwnedChildDirectory(j.config.SettlementRoot, "segments"); err != nil {
		return err
	}
	if err := ensureRootOwnedChildDirectory(segmentsRoot, segmentID); err != nil {
		return err
	}
	for _, name := range []string{"events", "prepared", "checkpoints"} {
		if err := ensureRootOwnedChildDirectory(j.segmentRoot(segmentID), name); err != nil {
			return err
		}
	}
	if err := ensureRootOwnedChildDirectory(j.preparedRoot(segmentID), "archive"); err != nil {
		return err
	}
	return nil
}

func canonicalSegmentID(value string) bool {
	parsed, err := time.Parse("2006-01-02", value)
	return err == nil && parsed.Format("2006-01-02") == value
}

func (j *authorityLedgerJournal) segmentRoot(segmentID string) string {
	return filepath.Join(j.config.SettlementRoot, "segments", segmentID)
}

func (j *authorityLedgerJournal) eventsRoot(segmentID string) string {
	return filepath.Join(j.segmentRoot(segmentID), "events")
}

func (j *authorityLedgerJournal) checkpointPath(segmentID string) string {
	return filepath.Join(j.segmentRoot(segmentID), "checkpoints", "checkpoint.json")
}

func (j *authorityLedgerJournal) checkpointHeadPath(segmentID string) string {
	return filepath.Join(j.config.SettlementRoot, "checkpoint-heads", "head-"+segmentID+".json")
}

func (j *authorityLedgerJournal) ensureCheckpointHead(segmentID string, checkpointSHA256 string, finalizedAt string, previousSegment string, previousSHA256 string) ([]byte, error) {
	if !canonicalSegmentID(segmentID) || !isDigest(checkpointSHA256) ||
		(previousSegment == "") != (previousSHA256 == "") ||
		(previousSegment != "" && (!canonicalSegmentID(previousSegment) || previousSegment >= segmentID || !isDigest(previousSHA256))) {
		return nil, errors.New("authority ledger global head identity is invalid")
	}
	if err := ensureAuthorityLedgerCapacity(j.config, maximumLedgerHeadBytes, 3); err != nil {
		return nil, err
	}
	if err := ensureRootOwnedChildDirectory(j.config.SettlementRoot, "checkpoint-heads"); err != nil {
		return nil, err
	}
	payload := authorityLedgerHeadPayload{
		Schema: authorityLedgerHeadSchema,
		ClusterID: j.config.ClusterID,
		DeploymentID: j.config.DeploymentID,
		LedgerEpochID: j.config.LedgerEpochID,
		SegmentID: segmentID,
		CheckpointSHA256: checkpointSHA256,
		PreviousHeadSegment: previousSegment,
		PreviousHeadSHA256: previousSHA256,
		CheckpointFinalizedAt: finalizedAt,
	}
	if previousSegment == "" {
		payload.PreviousEpochID = j.config.PreviousLedgerEpochID
		payload.PreviousEpochHeadSHA256 = j.config.PreviousLedgerFinalHeadSHA256
	}
	payloadRaw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	head := authorityLedgerHead{
		Payload: payload,
		Issuer: j.config.SigningIssuer,
		KeyID: j.config.SigningKeyID,
		Algorithm: "ed25519",
		Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(j.privateKey, ledgerSignatureMessage(authorityLedgerHeadSchema, j.config.SigningIssuer, j.config.SigningKeyID, payloadRaw))),
	}
	raw, err := json.Marshal(head)
	if err != nil || len(raw) > maximumLedgerHeadBytes {
		return nil, errors.New("authority ledger global head exceeds its exact bound")
	}
	path := j.checkpointHeadPath(segmentID)
	if err := publishAnonymousRegular(path, raw, 0o400); err != nil {
		retainedRaw, retained, readErr := j.readCheckpointHead(segmentID)
		if readErr != nil || !bytes.Equal(retainedRaw, raw) || retained.Payload != payload {
			return nil, err
		}
		if syncErr := fsyncDirectory(filepath.Dir(path)); syncErr != nil {
			return nil, syncErr
		}
		return retainedRaw, nil
	}
	return raw, nil
}

func (j *authorityLedgerJournal) readCheckpointHead(segmentID string) ([]byte, authorityLedgerHead, error) {
	raw, err := readRootRegular(j.checkpointHeadPath(segmentID), maximumLedgerHeadBytes)
	if err != nil {
		return nil, authorityLedgerHead{}, err
	}
	var head authorityLedgerHead
	if err := canonicalJSON(raw, &head); err != nil {
		return nil, head, err
	}
	payloadRaw, err := json.Marshal(head.Payload)
	if err != nil || head.Payload.Schema != authorityLedgerHeadSchema ||
		head.Payload.ClusterID != j.config.ClusterID || head.Payload.DeploymentID != j.config.DeploymentID ||
		head.Payload.LedgerEpochID != j.config.LedgerEpochID ||
		head.Payload.SegmentID != segmentID || !isDigest(head.Payload.CheckpointSHA256) ||
		(head.Payload.PreviousHeadSegment == "") != (head.Payload.PreviousHeadSHA256 == "") ||
		(head.Payload.PreviousEpochID == "") != (head.Payload.PreviousEpochHeadSHA256 == "") ||
		(head.Payload.PreviousEpochID != "" && (!isDigest(head.Payload.PreviousEpochID) || !isDigest(head.Payload.PreviousEpochHeadSHA256))) ||
		(head.Payload.PreviousHeadSegment != "" && (head.Payload.PreviousEpochID != "" || head.Payload.PreviousEpochHeadSHA256 != "")) ||
		(head.Payload.PreviousHeadSegment == "" && (head.Payload.PreviousEpochID != j.config.PreviousLedgerEpochID ||
			head.Payload.PreviousEpochHeadSHA256 != j.config.PreviousLedgerFinalHeadSHA256)) ||
		(head.Payload.PreviousHeadSegment != "" && (!canonicalSegmentID(head.Payload.PreviousHeadSegment) ||
			head.Payload.PreviousHeadSegment >= segmentID || !isDigest(head.Payload.PreviousHeadSHA256))) ||
		head.Issuer != j.config.SigningIssuer || head.KeyID != j.config.SigningKeyID || head.Algorithm != "ed25519" {
		return nil, head, errors.New("authority ledger global head payload is invalid")
	}
	finalizedAt, timeErr := time.Parse(time.RFC3339, head.Payload.CheckpointFinalizedAt)
	if timeErr != nil || finalizedAt.Nanosecond() != 0 || !strings.HasSuffix(head.Payload.CheckpointFinalizedAt, "Z") {
		return nil, head, errors.New("authority ledger global head chronology is invalid")
	}
	signature, err := base64.RawURLEncoding.DecodeString(head.Signature)
	if err != nil || len(signature) != ed25519.SignatureSize || base64.RawURLEncoding.EncodeToString(signature) != head.Signature ||
		!ed25519.Verify(j.publicKey, ledgerSignatureMessage(authorityLedgerHeadSchema, head.Issuer, head.KeyID, payloadRaw), signature) {
		return nil, head, errors.New("authority ledger global head signature is invalid")
	}
	checkpointRaw, checkpoint, err := j.readCheckpoint(segmentID)
	if err != nil || digest(checkpointRaw) != head.Payload.CheckpointSHA256 || checkpoint.Payload.FinalizedAt != head.Payload.CheckpointFinalizedAt {
		return nil, head, errors.New("authority ledger global head does not bind its exact retained checkpoint")
	}
	return raw, head, nil
}

func (j *authorityLedgerJournal) finalizeLoadedSegment(segmentID string, now time.Time) error {
	if segmentID == ledgerSegmentID(now) {
		return errors.New("authority ledger refuses to finalize the current UTC segment")
	}
	if j.segmentID != segmentID || len(j.pendingIntents) != 0 || j.nextSequence > j.config.LedgerExpectedMaximumEventsPerDay ||
		j.objectBytes > j.config.LedgerExpectedMaximumBytesPerDay {
		return errors.New("authority ledger refuses to checkpoint a different, unresolved or over-budget segment")
	}
	if _, err := os.Lstat(j.checkpointPath(segmentID)); err == nil {
		raw, checkpoint, err := j.readCheckpoint(segmentID)
		if err != nil || checkpoint.Payload.EventCount != j.nextSequence || checkpoint.Payload.ObjectBytes != j.objectBytes ||
			checkpoint.Payload.FinalEventSHA256 != j.previousEventSHA256 ||
			checkpoint.Payload.PreviousCheckpointSegment != j.previousCheckpointSegment ||
			checkpoint.Payload.PreviousCheckpointSHA256 != j.previousCheckpointSHA256 || digest(raw) == "" {
			return errors.New("authority ledger existing checkpoint differs from its exact loaded tail")
		}
		j.segmentFinalized = true
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := ensureAuthorityLedgerCapacity(j.config, maximumLedgerCheckpointBytes, 2); err != nil {
		return err
	}
	payload := authorityLedgerCheckpointPayload{
		Schema:                    authorityLedgerCheckpointSchema,
		ClusterID:                 j.config.ClusterID,
		DeploymentID:              j.config.DeploymentID,
		LedgerEpochID:             j.config.LedgerEpochID,
		SegmentID:                 segmentID,
		EventCount:                j.nextSequence,
		ObjectBytes:               j.objectBytes,
		FinalEventSHA256:          j.previousEventSHA256,
		PreviousCheckpointSegment: j.previousCheckpointSegment,
		PreviousCheckpointSHA256:  j.previousCheckpointSHA256,
		FinalizedAt:               now.UTC().Truncate(time.Second).Format(time.RFC3339),
	}
	payloadRaw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	checkpoint := authorityLedgerCheckpoint{
		Payload:   payload,
		Issuer:    j.config.SigningIssuer,
		KeyID:     j.config.SigningKeyID,
		Algorithm: "ed25519",
		Signature: base64.RawURLEncoding.EncodeToString(ed25519.Sign(j.privateKey, ledgerSignatureMessage(authorityLedgerCheckpointSchema, j.config.SigningIssuer, j.config.SigningKeyID, payloadRaw))),
	}
	raw, err := json.Marshal(checkpoint)
	if err != nil {
		return err
	}
	if err := publishAnonymousRegular(j.checkpointPath(segmentID), raw, 0o400); err != nil {
		retainedRaw, retained, readErr := j.readCheckpoint(segmentID)
		if readErr != nil || !bytes.Equal(retainedRaw, raw) || retained.Payload != payload {
			return err
		}
		if syncErr := fsyncDirectory(filepath.Dir(j.checkpointPath(segmentID))); syncErr != nil {
			return syncErr
		}
	}
	j.segmentFinalized = true
	return nil
}

func (j *authorityLedgerJournal) readCheckpoint(segmentID string) ([]byte, authorityLedgerCheckpoint, error) {
	raw, err := readRootRegular(j.checkpointPath(segmentID), maximumLedgerCheckpointBytes)
	if err != nil {
		return nil, authorityLedgerCheckpoint{}, err
	}
	var checkpoint authorityLedgerCheckpoint
	if err := canonicalJSON(raw, &checkpoint); err != nil {
		return nil, checkpoint, err
	}
	payloadRaw, err := json.Marshal(checkpoint.Payload)
	if err != nil || checkpoint.Payload.Schema != authorityLedgerCheckpointSchema || checkpoint.Payload.ClusterID != j.config.ClusterID ||
		checkpoint.Payload.DeploymentID != j.config.DeploymentID || checkpoint.Payload.LedgerEpochID != j.config.LedgerEpochID || checkpoint.Payload.SegmentID != segmentID ||
		checkpoint.Issuer != j.config.SigningIssuer || checkpoint.KeyID != j.config.SigningKeyID || checkpoint.Algorithm != "ed25519" ||
		(checkpoint.Payload.EventCount == 0 && checkpoint.Payload.FinalEventSHA256 != "") ||
		(checkpoint.Payload.EventCount > 0 && !isDigest(checkpoint.Payload.FinalEventSHA256)) ||
		(checkpoint.Payload.PreviousCheckpointSegment == "") != (checkpoint.Payload.PreviousCheckpointSHA256 == "") ||
		checkpoint.Payload.EventCount > j.config.LedgerExpectedMaximumEventsPerDay ||
		checkpoint.Payload.ObjectBytes > j.config.LedgerExpectedMaximumBytesPerDay ||
		(checkpoint.Payload.PreviousCheckpointSegment != "" && (!canonicalSegmentID(checkpoint.Payload.PreviousCheckpointSegment) ||
			checkpoint.Payload.PreviousCheckpointSegment >= segmentID)) {
		return nil, checkpoint, errors.New("authority ledger checkpoint payload is invalid")
	}
	finalizedAt, err := time.Parse(time.RFC3339, checkpoint.Payload.FinalizedAt)
	segmentStart, segmentErr := time.Parse("2006-01-02", segmentID)
	if err != nil || segmentErr != nil || finalizedAt.Nanosecond() != 0 || !strings.HasSuffix(checkpoint.Payload.FinalizedAt, "Z") ||
		finalizedAt.Before(segmentStart.Add(24*time.Hour)) {
		return nil, checkpoint, errors.New("authority ledger checkpoint chronology is invalid")
	}
	signature, err := base64.RawURLEncoding.DecodeString(checkpoint.Signature)
	if err != nil || len(signature) != ed25519.SignatureSize || base64.RawURLEncoding.EncodeToString(signature) != checkpoint.Signature ||
		!ed25519.Verify(j.publicKey, ledgerSignatureMessage(authorityLedgerCheckpointSchema, checkpoint.Issuer, checkpoint.KeyID, payloadRaw), signature) {
		return nil, checkpoint, errors.New("authority ledger checkpoint signature is invalid")
	}
	if checkpoint.Payload.PreviousCheckpointSegment != "" {
		previousRaw, err := readRootRegular(j.checkpointPath(checkpoint.Payload.PreviousCheckpointSegment), maximumLedgerCheckpointBytes)
		if err != nil || digest(previousRaw) != checkpoint.Payload.PreviousCheckpointSHA256 {
			return nil, checkpoint, errors.New("authority ledger checkpoint does not extend its retained predecessor")
		}
	}
	return raw, checkpoint, nil
}

func parseEventSequence(name string) (uint64, bool) {
	if !strings.HasPrefix(name, "event-") || !strings.HasSuffix(name, ".json") || len(name) != len("event-000000000000.json") {
		return 0, false
	}
	sequence, err := strconv.ParseUint(strings.TrimSuffix(strings.TrimPrefix(name, "event-"), ".json"), 10, 64)
	return sequence, err == nil && name == fmt.Sprintf("event-%012d.json", sequence)
}
