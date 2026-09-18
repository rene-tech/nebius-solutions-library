//go:build linux && (amd64 || arm64)

package main

import (
	"syscall"
	"unsafe"
)

func mkdirAt(parent int, name string, mode uint32) error {
	pointer, err := syscall.BytePtrFromString(name)
	if err != nil { return err }
	_, _, errno := syscall.RawSyscall(linuxSYSMkdirat, uintptr(parent), uintptr(unsafe.Pointer(pointer)), uintptr(mode))
	if errno != 0 { return errno }
	return nil
}

func fstatAtNoFollow(parent int, name string, result *syscall.Stat_t) error {
	pointer, err := syscall.BytePtrFromString(name)
	if err != nil { return err }
	_, _, errno := syscall.RawSyscall6(linuxSYSNewfstatat, uintptr(parent), uintptr(unsafe.Pointer(pointer)), uintptr(unsafe.Pointer(result)), uintptr(0x100), 0, 0)
	if errno != 0 { return errno }
	return nil
}
