//go:build linux && arm64

package main

const linuxSYSMkdirat = uintptr(34)
const linuxSYSNewfstatat = uintptr(79)
