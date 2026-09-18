//go:build linux && (amd64 || arm64)

package collector

import (
	"syscall"
	"unsafe"
)

// renameNoReplace publishes the completed prepared object without permitting
// an existing archive name to be replaced. The syscall number is supplied by
// an architecture-specific file because Go's frozen syscall package does not
// expose SYS_RENAMEAT2 consistently across supported Linux architectures.
func renameNoReplace(source string, destination string) error {
	sourcePointer, err := syscall.BytePtrFromString(source)
	if err != nil {
		return err
	}
	destinationPointer, err := syscall.BytePtrFromString(destination)
	if err != nil {
		return err
	}
	directoryFD := linuxATFDCWD
	_, _, errno := syscall.RawSyscall6(
		linuxSYSRenameat2,
		uintptr(directoryFD),
		uintptr(unsafe.Pointer(sourcePointer)),
		uintptr(directoryFD),
		uintptr(unsafe.Pointer(destinationPointer)),
		uintptr(linuxRenameNoReplace),
		0,
	)
	if errno != 0 {
		return errno
	}
	return nil
}
