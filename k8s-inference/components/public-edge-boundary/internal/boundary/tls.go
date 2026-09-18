package boundary

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/url"
	"path/filepath"
	"strings"
	"time"
)

const (
	AdmissionClientTrustSchema = "fs2-serve.nebius.ai/public-edge-admission-client-trust/v1"
	AdmissionChannelBindingSchema = "fs2-serve.nebius.ai/public-edge-admission-channel-binding/v1"
	maximumAdmissionClientTrustBytes = 64 * 1024
)

type AdmissionClientTrust struct {
	Schema string `json:"schema"`
	ClusterID string `json:"cluster_id"`
	DeploymentID string `json:"deployment_id"`
	ControlPlaneAdmissionConfigurationSHA256 string `json:"control_plane_admission_configuration_sha256"`
	WebhookServerName string `json:"webhook_server_name"`
	Identities []AdmissionClientIdentity `json:"identities"`
}

type AdmissionClientIdentity struct {
	Slot string `json:"slot"`
	CABundlePath string `json:"ca_bundle_path"`
	CABundleSHA256 string `json:"ca_bundle_sha256"`
	ClientSPKISHA256 string `json:"client_spki_sha256"`
	ClientSPIFFEURI string `json:"client_spiffe_uri"`
	NotBefore string `json:"not_before"`
	NotAfter string `json:"not_after"`
}

type AdmissionChannelBinding struct {
	Schema string `json:"schema"`
	IdentitySlot string `json:"identity_slot"`
	PeerCertificateSHA256 string `json:"peer_certificate_sha256"`
	PeerSPKISHA256 string `json:"peer_spki_sha256"`
	PeerSPIFFEURI string `json:"peer_spiffe_uri"`
	WebhookServerName string `json:"webhook_server_name"`
	ControlPlaneAdmissionConfigurationSHA256 string `json:"control_plane_admission_configuration_sha256"`
}

type admissionClientIdentityRuntime struct {
	identity AdmissionClientIdentity
	roots *x509.CertPool
	notBefore time.Time
	notAfter time.Time
}

type AdmissionChannelAuthenticator struct {
	clientRoots *x509.CertPool
	controlPlaneAdmissionConfigurationSHA256 string
	webhookServerName string
	identities []admissionClientIdentityRuntime
}

// LoadServerTLSIdentity pins the stable root-custodied bytes before the HTTP
// server starts. Callers retain the returned identity in tls.Config so no
// certificate or key pathname is reopened after acceptance.
func LoadServerTLSIdentity(certificatePath string, privateKeyPath string, expectedCertificateSHA256 string, expectedPrivateKeySHA256 string, expectedSPKISHA256 string, now time.Time) (tls.Certificate, error) {
	certificateRaw, err := readProtectedRegular(certificatePath, 4*1024*1024)
	if err != nil {
		return tls.Certificate{}, err
	}
	if digestHex(certificateRaw) != expectedCertificateSHA256 {
		return tls.Certificate{}, errors.New("server TLS certificate differs from accepted bytes")
	}
	privateKeyRaw, err := readProtectedPrivateKey(privateKeyPath, 4*1024*1024)
	if err != nil {
		return tls.Certificate{}, err
	}
	if digestHex(privateKeyRaw) != expectedPrivateKeySHA256 {
		return tls.Certificate{}, errors.New("server TLS private key differs from accepted bytes")
	}
	identity, err := tls.X509KeyPair(certificateRaw, privateKeyRaw)
	if err != nil || len(identity.Certificate) < 1 {
		return tls.Certificate{}, errors.New("server TLS certificate and private key are not an exact pair")
	}
	leaf, err := x509.ParseCertificate(identity.Certificate[0])
	if err != nil || digestHex(leaf.RawSubjectPublicKeyInfo) != expectedSPKISHA256 {
		return tls.Certificate{}, errors.New("server TLS leaf SPKI differs from accepted identity")
	}
	now = now.UTC()
	if now.Before(leaf.NotBefore) || !now.Before(leaf.NotAfter) || !hasExtendedKeyUsage(leaf, x509.ExtKeyUsageServerAuth) {
		return tls.Certificate{}, errors.New("server TLS leaf is not current or lacks serverAuth purpose")
	}
	identity.Leaf = leaf
	return identity, nil
}

// LoadAdmissionChannelAuthenticator binds the API-server client identity to a
// separately accepted AdmissionConfiguration/kubeconfig contract. The returned
// union pool is used only to make TLS request a verified client certificate;
// Authenticate performs the exact current/next slot and SPIFFE/SPKI check.
func LoadAdmissionChannelAuthenticator(path string, expectedSHA256 string, clusterID string, deploymentID string, now time.Time) (*AdmissionChannelAuthenticator, error) {
	raw, err := readProtectedRegular(path, maximumAdmissionClientTrustBytes)
	if err != nil {
		return nil, fmt.Errorf("read admission client trust: %w", err)
	}
	if digestHex(raw) != expectedSHA256 {
		return nil, errors.New("admission client trust differs from independently accepted bytes")
	}
	var trust AdmissionClientTrust
	if err := decodeExactJSON(raw, &trust); err != nil {
		return nil, err
	}
	canonical, err := json.Marshal(trust)
	if err != nil || !bytes.Equal(canonical, raw) || trust.Schema != AdmissionClientTrustSchema ||
		trust.ClusterID != clusterID || trust.DeploymentID != deploymentID ||
		!isSHA256(trust.ControlPlaneAdmissionConfigurationSHA256) ||
		!safeText(trust.WebhookServerName, false) || len(trust.WebhookServerName) > 253 ||
		(len(trust.Identities) != 1 && len(trust.Identities) != 2) {
		return nil, errors.New("admission client trust is incomplete or non-canonical")
	}
	if trust.Identities[0].Slot != "current" || len(trust.Identities) == 2 && trust.Identities[1].Slot != "next" {
		return nil, errors.New("admission client trust must contain ordered current and optional next identities")
	}
	union := x509.NewCertPool()
	runtimeIdentities := make([]admissionClientIdentityRuntime, 0, len(trust.Identities))
	seenSPKI := map[string]struct{}{}
	for _, identity := range trust.Identities {
		if !filepath.IsAbs(identity.CABundlePath) || filepath.Clean(identity.CABundlePath) != identity.CABundlePath ||
			!isSHA256(identity.CABundleSHA256) || !isSHA256(identity.ClientSPKISHA256) ||
			!safeText(identity.ClientSPIFFEURI, false) || len(identity.ClientSPIFFEURI) > 512 {
			return nil, errors.New("admission client identity is incomplete")
		}
		parsedURI, err := url.Parse(identity.ClientSPIFFEURI)
		if err != nil || parsedURI.Scheme != "spiffe" || parsedURI.Host == "" || parsedURI.String() != identity.ClientSPIFFEURI {
			return nil, errors.New("admission client SPIFFE identity is not canonical")
		}
		notBefore, beforeErr := parseWholeUTC(identity.NotBefore)
		notAfter, afterErr := parseWholeUTC(identity.NotAfter)
		if beforeErr != nil || afterErr != nil || !notBefore.Before(notAfter) || notAfter.Sub(notBefore) > 397*24*time.Hour || !now.UTC().Before(notAfter) {
			return nil, errors.New("admission client identity enrollment window is invalid or expired")
		}
		if _, exists := seenSPKI[identity.ClientSPKISHA256]; exists {
			return nil, errors.New("admission client current/next slots reuse an SPKI")
		}
		caRaw, err := readProtectedRegular(identity.CABundlePath, 4*1024*1024)
		if err != nil || digestHex(caRaw) != identity.CABundleSHA256 {
			return nil, errors.New("admission client CA bundle differs from its accepted digest")
		}
		roots, certificates, err := exactCertificatePool(caRaw)
		if err != nil {
			return nil, err
		}
		for _, certificate := range certificates {
			union.AddCert(certificate)
		}
		seenSPKI[identity.ClientSPKISHA256] = struct{}{}
		runtimeIdentities = append(runtimeIdentities, admissionClientIdentityRuntime{
			identity: identity, roots: roots, notBefore: notBefore, notAfter: notAfter,
		})
	}
	return &AdmissionChannelAuthenticator{
		clientRoots: union,
		controlPlaneAdmissionConfigurationSHA256: trust.ControlPlaneAdmissionConfigurationSHA256,
		webhookServerName: trust.WebhookServerName,
		identities: runtimeIdentities,
	}, nil
}

func (a *AdmissionChannelAuthenticator) ClientCAs() *x509.CertPool {
	if a == nil {
		return nil
	}
	return a.clientRoots
}

func (a *AdmissionChannelAuthenticator) Authenticate(state *tls.ConnectionState, requestHost string, now time.Time) (AdmissionChannelBinding, error) {
	if a == nil || state == nil || len(state.PeerCertificates) < 1 || requestHost != a.webhookServerName {
		return AdmissionChannelBinding{}, errors.New("admission request lacks its accepted API-server client certificate")
	}
	leaf := state.PeerCertificates[0]
	if !hasExtendedKeyUsage(leaf, x509.ExtKeyUsageClientAuth) || len(leaf.URIs) != 1 {
		return AdmissionChannelBinding{}, errors.New("admission API-server client certificate lacks exact clientAuth/SPIFFE identity")
	}
	spki := digestHex(leaf.RawSubjectPublicKeyInfo)
	spiffe := leaf.URIs[0].String()
	intermediates := x509.NewCertPool()
	for _, certificate := range state.PeerCertificates[1:] {
		intermediates.AddCert(certificate)
	}
	now = now.UTC()
	for _, enrolled := range a.identities {
		if enrolled.identity.ClientSPKISHA256 != spki || enrolled.identity.ClientSPIFFEURI != spiffe ||
			now.Before(enrolled.notBefore) || !now.Before(enrolled.notAfter) {
			continue
		}
		if _, err := leaf.Verify(x509.VerifyOptions{
			Roots: enrolled.roots, Intermediates: intermediates, CurrentTime: now,
			KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
		}); err != nil {
			return AdmissionChannelBinding{}, errors.New("admission API-server client chain does not terminate at its enrolled slot CA")
		}
		return AdmissionChannelBinding{
			Schema: AdmissionChannelBindingSchema,
			IdentitySlot: enrolled.identity.Slot,
			PeerCertificateSHA256: digestHex(leaf.Raw),
			PeerSPKISHA256: spki,
			PeerSPIFFEURI: spiffe,
			WebhookServerName: a.webhookServerName,
			ControlPlaneAdmissionConfigurationSHA256: a.controlPlaneAdmissionConfigurationSHA256,
		}, nil
	}
	return AdmissionChannelBinding{}, errors.New("admission API-server client identity is not an active accepted current/next slot")
}

func exactCertificatePool(raw []byte) (*x509.CertPool, []*x509.Certificate, error) {
	roots := x509.NewCertPool()
	remaining := raw
	certificates := []*x509.Certificate{}
	for len(bytes.TrimSpace(remaining)) > 0 {
		block, rest := pem.Decode(remaining)
		if block == nil || block.Type != "CERTIFICATE" || len(block.Headers) != 0 {
			return nil, nil, errors.New("admission client CA bundle contains a non-canonical PEM object")
		}
		certificate, err := x509.ParseCertificate(block.Bytes)
		if err != nil || !certificate.IsCA || certificate.KeyUsage&x509.KeyUsageCertSign == 0 {
			return nil, nil, errors.New("admission client trust contains a non-CA certificate")
		}
		roots.AddCert(certificate)
		certificates = append(certificates, certificate)
		remaining = rest
	}
	if len(certificates) == 0 || strings.TrimSpace(string(remaining)) != "" {
		return nil, nil, errors.New("admission client CA bundle is empty or has trailing bytes")
	}
	return roots, certificates, nil
}

func hasExtendedKeyUsage(certificate *x509.Certificate, expected x509.ExtKeyUsage) bool {
	for _, usage := range certificate.ExtKeyUsage {
		if usage == expected {
			return true
		}
	}
	return false
}
