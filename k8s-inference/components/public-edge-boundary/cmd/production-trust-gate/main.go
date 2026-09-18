package main

import (
	"errors"
	"io"
	"os"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
)

const maximumArtifactBytes = 4 * 1024 * 1024

func main() {
	if len(os.Args) != 8 { fail() }
	rootRaw, err := readRegular(os.Args[1])
	if err != nil { fail() }
	headRaw, err := readRegular(os.Args[2])
	if err != nil { fail() }
	envelopeRaw, err := readRegular(os.Args[3])
	if err != nil { fail() }
	predecessorRaw, err := readRegular(os.Args[4])
	if err != nil { fail() }
	acceptanceRaw, err := readRegular(os.Args[5])
	if err != nil { fail() }
	nativeRaw, err := readRegular(os.Args[6])
	if err != nil { fail() }
	snapshotRaw, err := readRegular(os.Args[7])
	if err != nil { fail() }
	if _, err := boundary.VerifyProductionTrustProvenance(rootRaw, headRaw, envelopeRaw, predecessorRaw, acceptanceRaw, nativeRaw, snapshotRaw, time.Now().UTC()); err != nil { fail() }
}

func readRegular(path string) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > maximumArtifactBytes || info.Mode().Perm()&0o022 != 0 { return nil, errors.New("production trust input is not one protected bounded regular file") }
	file, err := os.Open(path)
	if err != nil { return nil, err }
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) { return nil, errors.New("production trust input identity changed") }
	raw, err := io.ReadAll(io.LimitReader(file, maximumArtifactBytes+1))
	if err != nil || int64(len(raw)) != info.Size() { return nil, errors.New("production trust input cannot be read exactly") }
	return raw, nil
}

func fail() { os.Exit(1) }
