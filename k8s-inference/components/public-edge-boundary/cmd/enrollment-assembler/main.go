package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"unsafe"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
)

const maximumArtifactBytes = 64 * 1024 * 1024
const linuxOTmpfile = 0x410000
const linuxATSymlinkFollow = 0x400

type handoff struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	SnapshotAuthorityConfig outputArtifact `json:"snapshot_authority_config"`
	SourceArtifacts []sourceArtifact `json:"source_artifacts"`
	Components []componentHandoff `json:"components"`
}

type outputArtifact struct { OutputFile string `json:"output_file"`; SHA256 string `json:"sha256"` }
type sourceArtifact struct { OutputFile string `json:"output_file"`; SHA256 string `json:"sha256"` }
type projectedInput struct { SecretKey string `json:"secret_key"`; SourceOutputFile string `json:"source_output_file"`; SHA256 string `json:"sha256"` }
type componentHandoff struct {
	Component string `json:"component"`
	CustodyPayload outputArtifact `json:"custody_payload"`
	SignedEnvelopeConfigMapKey string `json:"signed_envelope_config_map_key"`
	ProjectedInputs []projectedInput `json:"projected_inputs"`
}

type helmValues struct { Custody map[string]custodyValue `json:"custody"` }
type custodyValue struct { EnrollmentEnvelope string `json:"enrollmentEnvelope"` }

func main() {
	if len(os.Args) != 5 { slog.Error("enrollment assembler requires acceptance trust, handoff manifest, signed-envelope directory and output directory"); os.Exit(1) }
	trustRaw, err := readRegular(os.Args[1], maximumArtifactBytes)
	if err != nil { slog.Error("custody signing trust is unavailable", "error", err); os.Exit(1) }
	handoffRaw, err := readRegular(os.Args[2], maximumArtifactBytes)
	if err != nil { slog.Error("enrollment handoff is unavailable", "error", err); os.Exit(1) }
	var plan handoff
	if err := decodeCanonical(handoffRaw, &plan); err != nil || plan.Schema != "fs2-serve.nebius.ai/public-edge-enrollment-handoff/v1" { slog.Error("enrollment handoff is invalid", "error", err); os.Exit(1) }
	artifactRoot := filepath.Dir(os.Args[2])
	if err := verifyMaterializedSources(artifactRoot, plan); err != nil { slog.Error("enrollment handoff source closure is incomplete", "error", err); os.Exit(1) }
	expectedComponents := []string{"collector", "native-authority", "settlement-custodian", "snapshot-authority", "webhook"}
	if len(plan.Components) != len(expectedComponents) { slog.Error("enrollment handoff component set is incomplete"); os.Exit(1) }
	values := helmValues{Custody: map[string]custodyValue{}}
	for index, component := range plan.Components {
		if component.Component != expectedComponents[index] || component.SignedEnvelopeConfigMapKey != "install-envelope.json" { slog.Error("enrollment handoff component order or ConfigMap key is invalid"); os.Exit(1) }
		payloadRaw, err := readArtifact(artifactRoot, component.CustodyPayload)
		if err != nil { slog.Error("custody payload is unavailable", "component", component.Component, "error", err); os.Exit(1) }
		envelopePath := filepath.Join(os.Args[3], component.Component+"-custody-envelope.json")
		envelopeRaw, err := readRegular(envelopePath, maximumArtifactBytes)
		if err != nil { slog.Error("signed custody envelope is unavailable", "component", component.Component, "error", err); os.Exit(1) }
		verified, err := boundary.VerifyCustodyEnrollment(trustRaw, envelopeRaw)
		if err != nil || !bytes.Equal(verified, payloadRaw) { slog.Error("signed custody envelope differs from its exact handoff payload", "component", component.Component, "error", err); os.Exit(1) }
		values.Custody[helmComponent(component.Component)] = custodyValue{EnrollmentEnvelope: string(envelopeRaw)}
	}
	valuesRaw, err := json.Marshal(values)
	if err != nil { slog.Error("Helm enrollment values cannot be encoded", "error", err); os.Exit(1) }
	outputFD, err := openDirectory(os.Args[4])
	if err != nil { slog.Error("enrollment assembly output directory is unsafe", "error", err); os.Exit(1) }
	defer syscall.Close(outputFD)
	name := "custody-enrollment-values-"+digest(valuesRaw)+".json"
	if err := publishNoReplace(outputFD, name, valuesRaw); err != nil { slog.Error("Helm enrollment values cannot be published", "error", err); os.Exit(1) }
}

func verifyMaterializedSources(root string, plan handoff) error {
	if _, err := readArtifact(root, plan.SnapshotAuthorityConfig); err != nil { return err }
	sources := map[string]sourceArtifact{}
	used := map[string]bool{plan.SnapshotAuthorityConfig.SHA256: true}
	for _, item := range plan.SourceArtifacts {
		if sources[item.SHA256].SHA256 != "" { return errors.New("handoff repeats a source artifact digest") }
		if _, err := readArtifact(root, outputArtifact(item)); err != nil { return err }
		sources[item.SHA256] = item
	}
	for _, component := range plan.Components {
		componentKeys := map[string]bool{}
		for _, item := range component.ProjectedInputs {
			artifact := sources[item.SHA256]
			if artifact.OutputFile != item.SourceOutputFile || !safeSecretKey(item.SecretKey) || componentKeys[item.SecretKey] { return errors.New("handoff projected input does not map uniquely to one materialized source artifact") }
			componentKeys[item.SecretKey] = true
			used[item.SHA256] = true
		}
	}
	for value := range sources { if !used[value] { return errors.New("handoff contains an unreferenced source artifact") } }
	return nil
}

func readArtifact(root string, artifact outputArtifact) ([]byte, error) {
	if filepath.Base(artifact.OutputFile) != artifact.OutputFile || !digestText(artifact.SHA256) { return nil, errors.New("artifact reference is invalid") }
	raw, err := readRegular(filepath.Join(root, artifact.OutputFile), maximumArtifactBytes)
	if err != nil || digest(raw) != artifact.SHA256 { return nil, errors.New("artifact bytes differ from the handoff digest") }
	return raw, nil
}

func helmComponent(value string) string {
	switch value {
	case "collector", "webhook": return value
	case "native-authority": return "nativeAuthority"
	case "snapshot-authority": return "snapshotAuthority"
	case "settlement-custodian": return "settlementCustodian"
	default: return ""
	}
}

func decodeCanonical(raw []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(target) != nil || decoder.Decode(&struct{}{}) != io.EOF { return errors.New("JSON is invalid, ambiguous or has unknown fields") }
	canonical, err := json.Marshal(target)
	if err != nil || !bytes.Equal(canonical, raw) { return errors.New("JSON is not canonical") }
	return nil
}

func readRegular(path string, maximum int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > maximum || info.Mode().Perm()&0o022 != 0 { return nil, errors.New("artifact is not one protected bounded regular file") }
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return nil, err }
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) { return nil, errors.New("artifact identity changed") }
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(raw)) != info.Size() { return nil, errors.New("artifact cannot be read exactly") }
	return raw, nil
}

func openDirectory(path string) (int, error) {
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.IsDir() || info.Mode().Perm()&0o022 != 0 { return -1, errors.New("directory is absent or writable by another identity") }
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return -1, err }
	return fd, nil
}

func publishNoReplace(parent int, name string, raw []byte) error {
	if filepath.Base(name) != name || name == "." || name == ".." || strings.ContainsAny(name, "\x00\r\n/") || len(raw) == 0 { return errors.New("assembled output name or bytes are invalid") }
	if existing, err := readAt(parent, name, int64(len(raw))); err == nil {
		if bytes.Equal(existing, raw) { return syscall.Fsync(parent) }
		return errors.New("assembled output conflicts with retained bytes")
	} else if !errors.Is(err, os.ErrNotExist) { return err }
	fd, err := syscall.Openat(parent, ".", syscall.O_RDWR|syscall.O_CLOEXEC|linuxOTmpfile, 0o600)
	if err != nil { return err }
	file := os.NewFile(uintptr(fd), name)
	defer file.Close()
	if _, err := file.Write(raw); err != nil { return err }
	if err := file.Sync(); err != nil { return err }
	procPath := "/proc/self/fd/"+strconv.Itoa(fd)
	procInfo, procErr := os.Stat(procPath)
	fileInfo, fileErr := file.Stat()
	if procErr != nil || fileErr != nil || !os.SameFile(procInfo, fileInfo) { return errors.New("anonymous assembled output inode identity cannot be proven") }
	oldPath, _ := syscall.BytePtrFromString(procPath)
	newPath, _ := syscall.BytePtrFromString(name)
	directory := -100
	_, _, errno := syscall.RawSyscall6(syscall.SYS_LINKAT, uintptr(directory), uintptr(unsafe.Pointer(oldPath)), uintptr(parent), uintptr(unsafe.Pointer(newPath)), uintptr(linuxATSymlinkFollow), 0)
	if errno != 0 && errno != syscall.EEXIST { return errno }
	retained, err := readAt(parent, name, int64(len(raw)))
	if err != nil || !bytes.Equal(retained, raw) { return errors.New("no-replace assembled output conflicts") }
	return syscall.Fsync(parent)
}

func readAt(parent int, name string, maximum int64) ([]byte, error) {
	fd, err := syscall.Openat(parent, name, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return nil, err }
	file := os.NewFile(uintptr(fd), name)
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 || info.Size() < 1 || info.Size() > maximum { return nil, errors.New("retained assembled output is not exact") }
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(raw)) != info.Size() { return nil, errors.New("retained assembled output cannot be read exactly") }
	return raw, nil
}

func digestText(value string) bool { if len(value) != 64 || value != strings.ToLower(value) { return false }; raw, err := hex.DecodeString(value); return err == nil && len(raw) == sha256.Size }
func safeSecretKey(value string) bool { if value == "" || len(value) > 253 { return false }; for _, character := range value { if character >= 'a' && character <= 'z' || character >= 'A' && character <= 'Z' || character >= '0' && character <= '9' || character == '-' || character == '_' || character == '.' { continue }; return false }; return true }
func digest(raw []byte) string { value := sha256.Sum256(raw); return hex.EncodeToString(value[:]) }
