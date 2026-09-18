package snapshotauthority

import (
	"bytes"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/collector"
)

type authenticationConfigurationEvidence struct {
	Schema string `json:"schema"`
	CommandArguments []string `json:"command_arguments"`
	ClientCABundleBase64 string `json:"client_ca_bundle_base64"`
	ClientCASHA256 string `json:"client_ca_sha256"`
	RequestHeaderCABundleBase64 string `json:"request_header_ca_bundle_base64"`
	RequestHeaderCASHA256 string `json:"request_header_ca_sha256"`
}

type authenticationTrust struct {
	clientCACertificates map[string]bool
	requestHeaderCACertificates map[string]bool
}

type authorizationConfigurationEvidence struct {
	Schema string `json:"schema"`
	CommandArguments []string `json:"command_arguments"`
	WebhookConfigurationBase64 string `json:"webhook_configuration_base64"`
	WebhookConfigurationSHA256 string `json:"webhook_configuration_sha256"`
}

type issuedCredential struct {
	Serial string `json:"serial"`
	CertificateDERBase64 string `json:"certificate_der_base64"`
	CertificateChainDERBase64 []string `json:"certificate_chain_der_base64"`
	SignerName string `json:"signer_name"`
	RevokedAt string `json:"revoked_at"`
}

type providerIAMPolicy struct {
	ID string `json:"id"`
	Actions []string `json:"actions"`
	Resources []string `json:"resources"`
	ParentPolicyIDs []string `json:"parent_policy_ids"`
}

type providerIAMBinding struct {
	ID string `json:"id"`
	Principal string `json:"principal"`
	PolicyID string `json:"policy_id"`
	ResourceScope string `json:"resource_scope"`
}

type providerIAMResource struct {
	ID string `json:"id"`
	ParentID string `json:"parent_id"`
}

type providerIAMPolicyPage struct {
	Items []providerIAMPolicy `json:"items"`
	Resources []providerIAMResource `json:"resources"`
	NextPageToken string `json:"next_page_token"`
	RemainingItemCount int64 `json:"remaining_item_count"`
}

type providerNodeGroup struct {
	ID string `json:"id"`
	Principal string `json:"principal"`
	ProviderID string `json:"provider_id"`
	NodeName string `json:"node_name"`
	NodeUID string `json:"node_uid"`
	KubeletIdentity string `json:"kubelet_identity"`
}

type secretClassification struct {
	Namespace string `json:"namespace"`
	Name string `json:"name"`
	UID string `json:"uid"`
	ResourceVersion string `json:"resource_version"`
	ObjectSHA256 string `json:"object_sha256"`
	CredentialClasses []string `json:"credential_classes"`
	KeyNames []string `json:"key_names"`
	KMSKeyID string `json:"kms_key_id"`
	ContentAttestationSHA256 string `json:"content_attestation_sha256"`
	ContentAttestationBase64 string `json:"content_attestation_base64"`
	Unknown bool `json:"unknown"`
}

type secretContentAttestation struct {
	Schema string `json:"schema"`
	Namespace string `json:"namespace"`
	Name string `json:"name"`
	UID string `json:"uid"`
	ResourceVersion string `json:"resource_version"`
	ObjectSHA256 string `json:"object_sha256"`
	CiphertextSHA256 string `json:"ciphertext_sha256"`
	KMSKeyID string `json:"kms_key_id"`
	KMSKeyVersion string `json:"kms_key_version"`
	KMSAuditID string `json:"kms_audit_id"`
	ClassifierRuntimeSHA256 string `json:"classifier_runtime_sha256"`
	ClassifierPolicySHA256 string `json:"classifier_policy_sha256"`
	KeyNames []string `json:"key_names"`
	CredentialClasses []string `json:"credential_classes"`
	RecognizedSecretFormats []string `json:"recognized_secret_formats"`
}

func deriveControllerActor(serviceAccount inventoryObject, enrolled boundary.Actor) (boundary.Actor, error) {
	derived := boundary.Actor{
		Username: "system:serviceaccount:" + serviceAccount.identity.Namespace + ":" + serviceAccount.identity.Name,
		UID: serviceAccount.identity.UID,
		Groups: []string{"system:authenticated", "system:serviceaccounts", "system:serviceaccounts:" + serviceAccount.identity.Namespace},
		Extra: map[string][]string{},
	}
	sort.Strings(derived.Groups)
	if enrolled.Username != derived.Username || enrolled.UID != derived.UID || !sameStringSet(enrolled.Groups, derived.Groups) || len(enrolled.Extra) != 0 {
		return boundary.Actor{}, errors.New("accepted controller actor does not equal the ServiceAccount identity derived from the native object")
	}
	return derived, nil
}

func validateSupplementalAuthorityEvidence(
	verified *collector.VerifiedEvidenceBundle,
	config collector.Config,
	policy SemanticPolicy,
	inventory *semanticInventory,
	serviceAccount inventoryObject,
	credentialSecrets []boundary.ObjectIdentity,
) error {
	if err := validateSemanticPaginationContract(config); err != nil { return err }
	consumed := map[string]bool{}
	for collection := range kubernetesCollections { consumed[collection] = true }
	for _, collection := range []string{
		"provider-load-balancers", "provider-load-balancer-listeners", "provider-load-balancer-backends", "provider-load-balancer-health-checks",
	} { consumed[collection] = true }
	responses := responsesByCollection(verified)
	authTrust, err := validateAuthenticationEvidence(responses["apiserver-authentication-configuration"], policy, verified.Bundle.CollectedAt)
	if err != nil { return err }
	consumed["apiserver-authentication-configuration"] = true
	if err := validateAuthorizationEvidence(responses["apiserver-authorization-configuration"]); err != nil { return err }
	consumed["apiserver-authorization-configuration"] = true
	if err := validateCertificateEvidence(responses["ca-issued-credentials"], responses["ca-revocation-status"], verified.Bundle.CollectedAt, policy, authTrust); err != nil { return err }
	consumed["ca-issued-credentials"] = true
	consumed["ca-revocation-status"] = true
	if err := validateProviderIAMEvidence(responses["provider-iam-policies"], responses["provider-iam-bindings"], policy, serviceAccount); err != nil { return err }
	consumed["provider-iam-policies"] = true
	consumed["provider-iam-bindings"] = true
	if err := validateNodeGroupEvidence(responses["provider-nodegroup-membership"], policy, inventory); err != nil { return err }
	consumed["provider-nodegroup-membership"] = true
	if err := validateSecretClassifications(responses["secret-kms-classification"], inventory, credentialSecrets, policy); err != nil { return err }
	consumed["secret-kms-classification"] = true
	for _, required := range config.MandatoryCollections {
		if !consumed[required] { return fmt.Errorf("mandatory semantic collection %s has no authoritative consumer", required) }
	}
	return nil
}

func validateSemanticPaginationContract(config collector.Config) error {
	if config.Schema != collector.ConfigSchema {
		return errors.New("semantic authority requires the additive collector v3 pagination contract")
	}
	for _, source := range config.Sources {
		expected := "provider-page-token"
		if strings.HasPrefix(source.SemanticCollection, "apiserver-") {
			expected = "kubernetes-continue"
		}
		if source.SemanticCollection == "apiserver-authentication-configuration" || source.SemanticCollection == "apiserver-authorization-configuration" || source.SemanticCollection == "ca-revocation-status" {
			expected = "none"
		}
		if source.Pagination != expected {
			return fmt.Errorf("semantic collection %s uses %s pagination instead of %s", source.SemanticCollection, source.Pagination, expected)
		}
	}
	return nil
}

func responsesByCollection(verified *collector.VerifiedEvidenceBundle) map[string][][]byte {
	result := map[string][][]byte{}
	for _, source := range verified.Bundle.Sources { result[source.SemanticCollection] = append(result[source.SemanticCollection], verified.RawResponses[source.SourceID]...) }
	return result
}

func validateAuthenticationEvidence(rawPages [][]byte, policy SemanticPolicy, collectedAt string) (authenticationTrust, error) {
	if len(rawPages) != 1 { return authenticationTrust{}, errors.New("API-server authentication configuration is not one exact authoritative response") }
	var evidence authenticationConfigurationEvidence
	if boundary.DecodeExactJSON(rawPages[0], &evidence) != nil || evidence.Schema != "fs2-serve.nebius.ai/apiserver-authentication-configuration/v2" { return authenticationTrust{}, errors.New("API-server authentication command evidence is incomplete") }
	clientCA, clientErr := base64.StdEncoding.Strict().DecodeString(evidence.ClientCABundleBase64)
	requestHeaderCA, requestErr := base64.StdEncoding.Strict().DecodeString(evidence.RequestHeaderCABundleBase64)
	if clientErr != nil || requestErr != nil || base64.StdEncoding.EncodeToString(clientCA) != evidence.ClientCABundleBase64 || base64.StdEncoding.EncodeToString(requestHeaderCA) != evidence.RequestHeaderCABundleBase64 || digestBytes(clientCA) != evidence.ClientCASHA256 || digestBytes(requestHeaderCA) != evidence.RequestHeaderCASHA256 || !validCABundle(clientCA) || !validCABundle(requestHeaderCA) { return authenticationTrust{}, errors.New("API-server authentication CA bytes are absent, malformed or not content addressed") }
	collected, collectedErr := time.Parse(time.RFC3339, collectedAt)
	clientRoots, err := caRootSPKIDigests(clientCA, collected)
	requestHeaderRoots, requestHeaderErr := caRootSPKIDigests(requestHeaderCA, collected)
	clientCertificates, clientCertificatesErr := caCertificateDigests(clientCA)
	requestHeaderCertificates, requestHeaderCertificatesErr := caCertificateDigests(requestHeaderCA)
	if collectedErr != nil || collected.Nanosecond() != 0 || err != nil || requestHeaderErr != nil || clientCertificatesErr != nil || requestHeaderCertificatesErr != nil || !sameStringSet(clientRoots, policy.AllowedClientCARootSPKISHA256) || !sameStringSet(requestHeaderRoots, policy.AllowedRequestHeaderCARootSPKISHA256) { return authenticationTrust{}, errors.New("API-server client and request-header CA bundles do not form their exact independently accepted root and intermediate closures") }
	flags, normalizedArguments, flagsErr := parseCommandFlags(evidence.CommandArguments)
	normalizedArgumentsRaw, normalizedArgumentsErr := json.Marshal(normalizedArguments)
	if flagsErr != nil || normalizedArgumentsErr != nil || digestBytes(normalizedArgumentsRaw) != policy.APIServerAuthenticationArgumentsSHA256 { return authenticationTrust{}, errors.New("API-server authentication argv is ambiguous or differs from the independently accepted normalized full flag set") }
	issuer := flags["service-account-issuer"]
	if issuer == "" || !exactFlag(flags, "anonymous-auth", "false") || !exactFlag(flags, "client-ca-sha256", evidence.ClientCASHA256) || !exactFlag(flags, "requestheader-client-ca-sha256", evidence.RequestHeaderCASHA256) ||
		!exactCSVFlag(flags, "requestheader-allowed-names", policy.AllowedRequestHeaderProxyCommonNames) ||
		!exactCSVFlag(flags, "requestheader-username-headers", policy.RequestHeaderUsernameHeaders) ||
		!exactCSVFlag(flags, "requestheader-group-headers", policy.RequestHeaderGroupHeaders) ||
		!exactCSVFlag(flags, "requestheader-extra-headers-prefix", policy.RequestHeaderExtraHeaderPrefixes) {
		return authenticationTrust{}, errors.New("API-server authentication flags do not derive the exact non-anonymous client and request-header proxy identity contract")
	}
	return authenticationTrust{clientCACertificates: clientCertificates, requestHeaderCACertificates: requestHeaderCertificates}, nil
}

func validateAuthorizationEvidence(rawPages [][]byte) error {
	if len(rawPages) != 1 { return errors.New("API-server authorization configuration is not one exact authoritative response") }
	var evidence authorizationConfigurationEvidence
	if boundary.DecodeExactJSON(rawPages[0], &evidence) != nil || evidence.Schema != "fs2-serve.nebius.ai/apiserver-authorization-configuration/v2" {
		return errors.New("API-server authorization command evidence is incomplete")
	}
	flags, _, err := parseCommandFlags(evidence.CommandArguments)
	if err != nil || flags["authorization-mode"] == "" { return errors.New("API-server authorization argv or mode is ambiguous") }
	modes := strings.Split(flags["authorization-mode"], ",")
	sort.Strings(modes)
	if !canonicalNonemptyStrings(modes) || !containsString(modes, "RBAC") || containsString(modes, "AlwaysAllow") {
		return errors.New("API-server authorization configuration does not enforce the exact RBAC boundary")
	}
	if containsString(modes, "Webhook") {
		webhookRaw, err := base64.StdEncoding.Strict().DecodeString(evidence.WebhookConfigurationBase64)
		if err != nil || base64.StdEncoding.EncodeToString(webhookRaw) != evidence.WebhookConfigurationBase64 || digestBytes(webhookRaw) != evidence.WebhookConfigurationSHA256 || !exactFlag(flags, "authorization-webhook-config-sha256", evidence.WebhookConfigurationSHA256) { return errors.New("authorization webhook configuration bytes are not reopened and content addressed") }
	} else if evidence.WebhookConfigurationBase64 != "" || evidence.WebhookConfigurationSHA256 != "" { return errors.New("authorization evidence carries unused webhook configuration") }
	return nil
}

func parseCommandFlags(arguments []string) (map[string]string, []string, error) {
	if len(arguments) == 0 || len(arguments) > 4096 { return nil, nil, errors.New("API-server argv is empty or outside its bound") }
	result := map[string]string{}
	for index := 0; index < len(arguments); index++ {
		argument := arguments[index]
		if len(argument) < 3 || len(argument) > 8192 || !strings.HasPrefix(argument, "--") || strings.ContainsAny(argument, "\x00\r\n") { return nil, nil, errors.New("API-server argv contains a positional, empty or unsafe argument") }
		projection := strings.TrimPrefix(argument, "--")
		parts := strings.SplitN(projection, "=", 2)
		name := parts[0]
		value := ""
		if len(parts) == 2 {
			value = parts[1]
		} else {
			if index+1 >= len(arguments) || strings.HasPrefix(arguments[index+1], "--") { return nil, nil, errors.New("API-server flag lacks one explicit value") }
			index++
			value = arguments[index]
		}
		if !canonicalFlagName(name) || value == "" || len(value) > 8192 || strings.ContainsAny(value, "\x00\r\n") { return nil, nil, errors.New("API-server flag name or value is invalid") }
		if _, duplicate := result[name]; duplicate { return nil, nil, errors.New("API-server argv repeats or mixes forms for one flag") }
		result[name] = value
	}
	names := make([]string, 0, len(result))
	for name := range result { names = append(names, name) }
	sort.Strings(names)
	normalized := make([]string, 0, len(names))
	for _, name := range names { normalized = append(normalized, "--"+name+"="+result[name]) }
	return result, normalized, nil
}

func canonicalFlagName(value string) bool {
	if value == "" || len(value) > 128 { return false }
	for _, character := range value { if character != '-' && (character < 'a' || character > 'z') && (character < '0' || character > '9') { return false } }
	return value[0] >= 'a' && value[0] <= 'z'
}

func exactFlag(flags map[string]string, name string, expected string) bool {
	value, exists := flags[name]
	return exists && value == expected
}

func exactCSVFlag(flags map[string]string, name string, expected []string) bool {
	value, exists := flags[name]
	return exists && len(expected) > 0 && value == strings.Join(expected, ",")
}

func validCABundle(raw []byte) bool {
	count := 0
	for len(raw) > 0 {
		block, rest := pem.Decode(raw)
		if block == nil || block.Type != "CERTIFICATE" { return false }
		certificate, err := x509.ParseCertificate(block.Bytes)
		if err != nil || !certificate.IsCA { return false }
		raw = rest
		count++
	}
	return count > 0
}

func caRootSPKIDigests(raw []byte, at time.Time) ([]string, error) {
	result := []string{}
	certificates := []*x509.Certificate{}
	seen := map[string]bool{}
	for len(raw) > 0 {
		block, rest := pem.Decode(raw)
		if block == nil || block.Type != "CERTIFICATE" { return nil, errors.New("client CA bundle is not canonical PEM") }
		certificate, err := x509.ParseCertificate(block.Bytes)
		if err != nil || certificate == nil || !certificate.IsCA || !certificate.BasicConstraintsValid || certificate.KeyUsage&x509.KeyUsageCertSign == 0 { return nil, errors.New("client CA bundle contains an invalid CA") }
		certificateDigest := digestBytes(certificate.Raw)
		if seen[certificateDigest] { return nil, errors.New("client CA bundle contains a duplicate CA") }
		seen[certificateDigest] = true
		certificates = append(certificates, certificate)
		if certificate.CheckSignatureFrom(certificate) == nil { result = append(result, digestBytes(certificate.RawSubjectPublicKeyInfo)) }
		raw = rest
	}
	sort.Strings(result)
	if !canonicalNonemptyStrings(result) { return nil, errors.New("client CA bundle has no unique self-signed trust roots") }
	roots := x509.NewCertPool()
	intermediates := x509.NewCertPool()
	for _, certificate := range certificates { if certificate.CheckSignatureFrom(certificate) == nil { roots.AddCert(certificate) } else { intermediates.AddCert(certificate) } }
	for _, certificate := range certificates {
		if certificate.CheckSignatureFrom(certificate) == nil { continue }
		chains, err := certificate.Verify(x509.VerifyOptions{Roots: roots, Intermediates: intermediates, CurrentTime: at, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageAny}})
		if err != nil || len(chains) != 1 || len(chains[0]) < 2 || digestBytes(chains[0][len(chains[0])-1].RawSubjectPublicKeyInfo) == "" { return nil, errors.New("client CA intermediate does not form one unique constrained chain to an enrolled root") }
	}
	return result, nil
}

func caCertificateDigests(raw []byte) (map[string]bool, error) {
	result := map[string]bool{}
	for len(raw) > 0 {
		block, rest := pem.Decode(raw)
		if block == nil || block.Type != "CERTIFICATE" { return nil, errors.New("CA bundle is not canonical PEM") }
		certificate, err := x509.ParseCertificate(block.Bytes)
		digest := digestBytes(block.Bytes)
		if err != nil || certificate == nil || !certificate.IsCA || result[digest] { return nil, errors.New("CA bundle has an invalid or duplicate certificate") }
		result[digest] = true
		raw = rest
	}
	if len(result) == 0 { return nil, errors.New("CA bundle is empty") }
	return result, nil
}

func validateCertificateEvidence(issuedPages [][]byte, revocationPages [][]byte, collectedAt string, policy SemanticPolicy, trust authenticationTrust) error {
	collected, err := time.Parse(time.RFC3339, collectedAt)
	if err != nil { return errors.New("certificate evidence collection time is invalid") }
	revocations := map[string]*x509.RevocationList{}
	for _, raw := range revocationPages {
		list, err := x509.ParseRevocationList(raw)
		if err != nil || list == nil { return errors.New("revocation evidence is not a parseable issuer CRL") }
		issuerKey := base64.RawURLEncoding.EncodeToString(list.AuthorityKeyId)
		if len(list.AuthorityKeyId) == 0 || issuerKey == "" || revocations[issuerKey] != nil || list.ThisUpdate.After(collected) || !list.NextUpdate.After(collected) { return errors.New("revocation evidence is not one fresh issuer-bound CRL") }
		revocations[issuerKey] = list
	}
	if len(revocationPages) == 0 { return errors.New("certificate revocation evidence is absent") }
	allowedSubjects := map[string]bool{}
	activeSubjects := map[string]bool{}
	for _, subject := range policy.AllowedClientCertificateSubjects { allowedSubjects[subject] = true }
	allowedProxyNames := map[string]bool{}
	activeProxyNames := map[string]bool{}
	for _, name := range policy.AllowedRequestHeaderProxyCommonNames { allowedProxyNames[name] = true }
	clientRoots := map[string]bool{}
	requestHeaderRoots := map[string]bool{}
	for _, spki := range policy.AllowedClientCARootSPKISHA256 {
		if !isDigestText(spki) || clientRoots[spki] { return errors.New("client CA root SPKI allowlist is invalid") }
		clientRoots[spki] = true
	}
	for _, spki := range policy.AllowedRequestHeaderCARootSPKISHA256 {
		if !isDigestText(spki) || requestHeaderRoots[spki] || clientRoots[spki] { return errors.New("request-header CA root SPKI allowlist is invalid or overlaps the client CA roots") }
		requestHeaderRoots[spki] = true
	}
	for _, raw := range issuedPages {
		var page providerPage[issuedCredential]
		if boundary.DecodeExactJSON(raw, &page) != nil { return errors.New("CA issuance history is not the exact native page schema") }
		for _, item := range page.Items {
			der, err := base64.StdEncoding.Strict().DecodeString(item.CertificateDERBase64)
			certificate, parseErr := x509.ParseCertificate(der)
			if err != nil || parseErr != nil || base64.StdEncoding.EncodeToString(der) != item.CertificateDERBase64 || strings.ToLower(certificate.SerialNumber.Text(16)) != strings.ToLower(item.Serial) || item.SignerName == "" || len(item.CertificateChainDERBase64) == 0 { return errors.New("CA issuance row differs from its exact DER certificate or chain") }
			chain := make([]*x509.Certificate, 0, len(item.CertificateChainDERBase64))
			for _, encoded := range item.CertificateChainDERBase64 {
				chainDER, decodeErr := base64.StdEncoding.Strict().DecodeString(encoded)
				chainCertificate, certificateErr := x509.ParseCertificate(chainDER)
				if decodeErr != nil || certificateErr != nil || base64.StdEncoding.EncodeToString(chainDER) != encoded || !chainCertificate.IsCA { return errors.New("CA issuance chain contains a malformed or non-CA certificate") }
				chain = append(chain, chainCertificate)
			}
			root := chain[len(chain)-1]
			rootSPKI := digestBytes(root.RawSubjectPublicKeyInfo)
			if !clientRoots[rootSPKI] && !requestHeaderRoots[rootSPKI] { return errors.New("CA issuance chain terminates at an unenrolled client or request-header root key") }
			enrolledChain := trust.requestHeaderCACertificates
			if clientRoots[rootSPKI] { enrolledChain = trust.clientCACertificates }
			for _, authority := range chain { if !enrolledChain[digestBytes(authority.Raw)] { return errors.New("CA issuance chain contains a certificate absent from its exact API-server authentication trust bundle") } }
			verificationTime, timeErr := historicalChainVerificationTime(certificate, chain)
			if timeErr != nil { return timeErr }
			roots := x509.NewCertPool()
			roots.AddCert(root)
			intermediates := x509.NewCertPool()
			for _, intermediate := range chain[:len(chain)-1] { intermediates.AddCert(intermediate) }
			verifiedChains, verifyErr := certificate.Verify(x509.VerifyOptions{Roots: roots, Intermediates: intermediates, CurrentTime: verificationTime, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}})
			if verifyErr != nil || len(verifiedChains) != 1 || len(verifiedChains[0]) < 2 { return errors.New("issued client certificate does not form one exact enrolled clientAuth chain") }
			issuer := verifiedChains[0][1]
			if !certificate.NotAfter.After(collected) {
				if item.RevokedAt != "" {
					revokedAt, revokedErr := time.Parse(time.RFC3339, item.RevokedAt)
					if revokedErr != nil || revokedAt.Nanosecond() != 0 || revokedAt.After(collected) { return errors.New("expired issuance has an invalid historical revocation time") }
				}
				continue
			}
			issuerKey := base64.RawURLEncoding.EncodeToString(certificate.AuthorityKeyId)
			list, hasRevocation := revocations[issuerKey]
			if len(certificate.AuthorityKeyId) == 0 || !hasRevocation || list.CheckSignatureFrom(issuer) != nil { return errors.New("issued client certificate lacks a fresh CRL signed by its exact chain issuer") }
			revoked := false
			for _, entry := range list.RevokedCertificateEntries { if entry.SerialNumber.Cmp(certificate.SerialNumber) == 0 { revoked = true } }
			if (item.RevokedAt != "") != revoked { return errors.New("CA issuance revocation assertion differs from the issuer-signed CRL") }
			if revoked {
				revokedAt, revokedErr := time.Parse(time.RFC3339, item.RevokedAt)
				if revokedErr != nil || revokedAt.After(collected) { return errors.New("CA issuance revocation time is invalid") }
				return errors.New("unexpired CRL-listed client certificate remains accepted by the API-server CA configuration, which proves no runtime revocation enforcement")
			}
			if clientRoots[rootSPKI] {
				if !allowedSubjects[certificate.Subject.String()] { return fmt.Errorf("active client certificate subject %s is outside the independently accepted enrollment", certificate.Subject.String()) }
				activeSubjects[certificate.Subject.String()] = true
			} else {
				if certificate.Subject.CommonName == "" || !allowedProxyNames[certificate.Subject.CommonName] { return fmt.Errorf("active request-header proxy common name %s is outside the independently accepted enrollment", certificate.Subject.CommonName) }
				activeProxyNames[certificate.Subject.CommonName] = true
			}
		}
	}
	for subject := range allowedSubjects { if !activeSubjects[subject] { return fmt.Errorf("accepted client certificate subject %s has no active issuer-bound certificate", subject) } }
	for name := range allowedProxyNames { if !activeProxyNames[name] { return fmt.Errorf("accepted request-header proxy common name %s has no active issuer-bound certificate", name) } }
	return nil
}

func historicalChainVerificationTime(leaf *x509.Certificate, chain []*x509.Certificate) (time.Time, error) {
	if leaf == nil || len(chain) == 0 { return time.Time{}, errors.New("historical certificate chain is empty") }
	notBefore := leaf.NotBefore.UTC()
	notAfter := leaf.NotAfter.UTC()
	for _, certificate := range chain {
		if certificate == nil || !certificate.IsCA || !certificate.BasicConstraintsValid || certificate.KeyUsage&x509.KeyUsageCertSign == 0 { return time.Time{}, errors.New("historical certificate chain contains an invalid CA constraint") }
		if certificate.NotBefore.After(notBefore) { notBefore = certificate.NotBefore.UTC() }
		if certificate.NotAfter.Before(notAfter) { notAfter = certificate.NotAfter.UTC() }
	}
	verificationTime := notBefore.Add(time.Second)
	if !verificationTime.Before(notAfter) { return time.Time{}, errors.New("historical certificate chain has no common validity instant") }
	return verificationTime, nil
}

func validateProviderIAMEvidence(policyPages [][]byte, bindingPages [][]byte, policyConfig SemanticPolicy, serviceAccount inventoryObject) error {
	policies := map[string]providerIAMPolicy{}
	resources := map[string]providerIAMResource{}
	for _, raw := range policyPages {
		var page providerIAMPolicyPage
		if boundary.DecodeExactJSON(raw, &page) != nil { return errors.New("provider IAM policy page is not exact") }
		for _, item := range page.Items {
			if item.ID == "" || policies[item.ID].ID != "" || !canonicalNonemptyStrings(item.Actions) || !canonicalNonemptyStrings(item.Resources) || !sort.StringsAreSorted(item.ParentPolicyIDs) {
				return errors.New("provider IAM policy identity, actions, resources or inheritance is ambiguous")
			}
			policies[item.ID] = item
		}
		for _, resource := range page.Resources {
			if resource.ID == "" || resource.ID == "*" || resources[resource.ID].ID != "" || resource.ParentID == resource.ID { return errors.New("provider IAM resource hierarchy is incomplete, duplicated or cyclic") }
			resources[resource.ID] = resource
		}
	}
	allowed := map[string]bool{"system:serviceaccount:"+serviceAccount.identity.Namespace+":"+serviceAccount.identity.Name: true}
	for _, principal := range policyConfig.AllowedProviderPrincipals { allowed[principal] = true }
	protected := map[string]bool{}
	for _, resource := range policyConfig.ProtectedProviderResources {
		if resource == "" || protected[resource] || resources[resource].ID == "" { return errors.New("protected provider resource scope is empty, duplicated or absent from native hierarchy") }
		protected[resource] = true
	}
	ancestors := map[string]map[string]bool{}
	for resource := range resources {
		chain, err := providerResourceAncestors(resource, resources)
		if err != nil { return err }
		ancestors[resource] = chain
	}
	bindings := map[string]bool{}
	for _, raw := range bindingPages {
		var page providerPage[providerIAMBinding]
		if boundary.DecodeExactJSON(raw, &page) != nil { return errors.New("provider IAM binding page is not exact") }
		for _, binding := range page.Items {
			policy, exists := policies[binding.PolicyID]
			if !exists || binding.ID == "" || bindings[binding.ID] || binding.Principal == "" || binding.ResourceScope == "" { return errors.New("provider IAM binding references an absent policy, duplicates identity or has incomplete scope") }
			bindings[binding.ID] = true
			if !canonicalProviderScope(binding.ResourceScope) { return errors.New("provider IAM binding carries an invalid resource scope") }
			if binding.ResourceScope != "*" && resources[binding.ResourceScope].ID == "" { return errors.New("provider IAM binding scope is absent from authoritative resource hierarchy") }
			relevantScope := false
			for protectedScope := range protected { if providerScopesIntersect(binding.ResourceScope, protectedScope, ancestors) { relevantScope = true } }
			dangerous, err := providerPolicyIsDangerous(policy, policies, protected, ancestors, map[string]bool{})
			if err != nil { return err }
			if relevantScope && dangerous && !allowed[binding.Principal] { return fmt.Errorf("unenrolled provider principal %s has inherited boundary-changing IAM authority on %s", binding.Principal, binding.ResourceScope) }
		}
	}
	return nil
}

func validateNodeGroupEvidence(rawPages [][]byte, policy SemanticPolicy, inventory *semanticInventory) error {
	allowedIDs := map[string]bool{}
	allowedPrincipals := map[string]bool{}
	for _, value := range policy.AllowedNodeGroupIDs { allowedIDs[value] = true }
	for _, value := range policy.AllowedProviderPrincipals { allowedPrincipals[value] = true }
	seen := map[string]bool{}
	nodes := map[string]inventoryObject{}
	for _, node := range inventory.byCollection["apiserver-nodes"] {
		spec, _ := object(node.value["spec"])
		providerID := stringValue(spec["providerID"])
		if providerID == "" || nodes[providerID].identity.Name != "" { return errors.New("Kubernetes Node inventory has an empty or duplicate providerID") }
		nodes[providerID] = node
	}
	for _, raw := range rawPages {
		var page providerPage[providerNodeGroup]
		if boundary.DecodeExactJSON(raw, &page) != nil { return errors.New("provider node-group page is not exact") }
		for _, item := range page.Items {
			node, exists := nodes[item.ProviderID]
			if item.ID == "" || seen[item.ID] || !allowedIDs[item.ID] || !allowedPrincipals[item.Principal] || !exists ||
				item.NodeName != node.identity.Name || item.NodeUID != node.identity.UID || item.KubeletIdentity != "system:node:"+node.identity.Name {
				return errors.New("provider node-group membership does not join an exact enrolled Node providerID, UID and kubelet identity")
			}
			seen[item.ID] = true
		}
	}
	if len(seen) != len(allowedIDs) { return errors.New("provider node-group inventory is incomplete") }
	return nil
}

func validateSecretClassifications(rawPages [][]byte, inventory *semanticInventory, credentialSecrets []boundary.ObjectIdentity, policy SemanticPolicy) error {
	rows := map[string]secretClassification{}
	for _, raw := range rawPages {
		var page providerPage[secretClassification]
		if boundary.DecodeExactJSON(raw, &page) != nil { return errors.New("KMS Secret classification page is not exact") }
		for _, row := range page.Items {
			key := row.Namespace+"\x00"+row.Name
			attestationRaw, decodeErr := base64.StdEncoding.Strict().DecodeString(row.ContentAttestationBase64)
			var attestation secretContentAttestation
			if rows[key].Name != "" || row.Unknown || !isDigestText(row.ObjectSHA256) || !isDigestText(row.ContentAttestationSHA256) ||
				decodeErr != nil || base64.StdEncoding.EncodeToString(attestationRaw) != row.ContentAttestationBase64 || digestBytes(attestationRaw) != row.ContentAttestationSHA256 ||
				boundary.DecodeExactJSON(attestationRaw, &attestation) != nil || attestation.Schema != "fs2-serve.nebius.ai/secret-content-classification-attestation/v1" ||
				attestation.Namespace != row.Namespace || attestation.Name != row.Name || attestation.UID != row.UID || attestation.ResourceVersion != row.ResourceVersion ||
				attestation.ObjectSHA256 != row.ObjectSHA256 || attestation.KMSKeyID != row.KMSKeyID || attestation.KeyNames == nil || !sameStringSet(attestation.KeyNames, row.KeyNames) ||
				!sameStringSet(attestation.CredentialClasses, row.CredentialClasses) || !isDigestText(attestation.CiphertextSHA256) || attestation.KMSKeyVersion == "" || attestation.KMSAuditID == "" ||
				attestation.ClassifierRuntimeSHA256 != policy.SecretClassifierRuntimeSHA256 || attestation.ClassifierPolicySHA256 != policy.SecretClassifierPolicySHA256 ||
				row.KMSKeyID == "" || !sort.StringsAreSorted(row.KeyNames) || !sort.StringsAreSorted(row.CredentialClasses) ||
				!sort.StringsAreSorted(attestation.KeyNames) || !sort.StringsAreSorted(attestation.CredentialClasses) || !sort.StringsAreSorted(attestation.RecognizedSecretFormats) {
				return errors.New("Secret classification is ambiguous, unknown or not derived from exact KMS/classifier attestation bytes")
			}
			for _, requiredClass := range credentialClassesFromKeyNames(row.KeyNames) {
				if !containsString(row.CredentialClasses, requiredClass) { return errors.New("Secret classification omits a credential class derived from its exact key taxonomy") }
			}
			rows[key] = row
		}
	}
	allowedKMS := map[string]bool{}
	for _, value := range policy.RequiredKMSKeyIDs { allowedKMS[value] = true }
	credential := map[string]bool{}
	for _, item := range credentialSecrets { credential[item.Namespace+"\x00"+item.Name] = true }
	for _, secret := range inventory.byCollection["apiserver-secrets-metadata"] {
		key := secret.identity.Namespace+"\x00"+secret.identity.Name
		row, exists := rows[key]
		if !exists || row.UID != secret.identity.UID || row.ResourceVersion != secret.identity.ResourceVersion || row.ObjectSHA256 != secret.identity.ObjectSHA256 || !allowedKMS[row.KMSKeyID] || credential[key] && len(row.CredentialClasses) == 0 { return errors.New("Secret metadata lacks an exact trusted KMS/content classification") }
	}
	if len(rows) != len(inventory.byCollection["apiserver-secrets-metadata"]) { return errors.New("Secret classification inventory contains missing or extra objects") }
	return nil
}

func credentialClassesFromKeyNames(keys []string) []string {
	classes := map[string]bool{}
	for _, key := range keys {
		normalized := strings.NewReplacer("_", "", "-", "", ".", "").Replace(strings.ToLower(key))
		switch normalized {
		case "apitoken": classes["api-token"] = true
		case "githubtoken": classes["github-token"] = true
		case "privatekey": classes["private-key"] = true
		case "secretaccesskey", "awssecretaccesskey": classes["aws-secret-access-key"] = true
		}
	}
	result := make([]string, 0, len(classes))
	for value := range classes { result = append(result, value) }
	sort.Strings(result)
	return result
}

func providerPolicyIsDangerous(policy providerIAMPolicy, policies map[string]providerIAMPolicy, protected map[string]bool, ancestors map[string]map[string]bool, visiting map[string]bool) (bool, error) {
	if visiting[policy.ID] { return false, errors.New("provider IAM policy inheritance contains a cycle") }
	visiting[policy.ID] = true
	defer delete(visiting, policy.ID)
	resourceRelevant := false
	for _, resource := range policy.Resources {
		if !canonicalProviderScope(resource) { return false, errors.New("provider IAM policy carries an invalid resource scope") }
		if resource != "*" && ancestors[resource] == nil { return false, errors.New("provider IAM policy resource is absent from authoritative resource hierarchy") }
		for protectedScope := range protected { if providerScopesIntersect(resource, protectedScope, ancestors) { resourceRelevant = true } }
	}
	if resourceRelevant {
		for _, action := range policy.Actions {
			if providerActionIsDangerous(action) { return true, nil }
		}
	}
	for _, parentID := range policy.ParentPolicyIDs {
		parent, exists := policies[parentID]
		if !exists { return false, errors.New("provider IAM policy inherits an absent policy") }
		dangerous, err := providerPolicyIsDangerous(parent, policies, protected, ancestors, visiting)
		if err != nil || dangerous { return dangerous, err }
	}
	return false, nil
}

func canonicalProviderScope(value string) bool { return value == "*" || value != "" && len(value) <= 1024 && !strings.ContainsAny(value, "*\x00\r\n") }

func providerResourceAncestors(resource string, resources map[string]providerIAMResource) (map[string]bool, error) {
	result := map[string]bool{resource: true}
	current := resource
	for steps := 0; steps <= len(resources); steps++ {
		item, exists := resources[current]
		if !exists { return nil, errors.New("provider IAM resource hierarchy references an absent node") }
		if item.ParentID == "" { return result, nil }
		if result[item.ParentID] { return nil, errors.New("provider IAM resource hierarchy contains a cycle") }
		result[item.ParentID] = true
		current = item.ParentID
	}
	return nil, errors.New("provider IAM resource hierarchy exceeds its bounded depth")
}

func providerScopesIntersect(left string, right string, ancestors map[string]map[string]bool) bool {
	return left == "*" || right == "*" || ancestors[left][right] || ancestors[right][left]
}

func providerActionIsDangerous(action string) bool {
	if action == "*" || action == "" { return true }
	parts := strings.FieldsFunc(strings.ToLower(action), func(value rune) bool { return value == '.' || value == ':' || value == '/' })
	if len(parts) == 0 { return true }
	switch parts[len(parts)-1] {
	case "get", "list", "read", "describe", "inspect", "monitor": return false
	default: return true
	}
}

func canonicalNonemptyStrings(values []string) bool {
	if len(values) == 0 || !sort.StringsAreSorted(values) { return false }
	for index, value := range values { if value == "" || index > 0 && values[index-1] == value { return false } }
	return true
}

func validateAdmissionAndCSRAuthority(inventory *semanticInventory, policy SemanticPolicy) error {
	approvalCount := 0
	for _, reference := range policy.ProtectedRoots {
		if reference.SemanticCollection == "apiserver-approval-objects" { approvalCount++ }
	}
	if approvalCount != 1 || len(inventory.byCollection["apiserver-approval-objects"]) != 1 {
		return errors.New("semantic policy does not bind one exact live public-edge authority approval object")
	}
	approvalCRD := false
	for _, crd := range inventory.byCollection["apiserver-crds"] {
		spec, _ := object(crd.value["spec"])
		names, _ := object(spec["names"])
		if stringValue(spec["group"]) == "security.fs2.nebius.ai" && stringValue(names["plural"]) == "publicedgenodeauthorityapprovals" {
			if stringValue(spec["scope"]) != "Cluster" || !hasServedStorageVersion(spec) { return errors.New("authority approval CRD is not a served cluster-scoped storage root") }
			approvalCRD = true
		}
	}
	if !approvalCRD { return errors.New("native CRD inventory omits the authority approval definition") }
	protected := map[string]bool{}
	for _, reference := range policy.ProtectedRoots { protected[referenceKey(reference)] = true }
	for _, collection := range []string{"apiserver-admission-mutating-webhooks", "apiserver-admission-validating-webhooks"} {
		for _, webhookConfig := range inventory.byCollection[collection] {
			intersectsAuthority := false
			for _, rawWebhook := range array(webhookConfig.value["webhooks"]) {
				webhook, _ := object(rawWebhook)
				webhookIntersectsAuthority := false
				for _, rawRule := range array(webhook["rules"]) {
					rule, _ := object(rawRule)
					for _, resource := range array(rule["resources"]) {
						value := stringValue(resource)
						if value == "*" || strings.Contains(value, "publicedgenodeauthorityapprovals") || strings.Contains(value, "validatingadmission") || strings.Contains(value, "mutatingadmission") {
							webhookIntersectsAuthority = true
						}
					}
				}
				if !webhookIntersectsAuthority {
					continue
				}
				intersectsAuthority = true
				if stringValue(webhook["failurePolicy"]) != "Fail" || (stringValue(webhook["sideEffects"]) != "None" && stringValue(webhook["sideEffects"]) != "NoneOnDryRun") || stringValue(webhook["matchPolicy"]) != "Equivalent" { return errors.New("authority-intersecting admission webhook can bypass fail-closed semantics") }
				client, _ := object(webhook["clientConfig"])
				service, serviceOK := object(client["service"])
				if !serviceOK || client["url"] != nil { return errors.New("authority-intersecting webhook uses an unbound external endpoint") }
				if _, err := inventory.require(ObjectReference{"apiserver-services", stringValue(service["namespace"]), stringValue(service["name"])}); err != nil { return errors.New("authority-intersecting webhook Service is absent from native transport inventory") }
			}
			if intersectsAuthority && !protected[referenceKey(ObjectReference{collection, webhookConfig.identity.Namespace, webhookConfig.identity.Name})] { return errors.New("authority-intersecting admission webhook is not an exact protected root") }
		}
	}
	if err := validateAdmissionParameterBindings(inventory, policy); err != nil { return err }
	return validateCurrentCSRs(inventory, policy)
}

func hasServedStorageVersion(spec map[string]any) bool {
	count := 0
	for _, raw := range array(spec["versions"]) { version, _ := object(raw); served, _ := version["served"].(bool); storage, _ := version["storage"].(bool); if served && storage { count++ } }
	return count == 1
}

func validateAdmissionParameterBindings(inventory *semanticInventory, policy SemanticPolicy) error {
	accepted := map[string]bool{}
	for _, ref := range policy.AdmissionParameterRoots { accepted[ref.Namespace+"\x00"+ref.Name] = true }
	for _, collection := range []string{"apiserver-admission-validating-policy-bindings", "apiserver-admission-mutating-policy-bindings"} {
		for _, binding := range inventory.byCollection[collection] {
			spec, _ := object(binding.value["spec"])
			parameter, exists := object(spec["paramRef"])
			if !exists { continue }
			namespace := stringValue(parameter["namespace"])
			if !accepted[namespace+"\x00"+stringValue(parameter["name"])] { return errors.New("admission policy binding references a parameter outside exact protected roots") }
		}
	}
	return nil
}

func validateCurrentCSRs(inventory *semanticInventory, policy SemanticPolicy) error {
	allowedSubjects := map[string]bool{}
	for _, value := range policy.AllowedClientCertificateSubjects { allowedSubjects[value] = true }
	allowedSigners := map[string]bool{}
	for _, value := range policy.AllowedCSRSignerNames { allowedSigners[value] = true }
	for _, objectValue := range inventory.byCollection["apiserver-csrs"] {
		spec, _ := object(objectValue.value["spec"])
		requestText := stringValue(spec["request"])
		requestPEM, err := base64.StdEncoding.Strict().DecodeString(requestText)
		if err != nil || base64.StdEncoding.EncodeToString(requestPEM) != requestText { return errors.New("CSR request is not canonical base64") }
		block, rest := pem.Decode(requestPEM)
		if block == nil || block.Type != "CERTIFICATE REQUEST" || len(bytes.TrimSpace(rest)) != 0 { return errors.New("CSR request is not one canonical PEM PKCS10 object") }
		csr, err := x509.ParseCertificateRequest(block.Bytes)
		if err != nil || csr.CheckSignature() != nil || !allowedSubjects[csr.Subject.String()] || !allowedSigners[stringValue(spec["signerName"])] { return errors.New("CSR requested identity, signer or self-signature is outside independent enrollment") }
		status, _ := object(objectValue.value["status"])
		certificateText := stringValue(status["certificate"])
		if certificateText == "" { continue }
		certificatePEM, err := base64.StdEncoding.Strict().DecodeString(certificateText)
		if err != nil || base64.StdEncoding.EncodeToString(certificatePEM) != certificateText { return errors.New("issued CSR certificate is not canonical base64") }
		certificateBlock, trailing := pem.Decode(certificatePEM)
		if certificateBlock == nil || certificateBlock.Type != "CERTIFICATE" || len(bytes.TrimSpace(trailing)) != 0 { return errors.New("issued CSR status is not one canonical PEM certificate") }
		certificate, err := x509.ParseCertificate(certificateBlock.Bytes)
		if err != nil || certificate.Subject.String() != csr.Subject.String() || !bytes.Equal(certificate.RawSubjectPublicKeyInfo, csr.RawSubjectPublicKeyInfo) { return errors.New("issued CSR certificate differs from the signed request identity or key") }
	}
	return nil
}

func containsString(values []string, expected string) bool { for _, value := range values { if value == expected { return true } }; return false }
func isDigestText(value string) bool { return len(value) == 64 && strings.Trim(value, "0123456789abcdef") == "" && value != strings.Repeat("0", 64) }
