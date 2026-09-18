package snapshotauthority

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
)

const collectorPossessionHeader = "X-FS2-Collector-Possession"
const collectorExporterLabel = "EXPORTER-fs2-public-edge-snapshot-collector-v1"

type acceptedServerIdentity struct {
	slot string
	certificate tls.Certificate
	leaf *x509.Certificate
	issuer *x509.Certificate
	revocation *x509.RevocationList
	notBefore time.Time
	notAfter time.Time
}

type acceptedCollectorIdentity struct {
	slot string
	spkiSHA256 string
	spiffeURI string
	crlSHA256 string
	roots *x509.CertPool
	revocation *x509.RevocationList
	notBefore time.Time
	notAfter time.Time
}

type channelAuthenticator struct {
	servers []acceptedServerIdentity
	collectors map[string]acceptedCollectorIdentity
}

type collectorChannelVerifier struct {
	collectors map[string]acceptedCollectorIdentity
}

func loadChannelAuthenticator(config Config) (*channelAuthenticator, error) {
	collectorVerifier, err := loadCollectorChannelVerifier(config)
	if err != nil { return nil, err }
	result := &channelAuthenticator{collectors: collectorVerifier.collectors}
	for _, enrolled := range config.ServerIdentities {
		identity, err := loadServerIdentity(enrolled)
		if err != nil { return nil, fmt.Errorf("load snapshot-authority server identity %s: %w", enrolled.Slot, err) }
		result.servers = append(result.servers, identity)
	}
	if len(result.servers) < 1 || len(result.servers) > 2 {
		return nil, errors.New("snapshot authority requires one current and at most one next server identity")
	}
	sort.Slice(result.servers, func(i, j int) bool { return result.servers[i].notBefore.Before(result.servers[j].notBefore) })
	return result, nil
}

func loadCollectorChannelVerifier(config Config) (*collectorChannelVerifier, error) {
	result := &collectorChannelVerifier{collectors: map[string]acceptedCollectorIdentity{}}
	for _, enrolled := range config.CollectorIdentities {
		identity, err := loadCollectorIdentity(enrolled)
		if err != nil { return nil, fmt.Errorf("load collector client identity %s: %w", enrolled.Slot, err) }
		if _, duplicate := result.collectors[identity.spkiSHA256]; duplicate { return nil, errors.New("collector mTLS identities repeat an SPKI") }
		result.collectors[identity.spkiSHA256] = identity
	}
	if len(result.collectors) < 1 || len(result.collectors) > 2 {
		return nil, errors.New("snapshot authority requires one current and at most one next collector identity")
	}
	return result, nil
}

func (auth *channelAuthenticator) tlsConfig() *tls.Config {
	return &tls.Config{
		MinVersion: tls.VersionTLS13,
		ClientAuth: tls.RequestClientCert,
		GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
			identity, err := auth.currentServer(time.Now().UTC())
			if err != nil { return nil, err }
			return &identity.certificate, nil
		},
		VerifyConnection: func(state tls.ConnectionState) error {
			if len(state.PeerCertificates) == 0 { return nil }
			_, err := auth.authenticateCollector(state, time.Now().UTC())
			return err
		},
	}
}

func (auth *channelAuthenticator) currentServer(now time.Time) (*acceptedServerIdentity, error) {
	var selected *acceptedServerIdentity
	for index := range auth.servers {
		identity := &auth.servers[index]
		if now.Before(identity.notBefore) || !now.Before(identity.notAfter) || now.Before(identity.leaf.NotBefore) || !now.Before(identity.leaf.NotAfter) || revoked(identity.revocation, identity.leaf) { continue }
		if selected == nil || identity.notBefore.After(selected.notBefore) { selected = identity }
	}
	if selected == nil { return nil, errors.New("no unrevoked current snapshot-authority server identity is active") }
	return selected, nil
}

func (auth *channelAuthenticator) currentCollector(now time.Time) error {
	for _, identity := range auth.collectors {
		if !now.Before(identity.notBefore) && now.Before(identity.notAfter) && freshCRL(identity.revocation, now) { return nil }
	}
	return errors.New("no unrevoked current collector identity is active")
}

func (auth *channelAuthenticator) authenticateCollector(state tls.ConnectionState, now time.Time) (CollectorChannelEvidence, error) {
	if len(state.PeerCertificates) < 1 { return CollectorChannelEvidence{}, errors.New("snapshot authority requires an authenticated collector certificate") }
	leaf := state.PeerCertificates[0]
	spki := digestBytes(leaf.RawSubjectPublicKeyInfo)
	enrolled, exists := auth.collectors[spki]
	if !exists || now.Before(enrolled.notBefore) || !now.Before(enrolled.notAfter) || revoked(enrolled.revocation, leaf) {
		return CollectorChannelEvidence{}, errors.New("collector certificate identity is absent, retired or revoked")
	}
	intermediates := x509.NewCertPool()
	for _, certificate := range state.PeerCertificates[1:] { intermediates.AddCert(certificate) }
	chains, err := leaf.Verify(x509.VerifyOptions{Roots: enrolled.roots, Intermediates: intermediates, CurrentTime: now, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}})
	if err != nil || len(chains) != 1 || len(chains[0]) < 2 || enrolled.revocation.CheckSignatureFrom(chains[0][1]) != nil {
		return CollectorChannelEvidence{}, errors.New("collector certificate and revocation status do not form one exact current clientAuth issuer chain")
	}
	identityCount := 0
	for _, uri := range leaf.URIs { if uri.String() == enrolled.spiffeURI { identityCount++ } }
	if identityCount != 1 { return CollectorChannelEvidence{}, errors.New("collector certificate does not carry its one exact SPIFFE identity") }
	chainDER := make([]string, 0, len(chains[0]))
	for _, certificate := range chains[0] { chainDER = append(chainDER, base64.StdEncoding.EncodeToString(certificate.Raw)) }
	return CollectorChannelEvidence{Slot: enrolled.slot, SPKISHA256: spki, SPIFFEURI: enrolled.spiffeURI, CRLSHA256: enrolled.crlSHA256, VerifiedChainDERBase64: chainDER, ObservedAt: now.UTC().Truncate(time.Second).Format(time.RFC3339)}, nil
}

func (auth *channelAuthenticator) verifyCollectorPossession(request *http.Request, bundleSHA256 string, now time.Time) ([]byte, error) {
	if request == nil || request.TLS == nil || len(request.TLS.PeerCertificates) < 1 || !isDigestText(bundleSHA256) {
		return nil, errors.New("collector possession request lacks its authenticated TLS channel")
	}
	encoded := request.Header.Get(collectorPossessionHeader)
	if encoded == "" || len(encoded) > 16384 { return nil, errors.New("collector possession proof is absent or oversized") }
	raw, err := base64.RawURLEncoding.Strict().DecodeString(encoded)
	if err != nil || base64.RawURLEncoding.EncodeToString(raw) != encoded { return nil, errors.New("collector possession proof is not canonical base64url") }
	var proof CollectorPossessionProof
	if _, err := boundary.CanonicalJSON(raw, &proof); err != nil { return nil, errors.New("collector possession proof is not canonical JSON") }
	exporter, err := request.TLS.ExportKeyingMaterial(collectorExporterLabel, []byte(bundleSHA256), 32)
	if err != nil { return nil, errors.New("collector TLS exporter is unavailable") }
	if err := verifyCollectorPossessionProof(proof, request.TLS.PeerCertificates[0], bundleSHA256, digestBytes(exporter), now); err != nil { return nil, err }
	return raw, nil
}

func (verifier *collectorChannelVerifier) verifyEvidence(evidence CollectorChannelEvidence, now time.Time) error {
	observedAt, err := time.Parse(time.RFC3339, evidence.ObservedAt)
	if err != nil || observedAt.Nanosecond() != 0 || observedAt.After(now.Add(5*time.Second)) || now.Sub(observedAt) > 30*time.Second || len(evidence.VerifiedChainDERBase64) < 2 || len(evidence.VerifiedChainDERBase64) > 8 {
		return errors.New("collector channel observation is stale or malformed")
	}
	enrolled, exists := verifier.collectors[evidence.SPKISHA256]
	if !exists || evidence.Slot != enrolled.slot || evidence.SPIFFEURI != enrolled.spiffeURI || evidence.CRLSHA256 != enrolled.crlSHA256 || now.Before(enrolled.notBefore) || !now.Before(enrolled.notAfter) || observedAt.Before(enrolled.notBefore) || !observedAt.Before(enrolled.notAfter) || !freshCRL(enrolled.revocation, now) {
		return errors.New("collector channel identity is not independently enrolled and current")
	}
	certificates := make([]*x509.Certificate, 0, len(evidence.VerifiedChainDERBase64))
	for _, encoded := range evidence.VerifiedChainDERBase64 {
		der, decodeErr := base64.StdEncoding.Strict().DecodeString(encoded)
		if decodeErr != nil || base64.StdEncoding.EncodeToString(der) != encoded { return errors.New("collector channel chain is not canonical DER base64") }
		certificate, parseErr := x509.ParseCertificate(der)
		if parseErr != nil { return errors.New("collector channel chain contains an invalid certificate") }
		certificates = append(certificates, certificate)
	}
	leaf := certificates[0]
	if digestBytes(leaf.RawSubjectPublicKeyInfo) != evidence.SPKISHA256 || revoked(enrolled.revocation, leaf) { return errors.New("collector channel leaf differs from enrollment or is revoked") }
	identityCount := 0
	for _, uri := range leaf.URIs { if uri.String() == evidence.SPIFFEURI { identityCount++ } }
	if identityCount != 1 { return errors.New("collector channel leaf lacks its exact SPIFFE identity") }
	intermediates := x509.NewCertPool()
	for _, certificate := range certificates[1:] { intermediates.AddCert(certificate) }
	chains, verifyErr := leaf.Verify(x509.VerifyOptions{Roots: enrolled.roots, Intermediates: intermediates, CurrentTime: now, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}})
	if verifyErr != nil || len(chains) != 1 || len(chains[0]) != len(certificates) || enrolled.revocation.CheckSignatureFrom(chains[0][1]) != nil { return errors.New("collector channel chain is not the unique current clientAuth chain") }
	for index := range certificates { if !bytes.Equal(certificates[index].Raw, chains[0][index].Raw) { return errors.New("collector channel transcript differs from the verified chain") } }
	proofRaw, err := base64.StdEncoding.Strict().DecodeString(evidence.CollectorPossessionProofBase64)
	if err != nil || base64.StdEncoding.EncodeToString(proofRaw) != evidence.CollectorPossessionProofBase64 || digestBytes(proofRaw) != evidence.CollectorPossessionProofSHA256 { return errors.New("collector possession proof differs from its signed transcript") }
	var proof CollectorPossessionProof
	if _, err := boundary.CanonicalJSON(proofRaw, &proof); err != nil { return errors.New("collector possession proof is not canonical") }
	if err := verifyCollectorPossessionProof(proof, leaf, evidence.EvidenceBundleSHA256, proof.TLSExporterSHA256, now); err != nil { return err }
	return nil
}

func verifyCollectorPossessionProof(proof CollectorPossessionProof, leaf *x509.Certificate, bundleSHA256 string, exporterSHA256 string, now time.Time) error {
	observedAt, err := time.Parse(time.RFC3339, proof.ObservedAt)
	nonce, nonceErr := base64.RawURLEncoding.Strict().DecodeString(proof.Nonce)
	signature, signatureErr := base64.RawURLEncoding.Strict().DecodeString(proof.Signature)
	if err != nil || observedAt.Nanosecond() != 0 || observedAt.After(now.Add(5*time.Second)) || now.Sub(observedAt) > 30*time.Second ||
		nonceErr != nil || len(nonce) != 32 || base64.RawURLEncoding.EncodeToString(nonce) != proof.Nonce ||
		signatureErr != nil || len(signature) < 32 || len(signature) > 1024 || base64.RawURLEncoding.EncodeToString(signature) != proof.Signature ||
		proof.Schema != CollectorPossessionProofSchema || proof.EvidenceBundleSHA256 != bundleSHA256 || proof.TLSExporterSHA256 != exporterSHA256 || !isDigestText(exporterSHA256) {
		return errors.New("collector possession proof fields are invalid or stale")
	}
	statementRaw, err := json.Marshal(collectorPossessionStatement{Schema: proof.Schema, EvidenceBundleSHA256: proof.EvidenceBundleSHA256, TLSExporterSHA256: proof.TLSExporterSHA256, Nonce: proof.Nonce, ObservedAt: proof.ObservedAt})
	if err != nil { return err }
	algorithm := x509.UnknownSignatureAlgorithm
	switch leaf.PublicKey.(type) {
	case ed25519.PublicKey:
		if proof.SignatureAlgorithm == "ed25519" { algorithm = x509.PureEd25519 }
	case *ecdsa.PublicKey:
		if proof.SignatureAlgorithm == "ecdsa-sha256" { algorithm = x509.ECDSAWithSHA256 }
	case *rsa.PublicKey:
		if proof.SignatureAlgorithm == "rsa-pkcs1-sha256" { algorithm = x509.SHA256WithRSA }
	}
	if algorithm == x509.UnknownSignatureAlgorithm || leaf.CheckSignature(algorithm, statementRaw, signature) != nil {
		return errors.New("collector possession proof was not signed by the authenticated TLS leaf")
	}
	return nil
}

func loadServerIdentity(enrolled ServerIdentity) (acceptedServerIdentity, error) {
	certificateRaw, err := readAcceptedRegular(enrolled.CertificatePath, 4*1024*1024, enrolled.CertificateSHA256, false)
	if err != nil { return acceptedServerIdentity{}, err }
	keyRaw, err := readAcceptedRegular(enrolled.PrivateKeyPath, 256*1024, enrolled.PrivateKeySHA256, true)
	if err != nil { return acceptedServerIdentity{}, err }
	pair, err := tls.X509KeyPair(certificateRaw, keyRaw)
	if err != nil || len(pair.Certificate) < 1 { return acceptedServerIdentity{}, errors.New("server TLS certificate/key pair is invalid") }
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil || digestBytes(leaf.RawSubjectPublicKeyInfo) != enrolled.SPKISHA256 || !containsUsage(leaf.ExtKeyUsage, x509.ExtKeyUsageServerAuth) { return acceptedServerIdentity{}, errors.New("server TLS leaf differs from accepted SPKI or serverAuth purpose") }
	if enrolled.DNSName != "" && leaf.VerifyHostname(enrolled.DNSName) != nil { return acceptedServerIdentity{}, errors.New("server TLS leaf lacks its accepted DNS SAN") }
	issuer, revocation, err := loadIssuerAndCRL(enrolled.IssuerBundlePath, enrolled.IssuerBundleSHA256, enrolled.CRLPath, enrolled.CRLSHA256, leaf)
	if err != nil { return acceptedServerIdentity{}, err }
	notBefore, notAfter, err := acceptedWindow(enrolled.NotBefore, enrolled.NotAfter, leaf.NotBefore, leaf.NotAfter)
	if err != nil { return acceptedServerIdentity{}, err }
	return acceptedServerIdentity{slot: enrolled.Slot, certificate: pair, leaf: leaf, issuer: issuer, revocation: revocation, notBefore: notBefore, notAfter: notAfter}, nil
}

func loadCollectorIdentity(enrolled CollectorIdentity) (acceptedCollectorIdentity, error) {
	caRaw, err := readAcceptedRegular(enrolled.CABundlePath, 4*1024*1024, enrolled.CABundleSHA256, false)
	if err != nil { return acceptedCollectorIdentity{}, err }
	roots, certificates, err := certificatePool(caRaw)
	if err != nil { return acceptedCollectorIdentity{}, err }
	crlRaw, err := readAcceptedRegular(enrolled.CRLPath, 4*1024*1024, enrolled.CRLSHA256, false)
	if err != nil { return acceptedCollectorIdentity{}, err }
	revocation, err := x509.ParseRevocationList(crlRaw)
	if err != nil || !freshCRL(revocation, time.Now().UTC()) { return acceptedCollectorIdentity{}, errors.New("collector client CRL is invalid or stale") }
	signed := false
	for _, certificate := range certificates { if revocation.CheckSignatureFrom(certificate) == nil { signed = true } }
	if !signed { return acceptedCollectorIdentity{}, errors.New("collector client CRL is not signed by its accepted CA") }
	notBefore, notAfter, err := acceptedWindow(enrolled.NotBefore, enrolled.NotAfter, time.Time{}, time.Time{})
	if err != nil { return acceptedCollectorIdentity{}, err }
	if _, err := url.ParseRequestURI(enrolled.SPIFFEURI); err != nil || !strings.HasPrefix(enrolled.SPIFFEURI, "spiffe://") { return acceptedCollectorIdentity{}, errors.New("collector SPIFFE identity is invalid") }
	return acceptedCollectorIdentity{slot: enrolled.Slot, spkiSHA256: enrolled.SPKISHA256, spiffeURI: enrolled.SPIFFEURI, crlSHA256: enrolled.CRLSHA256, roots: roots, revocation: revocation, notBefore: notBefore, notAfter: notAfter}, nil
}

func loadIssuerAndCRL(issuerPath string, issuerDigest string, crlPath string, crlDigest string, leaf *x509.Certificate) (*x509.Certificate, *x509.RevocationList, error) {
	raw, err := readAcceptedRegular(issuerPath, 4*1024*1024, issuerDigest, false)
	if err != nil { return nil, nil, err }
	_, issuers, err := certificatePool(raw)
	if err != nil { return nil, nil, err }
	crlRaw, err := readAcceptedRegular(crlPath, 4*1024*1024, crlDigest, false)
	if err != nil { return nil, nil, err }
	list, err := x509.ParseRevocationList(crlRaw)
	if err != nil || !freshCRL(list, time.Now().UTC()) { return nil, nil, errors.New("TLS issuer CRL is invalid or stale") }
	for _, issuer := range issuers {
		if leaf.CheckSignatureFrom(issuer) == nil && list.CheckSignatureFrom(issuer) == nil { return issuer, list, nil }
	}
	return nil, nil, errors.New("TLS leaf and CRL do not share one accepted issuer")
}

func certificatePool(raw []byte) (*x509.CertPool, []*x509.Certificate, error) {
	pool := x509.NewCertPool()
	certificates := []*x509.Certificate{}
	for len(raw) > 0 {
		block, rest := pem.Decode(raw)
		if block == nil || block.Type != "CERTIFICATE" { return nil, nil, errors.New("CA bundle contains malformed or non-certificate PEM") }
		certificate, err := x509.ParseCertificate(block.Bytes)
		if err != nil || !certificate.IsCA { return nil, nil, errors.New("CA bundle contains a non-CA certificate") }
		pool.AddCert(certificate)
		certificates = append(certificates, certificate)
		raw = rest
	}
	if len(certificates) == 0 { return nil, nil, errors.New("CA bundle is empty") }
	return pool, certificates, nil
}

func acceptedWindow(rawNotBefore string, rawNotAfter string, certificateNotBefore time.Time, certificateNotAfter time.Time) (time.Time, time.Time, error) {
	notBefore, firstErr := time.Parse(time.RFC3339, rawNotBefore)
	notAfter, secondErr := time.Parse(time.RFC3339, rawNotAfter)
	if firstErr != nil || secondErr != nil || notBefore.Nanosecond() != 0 || notAfter.Nanosecond() != 0 || !notAfter.After(notBefore) ||
		(!certificateNotBefore.IsZero() && notBefore.Before(certificateNotBefore)) || (!certificateNotAfter.IsZero() && notAfter.After(certificateNotAfter)) {
		return time.Time{}, time.Time{}, errors.New("accepted TLS identity window is invalid")
	}
	return notBefore.UTC(), notAfter.UTC(), nil
}

func revoked(list *x509.RevocationList, leaf *x509.Certificate) bool {
	if list == nil || leaf == nil || !freshCRL(list, time.Now().UTC()) { return true }
	for _, entry := range list.RevokedCertificateEntries { if entry.SerialNumber.Cmp(leaf.SerialNumber) == 0 { return true } }
	return false
}

func freshCRL(list *x509.RevocationList, now time.Time) bool {
	return list != nil && !list.ThisUpdate.After(now.Add(30*time.Second)) && list.NextUpdate.After(now)
}

func containsUsage(values []x509.ExtKeyUsage, expected x509.ExtKeyUsage) bool { for _, value := range values { if value == expected { return true } }; return false }

func readAcceptedRegular(path string, maximum int64, expectedDigest string, private bool) ([]byte, error) {
	if path == "" || maximum < 1 || !isDigestText(expectedDigest) { return nil, errors.New("accepted regular-file contract is incomplete") }
	info, err := os.Lstat(path)
	if err != nil || info == nil || !info.Mode().IsRegular() || info.Size() < 1 || info.Size() > maximum { return nil, errors.New("accepted path is not a bounded regular file") }
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 || info.Mode().Perm()&0o022 != 0 || private && (info.Mode().Perm() != 0o440 || stat.Gid != uint32(os.Getegid())) { return nil, errors.New("accepted regular file lacks root custody or exact private reader group mode") }
	raw, err := os.ReadFile(path)
	if err != nil || int64(len(raw)) != info.Size() || digestBytes(raw) != expectedDigest { return nil, errors.New("accepted regular file bytes changed") }
	after, err := os.Lstat(path)
	if err != nil || !os.SameFile(info, after) { return nil, errors.New("accepted regular file changed during read") }
	return raw, nil
}

func digestBytes(raw []byte) string { sum := sha256.Sum256(raw); return hex.EncodeToString(sum[:]) }
