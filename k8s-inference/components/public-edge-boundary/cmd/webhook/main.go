package main

import (
	"crypto/tls"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
)

const maximumAdmissionBytes = 8 * 1024 * 1024
const maximumConcurrentAdmissions = 64
const acceptanceTrustPath = "/usr/local/share/fs2-boundary/trusted-acceptance-issuers.json"
const acceptanceEnvelopePath = "/var/run/fs2-boundary/acceptance/accepted-boundary-envelope.json"
const runtimeTrustPath = "/var/run/fs2-boundary/snapshot-trust.json"
const runtimeSnapshotPath = "/var/run/fs2-boundary/snapshot-envelope.json"
const tlsCertificatePath = "/var/run/fs2-boundary/tls/tls.crt"
const tlsPrivateKeyPath = "/var/run/fs2-boundary/tls/tls.key"

type config struct {
	listenAddress       string
	tlsCertificatePath  string
	tlsPrivateKeyPath   string
	trustPath           string
	snapshotPath        string
	trustSHA256         string
	clusterID           string
	deploymentID        string
	authoritySnapshotID string
	authorityClosureSHA256  string
	tlsCertificate      tls.Certificate
}

func main() {
	configuration, err := loadConfig()
	if err != nil {
		slog.Error("public-edge boundary configuration rejected", "error", err)
		os.Exit(1)
	}
	handler := &admissionHandler{
		config: configuration,
		slots:  make(chan struct{}, maximumConcurrentAdmissions),
		provider: boundary.NewProvider(
			configuration.trustPath,
			configuration.snapshotPath,
			configuration.trustSHA256,
			configuration.clusterID,
			configuration.deploymentID,
			configuration.authoritySnapshotID,
			configuration.authorityClosureSHA256,
		),
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", handler.health)
	mux.HandleFunc("/readyz", handler.ready)
	mux.HandleFunc("/validate", handler.validate)
	server := &http.Server{
		Addr:              configuration.listenAddress,
		Handler:           mux,
		ReadHeaderTimeout: 3 * time.Second,
		ReadTimeout:       6 * time.Second,
		WriteTimeout:      6 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    32 * 1024,
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS13,
			Certificates: []tls.Certificate{configuration.tlsCertificate},
		},
	}
	slog.Info("public-edge admission boundary starting", "address", configuration.listenAddress)
	if err := server.ListenAndServeTLS("", ""); err != nil {
		slog.Error("public-edge admission boundary stopped", "error", err)
		os.Exit(1)
	}
}

type admissionHandler struct {
	config   config
	provider *boundary.Provider
	slots    chan struct{}
}

func (h *admissionHandler) runtime(now time.Time) (*boundary.Runtime, error) {
	return h.provider.Runtime(now)
}

func (h *admissionHandler) health(response http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		response.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	response.Header().Set("Content-Type", "text/plain; charset=utf-8")
	response.WriteHeader(http.StatusOK)
	_, _ = response.Write([]byte("ok\n"))
}

func (h *admissionHandler) ready(response http.ResponseWriter, request *http.Request) {
	if request.Method != http.MethodGet {
		response.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	runtime, err := h.runtime(time.Now())
	if err != nil || runtime.Ready(time.Now()) != nil {
		http.Error(response, "boundary snapshot unavailable", http.StatusServiceUnavailable)
		return
	}
	response.Header().Set("Content-Type", "text/plain; charset=utf-8")
	response.WriteHeader(http.StatusOK)
	_, _ = response.Write([]byte("ready\n"))
}

func (h *admissionHandler) validate(response http.ResponseWriter, request *http.Request) {
	response.Header().Set("Content-Type", "application/json")
	if request.Method != http.MethodPost || !strings.HasPrefix(request.Header.Get("Content-Type"), "application/json") {
		writeReview(response, boundary.AdmissionReview{
			APIVersion: "admission.k8s.io/v1",
			Kind:       "AdmissionReview",
			Response: &boundary.AdmissionResponse{Allowed: false, Status: &boundary.Status{
				Code: 403, Reason: "MalformedRequest", Message: "POST application/json is required",
			}},
		})
		return
	}
	select {
	case h.slots <- struct{}{}:
		defer func() { <-h.slots }()
	default:
		writeReview(response, boundary.AdmissionReview{
			APIVersion: "admission.k8s.io/v1",
			Kind:       "AdmissionReview",
			Response: &boundary.AdmissionResponse{Allowed: false, Status: &boundary.Status{
				Code: 429, Reason: "TooManyRequests", Message: "public-edge admission boundary is at its reviewed concurrency limit",
			}},
		})
		return
	}
	raw, err := io.ReadAll(io.LimitReader(request.Body, maximumAdmissionBytes+1))
	if err != nil || len(raw) > maximumAdmissionBytes {
		writeReview(response, boundary.AdmissionReview{
			APIVersion: "admission.k8s.io/v1",
			Kind:       "AdmissionReview",
			Response: &boundary.AdmissionResponse{Allowed: false, Status: &boundary.Status{
				Code: 403, Reason: "MalformedRequest", Message: "admission body exceeds its bound",
			}},
		})
		return
	}
	runtime, err := h.runtime(time.Now())
	if err != nil {
		writeReview(response, boundary.AdmissionReview{
			APIVersion: "admission.k8s.io/v1",
			Kind:       "AdmissionReview",
			Response: &boundary.AdmissionResponse{Allowed: false, Status: &boundary.Status{
				Code: 403, Reason: "Forbidden", Message: "independent public-edge authority is unavailable",
			}},
		})
		return
	}
	writeReview(response, runtime.Review(raw, time.Now()))
}

func writeReview(response http.ResponseWriter, review boundary.AdmissionReview) {
	encoder := json.NewEncoder(response)
	encoder.SetEscapeHTML(true)
	if err := encoder.Encode(review); err != nil {
		slog.Error("admission response encoding failed", "error", err)
	}
}

func loadConfig() (config, error) {
	acceptance, err := boundary.LoadAcceptance(acceptanceTrustPath, acceptanceEnvelopePath, "boundary")
	if err != nil {
		return config{}, err
	}
	tlsIdentity, err := boundary.LoadServerTLSIdentity(
		tlsCertificatePath,
		tlsPrivateKeyPath,
		acceptance.BoundaryTLSCertificateSHA256,
		acceptance.BoundaryTLSPrivateKeySHA256,
		acceptance.BoundaryTLSSPKISHA256,
		time.Now(),
	)
	if err != nil {
		return config{}, err
	}
	return config{
		listenAddress:          ":8443",
		tlsCertificatePath:     tlsCertificatePath,
		tlsPrivateKeyPath:      tlsPrivateKeyPath,
		trustPath:              runtimeTrustPath,
		snapshotPath:           runtimeSnapshotPath,
		trustSHA256:            acceptance.SnapshotTrustSHA256,
		clusterID:              acceptance.ClusterID,
		deploymentID:           acceptance.DeploymentID,
		authoritySnapshotID:    acceptance.AuthoritySnapshotID,
		authorityClosureSHA256: acceptance.AuthorityClosureSHA256,
		tlsCertificate:         tlsIdentity,
	}, nil
}
