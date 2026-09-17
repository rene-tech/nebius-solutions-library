// provider-authority-exporter is the hermetic production client for the
// provider-native effective-authority API. Build for Linux with CGO disabled;
// inference-stack independently rejects artifacts containing PT_INTERP.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	maxDocumentBytes = 16 * 1024 * 1024
	fGetSeals        = 1034
	requiredSeals    = 0x0001 | 0x0002 | 0x0004 | 0x0008
)

var (
	sha256Pattern      = regexp.MustCompile(`^[a-f0-9]{64}$`)
	projectPattern     = regexp.MustCompile(`^project-[a-z0-9]+$`)
	clusterPattern     = regexp.MustCompile(`^mk8scluster-[a-z0-9]+$`)
	transactionPattern = regexp.MustCompile(`^[a-z][a-z0-9]{5,31}:[1-9][0-9]*:[a-f0-9]{32}$`)
	applyIDPattern      = regexp.MustCompile(`^[a-f0-9]{32}$`)
	sealedFDPattern    = regexp.MustCompile(`^/proc/self/fd/([1-9][0-9]*)$`)
)

type requestDocument struct {
	Schema              string `json:"schema"`
	Challenge           string `json:"challenge"`
	ProjectID           string `json:"project_id"`
	ClusterID           string `json:"cluster_id"`
	FreezeTransactionID string `json:"freeze_transaction_id"`
	RequiredProjection  string `json:"required_projection"`
}

type settlementRequestDocument struct {
	Schema                     string `json:"schema"`
	Challenge                  string `json:"challenge"`
	ProjectID                  string `json:"project_id"`
	ClusterID                  string `json:"cluster_id"`
	AbortedFreezeTransactionID string `json:"aborted_freeze_transaction_id"`
	ApplyID                    string `json:"apply_id"`
	StartedAt                  string `json:"started_at"`
	RequiredProjection         string `json:"required_projection"`
}

type responseDocument struct {
	Schema                 string          `json:"schema"`
	Challenge              string          `json:"challenge"`
	Observation            json.RawMessage `json:"observation"`
	ProviderResponseSHA256 string          `json:"provider_response_sha256"`
}

func fail(format string, values ...any) {
	fmt.Fprintf(os.Stderr, "provider authority export failed: "+format+"\n", values...)
	os.Exit(2)
}

func readSealedFD(environmentName string) ([]byte, error) {
	match := sealedFDPattern.FindStringSubmatch(os.Getenv(environmentName))
	if match == nil {
		return nil, fmt.Errorf("%s is not an inherited descriptor", environmentName)
	}
	fd, err := strconv.Atoi(match[1])
	if err != nil {
		return nil, err
	}
	var metadata syscall.Stat_t
	if err := syscall.Fstat(fd, &metadata); err != nil {
		return nil, err
	}
	seals, _, errno := syscall.Syscall(syscall.SYS_FCNTL, uintptr(fd), uintptr(fGetSeals), 0)
	if errno != 0 {
		return nil, errno
	}
	if metadata.Mode&syscall.S_IFMT != syscall.S_IFREG || metadata.Mode&0077 != 0 || int(seals) != requiredSeals {
		return nil, fmt.Errorf("%s is not an immutable private memfd", environmentName)
	}
	file := os.NewFile(uintptr(fd), environmentName)
	if file == nil {
		return nil, errors.New("inherited descriptor is unavailable")
	}
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	value, err := io.ReadAll(io.LimitReader(file, maxDocumentBytes+1))
	if err != nil {
		return nil, err
	}
	if len(value) == 0 || len(value) > maxDocumentBytes {
		return nil, errors.New("inherited credential size is invalid")
	}
	return value, nil
}

func exactOrigin(raw string) (*url.URL, error) {
	parsed, err := url.Parse(strings.TrimSuffix(raw, "/"))
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil || parsed.Path != "" || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, errors.New("provider authority endpoint is not an exact HTTPS origin")
	}
	return parsed, nil
}

func exactArguments(arguments []string) (string, string, string, error) {
	if len(arguments) != 9 || arguments[0] != "snapshot" || arguments[1] != "--project-id" || arguments[3] != "--cluster-id" || arguments[5] != "--freeze-transaction-id" || arguments[7] != "--output" || arguments[8] != "json" {
		return "", "", "", errors.New("arguments are not exact")
	}
	if !projectPattern.MatchString(arguments[2]) || !clusterPattern.MatchString(arguments[4]) || !transactionPattern.MatchString(arguments[6]) {
		return "", "", "", errors.New("provider boundary identity is malformed")
	}
	return arguments[2], arguments[4], arguments[6], nil
}

func settlementMain(arguments []string) {
	if len(arguments) != 13 || arguments[0] != "settlement" || arguments[1] != "--project-id" || arguments[3] != "--cluster-id" || arguments[5] != "--aborted-freeze-transaction-id" || arguments[7] != "--apply-id" || arguments[9] != "--started-at" || arguments[11] != "--output" || arguments[12] != "json" {
		fail("settlement arguments are not exact")
	}
	projectID, clusterID := arguments[2], arguments[4]
	transactionID, applyID, startedAt := arguments[6], arguments[8], arguments[10]
	parsedStartedAt, timeError := time.Parse(time.RFC3339Nano, startedAt)
	if !projectPattern.MatchString(projectID) || !clusterPattern.MatchString(clusterID) || !transactionPattern.MatchString(transactionID) || !applyIDPattern.MatchString(applyID) || timeError != nil || parsedStartedAt.Location() != time.UTC || parsedStartedAt.After(time.Now().UTC().Add(30*time.Second)) {
		fail("settlement identity is malformed")
	}
	origin, err := exactOrigin(os.Getenv("FS2_PROVIDER_AUTHORITY_API_URL"))
	if err != nil {
		fail("%v", err)
	}
	expectedLeaf := os.Getenv("FS2_PROVIDER_AUTHORITY_API_SERVER_CERT_SHA256")
	if !sha256Pattern.MatchString(expectedLeaf) {
		fail("provider authority TLS leaf digest is absent")
	}
	caPEM, err := readSealedFD("FS2_PROVIDER_AUTHORITY_API_CA")
	if err != nil {
		fail("authority CA: %v", err)
	}
	certificatePEM, err := readSealedFD("FS2_PROVIDER_AUTHORITY_API_CLIENT_CERT")
	if err != nil {
		fail("client certificate: %v", err)
	}
	keyPEM, err := readSealedFD("FS2_PROVIDER_AUTHORITY_API_CLIENT_KEY")
	if err != nil {
		fail("client key: %v", err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		fail("authority CA is not PEM")
	}
	certificate, err := tls.X509KeyPair(certificatePEM, keyPEM)
	if err != nil {
		fail("client key pair is invalid")
	}
	tlsConfig := &tls.Config{
		Certificates: []tls.Certificate{certificate}, RootCAs: roots,
		ServerName: origin.Hostname(), MinVersion: tls.VersionTLS13, MaxVersion: tls.VersionTLS13,
		VerifyConnection: func(state tls.ConnectionState) error {
			if len(state.PeerCertificates) != 1 {
				return errors.New("authority TLS chain is not exact")
			}
			digest := sha256.Sum256(state.PeerCertificates[0].Raw)
			if hex.EncodeToString(digest[:]) != expectedLeaf {
				return errors.New("authority TLS leaf differs from signed custody")
			}
			return nil
		},
	}
	challengeBytes := make([]byte, 32)
	if _, err := rand.Read(challengeBytes); err != nil {
		fail("cannot obtain challenge entropy")
	}
	challenge := hex.EncodeToString(challengeBytes)
	payload, err := json.Marshal(settlementRequestDocument{
		Schema: "fs2-serve.nebius.ai/provider-operation-settlement-request/v1",
		Challenge: challenge, ProjectID: projectID, ClusterID: clusterID,
		AbortedFreezeTransactionID: transactionID, ApplyID: applyID, StartedAt: startedAt,
		RequiredProjection: "all-accepted-operations-through-terminal-settlement",
	})
	if err != nil {
		fail("cannot encode settlement request")
	}
	request, err := http.NewRequestWithContext(context.Background(), http.MethodPost, origin.String()+"/v1/effective-authority/apply-settlement", bytes.NewReader(payload))
	if err != nil {
		fail("cannot construct settlement request")
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Content-Type", "application/json")
	client := &http.Client{
		Timeout: 30 * time.Second,
		Transport: &http.Transport{TLSClientConfig: tlsConfig, Proxy: nil},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	response, err := client.Do(request)
	if err != nil {
		fail("provider settlement request failed")
	}
	defer response.Body.Close()
	content, err := io.ReadAll(io.LimitReader(response.Body, maxDocumentBytes+1))
	if err != nil || response.StatusCode != http.StatusOK || len(content) > maxDocumentBytes || response.Header.Get("Content-Type") != "application/json" {
		fail("provider settlement response failed closed")
	}
	var envelope responseDocument
	if err := json.Unmarshal(content, &envelope); err != nil || envelope.Schema != "nebius.ai/effective-control-plane-authority-response/v1" || envelope.Challenge != challenge || !sha256Pattern.MatchString(envelope.ProviderResponseSHA256) || len(envelope.Observation) == 0 {
		fail("provider settlement response identity is incomplete")
	}
	var observation map[string]any
	if err := json.Unmarshal(envelope.Observation, &observation); err != nil || observation["schema"] != "fs2-serve.nebius.ai/provider-operation-settlement-observation/v1" || observation["project_id"] != projectID || observation["cluster_id"] != clusterID || observation["aborted_freeze_transaction_id"] != transactionID || observation["apply_id"] != applyID || observation["provider_response_sha256"] != envelope.ProviderResponseSHA256 {
		fail("provider settlement observation is outside the requested boundary")
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(observation); err != nil {
		fail("cannot emit settlement observation")
	}
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "settlement" {
		settlementMain(os.Args[1:])
		return
	}
	projectID, clusterID, transactionID, err := exactArguments(os.Args[1:])
	if err != nil {
		fail("%v", err)
	}
	origin, err := exactOrigin(os.Getenv("FS2_PROVIDER_AUTHORITY_API_URL"))
	if err != nil {
		fail("%v", err)
	}
	expectedLeaf := os.Getenv("FS2_PROVIDER_AUTHORITY_API_SERVER_CERT_SHA256")
	if !sha256Pattern.MatchString(expectedLeaf) {
		fail("provider authority TLS leaf digest is absent")
	}
	caPEM, err := readSealedFD("FS2_PROVIDER_AUTHORITY_API_CA")
	if err != nil {
		fail("authority CA: %v", err)
	}
	certificatePEM, err := readSealedFD("FS2_PROVIDER_AUTHORITY_API_CLIENT_CERT")
	if err != nil {
		fail("client certificate: %v", err)
	}
	keyPEM, err := readSealedFD("FS2_PROVIDER_AUTHORITY_API_CLIENT_KEY")
	if err != nil {
		fail("client key: %v", err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		fail("authority CA is not PEM")
	}
	certificate, err := tls.X509KeyPair(certificatePEM, keyPEM)
	if err != nil {
		fail("client key pair is invalid")
	}
	tlsConfig := &tls.Config{
		Certificates: []tls.Certificate{certificate},
		RootCAs:      roots,
		ServerName:   origin.Hostname(),
		MinVersion:   tls.VersionTLS13,
		MaxVersion:   tls.VersionTLS13,
		VerifyConnection: func(state tls.ConnectionState) error {
			if len(state.PeerCertificates) != 1 {
				return errors.New("authority TLS chain is not exact")
			}
			digest := sha256.Sum256(state.PeerCertificates[0].Raw)
			if hex.EncodeToString(digest[:]) != expectedLeaf {
				return errors.New("authority TLS leaf differs from signed custody")
			}
			return nil
		},
	}
	challengeBytes := make([]byte, 32)
	if _, err := rand.Read(challengeBytes); err != nil {
		fail("cannot obtain challenge entropy")
	}
	challenge := hex.EncodeToString(challengeBytes)
	payload, err := json.Marshal(requestDocument{
		Schema:              "fs2-serve.nebius.ai/provider-control-plane-authority-request/v1",
		Challenge:           challenge,
		ProjectID:           projectID,
		ClusterID:           clusterID,
		FreezeTransactionID: transactionID,
		RequiredProjection:  "effective-iam-network-runtime-and-freeze",
	})
	if err != nil {
		fail("cannot encode request")
	}
	request, err := http.NewRequestWithContext(context.Background(), http.MethodPost, origin.String()+"/v1/effective-authority/snapshot", bytes.NewReader(payload))
	if err != nil {
		fail("cannot construct request")
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Content-Type", "application/json")
	client := &http.Client{
		Timeout: 30 * time.Second,
		Transport: &http.Transport{TLSClientConfig: tlsConfig, Proxy: nil},
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	response, err := client.Do(request)
	if err != nil {
		fail("provider authority request failed")
	}
	defer response.Body.Close()
	content, err := io.ReadAll(io.LimitReader(response.Body, maxDocumentBytes+1))
	if err != nil || response.StatusCode != http.StatusOK || len(content) > maxDocumentBytes || response.Header.Get("Content-Type") != "application/json" {
		fail("provider authority response failed closed")
	}
	var envelope responseDocument
	if err := json.Unmarshal(content, &envelope); err != nil || envelope.Schema != "nebius.ai/effective-control-plane-authority-response/v1" || envelope.Challenge != challenge || !sha256Pattern.MatchString(envelope.ProviderResponseSHA256) || len(envelope.Observation) == 0 {
		fail("provider authority response identity is incomplete")
	}
	var observation map[string]any
	if err := json.Unmarshal(envelope.Observation, &observation); err != nil || observation["schema"] != "fs2-serve.nebius.ai/provider-control-plane-authority-observation/v1" {
		fail("provider authority observation is malformed")
	}
	snapshot, ok := observation["snapshot"].(map[string]any)
	if !ok || snapshot["project_id"] != projectID || snapshot["cluster_id"] != clusterID || strings.TrimSuffix(fmt.Sprint(snapshot["provider_api_endpoint"]), "/") != origin.String() || snapshot["authority_api_server_certificate_sha256"] != expectedLeaf {
		fail("provider authority observation is outside the requested boundary")
	}
	freeze, ok := snapshot["provider_mutation_freeze"].(map[string]any)
	if !ok || freeze["transaction_id"] != transactionID {
		fail("provider mutation freeze differs from the request")
	}
	completeness, ok := snapshot["completeness_token"].(map[string]any)
	if !ok || completeness["provider_response_sha256"] != envelope.ProviderResponseSHA256 {
		fail("provider completeness token is absent")
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(observation); err != nil {
		fail("cannot emit observation")
	}
}
