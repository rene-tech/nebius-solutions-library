package boundary

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"time"
)

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

func hasExtendedKeyUsage(certificate *x509.Certificate, expected x509.ExtKeyUsage) bool {
	for _, usage := range certificate.ExtKeyUsage {
		if usage == expected {
			return true
		}
	}
	return false
}
