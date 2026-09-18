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
	"time"
	"unsafe"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/custodycontract"
)

const manifestSchema = "fs2-serve.nebius.ai/public-edge-custody-install/v3"
const bakedAcceptanceTrustPath = "/usr/local/share/fs2-boundary/trusted-acceptance-issuers.json"
const maximumManifestBytes = 1024 * 1024
const maximumEntryBytes = 64 * 1024 * 1024
const linuxOTmpfile = 0x410000
const linuxATSymlinkFollow = 0x400
const linuxTmpfsMagic = 0x01021994
const linuxExtMagic = 0xef53
const linuxXFSMagic = 0x58465342
const custodyRootInitialMode = 0o777
const custodyRootSealedMode = 0o700

type manifest struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	Component string `json:"component"`
	InstallationID string `json:"installation_id"`
	VolumeRoots []volumeRoot `json:"volume_roots"`
	Directories []directory `json:"directories"`
	WritableRoots []writableRoot `json:"writable_roots"`
	Entries []entry `json:"entries"`
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

type entry struct {
	Source string `json:"source"`
	Destination string `json:"destination"`
	SHA256 string `json:"sha256"`
	UID uint32 `json:"uid"`
	GID uint32 `json:"gid"`
	Mode uint32 `json:"mode"`
}

func main() {
	if os.Geteuid() != 0 || os.Getegid() != 0 || len(os.Args) != 3 || !safeText(os.Args[2], 64) { slog.Error("custody installer requires root, one signed enrollment envelope and one component identity"); os.Exit(1) }
	if err := requireCurrentProductionTrust(); err != nil { slog.Error("production trust lifecycle rejected before custody enrollment", "error", err); os.Exit(1) }
	if !filepath.IsAbs(os.Args[1]) || !strings.HasPrefix(filepath.Clean(os.Args[1]), "/projected/manifest/") || filepath.Clean(os.Args[1]) != os.Args[1] { slog.Error("custody enrollment envelope path is outside the projected manifest lane"); os.Exit(1) }
	envelopeRaw, err := readBounded(os.Args[1], maximumManifestBytes)
	if err != nil { slog.Error("custody enrollment envelope unavailable", "error", err); os.Exit(1) }
	trustRaw, err := readBakedTrust(bakedAcceptanceTrustPath, maximumManifestBytes)
	if err != nil { slog.Error("independently baked custody trust unavailable", "error", err); os.Exit(1) }
	raw, err := boundary.VerifyCustodyEnrollment(trustRaw, envelopeRaw)
	if err != nil { slog.Error("custody enrollment signature invalid", "error", err); os.Exit(1) }
	var plan manifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&plan); err != nil || decoder.Decode(&struct{}{}) != io.EOF || plan.Schema != manifestSchema || !safeText(plan.ClusterID, 128) || !safeText(plan.DeploymentID, 128) || plan.Component != os.Args[2] || !digestText(plan.InstallationID) || len(plan.VolumeRoots) > 8 || len(plan.Directories) > 64 || len(plan.WritableRoots) > 8 || len(plan.Entries) > 256 || len(plan.VolumeRoots)+len(plan.Directories)+len(plan.WritableRoots)+len(plan.Entries) < 1 { slog.Error("custody manifest invalid"); os.Exit(1) }
	canonical, err := json.Marshal(plan)
	if err != nil || !bytes.Equal(canonical, raw) { slog.Error("custody manifest is not canonical signed JSON"); os.Exit(1) }
	if err := requireCurrentProductionTrust(); err != nil { slog.Error("production trust lifecycle expired before custody root mutation", "error", err); os.Exit(1) }
	if err := sealCustodyRoot(); err != nil { slog.Error("custody root cannot be authenticated and sealed", "error", err); os.Exit(1) }
	seen := map[string]bool{}
	directories := map[string]directory{}
	previous := ""
	for _, item := range plan.VolumeRoots {
		if err := requireCurrentProductionTrust(); err != nil { slog.Error("production trust lifecycle expired during custody volume enrollment", "error", err); os.Exit(1) }
		if item.Destination <= previous { slog.Error("custody volume roots are not strictly ordered"); os.Exit(1) }
		previous = item.Destination
		contract, err := sealVolumeRoot(item, seen)
		if err != nil { slog.Error("custody volume root failed", "destination", item.Destination, "error", err); os.Exit(1) }
		directories[item.Destination] = contract
	}
	previous = ""
	for _, item := range plan.Directories {
		if err := requireCurrentProductionTrust(); err != nil { slog.Error("production trust lifecycle expired during custody directory enrollment", "error", err); os.Exit(1) }
		if item.Destination <= previous { slog.Error("custody directories are not strictly ordered"); os.Exit(1) }
		previous = item.Destination
		if err := installDirectory(item, seen, directories); err != nil { slog.Error("custody directory failed", "destination", item.Destination, "error", err); os.Exit(1) }
	}
	previous = ""
	for _, item := range plan.WritableRoots {
		if err := requireCurrentProductionTrust(); err != nil { slog.Error("production trust lifecycle expired during custody writer enrollment", "error", err); os.Exit(1) }
		if item.Destination <= previous { slog.Error("custody writable roots are not strictly ordered"); os.Exit(1) }
		previous = item.Destination
		if err := installWritableRoot(item, seen, directories); err != nil { slog.Error("custody writable root failed", "destination", item.Destination, "error", err); os.Exit(1) }
	}
	previous = ""
	for _, item := range plan.Entries {
		if err := requireCurrentProductionTrust(); err != nil { slog.Error("production trust lifecycle expired during custody file publication", "error", err); os.Exit(1) }
		if item.Destination <= previous { slog.Error("custody entries are not strictly ordered"); os.Exit(1) }
		previous = item.Destination
		if err := install(item, seen, directories); err != nil { slog.Error("custody publication failed", "destination", item.Destination, "error", err); os.Exit(1) }
	}
	if err := requireCurrentProductionTrust(); err != nil { slog.Error("production trust lifecycle expired before custody completion", "error", err); os.Exit(1) }
}

func requireCurrentProductionTrust() error {
	_, err := boundary.VerifyInstalledProductionTrust(time.Now().UTC())
	return err
}

func sealVolumeRoot(item volumeRoot, seen map[string]bool) (directory, error) {
	contract := directory{Destination: item.Destination, UID: 0, GID: item.GID, Mode: item.Mode}
	if !validCustodyPath(item.Destination) || filepath.Dir(item.Destination) != "/custody" || seen[item.Destination] || item.Filesystem != "ext4" && item.Filesystem != "xfs" || item.Mode != 0o750 { return directory{}, errors.New("custody volume root contract is invalid") }
	seen[item.Destination] = true
	rootFD, err := openSealedCustodyRoot()
	if err != nil { return directory{}, err }
	defer syscall.Close(rootFD)
	fd, err := syscall.Openat(rootFD, filepath.Base(item.Destination), syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return directory{}, err }
	defer syscall.Close(fd)
	var rootStat syscall.Stat_t
	var stat syscall.Stat_t
	var filesystem syscall.Statfs_t
	if syscall.Fstat(rootFD, &rootStat) != nil || syscall.Fstat(fd, &stat) != nil || syscall.Fstatfs(fd, &filesystem) != nil || stat.Mode&syscall.S_IFMT != syscall.S_IFDIR || stat.Uid != 0 || stat.Dev == rootStat.Dev || filesystemName(filesystem.Type) != item.Filesystem { return directory{}, errors.New("custody volume root is not the exact separate root-owned filesystem mount") }
	mode := uint32(stat.Mode)&0o7777
	initial := mode == 0o755 && stat.Gid == 0
	interrupted := mode == 0o755 && stat.Gid == contract.GID
	retained := mode == contract.Mode && stat.Gid == contract.GID
	if !initial && !interrupted && !retained { return directory{}, errors.New("custody volume root has an unexpected pre-seal identity or mode") }
	if mode == 0o755 {
		if initial {
			if err := syscall.Fchown(fd, 0, int(contract.GID)); err != nil { return directory{}, err }
		}
		if err := syscall.Fchmod(fd, contract.Mode); err != nil { return directory{}, err }
	}
	if err := syscall.Fsync(fd); err != nil { return directory{}, err }
	if err := syscall.Fsync(rootFD); err != nil { return directory{}, err }
	var sealed syscall.Stat_t
	if syscall.Fstat(fd, &sealed) != nil || sealed.Dev != stat.Dev || sealed.Ino != stat.Ino || sealed.Uid != 0 || sealed.Gid != contract.GID || uint32(sealed.Mode)&0o2777 != contract.Mode || filesystemName(filesystem.Type) != item.Filesystem { return directory{}, errors.New("custody volume root changed or was not atomically sealed") }
	return contract, nil
}

func installWritableRoot(item writableRoot, seen map[string]bool, directories map[string]directory) error {
	if !validCustodyPath(item.Destination) || seen[item.Destination] || item.UID == 0 || item.GID == 0 || item.Mode != 0o700 { return errors.New("custody writable root contract is invalid") }
	parent := filepath.Dir(item.Destination)
	if _, declared := directories[parent]; !declared { return errors.New("custody writable root parent is absent from the signed protected directory plan") }
	seen[item.Destination] = true
	parentFD, err := openCustodyDirectory(parent, directories)
	if err != nil { return err }
	defer syscall.Close(parentFD)
	name := filepath.Base(item.Destination)
	var retained syscall.Stat_t
	if err := fstatAtNoFollow(parentFD, name, &retained); err == nil {
		mode := uint32(retained.Mode)&0o7777
		if retained.Mode&syscall.S_IFMT != syscall.S_IFDIR || mode != item.Mode { return errors.New("retained custody writable root differs from its exact private contract") }
		if retained.Uid == item.UID && retained.Gid == item.GID { return syscall.Fsync(parentFD) }
		if retained.Uid != 0 || retained.Gid != 0 { return errors.New("retained custody writable root is not an exact signed in-progress identity") }
	} else if err != syscall.ENOENT { return err }
	if retained.Mode == 0 {
		if err := mkdirAt(parentFD, name, item.Mode); err != nil { return err }
	}
	fd, err := syscall.Openat(parentFD, name, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return err }
	defer syscall.Close(fd)
	if err := syscall.Fchown(fd, int(item.UID), int(item.GID)); err != nil { return err }
	if err := syscall.Fchmod(fd, item.Mode); err != nil { return err }
	if err := syscall.Fsync(fd); err != nil { return err }
	var stat syscall.Stat_t
	if syscall.Fstat(fd, &stat) != nil || stat.Mode&syscall.S_IFMT != syscall.S_IFDIR || stat.Uid != item.UID || stat.Gid != item.GID || uint32(stat.Mode)&0o7777 != item.Mode { return errors.New("custody writable root cannot be established exactly") }
	return syscall.Fsync(parentFD)
}

func filesystemName(value int64) string {
	switch uint64(value) {
	case linuxExtMagic: return "ext4"
	case linuxXFSMagic: return "xfs"
	default: return ""
	}
}

func installDirectory(item directory, seen map[string]bool, directories map[string]directory) error {
	if !custodycontract.ValidDirectoryDestination(item.Destination) || seen[item.Destination] || item.UID != 0 || item.Mode != 0o700 && item.Mode != 0o750 && item.Mode != 0o2750 { return errors.New("custody directory contract is invalid") }
	seen[item.Destination] = true
	parentPath := filepath.Dir(item.Destination)
	if parentPath != "/custody" {
		if _, declared := directories[parentPath]; !declared { return errors.New("custody directory parent is absent from the signed installed directory plan") }
	}
	rootFD, err := openCustodyDirectory(parentPath, directories)
	if err != nil { return err }
	defer syscall.Close(rootFD)
	name := filepath.Base(item.Destination)
	fd, err := syscall.Openat(rootFD, name, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err == syscall.ENOENT {
		if err := mkdirAt(rootFD, name, item.Mode); err != nil { return err }
		fd, err = syscall.Openat(rootFD, name, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	}
	if err != nil { return err }
	defer syscall.Close(fd)
	var before syscall.Stat_t
	if syscall.Fstat(fd, &before) != nil || before.Mode&syscall.S_IFMT != syscall.S_IFDIR { return errors.New("custody directory is not an exact directory inode") }
	mode := uint32(before.Mode)&0o2777
	if before.Uid == item.UID && before.Gid == item.GID && mode == item.Mode {
		// Exact retained state; only durability confirmation remains.
	} else if before.Uid == 0 && before.Gid == 0 && mode == item.Mode {
		// mkdirat published the one signed in-progress state. Finish it
		// idempotently after a crash between mkdir, chown and chmod.
		if err := syscall.Fchown(fd, int(item.UID), int(item.GID)); err != nil { return err }
		if err := syscall.Fchmod(fd, item.Mode); err != nil { return err }
		if err := syscall.Fsync(fd); err != nil { return err }
	} else {
		return errors.New("custody directory differs from its exact final or signed in-progress state")
	}
	var stat syscall.Stat_t
	if syscall.Fstat(fd, &stat) != nil || stat.Mode&syscall.S_IFMT != syscall.S_IFDIR || stat.Uid != item.UID || stat.Gid != item.GID || uint32(stat.Mode)&0o2777 != item.Mode { return errors.New("custody directory cannot be established exactly") }
	if err := syscall.Fsync(rootFD); err != nil { return err }
	directories[item.Destination] = item
	return nil
}

func install(item entry, seen map[string]bool, directories map[string]directory) error {
	if !filepath.IsAbs(item.Source) || !strings.HasPrefix(filepath.Clean(item.Source), "/projected/inputs/") || !filepath.IsAbs(item.Destination) || !strings.HasPrefix(filepath.Clean(item.Destination), "/custody/") || item.Source != filepath.Clean(item.Source) || item.Destination != filepath.Clean(item.Destination) || strings.ContainsAny(item.Source+item.Destination, "\x00\r\n") || seen[item.Destination] || len(item.SHA256) != 64 || item.UID != 0 || (item.Mode != 0o400 && item.Mode != 0o440 && item.Mode != 0o444 && item.Mode != 0o600) {
		return errors.New("custody entry is outside its exact path, identity, mode or digest contract")
	}
	if _, err := hex.DecodeString(item.SHA256); err != nil { return errors.New("custody digest is not lowercase SHA-256") }
	seen[item.Destination] = true
	raw, err := readBounded(item.Source, maximumEntryBytes)
	if err != nil || digest(raw) != item.SHA256 { return errors.New("projected source bytes differ from the accepted digest") }
	parent := filepath.Dir(item.Destination)
	parentContract, declared := directories[parent]
	if !declared || parentContract.GID != item.GID { return errors.New("custody entry parent is absent from or has a different group than the signed directory plan") }
	parentFD, err := openCustodyDirectory(parent, directories)
	if err != nil { return err }
	defer syscall.Close(parentFD)
	if existing, err := readExactDestinationAt(parentFD, filepath.Base(item.Destination), item); err == nil { if bytes.Equal(existing, raw) { return syscall.Fsync(parentFD) }; return errors.New("custody destination conflicts with retained bytes") } else if !errors.Is(err, os.ErrNotExist) { return err }
	fd, err := syscall.Openat(parentFD, ".", syscall.O_RDWR|syscall.O_CLOEXEC|linuxOTmpfile, item.Mode)
	if err != nil { return err }
	file := os.NewFile(uintptr(fd), item.Destination)
	defer file.Close()
	if err := syscall.Fchown(fd, int(item.UID), int(item.GID)); err != nil { return err }
	if err := syscall.Fchmod(fd, item.Mode); err != nil { return err }
	if _, err := io.Copy(file, bytes.NewReader(raw)); err != nil { return err }
	if err := file.Sync(); err != nil { return err }
	procPath := "/proc/self/fd/"+strconv.Itoa(fd)
	procInfo, procErr := os.Stat(procPath)
	fileInfo, fileErr := file.Stat()
	if procErr != nil || fileErr != nil || !os.SameFile(procInfo, fileInfo) { return errors.New("anonymous custody inode identity cannot be proven") }
	oldPath, _ := syscall.BytePtrFromString(procPath)
	newPath, _ := syscall.BytePtrFromString(filepath.Base(item.Destination))
	directory := -100
	_, _, errno := syscall.RawSyscall6(syscall.SYS_LINKAT, uintptr(directory), uintptr(unsafe.Pointer(oldPath)), uintptr(parentFD), uintptr(unsafe.Pointer(newPath)), uintptr(linuxATSymlinkFollow), 0)
	if errno != 0 && errno != syscall.EEXIST { return errno }
	retained, err := readExactDestinationAt(parentFD, filepath.Base(item.Destination), item)
	if err != nil || !bytes.Equal(retained, raw) { return errors.New("no-replace custody publication conflicts") }
	return syscall.Fsync(parentFD)
}

func openCustodyDirectory(path string, directories map[string]directory) (int, error) {
	if path != "/custody" && !validCustodyPath(path) { return -1, errors.New("custody parent escapes its signed root") }
	rootFD, err := openSealedCustodyRoot()
	if err != nil { return -1, err }
	current := rootFD
	components := strings.Split(strings.TrimPrefix(filepath.Clean(path), "/custody/"), "/")
	if path == "/custody" { components = nil }
	if len(components) > custodycontract.MaximumDirectoryDepth { syscall.Close(current); return -1, errors.New("custody parent depth exceeds its signed bound") }
	prefix := "/custody"
	for _, component := range components {
		if component == "" || component == "." || component == ".." { syscall.Close(current); return -1, errors.New("custody parent component is invalid") }
		prefix += "/"+component
		contract, declared := directories[prefix]
		if !declared { syscall.Close(current); return -1, errors.New("custody parent ancestry is absent from the signed installed directory plan") }
		next, openErr := syscall.Openat(current, component, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
		syscall.Close(current)
		if openErr != nil { return -1, openErr }
		var stat syscall.Stat_t
		if syscall.Fstat(next, &stat) != nil || stat.Mode&syscall.S_IFMT != syscall.S_IFDIR || stat.Uid != contract.UID || stat.Gid != contract.GID || uint32(stat.Mode)&0o2777 != contract.Mode || stat.Mode&0o022 != 0 { syscall.Close(next); return -1, errors.New("custody parent ancestry differs from its signed root-owned protected directory contract") }
		current = next
	}
	return current, nil
}

func readExactDestinationAt(parent int, name string, item entry) ([]byte, error) {
	fd, err := syscall.Openat(parent, name, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return nil, err }
	file := os.NewFile(uintptr(fd), name)
	defer file.Close()
	var stat syscall.Stat_t
	if syscall.Fstat(fd, &stat) != nil || stat.Mode&syscall.S_IFMT != syscall.S_IFREG || uint32(stat.Mode)&0o777 != item.Mode || stat.Uid != item.UID || stat.Gid != item.GID || stat.Size < 1 || stat.Size > maximumEntryBytes { return nil, errors.New("custody destination lacks exact regular-file ownership or mode") }
	raw, err := io.ReadAll(io.LimitReader(file, maximumEntryBytes+1))
	if err != nil || int64(len(raw)) != stat.Size || digest(raw) != item.SHA256 { return nil, errors.New("custody destination bytes changed") }
	return raw, nil
}

func readBounded(path string, maximum int64) ([]byte, error) {
	file, err := os.Open(path)
	if err != nil { return nil, err }
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > maximum { return nil, errors.New("source is not a bounded regular target") }
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(raw)) != info.Size() { return nil, errors.New("source cannot be read exactly") }
	return raw, nil
}

func readBakedTrust(path string, maximum int64) ([]byte, error) {
	for parent := filepath.Dir(path); parent != "/"; parent = filepath.Dir(parent) {
		info, err := os.Lstat(parent)
		stat, ok := func() (*syscall.Stat_t, bool) { if info == nil { return nil, false }; value, valid := info.Sys().(*syscall.Stat_t); return value, valid }()
		if err != nil || !ok || !info.IsDir() || stat.Uid != 0 || info.Mode().Perm()&0o022 != 0 { return nil, errors.New("baked trust ancestry is not root-owned and protected") }
	}
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o022 != 0 { return nil, errors.New("baked trust is not an immutable regular file") }
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 { return nil, errors.New("baked trust is not root owned") }
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return nil, err }
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) || opened.Size() < 1 || opened.Size() > maximum { return nil, errors.New("baked trust identity changed") }
	return io.ReadAll(io.LimitReader(file, maximum+1))
}

func sealCustodyRoot() error {
	info, err := os.Lstat("/custody")
	if err != nil || info == nil || !info.IsDir() { return errors.New("custody root is not a directory") }
	fd, err := syscall.Open("/custody", syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return err }
	defer syscall.Close(fd)
	var opened syscall.Stat_t
	var rootFilesystem syscall.Stat_t
	var filesystem syscall.Statfs_t
	if syscall.Fstat(fd, &opened) != nil || syscall.Stat("/", &rootFilesystem) != nil || syscall.Fstatfs(fd, &filesystem) != nil || !sameStatFile(info, &opened) || opened.Mode&syscall.S_IFMT != syscall.S_IFDIR || opened.Uid != 0 || opened.Gid != 0 || opened.Dev == rootFilesystem.Dev || uint64(filesystem.Type) != linuxTmpfsMagic { return errors.New("custody root is not the exact distinct root-owned tmpfs mount") }
	mode := uint32(opened.Mode)&0o7777
	if mode != custodyRootInitialMode && mode != custodyRootSealedMode { return errors.New("custody root has an unexpected pre-seal mode") }
	if mode == custodyRootInitialMode {
		if err := syscall.Fchmod(fd, custodyRootSealedMode); err != nil { return err }
	}
	if err := syscall.Fsync(fd); err != nil { return err }
	var sealed syscall.Stat_t
	if syscall.Fstat(fd, &sealed) != nil || sealed.Dev != opened.Dev || sealed.Ino != opened.Ino || sealed.Uid != 0 || sealed.Gid != 0 || uint32(sealed.Mode)&0o7777 != custodyRootSealedMode { return errors.New("custody root changed or was not atomically sealed") }
	return nil
}

func openSealedCustodyRoot() (int, error) {
	fd, err := syscall.Open("/custody", syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return -1, err }
	var stat syscall.Stat_t
	var rootFilesystem syscall.Stat_t
	var filesystem syscall.Statfs_t
	if syscall.Fstat(fd, &stat) != nil || syscall.Stat("/", &rootFilesystem) != nil || syscall.Fstatfs(fd, &filesystem) != nil || stat.Mode&syscall.S_IFMT != syscall.S_IFDIR || stat.Uid != 0 || stat.Gid != 0 || uint32(stat.Mode)&0o7777 != custodyRootSealedMode || stat.Dev == rootFilesystem.Dev || uint64(filesystem.Type) != linuxTmpfsMagic { syscall.Close(fd); return -1, errors.New("custody root is not the sealed distinct root-owned tmpfs mount") }
	return fd, nil
}

func sameStatFile(info os.FileInfo, opened *syscall.Stat_t) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Dev == opened.Dev && stat.Ino == opened.Ino
}

func validCustodyPath(path string) bool { return filepath.IsAbs(path) && strings.HasPrefix(filepath.Clean(path), "/custody/") && path == filepath.Clean(path) && !strings.ContainsAny(path, "\x00\r\n") }
func safeText(value string, maximum int) bool { return value != "" && len(value) <= maximum && !strings.ContainsAny(value, "\x00\r\n") }
func digestText(value string) bool { if len(value) != 64 || value != strings.ToLower(value) { return false }; raw, err := hex.DecodeString(value); return err == nil && len(raw) == sha256.Size }

func digest(raw []byte) string { sum := sha256.Sum256(raw); return hex.EncodeToString(sum[:]) }
