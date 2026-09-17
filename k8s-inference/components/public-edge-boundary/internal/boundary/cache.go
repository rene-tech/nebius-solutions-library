package boundary

import (
	"errors"
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
	expectedTrustSHA256             string
	expectedClusterID               string
	expectedDeploymentID            string
	expectedAuthoritySnapshotID     string
	expectedAuthorityClosureSHA256  string
	current                         *Runtime
	currentKey                      regularFileKey
	failedKey                       regularFileKey
	failedErr                       error
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

	key, err := protectedFileKey(p.snapshotPath)
	if err != nil {
		if p.current != nil && p.current.Current(now) {
			return p.current, nil
		}
		return p.loadPrevious(now, err)
	}
	if p.current != nil && key == p.currentKey {
		if p.current.Current(now) {
			return p.current, nil
		}
		return nil, errors.New("cached boundary snapshot is expired")
	}
	if p.failedErr != nil && key == p.failedKey {
		if p.current != nil && p.current.Current(now) {
			return p.current, nil
		}
		return nil, p.failedErr
	}

	candidate, err := LoadRuntime(
		p.trustPath,
		p.snapshotPath,
		p.expectedTrustSHA256,
		p.expectedClusterID,
		p.expectedDeploymentID,
		p.expectedAuthoritySnapshotID,
		p.expectedAuthorityClosureSHA256,
		now,
	)
	if err != nil {
		p.failedKey = key
		p.failedErr = err
		if p.current != nil && p.current.Current(now) {
			return p.current, nil
		}
		return p.loadPrevious(now, err)
	}
	p.current = candidate
	p.currentKey = key
	p.failedKey = regularFileKey{}
	p.failedErr = nil
	return candidate, nil
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
	return previous, nil
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
