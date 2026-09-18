package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"syscall"
	"time"
)

const probeConfigSchema = "fs2-serve.nebius.ai/public-edge-mtls-readiness-probe/v1"

type probeConfig struct {
	Schema string `json:"schema"`
	URL string `json:"url"`
	ServerName string `json:"server_name"`
	CABundlePath string `json:"ca_bundle_path"`
	CABundleSHA256 string `json:"ca_bundle_sha256"`
	ClientCertificatePath string `json:"client_certificate_path"`
	ClientCertificateSHA256 string `json:"client_certificate_sha256"`
	ClientKeyPath string `json:"client_key_path"`
	ClientKeySHA256 string `json:"client_key_sha256"`
}

func main() {
	if len(os.Args) != 2 { os.Exit(2) }
	raw, err := readProtected(os.Args[1], 64*1024, false)
	if err != nil { os.Exit(1) }
	var config probeConfig
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&config) != nil || decoder.Decode(&struct{}{}) != io.EOF { os.Exit(1) }
	canonical, err := json.Marshal(config)
	parsed, parseErr := url.Parse(config.URL)
	if err != nil || !bytes.Equal(canonical, raw) || config.Schema != probeConfigSchema || parseErr != nil || parsed.Scheme != "https" || parsed.Hostname() != "127.0.0.1" || parsed.Path != "/readyz" || parsed.RawQuery != "" || parsed.Fragment != "" || config.ServerName == "" || strings.ContainsAny(config.ServerName, "\x00\r\n") { os.Exit(1) }
	caRaw, err := readProtected(config.CABundlePath, 4*1024*1024, false)
	if err != nil || digest(caRaw) != config.CABundleSHA256 { os.Exit(1) }
	certificateRaw, err := readProtected(config.ClientCertificatePath, 4*1024*1024, false)
	if err != nil || digest(certificateRaw) != config.ClientCertificateSHA256 { os.Exit(1) }
	keyRaw, err := readProtected(config.ClientKeyPath, 1024*1024, true)
	if err != nil || digest(keyRaw) != config.ClientKeySHA256 { os.Exit(1) }
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caRaw) { os.Exit(1) }
	pair, err := tls.X509KeyPair(certificateRaw, keyRaw)
	if err != nil || len(pair.Certificate) == 0 { os.Exit(1) }
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil || time.Now().UTC().Before(leaf.NotBefore) || !time.Now().UTC().Before(leaf.NotAfter) || !hasUsage(leaf, x509.ExtKeyUsageClientAuth) { os.Exit(1) }
	pair.Leaf = leaf
	client := &http.Client{Timeout: 2 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }, Transport: &http.Transport{Proxy: nil, DisableCompression: true, DisableKeepAlives: true, TLSHandshakeTimeout: time.Second, ResponseHeaderTimeout: time.Second, TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS13, RootCAs: roots, ServerName: config.ServerName, Certificates: []tls.Certificate{pair}}}}
	request, err := http.NewRequestWithContext(context.Background(), http.MethodGet, config.URL, nil)
	if err != nil { os.Exit(1) }
	response, err := client.Do(request)
	if err != nil { os.Exit(1) }
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK { os.Exit(1) }
}

func readProtected(path string, maximum int64, private bool) ([]byte, error) {
	if !strings.HasPrefix(filepathClean(path), "/var/run/fs2-boundary/") { return nil, errors.New("probe path is outside accepted runtime custody") }
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > maximum || info.Mode().Perm()&0o022 != 0 || private && info.Mode().Perm()&0o077 != 0 { return nil, errors.New("probe input custody is invalid") }
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 { return nil, errors.New("probe input is not root owned") }
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil { return nil, err }
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) { return nil, errors.New("probe input changed") }
	return io.ReadAll(io.LimitReader(file, maximum+1))
}

func filepathClean(path string) string {
	parts := strings.Split(path, "/")
	clean := []string{}
	for _, part := range parts { if part == "" || part == "." { continue }; if part == ".." { return "" }; clean = append(clean, part) }
	return "/"+strings.Join(clean, "/")
}

func digest(raw []byte) string { value := sha256.Sum256(raw); return hex.EncodeToString(value[:]) }
func hasUsage(certificate *x509.Certificate, usage x509.ExtKeyUsage) bool { for _, value := range certificate.ExtKeyUsage { if value == usage { return true } }; return false }
