//go:build linux && arm64

package collector

// Linux UAPI __NR_renameat2 for arm64.
const linuxSYSRenameat2 = uintptr(276)
