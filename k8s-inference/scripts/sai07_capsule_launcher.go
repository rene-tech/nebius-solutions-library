// Command sai07-capsule-launcher is the root-custodied first process for one
// SAI-07 execution capsule. It authenticates the external runtime attestation,
// its own binary/build provenance, the exact image evidence and the complete
// Python worker closure before it seals every executable input and replaces
// itself with that measured worker. Python is never the bootstrap trust root.
//
// This source is deliberately inactive. A reviewed activation commit must pin
// the external root authority below and provide an active, root-signed launcher
// activation contract plus two detached builder-signed provenance receipts.
package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	rootAuthorityKeyID       = ""
	rootAuthorityPrincipalID = ""
	rootAuthorityKeySHA256   = ""

	rootAuthorityKeyPath = "/opt/fs2-sai07/attestations/execution-capsule-attestation-authority.pub"
	runtimeAttestationPath = "/opt/fs2-sai07/attestations/execution-capsule-runtime-attestation-v5.json"
	activationContractPath = "/opt/fs2-sai07/contracts/capsule-launcher-activation-v1.json"
	buildContractPath      = "/opt/fs2-sai07/contracts/capsule-launcher-build-v1.json"
	capsuleContractPath    = "/opt/fs2-sai07/contracts/execution-capsule-contract-v4.json"
	imageProvenancePath    = "/opt/fs2-sai07/attestations/image-provenance-v1.json"
	imageSBOMPath          = "/opt/fs2-sai07/attestations/image-sbom-v1.json"
	builderReceiptSchemaPath = "/opt/fs2-sai07/contracts/launcher-builder-provenance-v1.schema.json"

	maxJSONBytes    = 8 * 1024 * 1024
	maxRuntimeBytes = 512 * 1024 * 1024
	maxClockSkew    = 30 * time.Second
	maxLease        = 10 * time.Minute

	fdCapsuleContract   = 180
	fdRuntimeAttestation = 184
	fdSourceBundle      = 190
	fdPython            = 191
	fdLaunchGrant       = 207
	fdWorker            = 208

	mfdCloexec      = 0x0001
	mfdAllowSealing = 0x0002
	fAddSeals       = 1033
	fGetSeals       = 1034
	fSealSeal       = 0x0001
	fSealShrink     = 0x0002
	fSealGrow       = 0x0004
	fSealWrite      = 0x0008
)

var requiredSeals = uintptr(fSealSeal | fSealShrink | fSealGrow | fSealWrite)
var goVersionPattern = regexp.MustCompile(`^go1\.[0-9]+(?:\.[0-9]+)?$`)
var exactBuildArguments = []string{
	"build",
	"-buildmode=exe",
	"-trimpath",
	"-buildvcs=false",
	"-ldflags=-buildid= -s -w",
	"-o",
	"sai07-capsule-launcher",
	"./scripts/sai07_capsule_launcher.go",
}

type signature struct {
	Algorithm string `json:"algorithm"`
	KeyID     string `json:"key_id"`
	Value     string `json:"value"`
}

type objectIdentity struct {
	APIPath         string `json:"api_path"`
	ObjectSHA256    string `json:"object_sha256"`
	ResourceVersion string `json:"resource_version"`
	UID             string `json:"uid"`
}

type imageIdentity struct {
	Digest           string `json:"digest"`
	ProvenanceSHA256 string `json:"provenance_sha256"`
	Reference        string `json:"reference"`
	SBOMSHA256       string `json:"sbom_sha256"`
}

type imageReference struct {
	Digest    string `json:"digest"`
	Reference string `json:"reference"`
}

type imageProvenance struct {
	BuildType       string         `json:"build_type"`
	BuilderID       string         `json:"builder_id"`
	Image           imageReference `json:"image"`
	MaterialsSHA256 string         `json:"materials_sha256"`
	PredicateType   string         `json:"predicate_type"`
	Schema          string         `json:"schema"`
}

type imageSBOM struct {
	Document map[string]any `json:"document"`
	Format   string         `json:"format"`
	Image    imageReference `json:"image"`
	Schema   string         `json:"schema"`
}

type podIdentity struct {
	ContainerName           string `json:"container_name"`
	ImageDigest             string `json:"image_digest"`
	ImageID                 string `json:"image_id"`
	Name                    string `json:"name"`
	Namespace               string `json:"namespace"`
	ResourceVersion         string `json:"resource_version"`
	SecurityProjectionSHA256 string `json:"security_projection_sha256"`
	ServiceAccountName      string `json:"service_account_name"`
	UID                     string `json:"uid"`
}

type launcherClaim struct {
	ActivationContractSHA256 string   `json:"activation_contract_sha256"`
	BinarySHA256             string   `json:"binary_sha256"`
	BuildContractSHA256      string   `json:"build_contract_sha256"`
	BuilderReceiptSHA256s    []string `json:"builder_receipt_sha256s"`
	SourceSHA256             string   `json:"source_sha256"`
}

type apiIdentity struct {
	CASHA256 string `json:"ca_sha256"`
	Context  string `json:"context"`
	Origin   string `json:"origin"`
}

type handoffIdentity struct {
	DirectoryIdentitySHA256 string `json:"directory_identity_sha256"`
	Root                    string `json:"root"`
}

type workerClaim struct {
	Argv               []string `json:"argv"`
	PythonSHA256       string   `json:"python_sha256"`
	ScriptSHA256       string   `json:"script_sha256"`
	SourceBundleSHA256 string   `json:"source_bundle_sha256"`
}

type runtimeClaims struct {
	AdmissionObjects      []objectIdentity `json:"admission_objects"`
	API                   apiIdentity      `json:"api"`
	CapsuleContractSHA256 string           `json:"capsule_contract_sha256"`
	ExpiresAt             string           `json:"expires_at"`
	Handoff               handoffIdentity  `json:"handoff"`
	Image                 imageIdentity    `json:"image"`
	IssuedAt              string           `json:"issued_at"`
	Launcher              launcherClaim    `json:"launcher"`
	Nonce                 string           `json:"nonce"`
	Pod                   podIdentity      `json:"pod"`
	Role                  string           `json:"role"`
	Schema                string           `json:"schema"`
	SigningPrincipalID    string           `json:"signing_principal_id"`
	Worker                workerClaim      `json:"worker"`
}

type filePin struct {
	Path   string  `json:"path"`
	SHA256 *string `json:"sha256"`
}

type builderAuthority struct {
	IsolationDomain         string  `json:"isolation_domain"`
	IsolationEvidenceSHA256 *string `json:"isolation_evidence_sha256"`
	KeyID                   string  `json:"key_id"`
	PrincipalID             string  `json:"principal_id"`
	PublicKeyPath           string  `json:"public_key_path"`
	PublicKeySHA256         *string `json:"public_key_sha256"`
	Role                    string  `json:"role"`
}

type builderReceiptRef struct {
	Path   string  `json:"path"`
	Role   string  `json:"role"`
	SHA256 *string `json:"sha256"`
}

type admissionContract struct {
	EntrypointByRole     map[string][]string `json:"entrypoint_by_role"`
	RequiredObjectsByRole map[string][]string `json:"required_objects_by_role"`
}

type loaderContract struct {
	Mechanism                   string            `json:"mechanism"`
	RequiredContainerNameByRole map[string]string `json:"required_container_name_by_role"`
	RequireDigestImage          bool              `json:"require_digest_image"`
	RequirePID1                 bool              `json:"require_pid1"`
	RequireReadOnlyRoot         bool              `json:"require_read_only_root"`
}

type activationContract struct {
	Activation          string             `json:"activation"`
	Admission           admissionContract  `json:"admission"`
	BuilderAuthorities  []builderAuthority `json:"builder_authorities"`
	BuilderReceipts     []builderReceiptRef `json:"builder_receipts"`
	BuildContract       filePin            `json:"build_contract"`
	Launcher            filePin            `json:"launcher"`
	Loader              loaderContract     `json:"loader"`
	ReceiptSchemaPath   string             `json:"receipt_schema_path"`
	ReceiptSchemaSHA256 *string            `json:"receipt_schema_sha256"`
	RootAuthority       struct {
		KeyID           string  `json:"key_id"`
		PrincipalID     string  `json:"principal_id"`
		PublicKeyPath   string  `json:"public_key_path"`
		PublicKeySHA256 *string `json:"public_key_sha256"`
	} `json:"root_authority"`
	Schema string `json:"schema"`
	Source filePin `json:"source"`
	Status string `json:"status"`
	Worker struct {
		Python       filePin `json:"python"`
		Script       filePin `json:"script"`
		SourceBundle filePin `json:"source_bundle"`
	} `json:"worker"`
}

type launcherBuildContract struct {
	Activation string `json:"activation"`
	Build      struct {
		Arguments                 []string `json:"arguments"`
		CGOEnabled               string   `json:"cgo_enabled"`
		GoVersion                *string  `json:"go_version"`
		Goarch                   string   `json:"goarch"`
		Goos                     string   `json:"goos"`
		SourceDateEpoch          string   `json:"source_date_epoch"`
		StandardLibraryTreeSHA256 *string  `json:"standard_library_tree_sha256"`
		ToolchainArchiveSHA256   *string  `json:"toolchain_archive_sha256"`
	} `json:"build"`
	BuildEnvironmentSHA256 *string `json:"build_environment_sha256"`
	RecipeSHA256 *string `json:"recipe_sha256"`
	Schema       string  `json:"schema"`
	Source       filePin `json:"source"`
	Status       string  `json:"status"`
}

type builderProvenance struct {
	BuildEnvironmentSHA256 string `json:"build_environment_sha256"`
	Builder                struct {
		IsolationDomain         string `json:"isolation_domain"`
		IsolationEvidenceSHA256 string `json:"isolation_evidence_sha256"`
		KeyID                   string `json:"key_id"`
		PrincipalID             string `json:"principal_id"`
		Role                    string `json:"role"`
	} `json:"builder"`
	BuiltAt string `json:"built_at"`
	MaterialsSHA256 string `json:"materials_sha256"`
	Nonce string `json:"nonce"`
	Output struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"output"`
	Recipe struct {
		ArgumentsSHA256    string `json:"arguments_sha256"`
		BuildContractSHA256 string `json:"build_contract_sha256"`
		SourcePath          string `json:"source_path"`
		SourceSHA256        string `json:"source_sha256"`
	} `json:"recipe"`
	Schema string `json:"schema"`
	Toolchain struct {
		CGOEnabled               string `json:"cgo_enabled"`
		GoVersion                string `json:"go_version"`
		Goarch                   string `json:"goarch"`
		Goos                     string `json:"goos"`
		StandardLibraryTreeSHA256 string `json:"standard_library_tree_sha256"`
		ToolchainArchiveSHA256   string `json:"toolchain_archive_sha256"`
	} `json:"toolchain"`
}

type launchGrant struct {
	ActivationContractSHA256 string   `json:"activation_contract_sha256"`
	BuilderReceiptSHA256s    []string `json:"builder_receipt_sha256s"`
	CapsuleContractSHA256    string   `json:"capsule_contract_sha256"`
	ImageDigest              string   `json:"image_digest"`
	LauncherBinarySHA256     string   `json:"launcher_binary_sha256"`
	Role                     string   `json:"role"`
	RuntimeAttestationSHA256 string   `json:"runtime_attestation_sha256"`
	Schema                   string   `json:"schema"`
	WorkerArgvSHA256         string   `json:"worker_argv_sha256"`
	WorkerPythonSHA256       string   `json:"worker_python_sha256"`
	WorkerScriptSHA256       string   `json:"worker_script_sha256"`
	WorkerSourceBundleSHA256 string   `json:"worker_source_bundle_sha256"`
}

type capsuleLauncherContract struct {
	ActivationContract   filePin `json:"activation_contract"`
	Binary               filePin `json:"binary"`
	BuildContract        filePin `json:"build_contract"`
	BuilderReceiptSchema filePin `json:"builder_receipt_schema"`
	NativeLaunchGrantFD  int     `json:"native_launch_grant_fd"`
	RuntimeAttestationPath string `json:"runtime_attestation_path"`
	RuntimeAttestationSchema string `json:"runtime_attestation_schema"`
	WorkerScript         filePin `json:"worker_script"`
	WorkerScriptFD       int     `json:"worker_script_fd"`
}

func fail(format string, values ...any) {
	fmt.Fprintf(os.Stderr, "SAI-07 native capsule launcher rejected: "+format+"\n", values...)
	os.Exit(1)
}

func digest(payload []byte) string {
	value := sha256.Sum256(payload)
	return hex.EncodeToString(value[:])
}

func validDigest(value string) bool {
	if len(value) != 64 || value == strings.Repeat("0", 64) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil && value == strings.ToLower(value)
}

func canonical(value any) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(output.Bytes(), []byte("\n")), nil
}

func parseCanonical(path string, maximum int) ([]byte, map[string]json.RawMessage, error) {
	payload, err := readBounded(path, maximum)
	if err != nil {
		return nil, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.UseNumber()
	var generic any
	if err := decoder.Decode(&generic); err != nil {
		return nil, nil, err
	}
	if token, err := decoder.Token(); err != io.EOF || token != nil {
		return nil, nil, fmt.Errorf("%s has trailing JSON input", path)
	}
	encoded, err := canonical(generic)
	if err != nil || !bytes.Equal(payload, append(encoded, '\n')) {
		return nil, nil, fmt.Errorf("%s is not canonical JSON with one LF", path)
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(payload, &object); err != nil || object == nil {
		return nil, nil, fmt.Errorf("%s is not a JSON object", path)
	}
	return payload, object, nil
}

func exactKeys(object map[string]json.RawMessage, expected []string, label string) error {
	actual := make([]string, 0, len(object))
	for key := range object {
		actual = append(actual, key)
	}
	sort.Strings(actual)
	wanted := append([]string(nil), expected...)
	sort.Strings(wanted)
	if strings.Join(actual, "\x00") != strings.Join(wanted, "\x00") {
		return fmt.Errorf("%s fields differ from the closed contract", label)
	}
	return nil
}

func decodeExact(raw json.RawMessage, expected []string, output any, label string) error {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return fmt.Errorf("%s is not an object: %w", label, err)
	}
	if err := exactKeys(object, expected, label); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	return decoder.Decode(output)
}

func readExact(path string, maximum int, expectedSHA256 string) ([]byte, error) {
	payload, err := readBounded(path, maximum)
	if err != nil {
		return nil, err
	}
	if digest(payload) != expectedSHA256 {
		return nil, fmt.Errorf("%s differs from its authenticated digest", path)
	}
	return payload, nil
}

func readBounded(path string, maximum int) ([]byte, error) {
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	if file == nil {
		syscall.Close(fd)
		return nil, fmt.Errorf("%s could not be descriptor-opened", path)
	}
	defer file.Close()
	before, err := file.Stat()
	if err != nil || !before.Mode().IsRegular() || before.Size() <= 0 || before.Size() > int64(maximum) {
		return nil, fmt.Errorf("%s is not a bounded regular file", path)
	}
	payload, err := io.ReadAll(io.LimitReader(file, int64(maximum)+1))
	if err != nil || len(payload) == 0 || len(payload) > maximum {
		return nil, fmt.Errorf("%s violates its byte bound", path)
	}
	after, err := file.Stat()
	if err != nil || !os.SameFile(before, after) || before.Size() != after.Size() || !before.ModTime().Equal(after.ModTime()) || int64(len(payload)) != after.Size() {
		return nil, fmt.Errorf("%s changed during descriptor-fenced read", path)
	}
	return payload, nil
}

func readSelfExact(maximum int, expectedSHA256 string) ([]byte, error) {
	file, err := os.Open("/proc/self/exe")
	if err != nil {
		return nil, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() <= 0 || info.Size() > int64(maximum) {
		return nil, errors.New("launcher executable is not a bounded regular inode")
	}
	payload, err := io.ReadAll(io.LimitReader(file, int64(maximum)+1))
	if err != nil || int64(len(payload)) != info.Size() || digest(payload) != expectedSHA256 {
		return nil, errors.New("launcher executable differs from the signed binary digest")
	}
	return payload, nil
}

func verifyStaticLauncherELF(payload []byte) error {
	if len(payload) < 64 || !bytes.Equal(payload[:4], []byte{0x7f, 'E', 'L', 'F'}) ||
		payload[4] != 2 || payload[5] != 1 || payload[6] != 1 {
		return errors.New("launcher is not a supported ELF64 little-endian executable")
	}
	if binary.LittleEndian.Uint16(payload[16:18]) != 2 || binary.LittleEndian.Uint16(payload[18:20]) != 62 {
		return errors.New("launcher ELF type or architecture differs")
	}
	programOffset := binary.LittleEndian.Uint64(payload[32:40])
	entrySize := binary.LittleEndian.Uint16(payload[54:56])
	entryCount := binary.LittleEndian.Uint16(payload[56:58])
	if entrySize != 56 || entryCount == 0 || entryCount > 256 ||
		programOffset > uint64(len(payload)) || programOffset+uint64(entrySize)*uint64(entryCount) > uint64(len(payload)) {
		return errors.New("launcher ELF program table differs")
	}
	executableLoad := false
	for index := uint16(0); index < entryCount; index++ {
		offset := programOffset + uint64(index)*uint64(entrySize)
		entry := payload[offset : offset+uint64(entrySize)]
		programType := binary.LittleEndian.Uint32(entry[0:4])
		flags := binary.LittleEndian.Uint32(entry[4:8])
		fileOffset := binary.LittleEndian.Uint64(entry[8:16])
		fileSize := binary.LittleEndian.Uint64(entry[32:40])
		memorySize := binary.LittleEndian.Uint64(entry[40:48])
		if memorySize < fileSize || fileOffset > uint64(len(payload)) || fileOffset+fileSize > uint64(len(payload)) {
			return errors.New("launcher ELF segment escapes the file")
		}
		if programType == 2 || programType == 3 {
			return errors.New("launcher ELF has a dynamic loader or interpreter")
		}
		if programType == 1 {
			executableLoad = executableLoad || flags&1 != 0
			if flags&1 != 0 && flags&2 != 0 {
				return errors.New("launcher ELF has a writable executable segment")
			}
		}
		if programType == 0x6474e551 && flags&1 != 0 {
			return errors.New("launcher ELF requests an executable stack")
		}
	}
	if !executableLoad {
		return errors.New("launcher ELF has no executable load segment")
	}
	return nil
}

func verifySignedDocument(
	path string,
	expectedDocumentSHA256 string,
	keyPath string,
	expectedKeySHA256 string,
	expectedKeyID string,
) (json.RawMessage, []byte, error) {
	payload, object, err := parseCanonical(path, maxJSONBytes)
	if err != nil {
		return nil, nil, err
	}
	if digest(payload) != expectedDocumentSHA256 {
		return nil, nil, fmt.Errorf("%s differs from its authenticated digest", path)
	}
	if err := exactKeys(object, []string{"claims", "signature"}, path); err != nil {
		return nil, nil, err
	}
	var signed signature
	if err := decodeExact(
		object["signature"],
		[]string{"algorithm", "key_id", "value"},
		&signed,
		path+" signature",
	); err != nil {
		return nil, nil, err
	}
	if signed.Algorithm != "ed25519" || signed.KeyID != expectedKeyID {
		return nil, nil, fmt.Errorf("%s signer differs", path)
	}
	publicKey, err := readExact(keyPath, ed25519.PublicKeySize, expectedKeySHA256)
	if err != nil || len(publicKey) != ed25519.PublicKeySize {
		return nil, nil, fmt.Errorf("%s public key differs", path)
	}
	rawSignature, err := base64.StdEncoding.Strict().DecodeString(signed.Value)
	if err != nil || len(rawSignature) != ed25519.SignatureSize {
		return nil, nil, fmt.Errorf("%s signature encoding differs", path)
	}
	var claimsGeneric any
	decoder := json.NewDecoder(bytes.NewReader(object["claims"]))
	decoder.UseNumber()
	if err := decoder.Decode(&claimsGeneric); err != nil {
		return nil, nil, err
	}
	claimsPayload, err := canonical(claimsGeneric)
	if err != nil || !ed25519.Verify(publicKey, claimsPayload, rawSignature) {
		return nil, nil, fmt.Errorf("%s Ed25519 signature is invalid", path)
	}
	return object["claims"], payload, nil
}

func requiredString(value *string, label string) (string, error) {
	if value == nil || !validDigest(*value) {
		return "", fmt.Errorf("%s is not activated", label)
	}
	return *value, nil
}

func verifyFreshness(issuedAt string, expiresAt string) error {
	issued, err := time.Parse(time.RFC3339Nano, issuedAt)
	if err != nil {
		return errors.New("runtime attestation issued_at is malformed")
	}
	expires, err := time.Parse(time.RFC3339Nano, expiresAt)
	if err != nil {
		return errors.New("runtime attestation expires_at is malformed")
	}
	now := time.Now().UTC()
	if issued.After(now.Add(maxClockSkew)) || now.After(expires) || !expires.After(issued) || expires.Sub(issued) > maxLease {
		return errors.New("runtime attestation is stale or overlong")
	}
	return nil
}

func verifyRuntimeAttestation() (runtimeClaims, []byte, error) {
	if !validDigest(rootAuthorityKeySHA256) || rootAuthorityKeyID == "" || rootAuthorityPrincipalID == "" {
		return runtimeClaims{}, nil, errors.New("external root authority is not activated")
	}
	// The document hash is not caller-selected: the root signature covers all
	// claims, while canonical bytes prevent alternative encodings. The launcher
	// additionally seals the exact bytes before the measured worker can run.
	payload, object, err := parseCanonical(runtimeAttestationPath, maxJSONBytes)
	if err != nil {
		return runtimeClaims{}, nil, err
	}
	if err := exactKeys(object, []string{"claims", "signature"}, "runtime attestation"); err != nil {
		return runtimeClaims{}, nil, err
	}
	var signed signature
	if err := decodeExact(object["signature"], []string{"algorithm", "key_id", "value"}, &signed, "runtime signature"); err != nil {
		return runtimeClaims{}, nil, err
	}
	if signed.Algorithm != "ed25519" || signed.KeyID != rootAuthorityKeyID {
		return runtimeClaims{}, nil, errors.New("runtime signer differs from external root")
	}
	key, err := readExact(rootAuthorityKeyPath, ed25519.PublicKeySize, rootAuthorityKeySHA256)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return runtimeClaims{}, nil, errors.New("external root public key differs")
	}
	rawSignature, err := base64.StdEncoding.Strict().DecodeString(signed.Value)
	if err != nil || len(rawSignature) != ed25519.SignatureSize {
		return runtimeClaims{}, nil, errors.New("runtime signature is not raw Ed25519")
	}
	var generic any
	decoder := json.NewDecoder(bytes.NewReader(object["claims"]))
	decoder.UseNumber()
	if err := decoder.Decode(&generic); err != nil {
		return runtimeClaims{}, nil, err
	}
	claimsPayload, err := canonical(generic)
	if err != nil || !ed25519.Verify(key, claimsPayload, rawSignature) {
		return runtimeClaims{}, nil, errors.New("runtime attestation signature is invalid")
	}
	var claims runtimeClaims
	if err := decodeExact(
		object["claims"],
		[]string{
			"admission_objects", "api", "capsule_contract_sha256", "expires_at", "handoff", "image",
			"issued_at", "launcher", "nonce", "pod", "role", "schema",
			"signing_principal_id", "worker",
		},
		&claims,
		"runtime claims",
	); err != nil {
		return runtimeClaims{}, nil, err
	}
	if claims.Schema != "fs2-serve.nebius.ai/sai07-execution-capsule-runtime-attestation/v5" ||
		claims.SigningPrincipalID != rootAuthorityPrincipalID ||
		(claims.Role != "plan-apply" && claims.Role != "external-ack") ||
		claims.Nonce == "" {
		return runtimeClaims{}, nil, errors.New("runtime attestation identity differs")
	}
	if !validDigest(claims.API.CASHA256) || claims.API.Context == "" ||
		!strings.HasPrefix(claims.API.Origin, "https://") ||
		!validDigest(claims.Handoff.DirectoryIdentitySHA256) ||
		!strings.HasPrefix(claims.Handoff.Root, "/") {
		return runtimeClaims{}, nil, errors.New("runtime API or handoff identity is incomplete")
	}
	if err := verifyFreshness(claims.IssuedAt, claims.ExpiresAt); err != nil {
		return runtimeClaims{}, nil, err
	}
	return claims, payload, nil
}

func verifyActivation(claims runtimeClaims) (activationContract, []byte, error) {
	payload, object, err := parseCanonical(activationContractPath, maxJSONBytes)
	if err != nil {
		return activationContract{}, nil, err
	}
	if digest(payload) != claims.Launcher.ActivationContractSHA256 {
		return activationContract{}, nil, errors.New("launcher activation contract differs from signed claim")
	}
	if err := exactKeys(
		object,
		[]string{
			"activation", "admission", "builder_authorities", "builder_receipts",
			"build_contract", "launcher", "loader", "receipt_schema_path", "receipt_schema_sha256", "root_authority",
			"schema", "source", "status", "worker",
		},
		"launcher activation contract",
	); err != nil {
		return activationContract{}, nil, err
	}
	var contract activationContract
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&contract); err != nil {
		return activationContract{}, nil, err
	}
	launcherDigest, err := requiredString(contract.Launcher.SHA256, "launcher binary digest")
	if err != nil {
		return activationContract{}, nil, err
	}
	sourceDigest, err := requiredString(contract.Source.SHA256, "launcher source digest")
	if err != nil {
		return activationContract{}, nil, err
	}
	buildDigest, err := requiredString(contract.BuildContract.SHA256, "launcher build-contract digest")
	if err != nil {
		return activationContract{}, nil, err
	}
	receiptSchemaDigest, err := requiredString(contract.ReceiptSchemaSHA256, "builder receipt schema digest")
	if err != nil {
		return activationContract{}, nil, err
	}
	receiptSchemaPayload, err := readExact(builderReceiptSchemaPath, maxJSONBytes, receiptSchemaDigest)
	if err != nil || contract.ReceiptSchemaPath != builderReceiptSchemaPath || len(receiptSchemaPayload) == 0 {
		return activationContract{}, nil, errors.New("builder receipt schema differs from the root contract")
	}
	if contract.Activation != "active" ||
		contract.Schema != "fs2-serve.nebius.ai/sai07-capsule-launcher-activation/v1" ||
		contract.Status != "active-external-root-and-two-builder-provenance" ||
		contract.RootAuthority.KeyID != rootAuthorityKeyID ||
		contract.RootAuthority.PrincipalID != rootAuthorityPrincipalID ||
		contract.RootAuthority.PublicKeyPath != rootAuthorityKeyPath ||
		contract.RootAuthority.PublicKeySHA256 == nil || *contract.RootAuthority.PublicKeySHA256 != rootAuthorityKeySHA256 ||
		contract.Launcher.Path != "/opt/fs2-sai07/bootstrap/sai07-capsule-launcher" ||
		launcherDigest != claims.Launcher.BinarySHA256 ||
		contract.Source.Path != "scripts/sai07_capsule_launcher.go" ||
		sourceDigest != claims.Launcher.SourceSHA256 ||
		contract.BuildContract.Path != buildContractPath ||
		buildDigest != claims.Launcher.BuildContractSHA256 {
		return activationContract{}, nil, errors.New("launcher activation facts differ from signed root claims")
	}
	if !hasExactMapKeys(contract.Admission.EntrypointByRole, []string{"external-ack", "plan-apply"}) ||
		!hasExactMapKeys(contract.Admission.RequiredObjectsByRole, []string{"external-ack", "plan-apply"}) ||
		!hasExactMapKeys(contract.Loader.RequiredContainerNameByRole, []string{"external-ack", "plan-apply"}) ||
		contract.Loader.Mechanism != "kubelet-verified-oci-digest-and-root-signed-exact-admission" ||
		!contract.Loader.RequireDigestImage || !contract.Loader.RequirePID1 || !contract.Loader.RequireReadOnlyRoot ||
		contract.Loader.RequiredContainerNameByRole[claims.Role] != claims.Pod.ContainerName {
		return activationContract{}, nil, errors.New("launcher role/admission closure differs")
	}
	entrypoint := contract.Admission.EntrypointByRole[claims.Role]
	if len(entrypoint) < 2 || strings.Join(entrypoint, "\x00") != strings.Join(os.Args, "\x00") || entrypoint[0] != contract.Launcher.Path || entrypoint[1] != claims.Role {
		return activationContract{}, nil, errors.New("actual PID-1 entrypoint differs from root-signed role command")
	}
	requiredObjects := contract.Admission.RequiredObjectsByRole[claims.Role]
	if len(requiredObjects) == 0 || len(requiredObjects) != len(claims.AdmissionObjects) {
		return activationContract{}, nil, errors.New("root-signed admission-object closure is empty or incomplete")
	}
	paths := make([]string, 0, len(claims.AdmissionObjects))
	for _, item := range claims.AdmissionObjects {
		if item.APIPath == "" || item.UID == "" || item.ResourceVersion == "" || !validDigest(item.ObjectSHA256) {
			return activationContract{}, nil, errors.New("runtime admission identity is incomplete")
		}
		paths = append(paths, item.APIPath)
	}
	sort.Strings(paths)
	if strings.Join(requiredObjects, "\x00") != strings.Join(sortedCopy(requiredObjects), "\x00") || hasDuplicate(requiredObjects) {
		return activationContract{}, nil, errors.New("root-signed admission set is not sorted and unique")
	}
	if strings.Join(paths, "\x00") != strings.Join(requiredObjects, "\x00") {
		return activationContract{}, nil, errors.New("runtime admission set differs from external root contract")
	}
	return contract, payload, nil
}

func hasExactMapKeys[T any](values map[string]T, expected []string) bool {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	wanted := append([]string(nil), expected...)
	sort.Strings(wanted)
	return strings.Join(keys, "\x00") == strings.Join(wanted, "\x00")
}

func sortedCopy(values []string) []string {
	result := append([]string(nil), values...)
	sort.Strings(result)
	return result
}

func hasDuplicate(values []string) bool {
	for index := 1; index < len(values); index++ {
		if values[index-1] == values[index] {
			return true
		}
	}
	return false
}

func verifyBuildContract(claims runtimeClaims, activation activationContract) (launcherBuildContract, []byte, error) {
	expected, err := requiredString(activation.BuildContract.SHA256, "build-contract digest")
	if err != nil {
		return launcherBuildContract{}, nil, err
	}
	payload, object, err := parseCanonical(buildContractPath, maxJSONBytes)
	if err != nil {
		return launcherBuildContract{}, nil, err
	}
	if digest(payload) != expected || expected != claims.Launcher.BuildContractSHA256 {
		return launcherBuildContract{}, nil, errors.New("launcher build contract differs")
	}
	if err := exactKeys(object, []string{"activation", "build", "build_environment_sha256", "recipe_sha256", "schema", "source", "status"}, "launcher build contract"); err != nil {
		return launcherBuildContract{}, nil, err
	}
	var contract launcherBuildContract
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&contract); err != nil {
		return launcherBuildContract{}, nil, err
	}
	sourceDigest, err := requiredString(contract.Source.SHA256, "build source digest")
	if err != nil {
		return launcherBuildContract{}, nil, err
	}
	recipeDigest, err := requiredString(contract.RecipeSHA256, "build recipe digest")
	if err != nil {
		return launcherBuildContract{}, nil, err
	}
	toolchainDigest, err := requiredString(contract.Build.ToolchainArchiveSHA256, "Go toolchain digest")
	if err != nil {
		return launcherBuildContract{}, nil, err
	}
	stdlibDigest, err := requiredString(contract.Build.StandardLibraryTreeSHA256, "Go standard-library digest")
	if err != nil {
		return launcherBuildContract{}, nil, err
	}
	environmentDigest, err := requiredString(contract.BuildEnvironmentSHA256, "build environment digest")
	if err != nil {
		return launcherBuildContract{}, nil, err
	}
	if contract.Activation != "active" ||
		contract.Schema != "fs2-serve.nebius.ai/sai07-capsule-launcher-build/v1" ||
		contract.Status != "active-reviewed-static-launcher-recipe" ||
		contract.Source.Path != "scripts/sai07_capsule_launcher.go" ||
		sourceDigest != claims.Launcher.SourceSHA256 ||
		contract.Build.CGOEnabled != "0" || contract.Build.Goos != "linux" || contract.Build.Goarch != "amd64" ||
		contract.Build.GoVersion == nil || !goVersionPattern.MatchString(*contract.Build.GoVersion) ||
		contract.Build.SourceDateEpoch != "0" || !validDigest(toolchainDigest) || !validDigest(stdlibDigest) ||
		!validDigest(recipeDigest) || !validDigest(environmentDigest) ||
		strings.Join(contract.Build.Arguments, "\x00") != strings.Join(exactBuildArguments, "\x00") {
		return launcherBuildContract{}, nil, errors.New("launcher build recipe/toolchain closure is incomplete")
	}
	argumentsPayload, _ := canonical(contract.Build.Arguments)
	if digest(argumentsPayload) != recipeDigest {
		return launcherBuildContract{}, nil, errors.New("launcher recipe digest differs from exact arguments")
	}
	return contract, payload, nil
}

func verifyBuilderReceipts(
	claims runtimeClaims,
	activation activationContract,
	build launcherBuildContract,
	buildPayload []byte,
	launcherSHA256 string,
) ([]string, error) {
	if len(activation.BuilderAuthorities) != 2 || len(activation.BuilderReceipts) != 2 || len(claims.Launcher.BuilderReceiptSHA256s) != 2 {
		return nil, errors.New("exactly two independent builder receipts are required")
	}
	authorities := map[string]builderAuthority{}
	for _, authority := range activation.BuilderAuthorities {
		keyDigest, err := requiredString(authority.PublicKeySHA256, authority.Role+" public key digest")
		isolationDigest, isolationErr := requiredString(authority.IsolationEvidenceSHA256, authority.Role+" isolation evidence digest")
		if err != nil || isolationErr != nil ||
			(authority.Role != "builder-a" && authority.Role != "builder-b") ||
			authority.KeyID == "" || authority.PrincipalID == "" || authority.IsolationDomain == "" ||
			!strings.HasPrefix(authority.PublicKeyPath, "/opt/fs2-sai07/authorities/") ||
			!validDigest(keyDigest) || !validDigest(isolationDigest) {
			return nil, errors.New("builder authority is incomplete")
		}
		if _, exists := authorities[authority.Role]; exists {
			return nil, errors.New("duplicate builder authority role")
		}
		authorities[authority.Role] = authority
	}
	if len(authorities) != 2 {
		return nil, errors.New("builder authority closure differs")
	}
	receiptDigests := append([]string(nil), claims.Launcher.BuilderReceiptSHA256s...)
	sort.Strings(receiptDigests)
	if !validDigest(receiptDigests[0]) || !validDigest(receiptDigests[1]) || receiptDigests[0] == receiptDigests[1] {
		return nil, errors.New("builder receipt digest closure differs")
	}
	seenPrincipals := map[string]bool{}
	seenKeys := map[string]bool{}
	seenIsolation := map[string]bool{}
	seenNonces := map[string]bool{}
	verifiedDigests := make([]string, 0, 2)
	seenReceiptRoles := map[string]bool{}
	for _, reference := range activation.BuilderReceipts {
		expectedReceiptDigest, err := requiredString(reference.SHA256, reference.Role+" receipt digest")
		if err != nil {
			return nil, err
		}
		authority, present := authorities[reference.Role]
		if !present || seenReceiptRoles[reference.Role] || !strings.HasPrefix(reference.Path, "/opt/fs2-sai07/attestations/") {
			return nil, errors.New("builder receipt has no authorized identity")
		}
		seenReceiptRoles[reference.Role] = true
		keyDigest, _ := requiredString(authority.PublicKeySHA256, "builder public key digest")
		rawClaims, _, err := verifySignedDocument(reference.Path, expectedReceiptDigest, authority.PublicKeyPath, keyDigest, authority.KeyID)
		if err != nil {
			return nil, err
		}
		var receipt builderProvenance
		if err := decodeExact(
			rawClaims,
			[]string{"build_environment_sha256", "builder", "built_at", "materials_sha256", "nonce", "output", "recipe", "schema", "toolchain"},
			&receipt,
			"builder provenance claims",
		); err != nil {
			return nil, err
		}
		isolationDigest, _ := requiredString(authority.IsolationEvidenceSHA256, "builder isolation evidence digest")
		if receipt.Schema != "fs2-serve.nebius.ai/sai07-launcher-builder-provenance/v1" ||
			receipt.Builder.Role != reference.Role || receipt.Builder.KeyID != authority.KeyID ||
			receipt.Builder.PrincipalID != authority.PrincipalID || receipt.Builder.IsolationDomain != authority.IsolationDomain ||
			receipt.Builder.IsolationEvidenceSHA256 != isolationDigest ||
			seenPrincipals[receipt.Builder.PrincipalID] || seenKeys[receipt.Builder.KeyID] || seenIsolation[receipt.Builder.IsolationDomain] || seenNonces[receipt.Nonce] ||
			receipt.Nonce == "" || receipt.BuildEnvironmentSHA256 != *build.BuildEnvironmentSHA256 || !validDigest(receipt.MaterialsSHA256) ||
			receipt.Output.Path != activation.Launcher.Path || receipt.Output.SHA256 != launcherSHA256 ||
			receipt.Recipe.SourcePath != build.Source.Path || receipt.Recipe.SourceSHA256 != claims.Launcher.SourceSHA256 ||
			receipt.Recipe.BuildContractSHA256 != digest(buildPayload) || receipt.Recipe.ArgumentsSHA256 != *build.RecipeSHA256 ||
			receipt.Toolchain.CGOEnabled != build.Build.CGOEnabled || receipt.Toolchain.GoVersion != *build.Build.GoVersion ||
			receipt.Toolchain.Goos != build.Build.Goos || receipt.Toolchain.Goarch != build.Build.Goarch ||
			receipt.Toolchain.ToolchainArchiveSHA256 != *build.Build.ToolchainArchiveSHA256 ||
			receipt.Toolchain.StandardLibraryTreeSHA256 != *build.Build.StandardLibraryTreeSHA256 {
			return nil, errors.New("builder provenance does not bind the exact independent build closure")
		}
		materials, _ := canonical(map[string]string{
			"arguments_sha256":             receipt.Recipe.ArgumentsSHA256,
			"build_contract_sha256":        receipt.Recipe.BuildContractSHA256,
			"build_environment_sha256":     receipt.BuildEnvironmentSHA256,
			"source_sha256":                receipt.Recipe.SourceSHA256,
			"standard_library_tree_sha256": receipt.Toolchain.StandardLibraryTreeSHA256,
			"toolchain_archive_sha256":     receipt.Toolchain.ToolchainArchiveSHA256,
		})
		if digest(materials) != receipt.MaterialsSHA256 {
			return nil, errors.New("builder provenance materials projection differs")
		}
		if _, err := time.Parse(time.RFC3339Nano, receipt.BuiltAt); err != nil {
			return nil, errors.New("builder provenance built_at is malformed")
		}
		seenPrincipals[receipt.Builder.PrincipalID] = true
		seenKeys[receipt.Builder.KeyID] = true
		seenIsolation[receipt.Builder.IsolationDomain] = true
		seenNonces[receipt.Nonce] = true
		verifiedDigests = append(verifiedDigests, expectedReceiptDigest)
	}
	sort.Strings(verifiedDigests)
	if strings.Join(verifiedDigests, "\x00") != strings.Join(receiptDigests, "\x00") {
		return nil, errors.New("builder receipts differ from external root attestation")
	}
	return verifiedDigests, nil
}

func verifyImageEvidence(claims runtimeClaims) ([]byte, []byte, error) {
	if !strings.HasPrefix(claims.Image.Digest, "sha256:") || len(claims.Image.Digest) != 71 ||
		!strings.HasSuffix(claims.Image.Reference, "@"+claims.Image.Digest) || claims.Pod.ImageDigest != claims.Image.Digest ||
		(!strings.HasSuffix(claims.Pod.ImageID, "@"+claims.Image.Digest) && !strings.HasSuffix(claims.Pod.ImageID, "://"+claims.Image.Digest)) ||
		!validDigest(claims.Image.ProvenanceSHA256) || !validDigest(claims.Image.SBOMSHA256) {
		return nil, nil, errors.New("signed image reference, imageID and digest are not cross-bound")
	}
	provenancePayload, provenanceObject, err := parseCanonical(imageProvenancePath, maxJSONBytes)
	if err != nil || digest(provenancePayload) != claims.Image.ProvenanceSHA256 {
		return nil, nil, errors.New("canonical image provenance does not bind the signed image")
	}
	if err := exactKeys(provenanceObject, []string{"build_type", "builder_id", "image", "materials_sha256", "predicate_type", "schema"}, "image provenance"); err != nil {
		return nil, nil, err
	}
	var provenance imageProvenance
	decoder := json.NewDecoder(bytes.NewReader(provenancePayload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&provenance); err != nil ||
		provenance.Schema != "fs2-serve.nebius.ai/image-provenance/v1" ||
		provenance.PredicateType != "https://slsa.dev/provenance/v1" ||
		provenance.BuilderID == "" || provenance.BuildType == "" || !validDigest(provenance.MaterialsSHA256) ||
		provenance.Image.Digest != claims.Image.Digest || provenance.Image.Reference != claims.Image.Reference {
		return nil, nil, errors.New("canonical image provenance is incomplete or selects another image")
	}
	sbomPayload, sbomObject, err := parseCanonical(imageSBOMPath, maxJSONBytes)
	if err != nil || digest(sbomPayload) != claims.Image.SBOMSHA256 {
		return nil, nil, errors.New("canonical SPDX evidence does not bind the signed image")
	}
	if err := exactKeys(sbomObject, []string{"document", "format", "image", "schema"}, "image SBOM"); err != nil {
		return nil, nil, err
	}
	var sbom imageSBOM
	decoder = json.NewDecoder(bytes.NewReader(sbomPayload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&sbom); err != nil || sbom.Schema != "fs2-serve.nebius.ai/image-sbom/v1" ||
		sbom.Format != "spdx-json" || sbom.Image.Digest != claims.Image.Digest || sbom.Image.Reference != claims.Image.Reference {
		return nil, nil, errors.New("canonical SPDX envelope is incomplete or selects another image")
	}
	packages, packagesOK := sbom.Document["packages"].([]any)
	described, describedOK := sbom.Document["documentDescribes"].([]any)
	creation, creationOK := sbom.Document["creationInfo"].(map[string]any)
	name, nameOK := sbom.Document["name"].(string)
	namespace, namespaceOK := sbom.Document["documentNamespace"].(string)
	packageIDs := map[string]bool{}
	for _, raw := range packages {
		entry, ok := raw.(map[string]any)
		identifier, idOK := entry["SPDXID"].(string)
		if !ok || !idOK || identifier == "" {
			return nil, nil, errors.New("SPDX package identity is incomplete")
		}
		packageIDs[identifier] = true
	}
	for _, raw := range described {
		identifier, ok := raw.(string)
		if !ok || !packageIDs[identifier] {
			return nil, nil, errors.New("SPDX documentDescribes escapes the package closure")
		}
	}
	if sbom.Document["spdxVersion"] != "SPDX-2.3" || sbom.Document["SPDXID"] != "SPDXRef-DOCUMENT" ||
		sbom.Document["dataLicense"] != "CC0-1.0" || !nameOK || name == "" || !namespaceOK ||
		!strings.HasPrefix(namespace, "https://") || !creationOK || len(creation) == 0 ||
		!packagesOK || len(packages) == 0 || !describedOK || len(described) == 0 {
		return nil, nil, errors.New("canonical SPDX document is incomplete")
	}
	return provenancePayload, sbomPayload, nil
}

func exactGenericMap(value any, expected []string, label string) (map[string]any, error) {
	object, ok := value.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("%s is not an object", label)
	}
	actual := make([]string, 0, len(object))
	for key := range object {
		actual = append(actual, key)
	}
	sort.Strings(actual)
	wanted := append([]string(nil), expected...)
	sort.Strings(wanted)
	if strings.Join(actual, "\x00") != strings.Join(wanted, "\x00") {
		return nil, fmt.Errorf("%s fields differ from the closed contract", label)
	}
	return object, nil
}

func verifyCapsuleContract(
	claims runtimeClaims,
	activation activationContract,
	payload []byte,
) error {
	var generic any
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.UseNumber()
	if err := decoder.Decode(&generic); err != nil {
		return err
	}
	capsule, err := exactGenericMap(
		generic,
		[]string{"activation", "admission", "launcher", "runtime", "schema", "status"},
		"execution capsule",
	)
	if err != nil {
		return err
	}
	if capsule["activation"] != "active" ||
		capsule["schema"] != "fs2-serve.nebius.ai/sai07-execution-capsule-contract/v4" ||
		capsule["status"] != "active-native-rooted-launcher-and-external-evidence" {
		return errors.New("execution capsule is not activated for the native launcher")
	}
	launcherPayload, err := canonical(capsule["launcher"])
	if err != nil {
		return err
	}
	var launcher capsuleLauncherContract
	decoder = json.NewDecoder(bytes.NewReader(launcherPayload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&launcher); err != nil {
		return err
	}
	activationDigest, err := requiredString(launcher.ActivationContract.SHA256, "capsule launcher activation digest")
	if err != nil {
		return err
	}
	binaryDigest, err := requiredString(launcher.Binary.SHA256, "capsule launcher binary digest")
	if err != nil {
		return err
	}
	buildDigest, err := requiredString(launcher.BuildContract.SHA256, "capsule launcher build digest")
	if err != nil {
		return err
	}
	schemaDigest, err := requiredString(launcher.BuilderReceiptSchema.SHA256, "capsule builder receipt schema digest")
	if err != nil {
		return err
	}
	workerDigest, err := requiredString(launcher.WorkerScript.SHA256, "capsule worker script digest")
	if err != nil {
		return err
	}
	activationSchemaDigest, _ := requiredString(activation.ReceiptSchemaSHA256, "activation receipt schema digest")
	if launcher.ActivationContract.Path != activationContractPath || activationDigest != claims.Launcher.ActivationContractSHA256 ||
		launcher.Binary.Path != activation.Launcher.Path || binaryDigest != claims.Launcher.BinarySHA256 ||
		launcher.BuildContract.Path != buildContractPath || buildDigest != claims.Launcher.BuildContractSHA256 ||
		launcher.BuilderReceiptSchema.Path != builderReceiptSchemaPath || schemaDigest != activationSchemaDigest ||
		launcher.NativeLaunchGrantFD != fdLaunchGrant || launcher.WorkerScriptFD != fdWorker ||
		launcher.RuntimeAttestationPath != runtimeAttestationPath ||
		launcher.RuntimeAttestationSchema != claims.Schema ||
		launcher.WorkerScript.Path != activation.Worker.Script.Path || workerDigest != claims.Worker.ScriptSHA256 {
		return errors.New("capsule launcher closure differs from the external root claims")
	}
	runtime, err := exactGenericMap(
		capsule["runtime"],
		[]string{
			"authority_keys", "cluster_ca_fd", "contract_files", "filesystem", "handoff_root",
			"plan_variables_fd", "platform_kubeconfig_fd", "provider_bundle", "runtime_files",
			"saved_plan_fd", "terraform_configuration_sha256", "terraform_data_roots",
			"terraform_data_tree_sha256", "terraform_root_tree_sha256", "terraform_roots", "terraform_version",
		},
		"capsule runtime",
	)
	if err != nil {
		return err
	}
	files, err := exactGenericMap(
		runtime["runtime_files"],
		[]string{"image_provenance", "image_sbom", "kubectl", "openssl", "python", "source_bundle", "terraform", "terraform_cli_config"},
		"capsule runtime files",
	)
	if err != nil {
		return err
	}
	expectedFiles := map[string]filePin{
		"image_provenance": {Path: imageProvenancePath, SHA256: &claims.Image.ProvenanceSHA256},
		"image_sbom":       {Path: imageSBOMPath, SHA256: &claims.Image.SBOMSHA256},
		"python":           activation.Worker.Python,
		"source_bundle":    activation.Worker.SourceBundle,
	}
	expectedFDs := map[string]int{
		"image_provenance": 189,
		"image_sbom":       200,
		"python":           fdPython,
		"source_bundle":    fdSourceBundle,
	}
	for name, expected := range expectedFiles {
		encoded, err := canonical(files[name])
		if err != nil {
			return err
		}
		var pin struct {
			FD int `json:"fd"`
			Path string `json:"path"`
			SHA256 *string `json:"sha256"`
		}
		decoder = json.NewDecoder(bytes.NewReader(encoded))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&pin); err != nil || pin.FD != expectedFDs[name] || pin.SHA256 == nil || expected.SHA256 == nil ||
			pin.Path != expected.Path || *pin.SHA256 != *expected.SHA256 {
			return fmt.Errorf("capsule %s identity differs from the root activation", name)
		}
	}
	return nil
}

func memfdCreate(name string) (int, error) {
	nameBytes := append([]byte(name), 0)
	fd, _, errno := syscall.Syscall(
		syscall.SYS_MEMFD_CREATE,
		uintptr(unsafe.Pointer(&nameBytes[0])),
		uintptr(mfdCloexec|mfdAllowSealing),
		0,
	)
	if errno != 0 {
		return -1, errno
	}
	return int(fd), nil
}

func sealBytes(payload []byte, targetFD int, name string) error {
	if len(payload) == 0 || len(payload) > maxRuntimeBytes {
		return fmt.Errorf("%s violates its byte bound", name)
	}
	fd, err := memfdCreate(name)
	if err != nil {
		return err
	}
	defer syscall.Close(fd)
	for offset := 0; offset < len(payload); {
		written, err := syscall.Write(fd, payload[offset:])
		if err != nil || written <= 0 {
			return fmt.Errorf("%s memfd write failed", name)
		}
		offset += written
	}
	if err := syscall.Fsync(fd); err != nil {
		return err
	}
	if _, _, errno := syscall.Syscall(syscall.SYS_FCNTL, uintptr(fd), uintptr(fAddSeals), requiredSeals); errno != 0 {
		return errno
	}
	if err := syscall.Dup3(fd, targetFD, 0); err != nil {
		return err
	}
	seals, _, errno := syscall.Syscall(syscall.SYS_FCNTL, uintptr(targetFD), uintptr(fGetSeals), 0)
	if errno != 0 || seals&requiredSeals != requiredSeals {
		return fmt.Errorf("%s target descriptor is not write sealed", name)
	}
	_, err = syscall.Seek(targetFD, 0, io.SeekStart)
	return err
}

func verifyDownwardIdentity(claims runtimeClaims) error {
	if os.Getenv("FS2_SAI07_POD_NAME") != claims.Pod.Name ||
		os.Getenv("FS2_SAI07_POD_NAMESPACE") != claims.Pod.Namespace ||
		os.Getenv("FS2_SAI07_POD_UID") != claims.Pod.UID ||
		claims.Pod.ContainerName == "" || claims.Pod.ResourceVersion == "" || claims.Pod.SecurityProjectionSHA256 == "" || claims.Pod.ServiceAccountName == "" {
		return errors.New("downward Pod identity differs from root-signed attestation")
	}
	return nil
}

func main() {
	if os.Getpid() != 1 {
		fail("launcher must be the actual capsule PID 1")
	}
	claims, attestationPayload, err := verifyRuntimeAttestation()
	if err != nil {
		fail("%v", err)
	}
	activation, activationPayload, err := verifyActivation(claims)
	if err != nil {
		fail("%v", err)
	}
	build, buildPayload, err := verifyBuildContract(claims, activation)
	if err != nil {
		fail("%v", err)
	}
	selfPayload, err := readSelfExact(maxRuntimeBytes, claims.Launcher.BinarySHA256)
	if err != nil {
		fail("launcher self-measurement failed: %v", err)
	}
	if err := verifyStaticLauncherELF(selfPayload); err != nil {
		fail("launcher static-runtime closure failed: %v", err)
	}
	verifiedReceipts, err := verifyBuilderReceipts(claims, activation, build, buildPayload, digest(selfPayload))
	if err != nil {
		fail("%v", err)
	}
	if err := verifyDownwardIdentity(claims); err != nil {
		fail("%v", err)
	}
	provenancePayload, sbomPayload, err := verifyImageEvidence(claims)
	if err != nil {
		fail("%v", err)
	}
	capsulePayload, err := readExact(capsuleContractPath, maxJSONBytes, claims.CapsuleContractSHA256)
	if err != nil {
		fail("capsule contract differs: %v", err)
	}
	if err := verifyCapsuleContract(claims, activation, capsulePayload); err != nil {
		fail("capsule contract closure differs: %v", err)
	}
	pythonDigest, err := requiredString(activation.Worker.Python.SHA256, "worker Python digest")
	if err != nil || pythonDigest != claims.Worker.PythonSHA256 {
		fail("worker Python identity differs")
	}
	workerDigest, err := requiredString(activation.Worker.Script.SHA256, "worker script digest")
	if err != nil || workerDigest != claims.Worker.ScriptSHA256 {
		fail("worker script identity differs")
	}
	bundleDigest, err := requiredString(activation.Worker.SourceBundle.SHA256, "worker source-bundle digest")
	if err != nil || bundleDigest != claims.Worker.SourceBundleSHA256 {
		fail("worker source-bundle identity differs")
	}
	pythonPayload, err := readExact(activation.Worker.Python.Path, maxRuntimeBytes, pythonDigest)
	if err != nil {
		fail("worker Python differs: %v", err)
	}
	workerPayload, err := readExact(activation.Worker.Script.Path, maxRuntimeBytes, workerDigest)
	if err != nil {
		fail("worker script differs: %v", err)
	}
	bundlePayload, err := readExact(activation.Worker.SourceBundle.Path, maxRuntimeBytes, bundleDigest)
	if err != nil {
		fail("worker source bundle differs: %v", err)
	}
	workerArguments := append([]string{claims.Role}, os.Args[2:]...)
	if strings.Join(workerArguments, "\x00") != strings.Join(claims.Worker.Argv, "\x00") {
		fail("worker argv differs from the external root attestation")
	}
	argvPayload, _ := canonical(workerArguments)
	grant := launchGrant{
		ActivationContractSHA256: digest(activationPayload),
		BuilderReceiptSHA256s:    verifiedReceipts,
		CapsuleContractSHA256:    digest(capsulePayload),
		ImageDigest:              claims.Image.Digest,
		LauncherBinarySHA256:     digest(selfPayload),
		Role:                     claims.Role,
		RuntimeAttestationSHA256: digest(attestationPayload),
		Schema:                   "fs2-serve.nebius.ai/sai07-native-launch-grant/v1",
		WorkerArgvSHA256:         digest(argvPayload),
		WorkerPythonSHA256:       pythonDigest,
		WorkerScriptSHA256:       workerDigest,
		WorkerSourceBundleSHA256: bundleDigest,
	}
	grantPayload, err := canonical(grant)
	if err != nil {
		fail("launch grant encoding failed: %v", err)
	}
	for _, item := range []struct {
		payload []byte
		fd      int
		name    string
	}{
		{capsulePayload, fdCapsuleContract, "capsule-contract"},
		{attestationPayload, fdRuntimeAttestation, "runtime-attestation"},
		{bundlePayload, fdSourceBundle, "source-bundle"},
		{pythonPayload, fdPython, "python"},
		{provenancePayload, 189, "image-provenance"},
		{sbomPayload, 200, "image-sbom"},
		{grantPayload, fdLaunchGrant, "native-launch-grant"},
		{workerPayload, fdWorker, "python-worker"},
	} {
		if err := sealBytes(item.payload, item.fd, item.name); err != nil {
			fail("%s sealing failed: %v", item.name, err)
		}
	}
	environment := []string{
		"FS2_SAI07_NATIVE_LAUNCH_GRANT_FD=207",
		"FS2_SAI07_POD_NAME=" + claims.Pod.Name,
		"FS2_SAI07_POD_NAMESPACE=" + claims.Pod.Namespace,
		"FS2_SAI07_POD_UID=" + claims.Pod.UID,
	}
	pythonArgv := append([]string{"/proc/self/fd/191", "/proc/self/fd/208"}, workerArguments...)
	if err := syscall.Exec("/proc/self/fd/191", pythonArgv, environment); err != nil {
		fail("measured Python exec failed: %v", err)
	}
}
