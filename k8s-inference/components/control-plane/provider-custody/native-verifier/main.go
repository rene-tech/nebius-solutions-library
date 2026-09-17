// custody-verifier is the hermetic signature and X.509 verifier used by
// inference-stack. Build it with CGO disabled; the launcher independently
// rejects an artifact that contains PT_INTERP.
package main

import (
	"bytes"
	"crypto"
	"crypto/ecdsa"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"sort"
	"strconv"
	"syscall"
)

const (
	maxDocumentBytes = 16 * 1024 * 1024
	fGetSeals        = 1034
	requiredSeals    = 0x0001 | 0x0002 | 0x0004 | 0x0008
)

var sealedFDPattern = regexp.MustCompile(`^/proc/self/fd/([1-9][0-9]*)$`)

func fail(format string, values ...any) {
	fmt.Fprintf(os.Stderr, "custody verification failed: "+format+"\n", values...)
	os.Exit(2)
}

func readSealedFile(path string) ([]byte, error) {
	match := sealedFDPattern.FindStringSubmatch(path)
	if match == nil {
		return nil, errors.New("input is not an inherited descriptor")
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
		return nil, errors.New("input is not an immutable private memfd")
	}
	file := os.NewFile(uintptr(fd), path)
	if file == nil {
		return nil, errors.New("input descriptor is unavailable")
	}
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	value, err := io.ReadAll(io.LimitReader(file, maxDocumentBytes+1))
	if err != nil {
		return nil, err
	}
	if len(value) == 0 || len(value) > maxDocumentBytes {
		return nil, errors.New("input size is invalid")
	}
	return value, nil
}

func readStandardInput() ([]byte, error) {
	value, err := io.ReadAll(io.LimitReader(os.Stdin, maxDocumentBytes+1))
	if err != nil {
		return nil, err
	}
	if len(value) == 0 || len(value) > maxDocumentBytes {
		return nil, errors.New("certificate size is invalid")
	}
	return value, nil
}

func decodeOnePEM(value []byte) ([]byte, string, error) {
	block, remaining := pem.Decode(value)
	if block == nil || len(bytes.TrimSpace(remaining)) != 0 {
		return nil, "", errors.New("input must contain exactly one PEM block")
	}
	return block.Bytes, block.Type, nil
}

func parseCertificate(value []byte) (*x509.Certificate, error) {
	der := value
	if bytes.HasPrefix(bytes.TrimSpace(value), []byte("-----BEGIN")) {
		decoded, blockType, err := decodeOnePEM(value)
		if err != nil {
			return nil, err
		}
		if blockType != "CERTIFICATE" {
			return nil, errors.New("input PEM block is not a certificate")
		}
		der = decoded
	}
	return x509.ParseCertificate(der)
}

func parsePublicKey(value []byte) (crypto.PublicKey, error) {
	der, blockType, err := decodeOnePEM(value)
	if err != nil {
		return nil, err
	}
	switch blockType {
	case "PUBLIC KEY":
		return x509.ParsePKIXPublicKey(der)
	case "RSA PUBLIC KEY":
		return x509.ParsePKCS1PublicKey(der)
	case "CERTIFICATE":
		certificate, err := x509.ParseCertificate(der)
		if err != nil {
			return nil, err
		}
		return certificate.PublicKey, nil
	default:
		return nil, errors.New("unsupported public-key PEM type")
	}
}

func verifySHA256(arguments []string) error {
	if len(arguments) != 6 || arguments[0] != "--public-key" || arguments[2] != "--signature" || arguments[4] != "--document" {
		return errors.New("verify-sha256 arguments are not exact")
	}
	publicKeyBytes, err := readSealedFile(arguments[1])
	if err != nil {
		return fmt.Errorf("public key: %w", err)
	}
	signature, err := readSealedFile(arguments[3])
	if err != nil {
		return fmt.Errorf("signature: %w", err)
	}
	document, err := readSealedFile(arguments[5])
	if err != nil {
		return fmt.Errorf("document: %w", err)
	}
	publicKey, err := parsePublicKey(publicKeyBytes)
	if err != nil {
		return err
	}
	digest := sha256.Sum256(document)
	switch key := publicKey.(type) {
	case *rsa.PublicKey:
		err = rsa.VerifyPKCS1v15(key, crypto.SHA256, digest[:], signature)
	case *ecdsa.PublicKey:
		if !ecdsa.VerifyASN1(key, digest[:], signature) {
			err = errors.New("ECDSA signature is invalid")
		}
	default:
		err = errors.New("unsupported custody signing key type")
	}
	if err != nil {
		return err
	}
	_, err = os.Stdout.WriteString("verified\n")
	return err
}

func certificateDER() error {
	value, err := readStandardInput()
	if err != nil {
		return err
	}
	certificate, err := parseCertificate(value)
	if err != nil {
		return err
	}
	_, err = os.Stdout.Write(certificate.Raw)
	return err
}

func certificateSPIFFEURIs() error {
	value, err := readStandardInput()
	if err != nil {
		return err
	}
	certificate, err := parseCertificate(value)
	if err != nil {
		return err
	}
	identities := make([]string, 0, len(certificate.URIs))
	for _, identity := range certificate.URIs {
		if identity != nil && identity.Scheme == "spiffe" {
			identities = append(identities, identity.String())
		}
	}
	sort.Strings(identities)
	return json.NewEncoder(os.Stdout).Encode(identities)
}

func main() {
	if len(os.Args) < 2 {
		fail("one exact operation is required")
	}
	var err error
	switch os.Args[1] {
	case "verify-sha256":
		err = verifySHA256(os.Args[2:])
	case "x509-der":
		if len(os.Args) != 2 {
			err = errors.New("x509-der accepts no arguments")
		} else {
			err = certificateDER()
		}
	case "x509-spiffe-uris":
		if len(os.Args) != 2 {
			err = errors.New("x509-spiffe-uris accepts no arguments")
		} else {
			err = certificateSPIFFEURIs()
		}
	default:
		err = errors.New("unsupported operation")
	}
	if err != nil {
		fail("%v", err)
	}
}
