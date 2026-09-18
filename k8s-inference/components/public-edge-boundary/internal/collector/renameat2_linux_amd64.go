//go:build linux && amd64

package collector

// Linux UAPI __NR_renameat2 for amd64. Keep this architecture-local instead
// of depending on syscall.SYS_RENAMEAT2, which is absent from Go's frozen
// linux/amd64 syscall constants.
const linuxSYSRenameat2 = uintptr(316)
