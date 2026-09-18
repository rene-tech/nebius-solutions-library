package boundary

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

// Provider serializes snapshot reloads and serves only a previously verified,
// still-current Runtime. A malformed replacement cannot evict the last good
// snapshot. The failed file identity is remembered so hostile input is not
// rehashed or reverified on every admission request.
type Provider struct {
	mu                              sync.Mutex
	trustPath                       string
	snapshotPath                    string
	previousSnapshotPath            string
	activationPath                  string
	selectionPath                   string
	generationRoot                  string
	expectedTrustSHA256             string
	expectedClusterID               string
	expectedDeploymentID            string
	expectedAuthoritySnapshotID     string
	expectedAuthorityClosureSHA256  string
	current                         *Runtime
	currentKey                      regularFileKey
	currentActivationKey            regularFileKey
	failedKey                       regularFileKey
	failedActivationKey             regularFileKey
	failedErr                       error
}

const snapshotActivationReceiptSchema = "fs2-serve.nebius.ai/public-edge-snapshot-activation-receipt/v1"
const snapshotRuntimeSelectionSchema = "fs2-serve.nebius.ai/public-edge-snapshot-runtime-selection/v1"
const maximumSnapshotRuntimeSelectionBytes = 16 * 1024

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
	PredecessorSelectionSHA256 string                `json:"predecessor_selection_sha256"`
	Activation             snapshotActivationReceipt `json:"activation"`
}

type regularFileKey struct {
	device      uint64
	inode       uint64
	size        int64
	modifiedNS  int64
}

func NewProvider(
	trustPath string,
	snapshotPath string,
	expectedTrustSHA256 string,
	expectedClusterID string,
	expectedDeploymentID string,
	expectedAuthoritySnapshotID string,
	expectedAuthorityClosureSHA256 string,
) *Provider {
	return &Provider{
		trustPath:                       trustPath,
		snapshotPath:                    snapshotPath,
		previousSnapshotPath:            filepath.Join(filepath.Dir(snapshotPath), "snapshot-previous.json"),
		activationPath:                  filepath.Join(filepath.Dir(snapshotPath), "snapshot-activation.json"),
		selectionPath:                   filepath.Join(filepath.Dir(snapshotPath), "snapshot-runtime-selection.json"),
		generationRoot:                  filepath.Join(filepath.Dir(snapshotPath), "snapshot-generations"),
		expectedTrustSHA256:             expectedTrustSHA256,
		expectedClusterID:               expectedClusterID,
		expectedDeploymentID:            expectedDeploymentID,
		expectedAuthoritySnapshotID:     expectedAuthoritySnapshotID,
		expectedAuthorityClosureSHA256:  expectedAuthorityClosureSHA256,
	}
}

func (p *Provider) Runtime(now time.Time) (*Runtime, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	selection, activationKey, err := p.loadRuntimeSelection(now)
	if err != nil {
		if p.current != nil && p.current.Current(now) {
			return p.current, nil
		}
		return nil, err
	}
	activeRaw, key, err := readProtectedRegularWithKey(
		filepath.Join(p.generationRoot, selection.SnapshotEnvelopeName),
		maxSnapshotBytes,
	)
	receipt := selection.Activation
	if err != nil || digestHex(activeRaw) != selection.SnapshotEnvelopeSHA256 ||
		selection.SnapshotEnvelopeSHA256 != receipt.SnapshotEnvelopeSHA256 {
		if p.current != nil && p.current.Current(now) {
			return p.current, nil
		}
		return nil, errors.New("active snapshot does not match its exact activation receipt")
	}
	if p.current != nil && key == p.currentKey && activationKey == p.currentActivationKey {
		if p.current.Current(now) {
			return p.current, nil
		}
		return nil, errors.New("cached boundary snapshot is expired")
	}
	if p.failedErr != nil && key == p.failedKey && activationKey == p.failedActivationKey {
		if p.current != nil && p.current.Current(now) {
			return p.current, nil
		}
		return nil, p.failedErr
	}

	trustRaw, err := readProtectedRegular(p.trustPath, maxTrustBytes)
	if err != nil {
		if p.current != nil && p.current.Current(now) {
			return p.current, nil
		}
		return nil, err
	}
	candidate, err := LoadRuntimeFromBytes(
		trustRaw,
		activeRaw,
		p.expectedTrustSHA256,
		p.expectedClusterID,
		p.expectedDeploymentID,
		p.expectedAuthoritySnapshotID,
		p.expectedAuthorityClosureSHA256,
		now,
	)
	if err != nil {
		p.failedKey = key
		p.failedActivationKey = activationKey
		p.failedErr = err
		if p.current != nil && p.current.Current(now) {
			return p.current, nil
		}
		return nil, err
	}
	if digestHex(activeRaw) != receipt.SnapshotEnvelopeSHA256 ||
		candidate.Snapshot.EvidenceBundleSHA256 != receipt.EvidenceBundleSHA256 {
		return nil, errors.New("active snapshot payload differs from its activation receipt evidence bundle")
	}
	if candidate.Snapshot.ActivationCycleContractSHA256 != receipt.CycleContractSHA256 ||
		candidate.Snapshot.ActivationCycleID != receipt.CycleID ||
		candidate.Snapshot.ActivationCycleIssuedAt != receipt.CycleIssuedAt ||
		candidate.Snapshot.ActivationCycleDeadlineAt != receipt.CycleDeadlineAt ||
		candidate.Snapshot.ActivationPredecessorSelectionSHA256 != selection.PredecessorSelectionSHA256 ||
		receipt.PredecessorSelectionSHA256 != selection.PredecessorSelectionSHA256 {
		return nil, errors.New("active runtime selection differs from its signed predecessor and cycle transition")
	}
	if p.current != nil && candidate.Snapshot.SnapshotID != p.current.Snapshot.SnapshotID {
		candidateIssuedAt, candidateErr := parseWholeUTC(candidate.Snapshot.IssuedAt)
		currentIssuedAt, currentErr := parseWholeUTC(p.current.Snapshot.IssuedAt)
		if candidateErr != nil || currentErr != nil || !candidateIssuedAt.After(currentIssuedAt) {
			if p.current.Current(now) {
				return p.current, nil
			}
			return nil, errors.New("runtime selector attempts to move below the cached signed snapshot high-water")
		}
	}
	p.current = candidate
	p.currentKey = key
	p.currentActivationKey = activationKey
	p.failedKey = regularFileKey{}
	p.failedActivationKey = regularFileKey{}
	p.failedErr = nil
	return candidate, nil
}

func (p *Provider) loadRuntimeSelection(now time.Time) (snapshotRuntimeSelection, regularFileKey, error) {
	raw, key, err := readProtectedRegularWithKey(p.selectionPath, maximumSnapshotRuntimeSelectionBytes)
	if errors.Is(err, os.ErrNotExist) {
		raw, key, err = readProtectedRegularWithKey(
			filepath.Join(filepath.Dir(p.selectionPath), "snapshot-selection-successor-of-genesis.json"),
			maximumSnapshotRuntimeSelectionBytes,
		)
	}
	if err != nil {
		return snapshotRuntimeSelection{}, regularFileKey{}, err
	}
	// The mutable fixed name is only an accelerator. It cannot establish a head
	// after restart unless the exact same bytes are present at their immutable,
	// no-replace successor-of-predecessor commit path.
	var hint snapshotRuntimeSelection
	if err := decodeExactJSON(raw, &hint); err != nil {
		return snapshotRuntimeSelection{}, regularFileKey{}, err
	}
	predecessorKey := hint.PredecessorSelectionSHA256
	if predecessorKey == "" {
		predecessorKey = "genesis"
	} else if !isSHA256(predecessorKey) {
		return snapshotRuntimeSelection{}, regularFileKey{}, errors.New("runtime selection hint has an invalid predecessor")
	}
	committedPath := filepath.Join(filepath.Dir(p.selectionPath), "snapshot-selection-successor-of-"+predecessorKey+".json")
	committedRaw, committedKey, committedErr := readProtectedRegularWithKey(committedPath, maximumSnapshotRuntimeSelectionBytes)
	if committedErr != nil || !bytes.Equal(committedRaw, raw) {
		return snapshotRuntimeSelection{}, regularFileKey{}, errors.New("runtime selection accelerator is not its exact immutable committed successor")
	}
	raw = committedRaw
	key = committedKey
	for depth := 0; depth < 1024; depth++ {
		selection, decodeErr := p.decodeRuntimeSelection(raw, now)
		if decodeErr != nil {
			return snapshotRuntimeSelection{}, regularFileKey{}, decodeErr
		}
		nextPath := filepath.Join(filepath.Dir(p.selectionPath), "snapshot-selection-successor-of-"+digestHex(raw)+".json")
		nextRaw, nextKey, nextErr := readProtectedRegularWithKey(nextPath, maximumSnapshotRuntimeSelectionBytes)
		if errors.Is(nextErr, os.ErrNotExist) {
			return selection, key, nil
		}
		if nextErr != nil {
			return snapshotRuntimeSelection{}, regularFileKey{}, nextErr
		}
		var next snapshotRuntimeSelection
		if err := decodeExactJSON(nextRaw, &next); err != nil || next.PredecessorSelectionSHA256 != digestHex(raw) {
			return snapshotRuntimeSelection{}, regularFileKey{}, errors.New("runtime selection successor does not bind the exact current head")
		}
		raw = nextRaw
		key = nextKey
	}
	return snapshotRuntimeSelection{}, regularFileKey{}, errors.New("runtime selection successor chain exceeds its bounded recovery depth")
}

func (p *Provider) decodeRuntimeSelection(raw []byte, now time.Time) (snapshotRuntimeSelection, error) {
	var selection snapshotRuntimeSelection
	if err := decodeExactJSON(raw, &selection); err != nil {
		return snapshotRuntimeSelection{}, err
	}
	canonical, err := json.Marshal(selection)
	expectedName := "snapshot-envelope-" + selection.SnapshotEnvelopeSHA256 + ".json"
	if err != nil || !bytes.Equal(canonical, raw) || selection.Schema != snapshotRuntimeSelectionSchema ||
		!isSHA256(selection.SnapshotEnvelopeSHA256) || selection.SnapshotEnvelopeName != expectedName ||
		filepath.Base(selection.SnapshotEnvelopeName) != selection.SnapshotEnvelopeName ||
		selection.Activation.SnapshotEnvelopeSHA256 != selection.SnapshotEnvelopeSHA256 ||
		selection.Activation.PredecessorSelectionSHA256 != selection.PredecessorSelectionSHA256 ||
		(selection.PredecessorSelectionSHA256 != "" && !isSHA256(selection.PredecessorSelectionSHA256)) {
		return snapshotRuntimeSelection{}, errors.New("snapshot runtime selection is not one canonical complete envelope/receipt pair")
	}
	if err := p.validateActivationReceipt(selection.Activation, now); err != nil {
		return snapshotRuntimeSelection{}, err
	}
	envelopeRaw, err := readProtectedRegular(
		filepath.Join(p.generationRoot, selection.SnapshotEnvelopeName),
		maxSnapshotBytes,
	)
	if err != nil || digestHex(envelopeRaw) != selection.SnapshotEnvelopeSHA256 {
		return snapshotRuntimeSelection{}, errors.New("historical runtime selector lacks its exact immutable envelope")
	}
	trustRaw, err := readProtectedRegular(p.trustPath, maxTrustBytes)
	if err != nil {
		return snapshotRuntimeSelection{}, err
	}
	runtime, err := LoadRuntimeFromBytesForChainRecovery(
		trustRaw,
		envelopeRaw,
		p.expectedTrustSHA256,
		p.expectedClusterID,
		p.expectedDeploymentID,
		p.expectedAuthoritySnapshotID,
		p.expectedAuthorityClosureSHA256,
		now,
	)
	if err != nil || runtime.Snapshot.ActivationCycleContractSHA256 != selection.Activation.CycleContractSHA256 ||
		runtime.Snapshot.ActivationCycleID != selection.Activation.CycleID ||
		runtime.Snapshot.ActivationCycleIssuedAt != selection.Activation.CycleIssuedAt ||
		runtime.Snapshot.ActivationCycleDeadlineAt != selection.Activation.CycleDeadlineAt ||
		runtime.Snapshot.ActivationPredecessorSelectionSHA256 != selection.PredecessorSelectionSHA256 ||
		runtime.Snapshot.EvidenceBundleSHA256 != selection.Activation.EvidenceBundleSHA256 {
		return snapshotRuntimeSelection{}, errors.New("historical runtime selector differs from its signed activation transition")
	}
	return selection, nil
}

func (p *Provider) loadPrevious(now time.Time, currentErr error) (*Runtime, error) {
	previousKey, err := protectedFileKey(p.previousSnapshotPath)
	if err != nil {
		return nil, currentErr
	}
	if p.current != nil && p.currentKey == previousKey && p.current.Current(now) {
		return p.current, nil
	}
	previous, err := LoadRuntime(
		p.trustPath,
		p.previousSnapshotPath,
		p.expectedTrustSHA256,
		p.expectedClusterID,
		p.expectedDeploymentID,
		p.expectedAuthoritySnapshotID,
		p.expectedAuthorityClosureSHA256,
		now,
	)
	if err != nil {
		return nil, currentErr
	}
	p.current = previous
	p.currentKey = previousKey
	p.currentActivationKey = regularFileKey{}
	return previous, nil
}

func (p *Provider) loadActivationReceipt(now time.Time) (snapshotActivationReceipt, regularFileKey, error) {
	raw, key, err := readProtectedRegularWithKey(p.activationPath, maxTrustBytes)
	if err != nil {
		return snapshotActivationReceipt{}, regularFileKey{}, err
	}
	var receipt snapshotActivationReceipt
	if err := decodeExactJSON(raw, &receipt); err != nil {
		return snapshotActivationReceipt{}, regularFileKey{}, err
	}
	canonical, err := json.Marshal(receipt)
	if err != nil || !bytes.Equal(canonical, raw) {
		return snapshotActivationReceipt{}, regularFileKey{}, errors.New("snapshot activation receipt is not canonical JSON")
	}
	if err := p.validateActivationReceipt(receipt, now); err != nil {
		return snapshotActivationReceipt{}, regularFileKey{}, err
	}
	return receipt, key, nil
}

func (p *Provider) validateActivationReceipt(receipt snapshotActivationReceipt, now time.Time) error {
	issuedAt, issuedErr := parseWholeUTC(receipt.CycleIssuedAt)
	deadlineAt, deadlineErr := parseWholeUTC(receipt.CycleDeadlineAt)
	activatedAt, activationErr := parseWholeUTC(receipt.ActivatedAt)
	if issuedErr != nil || deadlineErr != nil || activationErr != nil ||
		receipt.Schema != snapshotActivationReceiptSchema || receipt.ClusterID != p.expectedClusterID ||
		receipt.DeploymentID != p.expectedDeploymentID || !isSHA256(receipt.CycleContractSHA256) || !isSHA256(receipt.CycleID) ||
		!isSHA256(receipt.EvidenceBundleSHA256) || !isSHA256(receipt.SnapshotEnvelopeSHA256) ||
		(receipt.PredecessorSelectionSHA256 != "" && !isSHA256(receipt.PredecessorSelectionSHA256)) ||
		receipt.Status != "activated-before-deadline" || !deadlineAt.After(issuedAt) || activatedAt.Before(issuedAt) ||
		!activatedAt.Before(deadlineAt) || now.Before(activatedAt.Add(-30*time.Second)) {
		return errors.New("snapshot activation receipt is not canonical or did not commit inside its signed cycle")
	}
	return nil
}

// readProtectedRegularWithKey returns bytes and identity from the same opened
// descriptor. It avoids a lstat/read/reopen sequence in which a privileged
// writer could rotate a valid file between the receipt digest check and runtime
// construction while leaving the cache keyed to stale metadata.
func readProtectedRegularWithKey(path string, maximum int64) ([]byte, regularFileKey, error) {
	if err := requireProtectedDirectoryAncestry(filepath.Dir(path)); err != nil {
		return nil, regularFileKey{}, err
	}
	fileDescriptor, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, regularFileKey{}, err
	}
	file := os.NewFile(uintptr(fileDescriptor), path)
	defer file.Close()
	info, err := file.Stat()
	if err != nil || info == nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o022 != 0 ||
		info.Size() < 1 || info.Size() > maximum {
		return nil, regularFileKey{}, errors.New("protected input is not a bounded non-writable regular file")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 {
		return nil, regularFileKey{}, errors.New("protected input is not owned by root")
	}
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	after, statErr := file.Stat()
	if err != nil || statErr != nil || after == nil || int64(len(raw)) > maximum ||
		!os.SameFile(info, after) || int64(len(raw)) != after.Size() {
		return nil, regularFileKey{}, errors.New("protected input changed during its bounded descriptor read")
	}
	return raw, regularFileKey{
		device:     uint64(stat.Dev),
		inode:      stat.Ino,
		size:       info.Size(),
		modifiedNS: info.ModTime().UnixNano(),
	}, nil
}

func protectedFileKey(path string) (regularFileKey, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return regularFileKey{}, err
	}
	if !info.Mode().IsRegular() || info.Mode().Perm()&0o022 != 0 || info.Size() < 1 || info.Size() > maxSnapshotBytes {
		return regularFileKey{}, errors.New("snapshot is not a bounded non-writable regular file")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 {
		return regularFileKey{}, errors.New("snapshot is not owned by root")
	}
	return regularFileKey{
		device:     uint64(stat.Dev),
		inode:      stat.Ino,
		size:       info.Size(),
		modifiedNS: info.ModTime().UnixNano(),
	}, nil
}
