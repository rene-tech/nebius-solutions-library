//go:build !linux || (!amd64 && !arm64)

package collector

import "syscall"

// The append-only archive contract requires kernel-enforced no-replace
// publication. Unsupported targets fail closed rather than emulate it with a
// check followed by rename.
func renameNoReplace(string, string) error {
	return syscall.ENOSYS
}
