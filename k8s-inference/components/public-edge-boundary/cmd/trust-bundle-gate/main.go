package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"sort"
	"strings"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
)

const maximumTrustBytes = 4 * 1024 * 1024

func main() {
	if len(os.Args) < 5 { fail() }
	path, expectedDigest, expectedSchema := os.Args[1], os.Args[2], os.Args[3]
	requiredRoles := append([]string(nil), os.Args[4:]...)
	sort.Strings(requiredRoles)
	if !canonicalStrings(requiredRoles) || !digestText(expectedDigest) { fail() }
	raw, err := readRegular(path)
	if err != nil || digest(raw) != expectedDigest { fail() }
	var registry boundary.TrustRegistry
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&registry) != nil || decoder.Decode(&struct{}{}) != io.EOF || registry.Schema != expectedSchema { fail() }
	canonical, err := json.Marshal(registry)
	if err != nil || !bytes.Equal(canonical, raw) { fail() }
	counts := map[string]int{}
	identities := map[string]bool{}
	keys := map[string]bool{}
	previous := ""
	for _, issuer := range registry.Issuers {
		identity := issuer.Role+"\x00"+issuer.ID+"\x00"+issuer.KeyID
		key, decodeErr := base64.RawURLEncoding.Strict().DecodeString(issuer.PublicKey)
		if decodeErr != nil || len(key) != ed25519.PublicKeySize || base64.RawURLEncoding.EncodeToString(key) != issuer.PublicKey || issuer.ID == "" || len(issuer.ID) > 256 || strings.ContainsAny(issuer.ID+issuer.Role, "\x00\r\n") || !contains(requiredRoles, issuer.Role) || issuer.KeyID != "sha256:"+digest(key) || identities[identity] || keys[issuer.PublicKey] || previous != "" && identity <= previous { fail() }
		identities[identity] = true
		keys[issuer.PublicKey] = true
		counts[issuer.Role]++
		previous = identity
	}
	for _, role := range requiredRoles { if counts[role] < 2 { fail() } }
}

func readRegular(path string) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > maximumTrustBytes || info.Mode().Perm()&0o022 != 0 { return nil, errors.New("trust bundle is not one protected bounded regular file") }
	file, err := os.Open(path)
	if err != nil { return nil, err }
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) { return nil, errors.New("trust bundle identity changed") }
	raw, err := io.ReadAll(io.LimitReader(file, maximumTrustBytes+1))
	if err != nil || int64(len(raw)) != info.Size() { return nil, errors.New("trust bundle cannot be read exactly") }
	return raw, nil
}

func canonicalStrings(values []string) bool { if len(values) == 0 { return false }; for index, value := range values { if value == "" || strings.ContainsAny(value, "\x00\r\n") || index > 0 && values[index-1] == value { return false } }; return true }
func contains(values []string, expected string) bool { index := sort.SearchStrings(values, expected); return index < len(values) && values[index] == expected }
func digestText(value string) bool { if len(value) != 64 || value != strings.ToLower(value) { return false }; raw, err := hex.DecodeString(value); return err == nil && len(raw) == sha256.Size }
func digest(raw []byte) string { value := sha256.Sum256(raw); return hex.EncodeToString(value[:]) }
func fail() { os.Exit(1) }
