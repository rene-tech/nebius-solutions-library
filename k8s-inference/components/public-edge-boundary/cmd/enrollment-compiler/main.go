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
	"sort"
	"strconv"
	"strings"
	"syscall"
	"unsafe"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/custodycontract"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/snapshotauthority"
)

const inputSchema = "fs2-serve.nebius.ai/public-edge-boundary-enrollment-input/v1"
const custodySchema = "fs2-serve.nebius.ai/public-edge-custody-install/v3"
const maximumInputBytes = 16 * 1024 * 1024
const maximumSourceBytes = 64 * 1024 * 1024
const linuxOTmpfile = 0x410000
const linuxATSymlinkFollow = 0x400

type input struct {
	Schema string `json:"schema"`
	SnapshotAuthorityConfig snapshotauthority.Config `json:"snapshot_authority_config"`
	Components []componentPlan `json:"components"`
}

type componentPlan struct {
	Component string `json:"component"`
	VolumeRoots []volumeRoot `json:"volume_roots"`
	Directories []directory `json:"directories"`
	WritableRoots []writableRoot `json:"writable_roots"`
	Entries []sourceEntry `json:"entries"`
}

type volumeRoot struct {
	Destination string `json:"destination"`
	Filesystem string `json:"filesystem"`
	GID uint32 `json:"gid"`
	Mode uint32 `json:"mode"`
}

type directory struct {
	Destination string `json:"destination"`
	UID uint32 `json:"uid"`
	GID uint32 `json:"gid"`
	Mode uint32 `json:"mode"`
}

type writableRoot struct {
	Destination string `json:"destination"`
	UID uint32 `json:"uid"`
	GID uint32 `json:"gid"`
	Mode uint32 `json:"mode"`
}

type sourceEntry struct {
	SourceFile string `json:"source_file"`
	ProjectedName string `json:"projected_name"`
	Destination string `json:"destination"`
	UID uint32 `json:"uid"`
	GID uint32 `json:"gid"`
	Mode uint32 `json:"mode"`
}

type custodyEntry struct {
	Source string `json:"source"`
	Destination string `json:"destination"`
	SHA256 string `json:"sha256"`
	UID uint32 `json:"uid"`
	GID uint32 `json:"gid"`
	Mode uint32 `json:"mode"`
}

type custodyManifest struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	Component string `json:"component"`
	InstallationID string `json:"installation_id"`
	VolumeRoots []volumeRoot `json:"volume_roots"`
	Directories []directory `json:"directories"`
	WritableRoots []writableRoot `json:"writable_roots"`
	Entries []custodyEntry `json:"entries"`
}

type handoff struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	SnapshotAuthorityConfig outputArtifact `json:"snapshot_authority_config"`
	SourceArtifacts []sourceArtifact `json:"source_artifacts"`
	Components []componentHandoff `json:"components"`
}

type outputArtifact struct {
	OutputFile string `json:"output_file"`
	SHA256 string `json:"sha256"`
}

type sourceArtifact struct {
	OutputFile string `json:"output_file"`
	SHA256 string `json:"sha256"`
}

type projectedInput struct {
	SecretKey string `json:"secret_key"`
	SourceOutputFile string `json:"source_output_file"`
	SHA256 string `json:"sha256"`
}

type componentHandoff struct {
	Component string `json:"component"`
	CustodyPayload outputArtifact `json:"custody_payload"`
	SignedEnvelopeConfigMapKey string `json:"signed_envelope_config_map_key"`
	ProjectedInputs []projectedInput `json:"projected_inputs"`
}

func main() {
	if len(os.Args) != 6 { slog.Error("enrollment compiler requires acceptance trust, acceptance envelope, the sealed target snapshot-authority executable, canonical input and an existing output directory"); os.Exit(1) }
	trustRaw, err := readRegular(os.Args[1], maximumInputBytes)
	if err != nil { slog.Error("enrollment acceptance trust is unavailable", "error", err); os.Exit(1) }
	envelopeRaw, err := readRegular(os.Args[2], maximumInputBytes)
	if err != nil { slog.Error("enrollment acceptance envelope is unavailable", "error", err); os.Exit(1) }
	acceptance, err := boundary.VerifyAcceptanceGeneration(trustRaw, envelopeRaw)
	if err != nil || acceptance.Schema != boundary.AcceptancePayloadSchema { slog.Error("enrollment acceptance is invalid or lacks the v5 authority contract", "error", err); os.Exit(1) }
	acceptedEnvelopeRaw, acceptedEnvelopeSHA256, err := acceptance.EnvelopeGeneration()
	if err != nil || !bytes.Equal(acceptedEnvelopeRaw, envelopeRaw) { slog.Error("enrollment acceptance generation cannot be retained exactly", "error", err); os.Exit(1) }
	targetRaw, err := readSealedTargetExecutable(os.Args[3], 256*1024*1024)
	if err != nil || digest(targetRaw) != acceptance.SnapshotAuthorityExecutableSHA256 { slog.Error("target snapshot-authority executable differs from the independently accepted digest", "error", err); os.Exit(1) }
	raw, err := readRegular(os.Args[4], maximumInputBytes)
	if err != nil { slog.Error("enrollment input is unavailable", "error", err); os.Exit(1) }
	var specification input
	if err := decodeCanonical(raw, &specification); err != nil || specification.Schema != inputSchema { slog.Error("enrollment input is not the exact canonical schema", "error", err); os.Exit(1) }
	if err := snapshotauthority.ValidateEnrollmentConfig(specification.SnapshotAuthorityConfig, acceptance); err != nil { slog.Error("snapshot authority enrollment is invalid", "error", err); os.Exit(1) }
	configRaw, err := json.Marshal(specification.SnapshotAuthorityConfig)
	if err != nil || digest(configRaw) != acceptance.SnapshotAuthorityConfigSHA256 { slog.Error("materialized snapshot authority config differs from accepted bytes"); os.Exit(1) }
	outputFD, err := openOutputDirectory(os.Args[5])
	if err != nil { slog.Error("enrollment output directory is unsafe", "error", err); os.Exit(1) }
	defer syscall.Close(outputFD)
	configOutputName := "snapshot-authority-"+digest(configRaw)+".json"
	if err := publishAt(outputFD, configOutputName, configRaw, 0o600); err != nil { slog.Error("snapshot authority config cannot be published", "error", err); os.Exit(1) }
	sources := map[string][]byte{digest(configRaw): configRaw, acceptedEnvelopeSHA256: acceptedEnvelopeRaw}
	manifests, err := materialize(specification, acceptance, configRaw, configOutputName, acceptedEnvelopeRaw, acceptedEnvelopeSHA256, sources)
	if err != nil { slog.Error("custody plans cannot be materialized", "error", err); os.Exit(1) }
	publication := handoff{Schema: "fs2-serve.nebius.ai/public-edge-enrollment-handoff/v1", ClusterID: acceptance.ClusterID, DeploymentID: acceptance.DeploymentID, SnapshotAuthorityConfig: outputArtifact{OutputFile: configOutputName, SHA256: digest(configRaw)}}
	sourceDigests := make([]string, 0, len(sources))
	for value := range sources { sourceDigests = append(sourceDigests, value) }
	sort.Strings(sourceDigests)
	for _, value := range sourceDigests {
		outputName := "input-"+value+".bin"
		if value == digest(configRaw) { outputName = configOutputName }
		if err := publishAt(outputFD, outputName, sources[value], 0o600); err != nil { slog.Error("projected input cannot be published", "digest", value, "error", err); os.Exit(1) }
		publication.SourceArtifacts = append(publication.SourceArtifacts, sourceArtifact{OutputFile: outputName, SHA256: value})
	}
	for _, manifest := range manifests {
		manifestRaw, marshalErr := json.Marshal(manifest)
		if marshalErr != nil { slog.Error("custody payload cannot be encoded", "error", marshalErr); os.Exit(1) }
		payloadName := manifest.Component+"-custody-payload-"+digest(manifestRaw)+".json"
		if err := publishAt(outputFD, payloadName, manifestRaw, 0o600); err != nil { slog.Error("custody payload cannot be published", "component", manifest.Component, "error", err); os.Exit(1) }
		component := componentHandoff{Component: manifest.Component, CustodyPayload: outputArtifact{OutputFile: payloadName, SHA256: digest(manifestRaw)}, SignedEnvelopeConfigMapKey: "install-envelope.json"}
		for _, item := range manifest.Entries {
			value := filepath.Base(item.Source)
			outputName := "input-"+item.SHA256+".bin"
			if item.SHA256 == digest(configRaw) { outputName = configOutputName }
			component.ProjectedInputs = append(component.ProjectedInputs, projectedInput{SecretKey: value, SourceOutputFile: outputName, SHA256: item.SHA256})
		}
		publication.Components = append(publication.Components, component)
	}
	handoffRaw, err := json.Marshal(publication)
	if err != nil { slog.Error("enrollment handoff cannot be encoded", "error", err); os.Exit(1) }
	if err := publishAt(outputFD, "enrollment-handoff-"+digest(handoffRaw)+".json", handoffRaw, 0o600); err != nil { slog.Error("enrollment handoff cannot be published", "error", err); os.Exit(1) }
}

func materialize(specification input, acceptance boundary.Acceptance, configRaw []byte, configProjectedName string, acceptanceRaw []byte, acceptanceSHA256 string, sources map[string][]byte) ([]custodyManifest, error) {
	expectedComponents := []string{"collector", "native-authority", "settlement-custodian", "snapshot-authority", "webhook"}
	if len(specification.Components) != len(expectedComponents) { return nil, errors.New("enrollment input does not contain every exact runtime component") }
	result := make([]custodyManifest, 0, len(expectedComponents))
	for index, component := range specification.Components {
		if component.Component != expectedComponents[index] { return nil, errors.New("enrollment components are not complete and canonically ordered") }
		manifest, err := materializeComponent(component, acceptance, specification.SnapshotAuthorityConfig, configRaw, configProjectedName, acceptanceRaw, acceptanceSHA256, sources)
		if err != nil { return nil, err }
		result = append(result, manifest)
	}
	return result, nil
}

func materializeComponent(plan componentPlan, acceptance boundary.Acceptance, config snapshotauthority.Config, configRaw []byte, configProjectedName string, acceptanceRaw []byte, acceptanceSHA256 string, sources map[string][]byte) (custodyManifest, error) {
	manifest := custodyManifest{Schema: custodySchema, ClusterID: acceptance.ClusterID, DeploymentID: acceptance.DeploymentID, Component: plan.Component, VolumeRoots: plan.VolumeRoots, Directories: plan.Directories, WritableRoots: plan.WritableRoots}
	if !strictlyOrderedVolumeRoots(plan.VolumeRoots) || !strictlyOrderedDirectories(plan.Directories) || !strictlyOrderedWritableRoots(plan.WritableRoots) { return custodyManifest{}, errors.New("custody roots and directories are not canonical") }
	seenProjected := map[string]bool{}
	seenDestination := map[string]bool{}
	acceptanceDestination := "/custody/run/acceptance/accepted-boundary-envelope.json"
	acceptanceParent := directory{}
	for _, candidate := range plan.Directories { if candidate.Destination == filepath.Dir(acceptanceDestination) { acceptanceParent = candidate } }
	if acceptanceParent.Destination == "" || len(acceptanceRaw) == 0 || digest(acceptanceRaw) != acceptanceSHA256 { return custodyManifest{}, errors.New("component omits the protected acceptance directory or exact accepted generation") }
	acceptanceProjectedName := projectedKey("accepted-boundary-envelope.json", acceptanceSHA256)
	if !safeSecretKey(acceptanceProjectedName) { return custodyManifest{}, errors.New("accepted boundary envelope projected key is invalid") }
	sources[acceptanceSHA256] = acceptanceRaw
	manifest.Entries = append(manifest.Entries, custodyEntry{Source: "/projected/inputs/"+acceptanceProjectedName, Destination: acceptanceDestination, SHA256: acceptanceSHA256, UID: 0, GID: acceptanceParent.GID, Mode: 0o440})
	seenProjected[acceptanceProjectedName] = true
	seenDestination[acceptanceDestination] = true
	for _, item := range plan.Entries {
		if !safeProjectedName(item.ProjectedName) || seenProjected[item.ProjectedName] || seenDestination[item.Destination] || item.UID != 0 || item.SourceFile == "" || !filepath.IsAbs(item.SourceFile) { return custodyManifest{}, errors.New("custody source entry is incomplete or duplicated") }
		raw, err := readRegular(item.SourceFile, maximumSourceBytes)
		if err != nil { return custodyManifest{}, err }
		valueDigest := digest(raw)
		projectedName := projectedKey(item.ProjectedName, valueDigest)
		if !safeSecretKey(projectedName) || seenProjected[projectedName] { return custodyManifest{}, errors.New("custody projected input key is invalid or duplicated after content addressing") }
		seenProjected[projectedName] = true
		seenDestination[item.Destination] = true
		if retained, exists := sources[valueDigest]; exists && !bytes.Equal(retained, raw) { return custodyManifest{}, errors.New("custody source digest collides with different bytes") }
		sources[valueDigest] = raw
		manifest.Entries = append(manifest.Entries, custodyEntry{Source: "/projected/inputs/"+projectedName, Destination: item.Destination, SHA256: valueDigest, UID: item.UID, GID: item.GID, Mode: item.Mode})
	}
	if plan.Component == "snapshot-authority" || plan.Component == "settlement-custodian" {
		destination := "/custody/run/config/snapshot-authority.json"
		if seenDestination[destination] || seenProjected[configProjectedName] { return custodyManifest{}, errors.New("snapshot authority config destination is duplicated") }
		gid := config.RuntimeGID
		if plan.Component == "settlement-custodian" { gid = config.SettlementRuntimeGID }
		manifest.Entries = append(manifest.Entries, custodyEntry{Source: "/projected/inputs/"+configProjectedName, Destination: destination, SHA256: digest(configRaw), UID: 0, GID: gid, Mode: 0o440})
	}
	sort.Slice(manifest.Entries, func(left, right int) bool { return manifest.Entries[left].Destination < manifest.Entries[right].Destination })
	for index, item := range manifest.Entries { if index > 0 && manifest.Entries[index-1].Destination == item.Destination { return custodyManifest{}, errors.New("custody manifest contains duplicate destinations") } }
	if err := validateManifestStructure(manifest); err != nil { return custodyManifest{}, err }
	if err := validateRequiredConfigFiles(manifest, acceptance, config); err != nil { return custodyManifest{}, err }
	if plan.Component == "settlement-custodian" && !exactSettlementRootPlan(manifest, config) { return custodyManifest{}, errors.New("settlement custodian does not have the exact dedicated volume and private writer root") }
	projectionRaw, err := json.Marshal(manifest)
	if err != nil { return custodyManifest{}, err }
	manifest.InstallationID = digest(projectionRaw)
	return manifest, nil
}

func validateManifestStructure(manifest custodyManifest) error {
	directories := map[string]directory{}
	seen := map[string]bool{}
	for _, item := range manifest.VolumeRoots {
		if !validCustodyPath(item.Destination) || filepath.Dir(item.Destination) != "/custody" || seen[item.Destination] || item.Filesystem != "ext4" && item.Filesystem != "xfs" || item.Mode != 0o750 { return errors.New("custody volume root is outside the exact protected contract") }
		seen[item.Destination] = true
		directories[item.Destination] = directory{Destination: item.Destination, UID: 0, GID: item.GID, Mode: item.Mode}
	}
	for _, item := range manifest.Directories {
		parent := filepath.Dir(item.Destination)
		if !custodycontract.ValidDirectoryDestination(item.Destination) || seen[item.Destination] || item.UID != 0 || item.Mode != 0o700 && item.Mode != 0o750 && item.Mode != 0o2750 || parent != "/custody" && directories[parent].Destination == "" { return errors.New("custody directory or its signed parent is invalid") }
		seen[item.Destination] = true
		directories[item.Destination] = item
	}
	for _, item := range manifest.WritableRoots {
		if !validCustodyPath(item.Destination) || seen[item.Destination] || item.UID == 0 || item.GID == 0 || item.Mode != 0o700 || directories[filepath.Dir(item.Destination)].Destination == "" { return errors.New("custody writable root or its protected parent is invalid") }
		seen[item.Destination] = true
	}
	for _, item := range manifest.Entries {
		parent := directories[filepath.Dir(item.Destination)]
		if !validCustodyPath(item.Destination) || seen[item.Destination] || !strings.HasPrefix(item.Source, "/projected/inputs/") || filepath.Dir(item.Source) != "/projected/inputs" || item.UID != 0 || parent.Destination == "" || parent.GID != item.GID || item.Mode != 0o400 && item.Mode != 0o440 && item.Mode != 0o444 && item.Mode != 0o600 { return errors.New("custody entry or its complete signed ancestry is invalid") }
		seen[item.Destination] = true
	}
	if manifest.Component == "snapshot-authority" || manifest.Component == "settlement-custodian" || manifest.Component == "webhook" {
		if err := validateRuntimeReaderAncestry(manifest.Entries, directories); err != nil { return err }
	}
	return nil
}

func validateRuntimeReaderAncestry(entries []custodyEntry, directories map[string]directory) error {
	for _, item := range entries {
		if item.Mode != 0o440 && item.Mode != 0o444 { return errors.New("nonroot runtime input is not readable under its exact immutable group/world mode") }
		for parent := filepath.Dir(item.Destination); parent != "/custody"; parent = filepath.Dir(parent) {
			contract, exists := directories[parent]
			if !exists || contract.UID != 0 || contract.GID != item.GID || contract.Mode&0o010 == 0 || contract.Mode&0o022 != 0 { return errors.New("nonroot runtime input ancestry lacks its exact root-owned reader group and group-execute contract") }
		}
	}
	return nil
}

func validateRequiredConfigFiles(manifest custodyManifest, acceptance boundary.Acceptance, config snapshotauthority.Config) error {
	type requiredFile struct { SHA256 string; GID uint32; Mode uint32 }
	required := map[string]requiredFile{}
	invalidRequired := false
	add := func(path, value string, gid uint32, mode uint32) {
		destination := runtimeDestination(path)
		if destination == "" || !digestText(value) { invalidRequired = true; return }
		if retained, exists := required[destination]; exists {
			if retained.SHA256 != value || retained.Mode != 0 && mode != 0 && (retained.Mode != mode || retained.GID != gid) { invalidRequired = true; return }
			if retained.Mode != 0 { return }
		}
		required[destination] = requiredFile{SHA256: value, GID: gid, Mode: mode}
	}
	_, acceptanceSHA256, acceptanceErr := acceptance.EnvelopeGeneration()
	if acceptanceErr != nil { return errors.New("accepted boundary envelope generation is unavailable") }
	acceptanceGID := uint32(0)
	acceptanceMode := uint32(0)
	switch manifest.Component {
	case "webhook":
		acceptanceGID = acceptance.BoundaryRuntimeReaderGID
		acceptanceMode = 0o440
	case "snapshot-authority":
		acceptanceGID = config.RuntimeGID
		acceptanceMode = 0o440
	case "settlement-custodian":
		acceptanceGID = config.SettlementRuntimeGID
		acceptanceMode = 0o440
	}
	add("/var/run/fs2-boundary/acceptance/accepted-boundary-envelope.json", acceptanceSHA256, acceptanceGID, acceptanceMode)
	if manifest.Component == "webhook" {
		readerGID := acceptance.BoundaryRuntimeReaderGID
		add("/var/run/fs2-boundary/tls/tls.crt", acceptance.BoundaryTLSCertificateSHA256, readerGID, 0o440)
		add("/var/run/fs2-boundary/tls/tls.key", acceptance.BoundaryTLSPrivateKeySHA256, readerGID, 0o440)
		add("/var/run/fs2-boundary/admission-client-trust.json", acceptance.BoundaryAdmissionClientTrustSHA256, readerGID, 0o440)
		add("/var/run/fs2-boundary/config/transition-settlement.json", acceptance.TransitionSettlementConfigSHA256, readerGID, 0o440)
	}
	if manifest.Component == "snapshot-authority" || manifest.Component == "settlement-custodian" {
		configGID := config.RuntimeGID
		if manifest.Component == "settlement-custodian" { configGID = config.SettlementRuntimeGID }
		add("/var/run/fs2-boundary/config/snapshot-authority.json", acceptance.SnapshotAuthorityConfigSHA256, configGID, 0o440)
		add(config.NativeCollectorConfigPath, acceptance.NativeCollectorConfigSHA256, 0, 0)
		add(config.NativeResponseTrustPath, acceptance.NativeResponseTrustSHA256, 0, 0)
		add(config.SnapshotTrustPath, acceptance.SnapshotTrustSHA256, 0, 0)
		for _, identity := range config.CollectorIdentities { add(identity.CABundlePath, identity.CABundleSHA256, 0, 0); add(identity.CRLPath, identity.CRLSHA256, 0, 0) }
	}
	if manifest.Component == "snapshot-authority" {
		add(config.SigningKeyPath, config.SigningKeySHA256, config.RuntimeGID, 0o440)
		for _, identity := range config.ServerIdentities { add(identity.CertificatePath, identity.CertificateSHA256, 0, 0); add(identity.PrivateKeyPath, identity.PrivateKeySHA256, config.RuntimeGID, 0o440); add(identity.IssuerBundlePath, identity.IssuerBundleSHA256, 0, 0); add(identity.CRLPath, identity.CRLSHA256, 0, 0) }
	}
	if manifest.Component == "settlement-custodian" {
		add(config.SettlementSigningKeyPath, config.SettlementSigningKeySHA256, config.SettlementRuntimeGID, 0o440)
		for _, key := range config.SettlementVerificationKeys { add(key.PublicKeyPath, key.PublicKeySHA256, 0, 0) }
	}
	if invalidRequired { return errors.New("acceptance-bound runtime input has an empty path, invalid digest or conflicting destination") }
	actual := map[string]custodyEntry{}
	for _, item := range manifest.Entries { actual[item.Destination] = item }
	for destination, expected := range required {
		item, exists := actual[destination]
		if destination == "" || !exists || item.SHA256 != expected.SHA256 || expected.Mode != 0 && (item.Mode != expected.Mode || item.GID != expected.GID) { return errors.New("custody payload omits or changes an acceptance-bound runtime file identity, mode or reader group") }
	}
	return nil
}

func exactSettlementRootPlan(manifest custodyManifest, config snapshotauthority.Config) bool {
	if config.SettlementRoot != "/var/lib/fs2-snapshot-settlement-volume/store" { return false }
	volume := false
	for _, item := range manifest.VolumeRoots { if item.Destination == "/custody/ledger" && (item.Filesystem == "ext4" || item.Filesystem == "xfs") && item.GID == config.SettlementRuntimeGID && item.Mode == 0o750 { volume = true } }
	writable := false
	for _, item := range manifest.WritableRoots { if item.Destination == "/custody/ledger/store" && item.UID == config.SettlementRuntimeUID && item.GID == config.SettlementRuntimeGID && item.Mode == 0o700 { writable = true } }
	return volume && writable
}

func runtimeDestination(path string) string {
	clean := filepath.Clean(path)
	if path != clean || !strings.HasPrefix(clean, "/var/run/fs2-boundary/") { return "" }
	return "/custody/run/"+strings.TrimPrefix(clean, "/var/run/fs2-boundary/")
}

func validCustodyPath(path string) bool { return filepath.IsAbs(path) && strings.HasPrefix(filepath.Clean(path), "/custody/") && path == filepath.Clean(path) && !strings.ContainsAny(path, "\x00\r\n") }

func strictlyOrderedVolumeRoots(values []volumeRoot) bool { previous := ""; for _, value := range values { if value.Destination <= previous { return false }; previous = value.Destination }; return true }
func strictlyOrderedDirectories(values []directory) bool { previous := ""; for _, value := range values { if value.Destination <= previous { return false }; previous = value.Destination }; return true }
func strictlyOrderedWritableRoots(values []writableRoot) bool { previous := ""; for _, value := range values { if value.Destination <= previous { return false }; previous = value.Destination }; return true }
func safeProjectedName(value string) bool { return value != "" && len(value) <= 128 && filepath.Base(value) == value && value != "." && value != ".." && !strings.ContainsAny(value, "\x00\r\n/") }

func safeSecretKey(value string) bool {
	if value == "" || len(value) > 253 { return false }
	for _, character := range value {
		if character >= 'a' && character <= 'z' || character >= 'A' && character <= 'Z' || character >= '0' && character <= '9' || character == '-' || character == '_' || character == '.' { continue }
		return false
	}
	return true
}

func projectedKey(value string, valueDigest string) string {
	extension := filepath.Ext(value)
	stem := strings.TrimSuffix(value, extension)
	return stem+"-"+valueDigest+extension
}

func decodeCanonical(raw []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil || decoder.Decode(&struct{}{}) != io.EOF { return errors.New("JSON is invalid, ambiguous or has unknown fields") }
	canonical, err := json.Marshal(target)
	if err != nil || !bytes.Equal(canonical, raw) { return errors.New("JSON is not canonical") }
	return nil
}

func readRegular(path string, maximum int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > maximum || info.Mode().Perm()&0o022 != 0 { return nil, errors.New("input is not one protected bounded regular file") }
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return nil, err }
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) { return nil, errors.New("input identity changed") }
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(raw)) != info.Size() { return nil, errors.New("input cannot be read exactly") }
	return raw, nil
}

func readSealedTargetExecutable(path string, maximum int64) ([]byte, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path { return nil, errors.New("target snapshot-authority executable path must be exact and absolute") }
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 || info.Mode().Perm()&0o022 != 0 { return nil, errors.New("target snapshot-authority executable is not sealed against group or world mutation") }
	raw, err := readRegular(path, maximum)
	if err != nil { return nil, err }
	return raw, nil
}

func openOutputDirectory(path string) (int, error) {
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.IsDir() || info.Mode().Perm()&0o022 != 0 { return -1, errors.New("output directory is absent or writable by another identity") }
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return -1, err }
	var stat syscall.Stat_t
	opened, ok := info.Sys().(*syscall.Stat_t)
	if !ok || syscall.Fstat(fd, &stat) != nil || stat.Dev != opened.Dev || stat.Ino != opened.Ino { syscall.Close(fd); return -1, errors.New("output directory identity changed") }
	return fd, nil
}

func publishAt(parent int, name string, raw []byte, mode uint32) error {
	if !safeProjectedName(name) || len(raw) == 0 { return errors.New("output publication is invalid") }
	if existing, err := readAt(parent, name, int64(len(raw))); err == nil { if bytes.Equal(existing, raw) { return syscall.Fsync(parent) }; return errors.New("output publication conflicts with retained bytes") } else if !errors.Is(err, os.ErrNotExist) { return err }
	fd, err := syscall.Openat(parent, ".", syscall.O_RDWR|syscall.O_CLOEXEC|linuxOTmpfile, mode)
	if err != nil { return err }
	file := os.NewFile(uintptr(fd), name)
	defer file.Close()
	if _, err := file.Write(raw); err != nil { return err }
	if err := file.Sync(); err != nil { return err }
	procPath := "/proc/self/fd/"+strconv.Itoa(fd)
	procInfo, procErr := os.Stat(procPath)
	fileInfo, fileErr := file.Stat()
	if procErr != nil || fileErr != nil || !os.SameFile(procInfo, fileInfo) { return errors.New("anonymous enrollment output inode identity cannot be proven") }
	oldPath, _ := syscall.BytePtrFromString(procPath)
	newPath, _ := syscall.BytePtrFromString(name)
	directory := -100
	_, _, errno := syscall.RawSyscall6(syscall.SYS_LINKAT, uintptr(directory), uintptr(unsafe.Pointer(oldPath)), uintptr(parent), uintptr(unsafe.Pointer(newPath)), uintptr(linuxATSymlinkFollow), 0)
	if errno != 0 && errno != syscall.EEXIST { return errno }
	retained, err := readAt(parent, name, int64(len(raw)))
	if err != nil || !bytes.Equal(retained, raw) { return errors.New("no-replace enrollment output conflicts") }
	return syscall.Fsync(parent)
}

func readAt(parent int, name string, maximum int64) ([]byte, error) {
	fd, err := syscall.Openat(parent, name, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return nil, err }
	file := os.NewFile(uintptr(fd), name)
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 || info.Size() < 1 || info.Size() > maximum { return nil, errors.New("retained enrollment output is not exact") }
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(raw)) != info.Size() { return nil, errors.New("retained enrollment output cannot be read exactly") }
	return raw, nil
}

func digestText(value string) bool { if len(value) != 64 || value != strings.ToLower(value) { return false }; raw, err := hex.DecodeString(value); return err == nil && len(raw) == sha256.Size }
func digest(raw []byte) string { value := sha256.Sum256(raw); return hex.EncodeToString(value[:]) }
