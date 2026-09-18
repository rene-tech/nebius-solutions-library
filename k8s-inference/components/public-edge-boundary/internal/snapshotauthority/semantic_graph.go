package snapshotauthority

import (
	"bytes"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/boundary"
	"github.com/rene-tech/nebius-solutions-library/k8s-inference/components/public-edge-boundary/internal/collector"
)

var workloadCollections = []string{
	"apiserver-pods",
	"apiserver-replicationcontrollers",
	"apiserver-deployments",
	"apiserver-daemonsets",
	"apiserver-statefulsets",
	"apiserver-replicasets",
	"apiserver-jobs",
	"apiserver-cronjobs",
}

type namespacedSecretReference struct {
	namespace string
	name      string
}

type redisTLSHandoff struct {
	Schema string `json:"schema"`
	IssuerGroup string `json:"issuer_group"`
	IssuerKind string `json:"issuer_kind"`
	IssuerName string `json:"issuer_name"`
	CARootSPKISHA256 string `json:"ca_root_spki_sha256"`
	ServerSecretName string `json:"server_secret_name"`
	ServerDNSNames []string `json:"server_dns_names"`
	ServerExtendedKeyUsage []string `json:"server_extended_key_usage"`
	ClientSecretName string `json:"client_secret_name"`
	ClientSPIFFEURI string `json:"client_spiffe_uri"`
	ClientExtendedKeyUsage []string `json:"client_extended_key_usage"`
	MaximumLifetimeSeconds int64 `json:"maximum_lifetime_seconds"`
	MinimumRemainingSeconds int64 `json:"minimum_remaining_seconds"`
	Generation int64 `json:"generation"`
	IssuedAt string `json:"issued_at"`
	ExpiresAt string `json:"expires_at"`
	PredecessorSHA256 string `json:"predecessor_sha256"`
}

func secretReferenceKey(reference namespacedSecretReference) string {
	return reference.namespace + "\x00" + reference.name
}

func deriveCredentialWorkloads(
	inventory *semanticInventory,
	serviceAccount inventoryObject,
	allowedImages []string,
) ([]boundary.ObjectIdentity, []boundary.ObjectIdentity, []namespacedSecretReference, error) {
	if !sort.StringsAreSorted(allowedImages) || len(allowedImages) == 0 {
		return nil, nil, nil, errors.New("controller image allowlist is empty or non-canonical")
	}
	allowed := map[string]struct{}{}
	for _, image := range allowedImages {
		if image == "" || !strings.Contains(image, "@sha256:") {
			return nil, nil, nil, errors.New("controller image allowlist contains an unpinned image")
		}
		if _, duplicate := allowed[image]; duplicate {
			return nil, nil, nil, errors.New("controller image allowlist repeats an image")
		}
		allowed[image] = struct{}{}
	}
	secretReferences := map[string]namespacedSecretReference{}
	collectServiceAccountSecrets(serviceAccount.value, serviceAccount.identity.Namespace, secretReferences)
	serviceAccounts := map[string]inventoryObject{}
	for _, candidate := range inventory.byCollection["apiserver-serviceaccounts"] {
		serviceAccounts[candidate.identity.Namespace+"\x00"+candidate.identity.Name] = candidate
	}
	workloads := []boundary.ObjectIdentity{}
	reachableServiceAccounts := map[string]boundary.ObjectIdentity{serviceAccount.identity.Namespace+"\x00"+serviceAccount.identity.Name: serviceAccount.identity}
	candidates := []inventoryObject{}
	for _, collection := range workloadCollections { candidates = append(candidates, inventory.byCollection[collection]...) }
	reachable := map[string]bool{}
	changed := true
	for changed {
		changed = false
		for _, candidate := range candidates {
			key := candidate.collection+"\x00"+candidate.identity.Namespace+"\x00"+candidate.identity.Name
			if reachable[key] { continue }
			podSpec, err := workloadPodSpec(candidate)
			if err != nil {
				return nil, nil, nil, err
			}
			serviceAccountName := stringValue(podSpec["serviceAccountName"])
			if serviceAccountName == "" {
				serviceAccountName = "default"
			}
			candidateSecrets := map[string]namespacedSecretReference{}
			if err := collectPodSecretReferences(inventory, podSpec, candidate.identity.Namespace, candidateSecrets); err != nil { return nil, nil, nil, err }
			candidateServiceAccount, exists := serviceAccounts[candidate.identity.Namespace+"\x00"+serviceAccountName]
			if !exists {
				return nil, nil, nil, fmt.Errorf("workload %s/%s references a ServiceAccount outside the complete inventory", candidate.identity.Namespace, candidate.identity.Name)
			}
			collectServiceAccountSecrets(candidateServiceAccount.value, candidate.identity.Namespace, candidateSecrets)
			reaches := candidateServiceAccount.identity.UID == serviceAccount.identity.UID && candidateServiceAccount.identity.Namespace == serviceAccount.identity.Namespace
			for referenceKey := range candidateSecrets { if _, exists := secretReferences[referenceKey]; exists { reaches = true } }
			if !reaches { continue }
			if err := validateWorkloadImages(podSpec, allowed); err != nil {
				return nil, nil, nil, fmt.Errorf("credential workload %s/%s: %w", candidate.identity.Namespace, candidate.identity.Name, err)
			}
			reachableServiceAccounts[candidateServiceAccount.identity.Namespace+"\x00"+candidateServiceAccount.identity.Name] = candidateServiceAccount.identity
			for referenceKey, reference := range candidateSecrets { if _, exists := secretReferences[referenceKey]; !exists { secretReferences[referenceKey] = reference; changed = true } }
			reachable[key] = true
			changed = true
			workloads = append(workloads, candidate.identity)
		}
	}
	if len(workloads) == 0 {
		return nil, nil, nil, errors.New("native inventory has no workload bound to the enrolled controller ServiceAccount")
	}
	secretValues := make([]namespacedSecretReference, 0, len(secretReferences))
	for _, reference := range secretReferences {
		if reference.namespace == "" || reference.name == "" {
			return nil, nil, nil, errors.New("controller credential graph contains an empty Secret reference")
		}
		secretValues = append(secretValues, reference)
	}
	sort.Slice(secretValues, func(left int, right int) bool { return secretReferenceKey(secretValues[left]) < secretReferenceKey(secretValues[right]) })
	serviceAccountValues := make([]boundary.ObjectIdentity, 0, len(reachableServiceAccounts))
	for _, identity := range reachableServiceAccounts { serviceAccountValues = append(serviceAccountValues, identity) }
	return sortIdentities(workloads), sortIdentities(serviceAccountValues), secretValues, nil
}

func workloadPodSpec(workload inventoryObject) (map[string]any, error) {
	spec, ok := object(workload.value["spec"])
	if !ok {
		return nil, errors.New("workload spec is absent")
	}
	if workload.kind == "Pod" {
		return spec, nil
	}
	if workload.kind == "CronJob" {
		jobTemplate, ok := object(spec["jobTemplate"])
		if !ok {
			return nil, errors.New("CronJob jobTemplate is absent")
		}
		spec, ok = object(jobTemplate["spec"])
		if !ok {
			return nil, errors.New("CronJob jobTemplate spec is absent")
		}
	}
	template, ok := object(spec["template"])
	if !ok {
		return nil, errors.New("workload Pod template is absent")
	}
	podSpec, ok := object(template["spec"])
	if !ok {
		return nil, errors.New("workload Pod template spec is absent")
	}
	return podSpec, nil
}

func collectServiceAccountSecrets(value map[string]any, namespace string, result map[string]namespacedSecretReference) {
	for _, field := range []string{"secrets", "imagePullSecrets"} {
		for _, item := range array(value[field]) {
			if reference, ok := object(item); ok {
				if name := stringValue(reference["name"]); name != "" {
					value := namespacedSecretReference{namespace: namespace, name: name}
					result[secretReferenceKey(value)] = value
				}
			}
		}
	}
}

func validateWorkloadImages(podSpec map[string]any, allowed map[string]struct{}) error {
	count := 0
	for _, field := range []string{"initContainers", "containers", "ephemeralContainers"} {
		for _, item := range array(podSpec[field]) {
			container, ok := object(item)
			if !ok {
				return errors.New("container entry is malformed")
			}
			image := stringValue(container["image"])
			if _, accepted := allowed[image]; !accepted {
				return fmt.Errorf("execution-capable container image %q is outside the accepted digest allowlist", image)
			}
			count++
		}
	}
	if count == 0 {
		return errors.New("workload has no execution-capable container")
	}
	return nil
}

func collectPodSecretReferences(inventory *semanticInventory, podSpec map[string]any, namespace string, result map[string]namespacedSecretReference) error {
	add := func(secretNamespace string, name string) error {
		if name == "" { return nil }
		if secretNamespace == "" { secretNamespace = namespace }
		if secretNamespace == "" { return errors.New("Secret reference has no namespace") }
		reference := namespacedSecretReference{namespace: secretNamespace, name: name}
		result[secretReferenceKey(reference)] = reference
		return nil
	}
	for _, item := range array(podSpec["imagePullSecrets"]) {
		if reference, ok := object(item); ok {
			if err := add(namespace, stringValue(reference["name"])); err != nil { return err }
		}
	}
	for _, field := range []string{"initContainers", "containers", "ephemeralContainers"} {
		for _, item := range array(podSpec[field]) {
			container, ok := object(item)
			if !ok {
				return errors.New("container entry is malformed")
			}
			for _, envFromValue := range array(container["envFrom"]) {
				envFrom, _ := object(envFromValue)
				secretRef, _ := object(envFrom["secretRef"])
				if err := add(namespace, stringValue(secretRef["name"])); err != nil { return err }
			}
			for _, envValue := range array(container["env"]) {
				env, _ := object(envValue)
				valueFrom, _ := object(env["valueFrom"])
				secretKeyRef, _ := object(valueFrom["secretKeyRef"])
				if err := add(namespace, stringValue(secretKeyRef["name"])); err != nil { return err }
			}
		}
	}
	for _, item := range array(podSpec["volumes"]) {
		volume, ok := object(item)
		if !ok {
			return errors.New("Pod volume is malformed")
		}
		secret, _ := object(volume["secret"])
		if err := add(namespace, stringValue(secret["secretName"])); err != nil { return err }
		projected, _ := object(volume["projected"])
		for _, sourceValue := range array(projected["sources"]) {
			source, _ := object(sourceValue)
			projectedSecret, _ := object(source["secret"])
			if err := add(namespace, stringValue(projectedSecret["name"])); err != nil { return err }
		}
		csi, _ := object(volume["csi"])
		for _, field := range []string{"nodePublishSecretRef", "controllerPublishSecretRef", "nodeStageSecretRef"} {
			reference, _ := object(csi[field])
			if err := add(stringValue(reference["namespace"]), stringValue(reference["name"])); err != nil { return err }
		}
		persistentVolumeClaim, _ := object(volume["persistentVolumeClaim"])
		if claimName := stringValue(persistentVolumeClaim["claimName"]); claimName != "" {
			if err := collectPersistentVolumeClaimSecrets(inventory, namespace, claimName, add); err != nil { return err }
		}
	}
	return nil
}

func collectPersistentVolumeClaimSecrets(inventory *semanticInventory, namespace string, claimName string, add func(string, string) error) error {
	claim, err := inventory.require(ObjectReference{"apiserver-persistentvolumeclaims", namespace, claimName})
	if err != nil { return fmt.Errorf("PVC %s/%s is outside the complete inventory", namespace, claimName) }
	spec, _ := object(claim.value["spec"])
	volumeName := stringValue(spec["volumeName"])
	if volumeName != "" {
		volume, err := inventory.require(ObjectReference{"apiserver-persistentvolumes", "", volumeName})
		if err != nil { return fmt.Errorf("PVC %s/%s binds a PV outside the complete inventory", namespace, claimName) }
		volumeSpec, _ := object(volume.value["spec"])
		csi, _ := object(volumeSpec["csi"])
		for _, field := range []string{"nodePublishSecretRef", "controllerPublishSecretRef", "nodeStageSecretRef", "controllerExpandSecretRef", "nodeExpandSecretRef"} {
			reference, _ := object(csi[field])
			if err := add(stringValue(reference["namespace"]), stringValue(reference["name"])); err != nil { return err }
		}
	}
	storageClassName := stringValue(spec["storageClassName"])
	if storageClassName == "" { return nil }
	storageClass, err := inventory.require(ObjectReference{"apiserver-storageclasses", "", storageClassName})
	if err != nil { return fmt.Errorf("PVC %s/%s uses a StorageClass outside the complete inventory", namespace, claimName) }
	parameters, _ := object(storageClass.value["parameters"])
	for _, prefix := range []string{"csi.storage.k8s.io/provisioner", "csi.storage.k8s.io/controller-publish", "csi.storage.k8s.io/node-stage", "csi.storage.k8s.io/node-publish", "csi.storage.k8s.io/controller-expand", "csi.storage.k8s.io/node-expand"} {
		name := stringValue(parameters[prefix+"-secret-name"])
		secretNamespace := stringValue(parameters[prefix+"-secret-namespace"])
		if strings.Contains(name, "${") || strings.Contains(secretNamespace, "${") {
			return errors.New("StorageClass uses an unresolved credential Secret template")
		}
		if err := add(secretNamespace, name); err != nil { return err }
	}
	return nil
}

func validateRBACClosure(inventory *semanticInventory, policy SemanticPolicy, credentialServiceAccounts []boundary.ObjectIdentity) error {
	allowed := map[string]struct{}{}
	for _, subject := range policy.AllowedDangerousSubjects {
		key, err := rbacSubjectKey(subject.Kind, subject.Namespace, subject.Name)
		if err != nil {
			return err
		}
		if _, duplicate := allowed[key]; duplicate {
			return errors.New("dangerous RBAC subject allowlist contains a duplicate")
		}
		allowed[key] = struct{}{}
	}
	controllerKey, err := rbacSubjectKey("ServiceAccount", policy.ControllerServiceAccount.Namespace, policy.ControllerServiceAccount.Name)
	if err != nil {
		return err
	}
	allowed[controllerKey] = struct{}{}
	for _, identity := range credentialServiceAccounts {
		key, err := rbacSubjectKey("ServiceAccount", identity.Namespace, identity.Name)
		if err != nil { return err }
		allowed[key] = struct{}{}
	}
	roles := map[string]inventoryObject{}
	for _, collection := range []string{"apiserver-roles", "apiserver-clusterroles"} {
		for _, role := range inventory.byCollection[collection] {
			roles[role.identity.Resource+"\x00"+role.identity.Namespace+"\x00"+role.identity.Name] = role
		}
	}
	for _, collection := range []string{"apiserver-rolebindings", "apiserver-clusterrolebindings"} {
		for _, binding := range inventory.byCollection[collection] {
			roleRef, ok := object(binding.value["roleRef"])
			if !ok {
				return errors.New("RBAC binding roleRef is malformed")
			}
			roleKind := stringValue(roleRef["kind"])
			roleName := stringValue(roleRef["name"])
			roleNamespace := binding.identity.Namespace
			roleResource := "roles"
			if roleKind == "ClusterRole" {
				roleResource = "clusterroles"
				roleNamespace = ""
			} else if roleKind != "Role" || binding.identity.Resource == "clusterrolebindings" {
				return errors.New("RBAC binding has an invalid roleRef kind")
			}
			role, exists := roles[roleResource+"\x00"+roleNamespace+"\x00"+roleName]
			if !exists {
				return errors.New("RBAC binding references a role outside the complete inventory")
			}
			if !roleHasDangerousAuthority(role.value, binding.identity.Namespace, policy.ControllerServiceAccount.Namespace) {
				continue
			}
			for _, rawSubject := range array(binding.value["subjects"]) {
				subject, ok := object(rawSubject)
				if !ok {
					return errors.New("RBAC binding subject is malformed")
				}
				key, err := rbacSubjectKey(stringValue(subject["kind"]), stringValue(subject["namespace"]), stringValue(subject["name"]))
				if err != nil {
					return err
				}
				if _, enrolled := allowed[key]; !enrolled {
					return fmt.Errorf("non-enrolled subject %s reaches controller credentials through %s/%s", key, binding.identity.Namespace, binding.identity.Name)
				}
			}
		}
	}
	return nil
}

func roleHasDangerousAuthority(value map[string]any, bindingNamespace string, credentialNamespace string) bool {
	for _, rawRule := range array(value["rules"]) {
		rule, ok := object(rawRule)
		if !ok {
			return true
		}
		verbs := stringSet(array(rule["verbs"]))
		resources := stringSet(array(rule["resources"]))
		apiGroups := stringSet(array(rule["apiGroups"]))
		if verbs["*"] || resources["*"] || apiGroups["*"] || verbs["bind"] || verbs["escalate"] || verbs["impersonate"] || verbs["approve"] || verbs["sign"] {
			return true
		}
		for resource := range resources {
			if dangerousResource(resource) && intersects(verbs, map[string]bool{
				"get": true, "list": true, "watch": true, "create": true, "update": true,
				"patch": true, "delete": true, "deletecollection": true, "connect": true, "approve": true, "sign": true,
			}) {
				return true
			}
			if (bindingNamespace == "" || bindingNamespace == credentialNamespace) && workloadMutationResource(resource) &&
				intersects(verbs, map[string]bool{"create": true, "update": true, "patch": true, "delete": true, "deletecollection": true}) {
				return true
			}
		}
	}
	return false
}

func dangerousResource(resource string) bool {
	for _, prefix := range []string{
		"secrets", "serviceaccounts/token", "roles", "rolebindings", "clusterroles", "clusterrolebindings",
		"pods/exec", "pods/attach", "pods/portforward", "pods/ephemeralcontainers", "nodes/proxy",
		"replicationcontrollers/scale", "deployments/scale", "statefulsets/scale", "replicasets/scale",
		"certificatesigningrequests", "certificatesigningrequests/approval", "certificatesigningrequests/status", "signers",
		"validatingwebhookconfigurations", "mutatingwebhookconfigurations",
		"validatingadmissionpolicies", "validatingadmissionpolicybindings", "mutatingadmissionpolicies",
		"mutatingadmissionpolicybindings", "publicedgenodeauthorityapprovals",
	} {
		if resource == prefix {
			return true
		}
	}
	return false
}

func workloadMutationResource(resource string) bool {
	switch resource {
	case "pods", "replicationcontrollers", "deployments", "daemonsets", "statefulsets", "replicasets", "jobs", "cronjobs", "serviceaccounts":
		return true
	default:
		return false
	}
}

func rbacSubjectKey(kind string, namespace string, name string) (string, error) {
	if name == "" || (kind != "ServiceAccount" && kind != "User" && kind != "Group") || kind == "ServiceAccount" && namespace == "" || kind != "ServiceAccount" && namespace != "" {
		return "", errors.New("RBAC subject identity is invalid")
	}
	return kind + "\x00" + namespace + "\x00" + name, nil
}

func stringSet(values []any) map[string]bool {
	result := map[string]bool{}
	for _, value := range values {
		if item, ok := value.(string); ok && item != "" {
			result[item] = true
		}
	}
	return result
}

func intersects(left map[string]bool, right map[string]bool) bool {
	for value := range left {
		if right[value] {
			return true
		}
	}
	return false
}

func validateEdgeGraph(
	inventory *semanticInventory,
	verified *collector.VerifiedEvidenceBundle,
	policy EdgeGraphPolicy,
) ([]boundary.ObjectIdentity, error) {
	gatewayClass, err := inventory.require(ObjectReference{"apiserver-gatewayclasses", "", policy.GatewayClassName})
	if err != nil {
		return nil, err
	}
	envoyProxy, err := inventory.require(ObjectReference{"apiserver-envoyproxies", policy.Namespace, policy.EnvoyProxyName})
	if err != nil || validateGatewayClassAttachment(gatewayClass.value, envoyProxy) != nil {
		return nil, errors.New("GatewayClass does not bind the exact accepted EnvoyProxy parameters object")
	}
	gateway, err := inventory.require(ObjectReference{"apiserver-gateways", policy.Namespace, policy.GatewayName})
	if err != nil || stringValue(nestedObject(gateway.value, "spec")["gatewayClassName"]) != policy.GatewayClassName ||
		!gatewayHasListeners(gateway.value, policy) {
		return nil, errors.New("public Gateway does not expose the exact accepted HTTP and HTTPS listeners")
	}
	httpPolicy, err := inventory.require(ObjectReference{"apiserver-client-traffic-policies", policy.Namespace, policy.HTTPClientTrafficPolicyName})
	if err != nil || validateClientTrafficPolicy(httpPolicy.value, policy.GatewayName, policy.HTTPListenerName, policy) != nil {
		return nil, errors.New("public HTTP listener ClientTrafficPolicy is absent or unsafe")
	}
	httpsPolicy, err := inventory.require(ObjectReference{"apiserver-client-traffic-policies", policy.Namespace, policy.HTTPSClientTrafficPolicyName})
	if err != nil || validateClientTrafficPolicy(httpsPolicy.value, policy.GatewayName, policy.HTTPSListenerName, policy) != nil {
		return nil, errors.New("public HTTPS listener ClientTrafficPolicy is absent or unsafe")
	}
	backendPolicy, err := inventory.require(ObjectReference{"apiserver-backend-traffic-policies", policy.Namespace, policy.BackendTrafficPolicyName})
	if err != nil {
		return nil, err
	}
	perSourceLimit, err := validateBackendTrafficPolicy(
		backendPolicy.value,
		policy.GatewayName,
		policy.HTTPListenerName,
		policy.HTTPSListenerName,
		policy.ProviderLoadBalancerID,
		policy,
	)
	if err != nil || perSourceLimit < policy.MinimumPerSourceConnectionLimit || perSourceLimit > policy.MaximumPerSourceConnectionLimit {
		return nil, errors.New("edge BackendTrafficPolicy does not enforce the accepted per-source rate and connection boundary")
	}
	if err := validateProviderLoadBalancerGraph(inventory, verified, policy, perSourceLimit); err != nil {
		return nil, err
	}
	refs := []ObjectReference{
		{"apiserver-gatewayclasses", "", policy.GatewayClassName},
		{"apiserver-gateways", policy.Namespace, policy.GatewayName},
		{"apiserver-client-traffic-policies", policy.Namespace, policy.HTTPClientTrafficPolicyName},
		{"apiserver-client-traffic-policies", policy.Namespace, policy.HTTPSClientTrafficPolicyName},
		{"apiserver-backend-traffic-policies", policy.Namespace, policy.BackendTrafficPolicyName},
		{"apiserver-envoyproxies", policy.Namespace, policy.EnvoyProxyName},
		policy.ControlPlaneWorkload,
		{"apiserver-services", policy.EnvoyGatewayControllerNamespace, policy.EnvoyGatewayControllerServiceName},
		{"apiserver-poddisruptionbudgets", policy.EnvoyGatewayControllerNamespace, policy.ControlPlanePDBName},
		{"apiserver-poddisruptionbudgets", policy.RateLimitPDBNamespace, policy.RateLimitPDBName},
		{"apiserver-poddisruptionbudgets", policy.RedisPDBNamespace, policy.RedisPDBName},
		{"apiserver-deployments", policy.RateLimitWorkloadNamespace, policy.RateLimitWorkloadName},
		{"apiserver-statefulsets", policy.RedisWorkloadNamespace, policy.RedisWorkloadName},
		{"apiserver-services", policy.RateLimitServiceNamespace, policy.RateLimitServiceName},
		{"apiserver-services", policy.RedisServiceNamespace, policy.RedisServiceName},
		{"apiserver-services", policy.RedisServiceNamespace, policy.RedisSentinelServiceName},
		{"apiserver-configmaps", policy.RedisServiceNamespace, policy.RedisBootstrapConfigMapName},
		policy.RedisTLSServerCertificate,
		policy.RedisTLSClientCertificate,
	}
	refs = append(refs, policy.PublicRoutes...)
	refs = append(refs, policy.PublicBackendServices...)
	refs = append(refs, policy.NetworkPolicies...)
	objects := make([]inventoryObject, 0, len(refs))
	for _, reference := range refs {
		item, err := inventory.require(reference)
		if err != nil {
			return nil, err
		}
		objects = append(objects, item)
	}
	// Fixed entries 8..10 are PDBs and 6,11,12 are their exact workloads.
	if err := validatePDBSelectsWorkload(objects[8].value, objects[6].value); err != nil {
		return nil, err
	}
	if err := validatePDBSelectsWorkload(objects[9].value, objects[11].value); err != nil {
		return nil, err
	}
	if err := validatePDBSelectsWorkload(objects[10].value, objects[12].value); err != nil {
		return nil, err
	}
	if !workloadCarriesLabels(objects[11].value, policy.RateLimitPodSelector) || !pdbSelectorEquals(objects[9].value, policy.RateLimitPodSelector) ||
		!pdbSelectorEquals(objects[10].value, policy.RedisPodSelector) || !pdbMinimumAvailableEquals(objects[8].value, 1) ||
		!pdbMinimumAvailableEquals(objects[9].value, 1) || !pdbMinimumAvailableEquals(objects[10].value, 2) {
		return nil, errors.New("edge controller, rate-limit or Redis PodDisruptionBudget differs from its exact selector or availability floor")
	}
	if replicas(objects[11].value) < 2 || replicas(objects[12].value) < 3 {
		return nil, errors.New("rate-limit or Redis workload lacks its accepted availability replica floor")
	}
	if err := validateInternalService(objects[7].value, policy.EnvoyGatewayControllerPodSelector, false, false, []servicePortContract{
		{name: "grpc", protocol: "TCP", port: 18000, numericTargetPort: 18000},
		{name: "ratelimit", protocol: "TCP", port: policy.RateLimitXDSPort, numericTargetPort: policy.RateLimitXDSPort},
		{name: "wasm", protocol: "TCP", port: 18002, numericTargetPort: 18002},
		{name: "metrics", protocol: "TCP", port: 19001, numericTargetPort: 19001},
	}); err != nil {
		return nil, fmt.Errorf("Envoy Gateway xDS Service: %w", err)
	}
	if err := validateInternalService(objects[13].value, policy.RateLimitPodSelector, false, false, []servicePortContract{
		{name: "grpc", protocol: "TCP", port: policy.RateLimitServicePort, numericTargetPort: policy.RateLimitServicePort},
		{name: "metrics", protocol: "TCP", port: 19001, numericTargetPort: 19001},
	}); err != nil {
		return nil, fmt.Errorf("rate-limit Service: %w", err)
	}
	if err := validateInternalService(objects[14].value, policy.RedisPodSelector, true, true, []servicePortContract{
		{name: "redis", protocol: "TCP", port: policy.RedisServicePort, namedTargetPort: "redis"},
		{name: "sentinel", protocol: "TCP", port: policy.RedisSentinelPort, namedTargetPort: "sentinel"},
	}); err != nil {
		return nil, fmt.Errorf("Redis headless Service: %w", err)
	}
	if err := validateInternalService(objects[15].value, policy.RedisPodSelector, false, false, []servicePortContract{
		{name: "sentinel", protocol: "TCP", port: policy.RedisSentinelPort, namedTargetPort: "sentinel"},
	}); err != nil {
		return nil, fmt.Errorf("Redis Sentinel Service: %w", err)
	}
	if !workloadCarriesLabels(objects[6].value, policy.EnvoyGatewayControllerPodSelector) ||
		!objectSelectorEquals(objects[7].value, policy.EnvoyGatewayControllerPodSelector) ||
		!pdbSelectorEquals(objects[8].value, policy.EnvoyGatewayControllerPodSelector) {
		return nil, errors.New("Envoy Gateway Deployment, xDS Service and PodDisruptionBudget do not share the exact accepted controller identity")
	}
	if !workloadCarriesLabels(objects[12].value, policy.RedisPodSelector) || !objectSelectorEquals(objects[14].value, policy.RedisPodSelector) ||
		!objectSelectorEquals(objects[15].value, policy.RedisPodSelector) {
		return nil, errors.New("Redis StatefulSet and both Redis/Sentinel Services do not share the exact accepted full pod identity")
	}
	if err := validateRateLimitRedisBinding(objects[11], objects[12], objects[16], objects[17], objects[18], verified.Bundle.CollectedAt, policy); err != nil {
		return nil, err
	}
	baseCount := 19
	routeObjects := objects[baseCount : baseCount+len(policy.PublicRoutes)]
	backendObjects := objects[baseCount+len(policy.PublicRoutes) : baseCount+len(policy.PublicRoutes)+len(policy.PublicBackendServices)]
	networkObjects := objects[baseCount+len(policy.PublicRoutes)+len(policy.PublicBackendServices):]
	if err := validatePublicRoutes(inventory, routeObjects, backendObjects, policy); err != nil {
		return nil, err
	}
	if err := validateNoUnaccountedBackendRoutes(inventory, routeObjects, backendObjects); err != nil { return nil, err }
	if err := validateEdgeNetworkPolicies(inventory, networkObjects, policy.NetworkPolicySpecs, policy, objects[6], objects[11], objects[12]); err != nil {
		return nil, err
	}
	identities := make([]boundary.ObjectIdentity, 0, len(objects))
	for _, item := range objects {
		identities = append(identities, item.identity)
	}
	return sortIdentities(identities), nil
}

func gatewayHasListeners(value map[string]any, policy EdgeGraphPolicy) bool {
	spec, _ := object(value["spec"])
	listeners := array(spec["listeners"])
	if len(listeners) != 2 || len(policy.PublicRouteHostnames) != 0 {
		return false
	}
	actual := map[string]bool{}
	for _, raw := range array(spec["listeners"]) {
		listener, _ := object(raw)
		name := stringValue(listener["name"])
		protocol := stringValue(listener["protocol"])
		port, validPort := number(listener["port"])
		allowedRoutes, _ := object(listener["allowedRoutes"])
		namespaces, _ := object(allowedRoutes["namespaces"])
		kinds := array(allowedRoutes["kinds"])
		var kind map[string]any
		if len(kinds) == 1 {
			kind, _ = object(kinds[0])
		}
		if stringValue(listener["hostname"]) != "" || !validPort || len(kinds) != 1 ||
			stringValue(namespaces["from"]) != "Same" || stringValue(kind["group"]) != "gateway.networking.k8s.io" ||
			stringValue(kind["kind"]) != "HTTPRoute" {
			return false
		}
		if name == policy.HTTPListenerName && protocol == "HTTP" && port == 80 {
			actual[name] = true
		} else if name == policy.HTTPSListenerName && protocol == "HTTPS" && port == 443 {
			actual[name] = true
		} else {
			return false
		}
	}
	for _, name := range []string{policy.HTTPListenerName, policy.HTTPSListenerName} {
		if name == "" || !actual[name] {
			return false
		}
	}
	return true
}

func validateClientTrafficPolicy(value map[string]any, gateway string, section string, policy EdgeGraphPolicy) error {
	spec, _ := object(value["spec"])
	if !exactTargetRefs(spec["targetRefs"], gateway, []string{section}) {
		return errors.New("ClientTrafficPolicy targetRef is not exact")
	}
	connection, _ := object(spec["connection"])
	limit, _ := object(connection["connectionLimit"])
	limitValue, valid := number(limit["value"])
	detection, _ := object(spec["clientIPDetection"])
	xff, _ := object(detection["xForwardedFor"])
	hops, hopsValid := number(xff["numTrustedHops"])
	http2, _ := object(spec["http2"])
	streams, streamsValid := number(http2["maxConcurrentStreams"])
	requests, requestsValid := number(connection["maxRequestsPerConnection"])
	timeout, _ := object(spec["timeout"])
	httpTimeout, _ := object(timeout["http"])
	if !valid || limitValue != policy.ConnectionLimit || !hopsValid || hops != policy.TrustedHopCount ||
		!streamsValid || streams != policy.MaximumHTTP2ConcurrentStreams || !requestsValid ||
		requests != policy.MaximumRequestsPerConnection || stringValue(connection["maxConnectionDuration"]) != policy.MaximumConnectionDuration ||
		stringValue(connection["maxStreamDuration"]) != policy.MaximumStreamDuration ||
		stringValue(httpTimeout["requestReceivedTimeout"]) != policy.RequestReceivedTimeout ||
		stringValue(httpTimeout["idleTimeout"]) != policy.IdleTimeout ||
		stringValue(httpTimeout["streamIdleTimeout"]) != policy.StreamIdleTimeout ||
		policy.ConnectionLimit < 1 || policy.TrustedHopCount < 1 || policy.MaximumHTTP2ConcurrentStreams < 1 ||
		policy.MaximumRequestsPerConnection < 1 || policy.MaximumConnectionDuration == "" || policy.MaximumStreamDuration == "" ||
		policy.RequestReceivedTimeout == "" || policy.IdleTimeout == "" || policy.StreamIdleTimeout == "" {
		return errors.New("ClientTrafficPolicy connection boundary is incomplete")
	}
	return nil
}

func validateBackendTrafficPolicy(value map[string]any, gateway string, httpListener string, httpsListener string, providerID string, policy EdgeGraphPolicy) (int64, error) {
	metadata, _ := object(value["metadata"])
	annotations, _ := object(metadata["annotations"])
	if stringValue(annotations["fs2.nebius.ai/provider-load-balancer-id"]) != providerID {
		return 0, errors.New("BackendTrafficPolicy provider load balancer annotation is wrong")
	}
	limit, err := strconv.ParseInt(stringValue(annotations["fs2.nebius.ai/provider-per-source-connection-limit"]), 10, 64)
	if err != nil || limit < 1 {
		return 0, errors.New("BackendTrafficPolicy provider per-source connection cap is invalid")
	}
	spec, _ := object(value["spec"])
	if !exactTargetRefs(spec["targetRefs"], gateway, []string{httpListener, httpsListener}) {
		return 0, errors.New("BackendTrafficPolicy does not bind both exact public listeners")
	}
	rateLimit, _ := object(spec["rateLimit"])
	global, _ := object(rateLimit["global"])
	rules := array(global["rules"])
	_, publicNetwork, publicCIDRErr := net.ParseCIDR(policy.PublicSourceCIDR)
	publicPrefix, publicBits := 0, 0
	if publicNetwork != nil { publicPrefix, publicBits = publicNetwork.Mask.Size() }
	if len(rules) != 2 || policy.PublicSourceCIDR == "" || policy.PublicRateLimitRequests < 1 ||
		policy.AdminPathPrefix != "/admin" || policy.AdminRateLimitRequests < 1 ||
		policy.PublicRateLimitUnit == "" || policy.AdminRateLimitUnit == "" || publicCIDRErr != nil ||
		publicNetwork.String() != policy.PublicSourceCIDR || publicPrefix != 0 || publicBits != 32 {
		return 0, errors.New("BackendTrafficPolicy omits public or admin rate limits")
	}
	publicRule := false
	adminRule := false
	for _, rawRule := range rules {
		rule, _ := object(rawRule)
		shared, ok := rule["shared"].(bool)
		if !ok || shared {
			return 0, errors.New("BackendTrafficPolicy rate limit is shared across sources")
		}
		selectors := array(rule["clientSelectors"])
		if len(selectors) != 1 {
			return 0, errors.New("BackendTrafficPolicy rule lacks one exact client selector")
		}
		selector, selectorOK := object(selectors[0])
		sourceCIDR, _ := object(selector["sourceCIDR"])
		if !selectorOK || stringValue(sourceCIDR["type"]) != "Distinct" || stringValue(sourceCIDR["value"]) != policy.PublicSourceCIDR {
			return 0, errors.New("BackendTrafficPolicy does not isolate source CIDRs")
		}
		limitSpec, limitOK := object(rule["limit"])
		requests, requestsOK := number(limitSpec["requests"])
		unit := stringValue(limitSpec["unit"])
		path, hasPath := object(selector["path"])
		if !limitOK || !requestsOK { return 0, errors.New("BackendTrafficPolicy rule lacks an exact numeric budget") }
		if !hasPath {
			if len(selector) != 1 || requests != policy.PublicRateLimitRequests || unit != policy.PublicRateLimitUnit || publicRule {
				return 0, errors.New("BackendTrafficPolicy public rule differs from its accepted exact budget")
			}
			publicRule = true
		} else {
			if len(selector) != 2 || stringValue(path["type"]) != "PathPrefix" || stringValue(path["value"]) != policy.AdminPathPrefix ||
				requests != policy.AdminRateLimitRequests || unit != policy.AdminRateLimitUnit || adminRule {
				return 0, errors.New("BackendTrafficPolicy admin rule differs from its accepted exact path and budget")
			}
			adminRule = true
		}
	}
	if !publicRule || !adminRule { return 0, errors.New("BackendTrafficPolicy lacks one exact public and one exact admin rule") }
	return limit, nil
}

func exactTargetRefs(value any, gateway string, sections []string) bool {
	refs := array(value)
	if len(refs) != len(sections) {
		return false
	}
	expected := map[string]bool{}
	for _, section := range sections {
		expected[section] = true
	}
	for _, raw := range refs {
		ref, ok := object(raw)
		if !ok || stringValue(ref["group"]) != "gateway.networking.k8s.io" || stringValue(ref["kind"]) != "Gateway" ||
			stringValue(ref["name"]) != gateway || !expected[stringValue(ref["sectionName"])] {
			return false
		}
		delete(expected, stringValue(ref["sectionName"]))
	}
	return len(expected) == 0
}

type providerPage[T any] struct {
	Items []T `json:"items"`
	NextPageToken string `json:"next_page_token"`
	RemainingItemCount int64 `json:"remaining_item_count"`
}

type providerLoadBalancer struct {
	ID string `json:"id"`
	ListenerIDs []string `json:"listener_ids"`
	BackendIDs []string `json:"backend_ids"`
	PerSourceConnectionLimit int64 `json:"per_source_connection_limit"`
}

type providerListener struct {
	ID string `json:"id"`
	LoadBalancerID string `json:"load_balancer_id"`
	Protocol string `json:"protocol"`
	Port int64 `json:"port"`
	BackendID string `json:"backend_id"`
	HealthCheckID string `json:"health_check_id"`
}

type providerBackend struct {
	ID string `json:"id"`
	LoadBalancerID string `json:"load_balancer_id"`
	ServiceNamespace string `json:"service_namespace"`
	ServiceName string `json:"service_name"`
	ServicePort int64 `json:"service_port"`
	HealthCheckID string `json:"health_check_id"`
	DirectAccessCIDRs []string `json:"direct_access_cidrs"`
}

type providerHealthCheck struct {
	ID string `json:"id"`
	BackendID string `json:"backend_id"`
	Protocol string `json:"protocol"`
	Port int64 `json:"port"`
	HealthyTargets int64 `json:"healthy_targets"`
	MinimumHealthyTargets int64 `json:"minimum_healthy_targets"`
}

func validateProviderLoadBalancerGraph(inventory *semanticInventory, verified *collector.VerifiedEvidenceBundle, policy EdgeGraphPolicy, perSourceLimit int64) error {
	loadBalancers := []providerLoadBalancer{}
	listeners := []providerListener{}
	backends := []providerBackend{}
	healthChecks := []providerHealthCheck{}
	for _, source := range verified.Bundle.Sources {
		for _, raw := range verified.RawResponses[source.SourceID] {
			switch source.SemanticCollection {
			case "provider-load-balancers":
				var page providerPage[providerLoadBalancer]
				if boundary.DecodeExactJSON(raw, &page) != nil { return errors.New("provider load-balancer page is not the exact native schema") }
				loadBalancers = append(loadBalancers, page.Items...)
			case "provider-load-balancer-listeners":
				var page providerPage[providerListener]
				if boundary.DecodeExactJSON(raw, &page) != nil { return errors.New("provider listener page is not the exact native schema") }
				listeners = append(listeners, page.Items...)
			case "provider-load-balancer-backends":
				var page providerPage[providerBackend]
				if boundary.DecodeExactJSON(raw, &page) != nil { return errors.New("provider backend page is not the exact native schema") }
				backends = append(backends, page.Items...)
			case "provider-load-balancer-health-checks":
				var page providerPage[providerHealthCheck]
				if boundary.DecodeExactJSON(raw, &page) != nil { return errors.New("provider health-check page is not the exact native schema") }
				healthChecks = append(healthChecks, page.Items...)
			}
		}
	}
	lb, ok := exactlyOne(loadBalancers, func(item providerLoadBalancer) bool { return item.ID == policy.ProviderLoadBalancerID })
	if !ok || lb.PerSourceConnectionLimit != perSourceLimit || !sameStringSet(lb.ListenerIDs, []string{policy.ProviderHTTPListenerID, policy.ProviderHTTPSListenerID}) ||
		!sameStringSet(lb.BackendIDs, []string{policy.ProviderBackendID}) {
		return errors.New("provider load balancer does not bind the exact listener/backend graph and per-source cap")
	}
	httpListener, httpOK := exactlyOne(listeners, func(item providerListener) bool { return item.ID == policy.ProviderHTTPListenerID })
	httpsListener, httpsOK := exactlyOne(listeners, func(item providerListener) bool { return item.ID == policy.ProviderHTTPSListenerID })
	if !httpOK || !httpsOK || httpListener.LoadBalancerID != lb.ID || httpsListener.LoadBalancerID != lb.ID ||
		httpListener.Protocol != "HTTP" || httpListener.Port != 80 || httpsListener.Protocol != "HTTPS" || httpsListener.Port != 443 ||
		httpListener.BackendID != policy.ProviderBackendID || httpsListener.BackendID != policy.ProviderBackendID ||
		httpListener.HealthCheckID != policy.ProviderHealthCheckID || httpsListener.HealthCheckID != policy.ProviderHealthCheckID {
		return errors.New("provider listeners do not terminate only on the exact accepted public ports and backend")
	}
	for _, listener := range listeners {
		if listener.LoadBalancerID == lb.ID && listener.ID != policy.ProviderHTTPListenerID && listener.ID != policy.ProviderHTTPSListenerID {
			return errors.New("provider load balancer exposes an unaccounted listener")
		}
	}
	backend, backendOK := exactlyOne(backends, func(item providerBackend) bool { return item.ID == policy.ProviderBackendID })
	if !backendOK || backend.LoadBalancerID != lb.ID || backend.HealthCheckID != policy.ProviderHealthCheckID ||
		backend.ServiceNamespace != policy.Namespace || backend.ServiceName == "" || backend.ServicePort < 1 || len(backend.DirectAccessCIDRs) != 0 {
		return errors.New("provider backend does not exclude direct reachability or bind the accepted service target")
	}
	for _, candidate := range backends {
		if candidate.LoadBalancerID == lb.ID && candidate.ID != policy.ProviderBackendID {
			return errors.New("provider load balancer reaches an unaccounted backend")
		}
	}
	serviceAccepted := false
	for _, ref := range policy.PublicBackendServices {
		if ref.SemanticCollection == "apiserver-services" && ref.Namespace == backend.ServiceNamespace && ref.Name == backend.ServiceName {
			serviceAccepted = true
		}
	}
	health, healthOK := exactlyOne(healthChecks, func(item providerHealthCheck) bool { return item.ID == policy.ProviderHealthCheckID })
	if !serviceAccepted || !healthOK || health.BackendID != backend.ID || health.Port != backend.ServicePort ||
		(health.Protocol != "HTTP" && health.Protocol != "HTTPS") || health.MinimumHealthyTargets < 1 || health.HealthyTargets < health.MinimumHealthyTargets {
		return errors.New("provider backend health and Kubernetes Service identity do not form the exact accepted graph")
	}
	for _, candidate := range healthChecks {
		if candidate.BackendID == backend.ID && candidate.ID != policy.ProviderHealthCheckID {
			return errors.New("provider backend uses an unaccounted health check")
		}
	}
	service, err := inventory.require(ObjectReference{"apiserver-services", backend.ServiceNamespace, backend.ServiceName})
	if err != nil || validateServicePort(service.value, backend.ServicePort) != nil {
		return errors.New("provider backend does not join the exact native Kubernetes Service port")
	}
	if err := validateServiceEndpointSlices(inventory, service, backend.ServicePort); err != nil { return err }
	return nil
}

func validateServiceEndpointSlices(inventory *semanticInventory, service inventoryObject, expectedPort int64) error {
	serviceSpec, _ := object(service.value["spec"])
	if serviceType := stringValue(serviceSpec["type"]); serviceType != "" && serviceType != "ClusterIP" { return errors.New("public backend Service exposes a direct non-ClusterIP path") }
	if len(array(serviceSpec["externalIPs"])) != 0 || stringValue(serviceSpec["externalName"]) != "" { return errors.New("public backend Service exposes an external direct path") }
	for _, rawPort := range array(serviceSpec["ports"]) { port, _ := object(rawPort); if nodePort, valid := number(port["nodePort"]); valid && nodePort != 0 { return errors.New("public backend Service exposes a direct NodePort") } }
	matched := 0
	for _, endpointSlice := range inventory.byCollection["apiserver-endpointslices"] {
		if endpointSlice.identity.Namespace != service.identity.Namespace { continue }
		metadata, _ := object(endpointSlice.value["metadata"])
		labels, _ := object(metadata["labels"])
		if stringValue(labels["kubernetes.io/service-name"]) != service.identity.Name { continue }
		portMatched := false
		for _, rawPort := range array(endpointSlice.value["ports"]) {
			port, _ := object(rawPort)
			value, valid := number(port["port"])
			if valid && value == expectedPort { portMatched = true }
		}
		if !portMatched { return errors.New("public backend EndpointSlice omits the provider-bound Service port") }
		for _, rawEndpoint := range array(endpointSlice.value["endpoints"]) {
			endpoint, _ := object(rawEndpoint)
			if len(array(endpoint["addresses"])) == 0 { return errors.New("public backend EndpointSlice contains an addressless endpoint") }
			target, targetExists := object(endpoint["targetRef"])
			if !targetExists || stringValue(target["kind"]) != "Pod" || stringValue(target["namespace"]) != service.identity.Namespace || stringValue(target["uid"]) == "" {
				return errors.New("public backend EndpointSlice target is not an exact Pod UID")
			}
			pod, err := inventory.require(ObjectReference{"apiserver-pods", service.identity.Namespace, stringValue(target["name"])})
			if err != nil || pod.identity.UID != stringValue(target["uid"]) { return errors.New("public backend EndpointSlice Pod target differs from native Pod inventory") }
		}
		matched++
	}
	if matched == 0 { return errors.New("public backend Service has no complete native EndpointSlice") }
	return nil
}

func validateNoUnaccountedBackendRoutes(inventory *semanticInventory, acceptedRoutes []inventoryObject, backends []inventoryObject) error {
	accepted := map[string]bool{}
	for _, route := range acceptedRoutes { accepted[route.collection+"\x00"+route.identity.Namespace+"\x00"+route.identity.Name] = true }
	backendNames := map[string]bool{}
	for _, backend := range backends { backendNames[backend.identity.Namespace+"\x00"+backend.identity.Name] = true }
	for _, collection := range []string{"apiserver-httproutes", "apiserver-grpcroutes", "apiserver-tcproutes", "apiserver-tlsroutes", "apiserver-udproutes"} {
		for _, route := range inventory.byCollection[collection] {
			if !routeReferencesBackend(route, backendNames) { continue }
			if !accepted[route.collection+"\x00"+route.identity.Namespace+"\x00"+route.identity.Name] { return fmt.Errorf("unaccounted route %s/%s reaches a protected public backend", route.identity.Namespace, route.identity.Name) }
		}
	}
	for _, ingress := range inventory.byCollection["apiserver-ingresses"] {
		if ingressReferencesBackend(ingress, backendNames) { return fmt.Errorf("Ingress %s/%s bypasses the accepted Gateway route graph", ingress.identity.Namespace, ingress.identity.Name) }
	}
	return nil
}

func routeReferencesBackend(route inventoryObject, backends map[string]bool) bool {
	spec, _ := object(route.value["spec"])
	for _, rawRule := range array(spec["rules"]) {
		rule, _ := object(rawRule)
		for _, rawRef := range array(rule["backendRefs"]) {
			ref, _ := object(rawRef)
			namespace := stringValue(ref["namespace"])
			if namespace == "" { namespace = route.identity.Namespace }
			if backends[namespace+"\x00"+stringValue(ref["name"])] { return true }
		}
	}
	return false
}

func ingressReferencesBackend(ingress inventoryObject, backends map[string]bool) bool {
	spec, _ := object(ingress.value["spec"])
	check := func(value any) bool {
		backend, _ := object(value)
		service, _ := object(backend["service"])
		return backends[ingress.identity.Namespace+"\x00"+stringValue(service["name"])]
	}
	if check(spec["defaultBackend"]) { return true }
	for _, rawRule := range array(spec["rules"]) {
		rule, _ := object(rawRule)
		httpValue, _ := object(rule["http"])
		for _, rawPath := range array(httpValue["paths"]) { path, _ := object(rawPath); if check(path["backend"]) { return true } }
	}
	return false
}

func exactlyOne[T any](values []T, predicate func(T) bool) (T, bool) {
	var result T
	count := 0
	for _, value := range values { if predicate(value) { result = value; count++ } }
	return result, count == 1
}

func sameStringSet(left []string, right []string) bool {
	if len(left) != len(right) { return false }
	values := map[string]bool{}
	for _, item := range left { if item == "" || values[item] { return false }; values[item] = true }
	for _, item := range right { if !values[item] { return false } }
	return true
}

func validateGatewayClassAttachment(gatewayClass map[string]any, envoyProxy inventoryObject) error {
	spec, _ := object(gatewayClass["spec"])
	parameters, ok := object(spec["parametersRef"])
	if !ok || stringValue(spec["controllerName"]) != "gateway.envoyproxy.io/gatewayclass-controller" ||
		stringValue(parameters["group"]) != "gateway.envoyproxy.io" || stringValue(parameters["kind"]) != "EnvoyProxy" ||
		stringValue(parameters["namespace"]) != envoyProxy.identity.Namespace || stringValue(parameters["name"]) != envoyProxy.identity.Name {
		return errors.New("GatewayClass parametersRef is not the exact EnvoyProxy")
	}
	return nil
}

func validatePublicRoutes(inventory *semanticInventory, routes []inventoryObject, backends []inventoryObject, policy EdgeGraphPolicy) error {
	if len(routes) == 0 || len(backends) == 0 {
		return errors.New("edge graph has no exact public route or backend Service")
	}
	acceptedBackends := map[string]bool{}
	for _, backend := range backends {
		if backend.identity.Resource != "services" { return errors.New("public route backend is not a Service") }
		acceptedBackends[backend.identity.Namespace+"\x00"+backend.identity.Name] = true
	}
	audioRules := 0
	for _, route := range routes {
		spec, _ := object(route.value["spec"])
		if !sameStringSet(stringValues(array(spec["hostnames"])), policy.PublicRouteHostnames) {
			return fmt.Errorf("route %s/%s hostname coverage differs from the exact accepted public listener coverage", route.identity.Namespace, route.identity.Name)
		}
		parents := array(spec["parentRefs"])
		parentMatched := false
		parentSection := ""
		if len(parents) != 1 { return fmt.Errorf("route %s/%s has additional or missing Gateway parents", route.identity.Namespace, route.identity.Name) }
		for _, rawParent := range parents {
			parent, _ := object(rawParent)
			section := stringValue(parent["sectionName"])
			parentNamespace := stringValue(parent["namespace"])
			if parentNamespace == "" { parentNamespace = route.identity.Namespace }
			if (stringValue(parent["group"]) == "" || stringValue(parent["group"]) == "gateway.networking.k8s.io") &&
				(stringValue(parent["kind"]) == "" || stringValue(parent["kind"]) == "Gateway") &&
				parentNamespace == policy.Namespace && stringValue(parent["name"]) == policy.GatewayName && (section == policy.HTTPListenerName || section == policy.HTTPSListenerName) {
				parentMatched = true
				parentSection = section
			}
		}
		if !parentMatched { return fmt.Errorf("route %s/%s is not attached to an exact public Gateway listener", route.identity.Namespace, route.identity.Name) }
		backendCount := 0
		for ruleIndex, rawRule := range array(spec["rules"]) {
			rule, _ := object(rawRule)
			if ruleMatchesExactPath(rule, policy.AudioStreamPath) {
				if parentSection != policy.HTTPSListenerName || ruleIndex != 0 {
					return errors.New("public audio rule is not the first rule on the exact HTTPS listener")
				}
				timeouts, _ := object(rule["timeouts"])
				if policy.AudioStreamPath != "/v1/audio/stream" || policy.AudioStreamRequestTimeout == "" || policy.AudioStreamBackendRequestTimeout == "" ||
					stringValue(timeouts["request"]) != policy.AudioStreamRequestTimeout ||
					stringValue(timeouts["backendRequest"]) != policy.AudioStreamBackendRequestTimeout {
					return errors.New("public audio route differs from its accepted exact path and timeout contract")
				}
				audioRules++
			} else if ruleCompetesWithExactPath(rule, policy.AudioStreamPath) {
				return errors.New("public routes contain a competing exact or regular-expression audio rule")
			}
			for _, rawRef := range array(rule["backendRefs"]) {
				ref, _ := object(rawRef)
				group := stringValue(ref["group"])
				kind := stringValue(ref["kind"])
				namespace := stringValue(ref["namespace"])
				if namespace == "" { namespace = route.identity.Namespace }
				if (group != "" && group != "core") || (kind != "" && kind != "Service") ||
					!acceptedBackends[namespace+"\x00"+stringValue(ref["name"])] {
					return fmt.Errorf("route %s/%s reaches a backend outside the accepted edge graph", route.identity.Namespace, route.identity.Name)
				}
				if namespace != route.identity.Namespace && !hasReferenceGrant(inventory, route, namespace, stringValue(ref["name"])) { return fmt.Errorf("route %s/%s lacks an exact cross-namespace ReferenceGrant", route.identity.Namespace, route.identity.Name) }
				port, valid := number(ref["port"])
				if !valid || port < 1 || port > 65535 { return errors.New("public route backend port is invalid") }
				service, serviceErr := inventory.require(ObjectReference{"apiserver-services", namespace, stringValue(ref["name"])})
				if serviceErr != nil || validateServicePort(service.value, port) != nil { return fmt.Errorf("route %s/%s backend port does not join one exact Service port", route.identity.Namespace, route.identity.Name) }
				backendCount++
			}
		}
		if backendCount == 0 { return errors.New("public route has no exact backend Service") }
	}
	if audioRules != 1 { return errors.New("public routes do not contain exactly one accepted audio streaming timeout rule") }
	return nil
}

func ruleMatchesExactPath(rule map[string]any, expected string) bool {
	if expected == "" { return false }
	matches := array(rule["matches"])
	if len(matches) != 1 { return false }
	match, ok := object(matches[0])
	if !ok || !onlyObjectKeys(match, "path") { return false }
	path, ok := object(match["path"])
	return ok && onlyObjectKeys(path, "type", "value") && stringValue(path["type"]) == "Exact" && stringValue(path["value"]) == expected
}

func ruleCompetesWithExactPath(rule map[string]any, expected string) bool {
	if expected == "" {
		return true
	}
	for _, rawMatch := range array(rule["matches"]) {
		match, ok := object(rawMatch)
		if !ok {
			return true
		}
		path, hasPath := object(match["path"])
		if !hasPath {
			continue
		}
		typeName := stringValue(path["type"])
		value := stringValue(path["value"])
		if typeName == "RegularExpression" || typeName == "Exact" && value == expected {
			return true
		}
		if typeName != "Exact" && typeName != "PathPrefix" {
			return true
		}
	}
	return false
}

func hasReferenceGrant(inventory *semanticInventory, route inventoryObject, backendNamespace string, backendName string) bool {
	for _, grant := range inventory.byCollection["apiserver-referencegrants"] {
		if grant.identity.Namespace != backendNamespace { continue }
		spec, _ := object(grant.value["spec"])
		fromMatched := false
		for _, rawFrom := range array(spec["from"]) {
			from, _ := object(rawFrom)
			if stringValue(from["group"]) == route.identity.APIGroup && stringValue(from["kind"]) == route.kind && stringValue(from["namespace"]) == route.identity.Namespace { fromMatched = true }
		}
		toMatched := false
		for _, rawTo := range array(spec["to"]) {
			to, _ := object(rawTo)
			group := stringValue(to["group"])
			if (group == "" || group == "core") && stringValue(to["kind"]) == "Service" && stringValue(to["name"]) == backendName { toMatched = true }
		}
		if fromMatched && toMatched { return true }
	}
	return false
}

func validatePDBSelectsWorkload(pdb map[string]any, workload map[string]any) error {
	if err := validatePDB(pdb); err != nil { return err }
	spec, _ := object(pdb["spec"])
	selector, _ := object(spec["selector"])
	matchLabels, _ := object(selector["matchLabels"])
	workloadSpec, _ := object(workload["spec"])
	template, _ := object(workloadSpec["template"])
	metadata, _ := object(template["metadata"])
	labels, _ := object(metadata["labels"])
	for key, value := range matchLabels { if labels[key] != value { return errors.New("PodDisruptionBudget selector does not select its exact workload") } }
	return nil
}

func workloadCarriesLabels(workload map[string]any, expected map[string]string) bool {
	spec, _ := object(workload["spec"])
	template, _ := object(spec["template"])
	metadata, _ := object(template["metadata"])
	labels, _ := object(metadata["labels"])
	for name, value := range expected {
		if stringValue(labels[name]) != value {
			return false
		}
	}
	return len(expected) > 0
}

func objectSelectorEquals(value map[string]any, expected map[string]string) bool {
	spec, _ := object(value["spec"])
	selector, _ := object(spec["selector"])
	if len(selector) != len(expected) {
		return false
	}
	for name, value := range expected {
		if stringValue(selector[name]) != value {
			return false
		}
	}
	return len(expected) > 0
}

func pdbSelectorEquals(value map[string]any, expected map[string]string) bool {
	spec, _ := object(value["spec"])
	selector, _ := object(spec["selector"])
	return exactMatchLabelsSelector(selector, expected)
}

func pdbMinimumAvailableEquals(value map[string]any, expected int64) bool {
	spec, _ := object(value["spec"])
	minimum, valid := number(spec["minAvailable"])
	return valid && minimum == expected
}

type servicePortContract struct {
	name string
	protocol string
	port int64
	numericTargetPort int64
	namedTargetPort string
}

func validateInternalService(service map[string]any, selector map[string]string, headless bool, publishNotReady bool, expectedPorts []servicePortContract) error {
	spec, specOK := object(service["spec"])
	actualSelector, selectorOK := object(spec["selector"])
	clusterIP := stringValue(spec["clusterIP"])
	clusterIPs := stringValues(array(spec["clusterIPs"]))
	if !specOK || !selectorOK || !objectStringMapEquals(actualSelector, selector) || stringValue(spec["type"]) != "ClusterIP" ||
		boolValue(spec["publishNotReadyAddresses"]) != publishNotReady || len(array(spec["externalIPs"])) != 0 ||
		stringValue(spec["externalName"]) != "" || stringValue(spec["loadBalancerIP"]) != "" ||
		stringValue(spec["loadBalancerClass"]) != "" || len(array(spec["loadBalancerSourceRanges"])) != 0 {
		return errors.New("Service identity, selector or internal-only exposure contract is invalid")
	}
	if headless {
		if clusterIP != "None" || len(clusterIPs) != 1 || clusterIPs[0] != "None" {
			return errors.New("headless Service does not use the exact None clusterIP contract")
		}
	} else if clusterIP == "" || clusterIP == "None" || len(clusterIPs) != 1 || clusterIPs[0] != clusterIP {
		return errors.New("internal Service does not have one exact allocated ClusterIP")
	}
	ports := array(spec["ports"])
	if len(ports) != len(expectedPorts) || len(ports) == 0 {
		return errors.New("Service port inventory differs from the exact accepted contract")
	}
	expectedByName := map[string]servicePortContract{}
	for _, expected := range expectedPorts {
		if expected.name == "" || expected.protocol != "TCP" || expected.port < 1 || expected.port > 65535 || expectedByName[expected.name].name != "" ||
			(expected.numericTargetPort == 0) == (expected.namedTargetPort == "") {
			return errors.New("accepted Service port contract is malformed")
		}
		expectedByName[expected.name] = expected
	}
	seen := map[string]bool{}
	for _, rawPort := range ports {
		port, ok := object(rawPort)
		name := stringValue(port["name"])
		expected, accepted := expectedByName[name]
		value, validValue := number(port["port"])
		nodePort, hasNodePort := number(port["nodePort"])
		if !ok || !accepted || seen[name] || stringValue(port["protocol"]) != expected.protocol || !validValue || value != expected.port ||
			stringValue(port["appProtocol"]) != "" || hasNodePort && nodePort != 0 || !targetPortEquals(port["targetPort"], expected) {
			return errors.New("Service carries an extra, externally exposed or inexact port")
		}
		seen[name] = true
	}
	return len(seen) == len(expectedByName)
}

func targetPortEquals(value any, expected servicePortContract) bool {
	if expected.namedTargetPort != "" {
		text, ok := value.(string)
		return ok && text == expected.namedTargetPort
	}
	numberValue, ok := number(value)
	return ok && numberValue == expected.numericTargetPort
}

func objectStringMapEquals(actual map[string]any, expected map[string]string) bool {
	if len(actual) != len(expected) || len(expected) == 0 {
		return false
	}
	for name, value := range expected {
		if stringValue(actual[name]) != value {
			return false
		}
	}
	return true
}

func validateRateLimitRedisBinding(rateLimit inventoryObject, redis inventoryObject, bootstrap inventoryObject, serverCertificate inventoryObject, clientCertificate inventoryObject, collectedAt string, policy EdgeGraphPolicy) error {
	rateLimitPodSpec, err := workloadPodSpec(rateLimit)
	if err != nil { return err }
	redisPodSpec, err := workloadPodSpec(redis)
	if err != nil { return err }
	bootstrapData, dataOK := object(bootstrap.value["data"])
	bootstrapRaw, bootstrapErr := json.Marshal(bootstrapData)
	if !dataOK || bootstrapErr != nil || digestBytes(bootstrapRaw) != policy.RedisBootstrapConfigDataSHA256 {
		return errors.New("Redis/Sentinel bootstrap bytes do not match the independently accepted exact configuration digest")
	}
	if err := validateRedisTLSHandoff(bootstrapData, collectedAt, policy); err != nil {
		return err
	}
	if err := validateRedisTLSCertificateEvidence(serverCertificate, clientCertificate, collectedAt, policy); err != nil {
		return err
	}
	if policy.RedisSentinelMasterName == "" || policy.RedisSentinelPort < 1 || policy.RedisSentinelPort > 65535 ||
		len(policy.RedisSentinelEndpoints) != 3 || !canonicalNonemptyStrings(policy.RedisSentinelEndpoints) {
		return errors.New("accepted Redis Sentinel discovery contract is incomplete")
	}
	expectedEndpoints := make([]string, 0, 3)
	for ordinal := 0; ordinal < 3; ordinal++ {
		expectedEndpoints = append(expectedEndpoints, fmt.Sprintf("%s-%d.%s.%s.svc.cluster.local:%d", policy.RedisWorkloadName, ordinal, policy.RedisServiceName, policy.RedisServiceNamespace, policy.RedisSentinelPort))
	}
	if !sameStringSet(policy.RedisSentinelEndpoints, expectedEndpoints) {
		return errors.New("accepted Redis Sentinel endpoints are not the exact three StatefulSet identities")
	}
	expectedURL := policy.RedisSentinelMasterName+","+strings.Join(policy.RedisSentinelEndpoints, ",")
	requiredEnvironment := map[string]string{
		"REDIS_TYPE": "sentinel",
		"REDIS_URL": expectedURL,
		"REDIS_CLOSE_CONNECTION_ON_READONLY_ERROR": "true",
		"REDIS_HEALTH_CHECK_ACTIVE_CONNECTION": "true",
		"REDIS_TLS": "true",
		"REDIS_TLS_SKIP_HOSTNAME_VERIFICATION": "false",
		"REDIS_TLS_CACERT": "/redis-client-tls/ca.crt",
		"REDIS_TLS_CLIENT_CERT": "/redis-client-tls/tls.crt",
		"REDIS_TLS_CLIENT_KEY": "/redis-client-tls/tls.key",
		"CONFIG_TYPE": "GRPC_XDS_SOTW",
		"CONFIG_GRPC_XDS_SERVER_URL": policy.RateLimitXDSAddress,
	}
	containers := array(rateLimitPodSpec["containers"])
	if len(containers) != 1 || len(array(rateLimitPodSpec["initContainers"])) != 0 || len(array(rateLimitPodSpec["ephemeralContainers"])) != 0 ||
		!explicitBool(rateLimitPodSpec["automountServiceAccountToken"], false) || boolValue(rateLimitPodSpec["hostNetwork"]) ||
		boolValue(rateLimitPodSpec["hostPID"]) || boolValue(rateLimitPodSpec["hostIPC"]) || boolValue(rateLimitPodSpec["shareProcessNamespace"]) ||
		stringValue(rateLimitPodSpec["serviceAccountName"]) != policy.RateLimitServiceAccountName || stringValue(rateLimitPodSpec["dnsPolicy"]) != "ClusterFirst" ||
		len(nestedObject(rateLimitPodSpec, "dnsConfig")) != 0 || len(array(rateLimitPodSpec["hostAliases"])) != 0 ||
		stringValue(rateLimitPodSpec["runtimeClassName"]) != "" || stringValue(rateLimitPodSpec["hostname"]) != "" ||
		stringValue(rateLimitPodSpec["subdomain"]) != "" || boolValue(rateLimitPodSpec["setHostnameAsFQDN"]) ||
		!exactRateLimitPodSecurityContext(rateLimitPodSpec, policy) {
		return errors.New("rate-limit workload broadens the exact single-container nonroot Pod execution contract")
	}
	container, containerOK := object(containers[0])
	if !containerOK || stringValue(container["name"]) != "envoy-ratelimit" || stringValue(container["image"]) != policy.RateLimitContainerImage ||
		stringValue(container["imagePullPolicy"]) != "IfNotPresent" || len(array(container["command"])) != 0 || len(array(container["args"])) != 0 ||
		len(array(container["envFrom"])) != 0 || !exactRateLimitEnvironment(container, requiredEnvironment) ||
		!exactRateLimitContainerSecurityContext(container, policy) || !exactRateLimitContainerPorts(container) || !exactRateLimitResources(container) ||
		!exactVolumeMounts(container, map[string]volumeMountContract{
			"ratelimit-tmp": {path: "/tmp"},
			"redis-client-tls": {path: "/redis-client-tls", readOnly: true},
		}) || !rateLimitVolumeBackingIsExact(rateLimitPodSpec, policy) {
		return errors.New("envoy-ratelimit image, environment, ports, resources, security context, mounts or volume backing differs from the exact accepted contract")
	}
	redisSecretBound := false
	bootstrapBound := false
	redisTLSContainers := map[string]bool{"redis": false, "sentinel": false}
	for _, rawVolume := range array(redisPodSpec["volumes"]) {
		volume, _ := object(rawVolume)
		secret, _ := object(volume["secret"])
		if stringValue(volume["name"]) == "redis-server-tls" && stringValue(secret["secretName"]) == policy.RedisTLSServerSecretName { redisSecretBound = true }
		configMap, _ := object(volume["configMap"])
		if stringValue(volume["name"]) == "bootstrap" && stringValue(configMap["name"]) == policy.RedisBootstrapConfigMapName { bootstrapBound = true }
	}
	for _, rawContainer := range array(redisPodSpec["containers"]) {
		container, _ := object(rawContainer)
		name := stringValue(container["name"])
		if _, required := redisTLSContainers[name]; !required { continue }
		for _, rawMount := range array(container["volumeMounts"]) {
			mount, _ := object(rawMount)
			if stringValue(mount["name"]) == "redis-server-tls" && stringValue(mount["mountPath"]) == "/redis-server-tls" && boolValue(mount["readOnly"]) { redisTLSContainers[name] = true }
		}
	}
	if !workloadAnnotationEquals(rateLimit.value, "fs2.nebius.ai/redis-tls-handoff-sha256", policy.RedisTLSHandoffSHA256) ||
		!workloadAnnotationEquals(redis.value, "fs2.nebius.ai/redis-tls-handoff-sha256", policy.RedisTLSHandoffSHA256) ||
		!redisBootstrapExecutionIsExact(redisPodSpec) || !redisTLSProbesAreExact(redisPodSpec) || !redisVolumeBackingIsExact(redisPodSpec, policy) ||
		!redisSecretBound || !bootstrapBound || !redisTLSContainers["redis"] || !redisTLSContainers["sentinel"] || replicas(redis.value) < 3 {
		return errors.New("rate-limit and Redis/Sentinel workloads do not bind the exact mutual-TLS Sentinel authority and availability floor")
	}
	return nil
}

func exactRateLimitPodSecurityContext(podSpec map[string]any, policy EdgeGraphPolicy) bool {
	context, ok := object(podSpec["securityContext"])
	uid, uidOK := number(context["runAsUser"])
	gid, gidOK := number(context["runAsGroup"])
	fsGroup, fsGroupOK := number(context["fsGroup"])
	seccomp, seccompOK := object(context["seccompProfile"])
	return ok && onlyObjectKeys(context, "runAsNonRoot", "runAsUser", "runAsGroup", "fsGroup", "fsGroupChangePolicy", "seccompProfile") &&
		explicitBool(context["runAsNonRoot"], true) && uidOK && uid == policy.RateLimitRuntimeUID &&
		gidOK && gid == policy.RateLimitRuntimeGID && fsGroupOK && fsGroup == policy.RateLimitTLSReaderGID &&
		stringValue(context["fsGroupChangePolicy"]) == "OnRootMismatch" && seccompOK &&
		onlyObjectKeys(seccomp, "type", "localhostProfile") && stringValue(seccomp["type"]) == "RuntimeDefault" && stringValue(seccomp["localhostProfile"]) == ""
}

func exactRateLimitContainerSecurityContext(container map[string]any, policy EdgeGraphPolicy) bool {
	context, ok := object(container["securityContext"])
	uid, uidOK := number(context["runAsUser"])
	gid, gidOK := number(context["runAsGroup"])
	capabilities, capabilitiesOK := object(context["capabilities"])
	seccomp, seccompOK := object(context["seccompProfile"])
	return ok && onlyObjectKeys(context, "allowPrivilegeEscalation", "privileged", "readOnlyRootFilesystem", "runAsNonRoot", "runAsUser", "runAsGroup", "capabilities", "seccompProfile") &&
		explicitBool(context["allowPrivilegeEscalation"], false) && explicitBool(context["privileged"], false) &&
		explicitBool(context["readOnlyRootFilesystem"], true) && explicitBool(context["runAsNonRoot"], true) &&
		uidOK && uid == policy.RateLimitRuntimeUID && gidOK && gid == policy.RateLimitRuntimeGID &&
		capabilitiesOK && onlyObjectKeys(capabilities, "add", "drop") && len(array(capabilities["add"])) == 0 &&
		sameOrderedStrings(stringValues(array(capabilities["drop"])), []string{"ALL"}) && seccompOK &&
		onlyObjectKeys(seccomp, "type", "localhostProfile") && stringValue(seccomp["type"]) == "RuntimeDefault" && stringValue(seccomp["localhostProfile"]) == ""
}

func exactRateLimitEnvironment(container map[string]any, expected map[string]string) bool {
	environment := array(container["env"])
	if len(environment) != len(expected) {
		return false
	}
	seen := map[string]bool{}
	for _, raw := range environment {
		entry, ok := object(raw)
		name := stringValue(entry["name"])
		if !ok || !onlyObjectKeys(entry, "name", "value") || seen[name] || expected[name] != stringValue(entry["value"]) {
			return false
		}
		seen[name] = true
	}
	return len(seen) == len(expected)
}

func exactRateLimitContainerPorts(container map[string]any) bool {
	expected := map[string]int64{"grpc": 8081, "metrics": 19001}
	ports := array(container["ports"])
	if len(ports) != len(expected) {
		return false
	}
	seen := map[string]bool{}
	for _, raw := range ports {
		port, ok := object(raw)
		name := stringValue(port["name"])
		containerPort, portOK := number(port["containerPort"])
		hostPort, hasHostPort := number(port["hostPort"])
		if !ok || !onlyObjectKeys(port, "name", "containerPort", "protocol", "hostIP", "hostPort") || seen[name] ||
			!portOK || expected[name] != containerPort || stringValue(port["protocol"]) != "TCP" || stringValue(port["hostIP"]) != "" || hasHostPort && hostPort != 0 {
			return false
		}
		seen[name] = true
	}
	return len(seen) == len(expected)
}

func exactRateLimitResources(container map[string]any) bool {
	resources, ok := object(container["resources"])
	requests, requestsOK := object(resources["requests"])
	limits, limitsOK := object(resources["limits"])
	return ok && onlyObjectKeys(resources, "requests", "limits", "claims") && len(array(resources["claims"])) == 0 &&
		requestsOK && limitsOK && objectStringMapEquals(requests, map[string]string{"cpu": "100m", "memory": "128Mi"}) &&
		objectStringMapEquals(limits, map[string]string{"cpu": "500m", "memory": "256Mi"})
}

func rateLimitVolumeBackingIsExact(podSpec map[string]any, policy EdgeGraphPolicy) bool {
	volumes := array(podSpec["volumes"])
	if len(volumes) != 2 {
		return false
	}
	seen := map[string]bool{}
	for _, raw := range volumes {
		volume, ok := object(raw)
		name := stringValue(volume["name"])
		if !ok || seen[name] {
			return false
		}
		switch name {
		case "ratelimit-tmp":
			emptyDir, exists := object(volume["emptyDir"])
			if !exists || !onlyObjectKeys(volume, "name", "emptyDir") || !onlyObjectKeys(emptyDir, "sizeLimit", "medium") ||
				stringValue(emptyDir["sizeLimit"]) != "64Mi" || stringValue(emptyDir["medium"]) != "" {
				return false
			}
		case "redis-client-tls":
			secret, exists := object(volume["secret"])
			mode, validMode := number(secret["defaultMode"])
			if !exists || !onlyObjectKeys(volume, "name", "secret") || stringValue(secret["secretName"]) != policy.RedisTLSClientSecretName ||
				!validMode || mode != policy.RateLimitTLSSecretDefaultMode || !exactTLSSecretItems(secret) {
				return false
			}
		default:
			return false
		}
		seen[name] = true
	}
	return len(seen) == 2
}

func validateRedisTLSHandoff(bootstrapData map[string]any, collectedAt string, policy EdgeGraphPolicy) error {
	rawText := stringValue(bootstrapData["tls-handoff-envelope.json"])
	raw := []byte(rawText)
	if rawText == "" || len(raw) > 64*1024 || digestBytes(raw) != policy.RedisTLSHandoffSHA256 {
		return errors.New("Redis TLS handoff envelope is absent or differs from its independently accepted digest")
	}
	var envelope boundary.SignedEnvelope
	if err := boundary.DecodeExactJSON(raw, &envelope); err != nil {
		return errors.New("Redis TLS handoff envelope is not exact JSON")
	}
	canonicalEnvelope, err := json.Marshal(envelope)
	if err != nil || !bytes.Equal(canonicalEnvelope, raw) || envelope.Schema != "fs2-serve.nebius.ai/edge-rate-limit-redis-tls-handoff-envelope/v1" ||
		envelope.Algorithm != "ed25519" || envelope.Issuer != policy.RedisTLSHandoffIssuer || envelope.KeyID != policy.RedisTLSHandoffKeyID {
		return errors.New("Redis TLS handoff envelope is non-canonical or signed by an unaccepted identity")
	}
	payloadRaw, err := base64.StdEncoding.Strict().DecodeString(envelope.PayloadBase64)
	if err != nil || base64.StdEncoding.EncodeToString(payloadRaw) != envelope.PayloadBase64 || digestBytes(payloadRaw) != envelope.PayloadSHA256 {
		return errors.New("Redis TLS handoff payload does not match its exact bytes")
	}
	publicKey, keyErr := base64.StdEncoding.Strict().DecodeString(policy.RedisTLSHandoffPublicKey)
	signature, signatureErr := base64.RawURLEncoding.Strict().DecodeString(envelope.Signature)
	message := bytes.Join([][]byte{
		[]byte(envelope.Schema),
		[]byte(envelope.Issuer),
		[]byte(envelope.KeyID),
		[]byte(envelope.PayloadSHA256),
		payloadRaw,
	}, []byte("\n"))
	if keyErr != nil || signatureErr != nil || len(publicKey) != ed25519.PublicKeySize || len(signature) != ed25519.SignatureSize ||
		!ed25519.Verify(ed25519.PublicKey(publicKey), message, signature) {
		return errors.New("Redis TLS handoff signature is invalid")
	}
	var handoff redisTLSHandoff
	if err := boundary.DecodeExactJSON(payloadRaw, &handoff); err != nil {
		return errors.New("Redis TLS handoff payload is not exact JSON")
	}
	canonicalPayload, err := json.Marshal(handoff)
	if err != nil || !bytes.Equal(canonicalPayload, payloadRaw) || handoff.Schema != "fs2-serve.nebius.ai/edge-rate-limit-redis-tls-handoff/v1" {
		return errors.New("Redis TLS handoff payload is not canonical")
	}
	issuedAt, issueErr := time.Parse(time.RFC3339, handoff.IssuedAt)
	expiresAt, expiryErr := time.Parse(time.RFC3339, handoff.ExpiresAt)
	observedAt, observedErr := time.Parse(time.RFC3339, collectedAt)
	if issueErr != nil || expiryErr != nil || observedErr != nil || issuedAt.Nanosecond() != 0 || expiresAt.Nanosecond() != 0 ||
		observedAt.Nanosecond() != 0 || observedAt.Before(issuedAt) || !observedAt.Before(expiresAt) ||
		expiresAt.Sub(issuedAt) > time.Duration(policy.RedisTLSMaximumLifetimeSeconds)*time.Second ||
		expiresAt.Sub(observedAt) < time.Duration(policy.RedisTLSMinimumRemainingSeconds)*time.Second ||
		handoff.Generation < policy.RedisTLSMinimumHandoffGeneration || !isDigestText(handoff.PredecessorSHA256) {
		return errors.New("Redis TLS handoff is stale, rolled back or outside its accepted rotation window")
	}
	if handoff.IssuerGroup != policy.RedisTLSIssuerGroup || handoff.IssuerKind != policy.RedisTLSIssuerKind || handoff.IssuerName != policy.RedisTLSIssuerName ||
		handoff.CARootSPKISHA256 != policy.RedisTLSCARootSPKISHA256 || handoff.ServerSecretName != policy.RedisTLSServerSecretName ||
		handoff.ClientSecretName != policy.RedisTLSClientSecretName || !sameStringSet(handoff.ServerDNSNames, policy.RedisTLSServerDNSNames) ||
		!sameStringSet(handoff.ServerExtendedKeyUsage, []string{"client auth", "server auth"}) ||
		handoff.ClientSPIFFEURI != policy.RedisTLSClientSPIFFEURI || !sameStringSet(handoff.ClientExtendedKeyUsage, []string{"client auth"}) ||
		handoff.MaximumLifetimeSeconds != policy.RedisTLSMaximumLifetimeSeconds || handoff.MinimumRemainingSeconds != policy.RedisTLSMinimumRemainingSeconds {
		return errors.New("Redis TLS handoff differs from the independently accepted issuer, identity, SAN, EKU or rotation contract")
	}
	return nil
}

func validateRedisTLSCertificateEvidence(server inventoryObject, client inventoryObject, collectedAt string, policy EdgeGraphPolicy) error {
	if server.identity.Namespace != policy.RedisServiceNamespace || server.identity.Name != policy.RedisTLSServerSecretName ||
		client.identity.Namespace != policy.RedisServiceNamespace || client.identity.Name != policy.RedisTLSClientSecretName ||
		server.kind != "Secret" || client.kind != "Secret" || stringValue(server.value["type"]) != "kubernetes.io/tls" ||
		stringValue(client.value["type"]) != "kubernetes.io/tls" {
		return errors.New("Redis TLS certificate evidence does not bind the two exact distinct Secret identities")
	}
	observedAt, err := time.Parse(time.RFC3339, collectedAt)
	if err != nil || observedAt.Nanosecond() != 0 {
		return errors.New("Redis TLS certificate evidence collection time is invalid")
	}
	serverRoot, serverChain, err := redisTLSCertificateChain(server.value)
	if err != nil {
		return fmt.Errorf("Redis server/replication certificate: %w", err)
	}
	clientRoot, clientChain, err := redisTLSCertificateChain(client.value)
	if err != nil {
		return fmt.Errorf("rate-limit client certificate: %w", err)
	}
	if !bytes.Equal(serverRoot.Raw, clientRoot.Raw) || digestBytes(serverRoot.RawSubjectPublicKeyInfo) != policy.RedisTLSCARootSPKISHA256 ||
		!serverRoot.IsCA || !serverRoot.BasicConstraintsValid || serverRoot.KeyUsage&x509.KeyUsageCertSign == 0 ||
		serverRoot.CheckSignatureFrom(serverRoot) != nil {
		return errors.New("Redis TLS certificates do not chain to the one exact independently accepted security CA")
	}
	serverLeaf := serverChain[0]
	clientLeaf := clientChain[0]
	if bytes.Equal(serverLeaf.Raw, clientLeaf.Raw) || bytes.Equal(serverLeaf.RawSubjectPublicKeyInfo, clientLeaf.RawSubjectPublicKeyInfo) {
		return errors.New("Redis server/replication and rate-limit client identities reuse a leaf or public key")
	}
	if !sameStringSet(serverLeaf.DNSNames, policy.RedisTLSServerDNSNames) || len(serverLeaf.URIs) != 0 ||
		!exactExtKeyUsages(serverLeaf, x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth) {
		return errors.New("Redis server/replication leaf lacks its exact DNS SAN and serverAuth/clientAuth identity")
	}
	if len(clientLeaf.DNSNames) != 0 || len(clientLeaf.URIs) != 1 || clientLeaf.URIs[0].String() != policy.RedisTLSClientSPIFFEURI ||
		!exactExtKeyUsages(clientLeaf, x509.ExtKeyUsageClientAuth) {
		return errors.New("rate-limit client leaf lacks its exact clientAuth-only SPIFFE identity")
	}
	for _, certificate := range []*x509.Certificate{serverLeaf, clientLeaf} {
		if observedAt.Before(certificate.NotBefore) || !observedAt.Before(certificate.NotAfter) ||
			certificate.NotAfter.Sub(certificate.NotBefore) > time.Duration(policy.RedisTLSMaximumLifetimeSeconds)*time.Second ||
			certificate.NotAfter.Sub(observedAt) < time.Duration(policy.RedisTLSMinimumRemainingSeconds)*time.Second {
			return errors.New("Redis TLS leaf is outside the accepted current rotation window")
		}
	}
	if err := verifyRedisTLSChain(serverChain, serverRoot, observedAt, x509.ExtKeyUsageServerAuth); err != nil {
		return err
	}
	if err := verifyRedisTLSChain(serverChain, serverRoot, observedAt, x509.ExtKeyUsageClientAuth); err != nil {
		return err
	}
	if err := verifyRedisTLSChain(clientChain, clientRoot, observedAt, x509.ExtKeyUsageClientAuth); err != nil {
		return err
	}
	return nil
}

func redisTLSCertificateChain(value map[string]any) (*x509.Certificate, []*x509.Certificate, error) {
	evidence, ok := object(value["certificate"])
	if !ok || !onlyObjectKeys(evidence, "ca_pem_base64", "leaf_pem_base64") {
		return nil, nil, errors.New("public certificate projection is absent or contains unapproved fields")
	}
	roots, err := parseCertificatePEMBase64(stringValue(evidence["ca_pem_base64"]))
	if err != nil || len(roots) != 1 {
		return nil, nil, errors.New("CA projection is not one canonical certificate")
	}
	chain, err := parseCertificatePEMBase64(stringValue(evidence["leaf_pem_base64"]))
	if err != nil || len(chain) == 0 {
		return nil, nil, errors.New("leaf projection is not a canonical certificate chain")
	}
	return roots[0], chain, nil
}

func parseCertificatePEMBase64(encoded string) ([]*x509.Certificate, error) {
	raw, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil || base64.StdEncoding.EncodeToString(raw) != encoded || len(raw) == 0 || len(raw) > 4*1024*1024 {
		return nil, errors.New("certificate projection is not canonical bounded base64")
	}
	certificates := []*x509.Certificate{}
	for len(raw) > 0 {
		block, trailing := pem.Decode(raw)
		if block == nil || block.Type != "CERTIFICATE" || len(block.Headers) != 0 {
			return nil, errors.New("certificate projection contains a malformed PEM object")
		}
		certificate, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, errors.New("certificate projection contains invalid DER")
		}
		certificates = append(certificates, certificate)
		raw = trailing
	}
	return certificates, nil
}

func verifyRedisTLSChain(chain []*x509.Certificate, root *x509.Certificate, at time.Time, usage x509.ExtKeyUsage) error {
	roots := x509.NewCertPool()
	roots.AddCert(root)
	intermediates := x509.NewCertPool()
	for _, certificate := range chain[1:] {
		if !certificate.IsCA || !certificate.BasicConstraintsValid || certificate.KeyUsage&x509.KeyUsageCertSign == 0 {
			return errors.New("Redis TLS chain contains an invalid intermediate CA")
		}
		intermediates.AddCert(certificate)
	}
	verified, err := chain[0].Verify(x509.VerifyOptions{Roots: roots, Intermediates: intermediates, CurrentTime: at, KeyUsages: []x509.ExtKeyUsage{usage}})
	if err != nil || len(verified) != 1 || !bytes.Equal(verified[0][len(verified[0])-1].Raw, root.Raw) {
		return errors.New("Redis TLS leaf does not form one exact enrolled issuer chain")
	}
	return nil
}

func exactExtKeyUsages(certificate *x509.Certificate, expected ...x509.ExtKeyUsage) bool {
	if certificate == nil || len(certificate.ExtKeyUsage) != len(expected) {
		return false
	}
	actual := map[x509.ExtKeyUsage]bool{}
	for _, usage := range certificate.ExtKeyUsage {
		if actual[usage] {
			return false
		}
		actual[usage] = true
	}
	for _, usage := range expected {
		if !actual[usage] {
			return false
		}
	}
	return true
}

func workloadAnnotationEquals(value map[string]any, name string, expected string) bool {
	spec, _ := object(value["spec"])
	template, _ := object(spec["template"])
	metadata, _ := object(template["metadata"])
	annotations, _ := object(metadata["annotations"])
	return expected != "" && stringValue(annotations[name]) == expected
}

func redisBootstrapExecutionIsExact(podSpec map[string]any) bool {
	initContainers := array(podSpec["initContainers"])
	containers := array(podSpec["containers"])
	if len(initContainers) != 1 || len(containers) != 2 {
		return false
	}
	configure, _ := object(initContainers[0])
	if stringValue(configure["name"]) != "configure" || !sameOrderedStrings(stringValues(array(configure["command"])), []string{"/bin/sh", "/bootstrap/configure.sh"}) ||
		len(array(configure["args"])) != 0 || !exactVolumeMounts(configure, map[string]volumeMountContract{
			"bootstrap": {path: "/bootstrap", readOnly: true},
			"configuration": {path: "/work"},
			"data": {path: "/data"},
		}) {
		return false
	}
	redisFound := false
	sentinelFound := false
	for _, rawContainer := range containers {
		container, _ := object(rawContainer)
		switch stringValue(container["name"]) {
		case "redis":
			redisFound = sameOrderedStrings(stringValues(array(container["command"])), []string{"/bin/sh", "/bootstrap/run-redis.sh"}) &&
				len(array(container["args"])) == 0 && exactVolumeMounts(container, map[string]volumeMountContract{
					"bootstrap": {path: "/bootstrap", readOnly: true},
					"configuration": {path: "/work"},
					"data": {path: "/data"},
					"redis-server-tls": {path: "/redis-server-tls", readOnly: true},
				})
		case "sentinel":
			sentinelFound = sameOrderedStrings(stringValues(array(container["command"])), []string{"redis-server", "/work/sentinel.conf", "--sentinel"}) &&
				len(array(container["args"])) == 0 && exactVolumeMounts(container, map[string]volumeMountContract{
					"bootstrap": {path: "/bootstrap", readOnly: true},
					"configuration": {path: "/work"},
					"redis-server-tls": {path: "/redis-server-tls", readOnly: true},
				})
		default:
			return false
		}
	}
	return redisFound && sentinelFound
}

type volumeMountContract struct {
	path string
	readOnly bool
}

func exactVolumeMounts(container map[string]any, expected map[string]volumeMountContract) bool {
	mounts := array(container["volumeMounts"])
	if len(mounts) != len(expected) {
		return false
	}
	seen := map[string]bool{}
	for _, rawMount := range mounts {
		mount, ok := object(rawMount)
		name := stringValue(mount["name"])
		contract, accepted := expected[name]
		if !ok || !onlyObjectKeys(mount, "name", "mountPath", "readOnly") || !accepted || seen[name] ||
			stringValue(mount["mountPath"]) != contract.path || boolValue(mount["readOnly"]) != contract.readOnly {
			return false
		}
		seen[name] = true
	}
	return len(seen) == len(expected)
}

func exactTLSSecretItems(secret map[string]any) bool {
	if !onlyObjectKeys(secret, "secretName", "defaultMode", "items", "optional") || boolValue(secret["optional"]) {
		return false
	}
	expected := map[string]string{"ca.crt": "ca.crt", "tls.crt": "tls.crt", "tls.key": "tls.key"}
	items := array(secret["items"])
	if len(items) != len(expected) {
		return false
	}
	seen := map[string]bool{}
	for _, rawItem := range items {
		item, ok := object(rawItem)
		key := stringValue(item["key"])
		if !ok || !onlyObjectKeys(item, "key", "path", "mode") || item["mode"] != nil || expected[key] != stringValue(item["path"]) || seen[key] {
			return false
		}
		seen[key] = true
	}
	return len(seen) == len(expected)
}

func redisVolumeBackingIsExact(podSpec map[string]any, policy EdgeGraphPolicy) bool {
	volumes := array(podSpec["volumes"])
	if len(volumes) != 4 {
		return false
	}
	seen := map[string]bool{}
	for _, rawVolume := range volumes {
		volume, ok := object(rawVolume)
		name := stringValue(volume["name"])
		if !ok || seen[name] {
			return false
		}
		switch name {
		case "bootstrap":
			if !onlyObjectKeys(volume, "name", "configMap") {
				return false
			}
			configMap, exists := object(volume["configMap"])
			mode, validMode := number(configMap["defaultMode"])
			if !exists || !onlyObjectKeys(configMap, "name", "defaultMode", "items", "optional") ||
				stringValue(configMap["name"]) != policy.RedisBootstrapConfigMapName || !validMode || mode != 0444 ||
				len(array(configMap["items"])) != 0 || boolValue(configMap["optional"]) {
				return false
			}
		case "configuration":
			emptyDir, exists := object(volume["emptyDir"])
			if !exists || !onlyObjectKeys(volume, "name", "emptyDir") || !onlyObjectKeys(emptyDir, "sizeLimit", "medium") ||
				stringValue(emptyDir["sizeLimit"]) != "16Mi" || stringValue(emptyDir["medium"]) != "" {
				return false
			}
		case "data":
			emptyDir, exists := object(volume["emptyDir"])
			if !exists || !onlyObjectKeys(volume, "name", "emptyDir") || !onlyObjectKeys(emptyDir, "sizeLimit", "medium") ||
				stringValue(emptyDir["sizeLimit"]) != "512Mi" || stringValue(emptyDir["medium"]) != "" {
				return false
			}
		case "redis-server-tls":
			secret, exists := object(volume["secret"])
			mode, validMode := number(secret["defaultMode"])
			if !exists || !onlyObjectKeys(volume, "name", "secret") || stringValue(secret["secretName"]) != policy.RedisTLSServerSecretName ||
				!validMode || mode != 0440 || !exactTLSSecretItems(secret) {
				return false
			}
		default:
			return false
		}
		seen[name] = true
	}
	return len(seen) == 4
}

func redisTLSProbesAreExact(podSpec map[string]any) bool {
	redisReady := []string{"/bin/sh", "/bootstrap/ready.sh"}
	redisLive := []string{"/bin/sh", "/bootstrap/ping-redis.sh"}
	sentinelProbe := []string{"/bin/sh", "/bootstrap/ping-sentinel.sh"}
	matched := 0
	for _, rawContainer := range array(podSpec["containers"]) {
		container, _ := object(rawContainer)
		readiness, _ := object(container["readinessProbe"])
		readinessExec, _ := object(readiness["exec"])
		liveness, _ := object(container["livenessProbe"])
		livenessExec, _ := object(liveness["exec"])
		switch stringValue(container["name"]) {
		case "redis":
			if !sameOrderedStrings(stringValues(array(readinessExec["command"])), redisReady) || !sameOrderedStrings(stringValues(array(livenessExec["command"])), redisLive) {
				return false
			}
			matched++
		case "sentinel":
			if !sameOrderedStrings(stringValues(array(readinessExec["command"])), sentinelProbe) || !sameOrderedStrings(stringValues(array(livenessExec["command"])), sentinelProbe) {
				return false
			}
			matched++
		}
	}
	return matched == 2
}

func containerHasReadOnlyMount(container map[string]any, name string, path string) bool {
	for _, rawMount := range array(container["volumeMounts"]) {
		mount, _ := object(rawMount)
		if stringValue(mount["name"]) == name && stringValue(mount["mountPath"]) == path && boolValue(mount["readOnly"]) {
			return true
		}
	}
	return false
}

func stringValues(values []any) []string {
	result := make([]string, 0, len(values))
	for _, value := range values {
		text, ok := value.(string)
		if !ok {
			return nil
		}
		result = append(result, text)
	}
	return result
}

func sameOrderedStrings(left []string, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func validateEdgeNetworkPolicies(inventory *semanticInventory, policies []inventoryObject, contracts []NetworkPolicySpecContract, edgePolicy EdgeGraphPolicy, workloads ...inventoryObject) error {
	if len(policies) == 0 { return errors.New("edge graph lacks accepted NetworkPolicy isolation") }
	accepted := map[string]bool{}
	contractByPolicy := map[string]string{}
	for _, contract := range contracts {
		key := contract.Namespace+"\x00"+contract.Name
		if contract.Namespace == "" || contract.Name == "" || !isDigestText(contract.SpecSHA256) || contractByPolicy[key] != "" { return errors.New("edge NetworkPolicy spec contracts are empty, duplicated or malformed") }
		contractByPolicy[key] = contract.SpecSHA256
	}
	for _, policy := range policies {
		key := policy.identity.Namespace+"\x00"+policy.identity.Name
		spec, ok := object(policy.value["spec"])
		specRaw, err := json.Marshal(spec)
		if !ok || err != nil || accepted[key] || contractByPolicy[key] == "" || digestBytes(specRaw) != contractByPolicy[key] { return errors.New("edge NetworkPolicy differs from its independently accepted exact spec") }
		accepted[key] = true
	}
	if len(accepted) != len(contractByPolicy) { return errors.New("edge NetworkPolicy spec contracts contain missing or extra objects") }
	if len(workloads) != 3 {
		return errors.New("edge NetworkPolicy validation lacks the exact controller, rate-limit and Redis workloads")
	}
	selectedPolicies := map[string]bool{}
	// The source-controlled foundation owns the RLS and Redis policies, so both
	// workloads must have explicit default-deny ingress and egress. The retained
	// control-plane chart policy is the exact ingress-only controller isolation
	// owner; validateControllerXDSIngress requires its proxy and RLS xDS peers.
	for _, workload := range workloads[1:] {
		workloadSpec, _ := object(workload.value["spec"])
		template, _ := object(workloadSpec["template"])
		metadata, _ := object(template["metadata"])
		labels, _ := object(metadata["labels"])
		ingressSelected := false
		egressSelected := false
		ingressDefaultDeny := false
		egressDefaultDeny := false
		for _, policy := range inventory.byCollection["apiserver-networkpolicies"] {
			if policy.identity.Namespace != workload.identity.Namespace { continue }
			spec, _ := object(policy.value["spec"])
			selector, _ := object(spec["podSelector"])
			matches, selectorErr := exactLabelSelectorMatches(selector, labels)
			if selectorErr != nil { return fmt.Errorf("NetworkPolicy %s/%s selector is invalid: %w", policy.identity.Namespace, policy.identity.Name, selectorErr) }
			if !matches { continue }
			policyKey := policy.identity.Namespace+"\x00"+policy.identity.Name
			if !accepted[policyKey] { return fmt.Errorf("unaccounted NetworkPolicy %s/%s additively broadens an edge workload policy union", policy.identity.Namespace, policy.identity.Name) }
			selectedPolicies[policyKey] = true
			rawTypes := array(spec["policyTypes"])
			types := stringSet(rawTypes)
			if len(types) != len(rawTypes) || len(types) == 0 || len(types) > 2 || len(types) != boolCount(types["Ingress"])+boolCount(types["Egress"]) { return errors.New("NetworkPolicy policyTypes is not the exact Ingress/Egress set") }
			if types["Ingress"] {
				rules := array(spec["ingress"])
				if len(rules) == 0 { ingressDefaultDeny = true } else if err := validateNetworkPolicyRules(rules, "from"); err != nil { return fmt.Errorf("NetworkPolicy %s/%s ingress: %w", policy.identity.Namespace, policy.identity.Name, err) }
				ingressSelected = true
			}
			if types["Egress"] {
				rules := array(spec["egress"])
				if len(rules) == 0 { egressDefaultDeny = true } else if err := validateNetworkPolicyRules(rules, "to"); err != nil { return fmt.Errorf("NetworkPolicy %s/%s egress: %w", policy.identity.Namespace, policy.identity.Name, err) }
				egressSelected = true
			}
		}
		if !ingressSelected || !egressSelected || !ingressDefaultDeny || !egressDefaultDeny { return fmt.Errorf("edge workload %s/%s lacks exact additive default-deny ingress and egress NetworkPolicy isolation", workload.identity.Namespace, workload.identity.Name) }
	}
	if err := validateRateLimitPolicyUnion(inventory, accepted, workloads[1], edgePolicy); err != nil {
		return err
	}
	if err := validateControllerXDSIngress(inventory, accepted, selectedPolicies, workloads[0], edgePolicy); err != nil {
		return err
	}
	if len(selectedPolicies) != len(accepted) { return errors.New("accepted NetworkPolicy union includes an unrelated non-edge selector") }
	return nil
}

func validateRateLimitPolicyUnion(inventory *semanticInventory, accepted map[string]bool, workload inventoryObject, policy EdgeGraphPolicy) error {
	workloadSpec, _ := object(workload.value["spec"])
	template, _ := object(workloadSpec["template"])
	metadata, _ := object(template["metadata"])
	labels, _ := object(metadata["labels"])
	required := map[string]int{
		"ingress-envoy": 0,
		"ingress-metrics": 0,
		"egress-redis": 0,
		"egress-dns": 0,
		"egress-xds": 0,
	}
	for _, item := range inventory.byCollection["apiserver-networkpolicies"] {
		if item.identity.Namespace != workload.identity.Namespace || !accepted[item.identity.Namespace+"\x00"+item.identity.Name] {
			continue
		}
		spec, _ := object(item.value["spec"])
		selector, _ := object(spec["podSelector"])
		matches, err := exactLabelSelectorMatches(selector, labels)
		if err != nil || !matches {
			continue
		}
		for _, rawRule := range array(spec["ingress"]) {
			rule, _ := object(rawRule)
			switch {
			case exactNetworkPolicyRule(rule, "from", map[string]string{"kubernetes.io/metadata.name": policy.EnvoyProxyNamespace}, policy.EnvoyProxyPodSelector, map[string]int64{"TCP": policy.RateLimitServicePort}):
				required["ingress-envoy"]++
			case exactNetworkPolicyRule(rule, "from", map[string]string{"kubernetes.io/metadata.name": "fs2-observability"}, map[string]string{"app.kubernetes.io/name": "prometheus"}, map[string]int64{"TCP": 19001}):
				required["ingress-metrics"]++
			default:
				return fmt.Errorf("rate-limit NetworkPolicy %s/%s contains an unaccepted ingress peer or port", item.identity.Namespace, item.identity.Name)
			}
		}
		for _, rawRule := range array(spec["egress"]) {
			rule, _ := object(rawRule)
			switch {
			case exactNetworkPolicyRule(rule, "to", nil, policy.RedisPodSelector, map[string]int64{"TCP/6379": 6379, "TCP/26379": 26379}):
				required["egress-redis"]++
			case exactNetworkPolicyRule(rule, "to", map[string]string{"kubernetes.io/metadata.name": "kube-system"}, map[string]string{"k8s-app": "kube-dns"}, map[string]int64{"UDP/53": 53, "TCP/53": 53}):
				required["egress-dns"]++
			case exactNetworkPolicyRule(rule, "to", map[string]string{"kubernetes.io/metadata.name": policy.EnvoyGatewayControllerNamespace}, policy.EnvoyGatewayControllerPodSelector, map[string]int64{"TCP": policy.RateLimitXDSPort}):
				required["egress-xds"]++
			default:
				return fmt.Errorf("rate-limit NetworkPolicy %s/%s contains an unaccepted egress peer or port", item.identity.Namespace, item.identity.Name)
			}
		}
	}
	for name, count := range required {
		if count != 1 {
			return fmt.Errorf("rate-limit NetworkPolicy union requires exactly one %s rule", name)
		}
	}
	return nil
}

func validateControllerXDSIngress(inventory *semanticInventory, accepted map[string]bool, selectedPolicies map[string]bool, workload inventoryObject, policy EdgeGraphPolicy) error {
	workloadSpec, _ := object(workload.value["spec"])
	template, _ := object(workloadSpec["template"])
	metadata, _ := object(template["metadata"])
	labels, _ := object(metadata["labels"])
	proxyMatches := 0
	rateLimitMatches := 0
	isolated := false
	for _, item := range inventory.byCollection["apiserver-networkpolicies"] {
		if item.identity.Namespace != workload.identity.Namespace {
			continue
		}
		spec, _ := object(item.value["spec"])
		selector, _ := object(spec["podSelector"])
		selected, err := exactLabelSelectorMatches(selector, labels)
		if err != nil {
			return fmt.Errorf("NetworkPolicy %s/%s selector is invalid: %w", item.identity.Namespace, item.identity.Name, err)
		}
		if !selected {
			continue
		}
		policyKey := item.identity.Namespace+"\x00"+item.identity.Name
		if !accepted[policyKey] {
			return fmt.Errorf("unaccounted NetworkPolicy %s/%s additively changes the Envoy Gateway controller policy union", item.identity.Namespace, item.identity.Name)
		}
		selectedPolicies[policyKey] = true
		rawTypes := array(spec["policyTypes"])
		types := stringSet(rawTypes)
		if len(rawTypes) != 1 || len(types) != 1 || !types["Ingress"] || len(array(spec["egress"])) != 0 {
			return fmt.Errorf("Envoy Gateway controller NetworkPolicy %s/%s is not exact ingress-only isolation", item.identity.Namespace, item.identity.Name)
		}
		isolated = true
		for _, rawRule := range array(spec["ingress"]) {
			rule, _ := object(rawRule)
			switch {
			case exactNetworkPolicyRule(rule, "from", map[string]string{"kubernetes.io/metadata.name": policy.EnvoyProxyNamespace}, policy.EnvoyProxyPodSelector, map[string]int64{"TCP": 18000}):
				proxyMatches++
			case exactNetworkPolicyRule(rule, "from", map[string]string{"kubernetes.io/metadata.name": policy.RateLimitWorkloadNamespace}, policy.RateLimitPodSelector, map[string]int64{"TCP": policy.RateLimitXDSPort}):
				rateLimitMatches++
			default:
				return fmt.Errorf("Envoy Gateway controller NetworkPolicy %s/%s contains an unaccepted ingress peer or port", item.identity.Namespace, item.identity.Name)
			}
		}
	}
	if !isolated || proxyMatches != 1 || rateLimitMatches != 1 {
		return errors.New("Envoy Gateway controller requires one exact proxy/18000 rule and one exact rate-limit/18001 rule under ingress isolation")
	}
	return nil
}

func exactNetworkPolicyRule(rule map[string]any, peerField string, namespaceLabels map[string]string, podLabels map[string]string, expectedPorts map[string]int64) bool {
	if !onlyObjectKeys(rule, peerField, "ports") {
		return false
	}
	peers := array(rule[peerField])
	ports := array(rule["ports"])
	if len(peers) != 1 || len(ports) != len(expectedPorts) {
		return false
	}
	peer, ok := object(peers[0])
	if !ok || !onlyObjectKeys(peer, "namespaceSelector", "podSelector") {
		return false
	}
	namespaceSelector, hasNamespace := object(peer["namespaceSelector"])
	podSelector, hasPod := object(peer["podSelector"])
	if hasNamespace != (namespaceLabels != nil) || hasPod != (podLabels != nil) ||
		hasNamespace && !exactMatchLabelsSelector(namespaceSelector, namespaceLabels) ||
		hasPod && !exactMatchLabelsSelector(podSelector, podLabels) {
		return false
	}
	seen := map[string]bool{}
	for _, rawPort := range ports {
		port, ok := object(rawPort)
		if !ok || !onlyObjectKeys(port, "protocol", "port") {
			return false
		}
		protocol := stringValue(port["protocol"])
		value, valid := number(port["port"])
		key := protocol
		if len(expectedPorts) > 1 {
			key = fmt.Sprintf("%s/%d", protocol, value)
		}
		if !valid || seen[key] || expectedPorts[key] != value {
			return false
		}
		seen[key] = true
	}
	return len(seen) == len(expectedPorts)
}

func exactMatchLabelsSelector(selector map[string]any, expected map[string]string) bool {
	if !onlyObjectKeys(selector, "matchLabels") {
		return false
	}
	labels, ok := object(selector["matchLabels"])
	if !ok || len(labels) != len(expected) {
		return false
	}
	for name, value := range expected {
		if stringValue(labels[name]) != value {
			return false
		}
	}
	return true
}

func exactLabelSelectorMatches(selector map[string]any, labels map[string]any) (bool, error) {
	if !onlyObjectKeys(selector, "matchLabels", "matchExpressions") { return false, errors.New("label selector carries unknown fields") }
	matchLabels, _ := object(selector["matchLabels"])
	expressions := array(selector["matchExpressions"])
	if len(matchLabels) == 0 && len(expressions) == 0 { return false, errors.New("empty selector is forbidden for edge isolation") }
	for key, expected := range matchLabels {
		expectedText, expectedOK := expected.(string)
		actualText, actualOK := labels[key].(string)
		if key == "" || !expectedOK || expectedText == "" { return false, errors.New("matchLabels is not an exact string map") }
		if !actualOK || actualText != expectedText { return false, nil }
	}
	for _, rawExpression := range expressions {
		expression, ok := object(rawExpression)
		if !ok || !onlyObjectKeys(expression, "key", "operator", "values") { return false, errors.New("matchExpression is not an exact object") }
		key := stringValue(expression["key"])
		operator := stringValue(expression["operator"])
		rawValues := array(expression["values"])
		values := stringSet(rawValues)
		if len(values) != len(rawValues) { return false, errors.New("matchExpression values are empty, duplicated or non-string") }
		if key == "" { return false, errors.New("matchExpression key is empty") }
		label, exists := labels[key]
		labelValue := stringValue(label)
		switch operator {
		case "In": if len(values) == 0 { return false, errors.New("In expression has no values") }; if !exists || !values[labelValue] { return false, nil }
		case "NotIn": if len(values) == 0 { return false, errors.New("NotIn expression has no values") }; if exists && values[labelValue] { return false, nil }
		case "Exists": if len(values) != 0 { return false, errors.New("Exists expression has values") }; if !exists { return false, nil }
		case "DoesNotExist": if len(values) != 0 { return false, errors.New("DoesNotExist expression has values") }; if exists { return false, nil }
		default: return false, errors.New("matchExpression operator is unsupported")
		}
	}
	return true, nil
}

func validateNetworkPolicyRules(rules []any, peerField string) error {
	if len(rules) == 0 { return errors.New("allow-rule validation requires at least one rule") }
	for _, rawRule := range rules {
		rule, ok := object(rawRule)
		if !ok || !onlyObjectKeys(rule, peerField, "ports") { return errors.New("policy rule is not an exact object") }
		peers := array(rule[peerField])
		ports := array(rule["ports"])
		if len(peers) == 0 || len(ports) == 0 { return errors.New("policy rule has a wildcard peer or port") }
		for _, rawPeer := range peers {
			peer, ok := object(rawPeer)
			if !ok || len(peer) == 0 || !onlyObjectKeys(peer, "podSelector", "namespaceSelector", "ipBlock") { return errors.New("policy peer is empty or has unknown fields") }
			podSelector, hasPod := object(peer["podSelector"])
			namespaceSelector, hasNamespace := object(peer["namespaceSelector"])
			ipBlock, hasIP := object(peer["ipBlock"])
			if hasIP && (hasPod || hasNamespace) || !hasIP && !hasPod && !hasNamespace { return errors.New("policy peer mixes or omits exact selector forms") }
			if hasPod { if _, err := exactLabelSelectorMatches(podSelector, map[string]any{}); err != nil { return err } }
			if hasNamespace { if _, err := exactLabelSelectorMatches(namespaceSelector, map[string]any{}); err != nil { return err } }
			if hasIP {
				if !onlyObjectKeys(ipBlock, "cidr", "except") { return errors.New("policy IPBlock has unknown fields") }
				cidr := stringValue(ipBlock["cidr"])
				_, network, parseErr := net.ParseCIDR(cidr)
				if parseErr != nil || network.String() != cidr || cidr == "0.0.0.0/0" || cidr == "::/0" { return errors.New("policy peer has a non-canonical or universal CIDR") }
				for _, excluded := range array(ipBlock["except"]) {
					excludedText := stringValue(excluded)
					excludedIP, excludedNetwork, excludedErr := net.ParseCIDR(excludedText)
					if excludedErr != nil || excludedNetwork.String() != excludedText || !network.Contains(excludedIP) { return errors.New("policy IPBlock has a non-canonical out-of-range exception") }
				}
			}
		}
		for _, rawPort := range ports {
			port, ok := object(rawPort)
			if !ok || !onlyObjectKeys(port, "protocol", "port", "endPort") { return errors.New("policy port is not an exact object") }
			protocol := stringValue(port["protocol"])
			value, valid := number(port["port"])
			endPort, hasEnd := number(port["endPort"])
			if (protocol != "TCP" && protocol != "UDP" && protocol != "SCTP") || !valid || value < 1 || value > 65535 || hasEnd && (endPort < value || endPort > 65535) { return errors.New("policy port is not one exact bounded numeric range") }
		}
	}
	return nil
}

func boolCount(value bool) int { if value { return 1 }; return 0 }

func boolValue(value any) bool { result, _ := value.(bool); return result }

func explicitBool(value any, expected bool) bool { result, ok := value.(bool); return ok && result == expected }

func onlyObjectKeys(value map[string]any, allowed ...string) bool {
	accepted := map[string]bool{}
	for _, key := range allowed { accepted[key] = true }
	for key := range value { if !accepted[key] { return false } }
	return true
}

func nestedObject(value map[string]any, key string) map[string]any {
	result, _ := object(value[key])
	return result
}

func validatePDB(value map[string]any) error {
	spec, _ := object(value["spec"])
	minimum, valid := number(spec["minAvailable"])
	selector, selectorOK := object(spec["selector"])
	matchLabels, labelsOK := object(selector["matchLabels"])
	if !valid || minimum < 1 || !selectorOK || !labelsOK || len(matchLabels) == 0 {
		return errors.New("edge PodDisruptionBudget lacks minAvailable and an exact nonempty selector")
	}
	return nil
}

func replicas(value map[string]any) int64 {
	spec, _ := object(value["spec"])
	result, _ := number(spec["replicas"])
	return result
}

func serviceSelectsWorkload(service map[string]any, workload map[string]any) bool {
	serviceSpec, _ := object(service["spec"])
	selector, selectorOK := object(serviceSpec["selector"])
	workloadSpec, _ := object(workload["spec"])
	template, _ := object(workloadSpec["template"])
	metadata, _ := object(template["metadata"])
	labels, labelsOK := object(metadata["labels"])
	if !selectorOK || !labelsOK || len(selector) == 0 {
		return false
	}
	for key, expected := range selector {
		if labels[key] != expected {
			return false
		}
	}
	return true
}

func deriveResourceGuards(namespaces []string, roots []boundary.ObjectIdentity) []boundary.ResourceGuard {
	guards := []boundary.ResourceGuard{
		{APIGroup: "", Resource: "nodes", Subresources: []string{"", "proxy"}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CONNECT", "CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-node-authority-transition"},
		{APIGroup: "rbac.authorization.k8s.io", Resource: "clusterroles", Subresources: []string{""}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "rbac.authorization.k8s.io", Resource: "clusterrolebindings", Subresources: []string{""}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "admissionregistration.k8s.io", Resource: "validatingwebhookconfigurations", Subresources: []string{""}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "admissionregistration.k8s.io", Resource: "mutatingwebhookconfigurations", Subresources: []string{""}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "admissionregistration.k8s.io", Resource: "validatingadmissionpolicies", Subresources: []string{""}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "admissionregistration.k8s.io", Resource: "validatingadmissionpolicybindings", Subresources: []string{""}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "admissionregistration.k8s.io", Resource: "mutatingadmissionpolicies", Subresources: []string{""}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "admissionregistration.k8s.io", Resource: "mutatingadmissionpolicybindings", Subresources: []string{""}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "certificates.k8s.io", Resource: "certificatesigningrequests", Subresources: []string{""}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CREATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "certificates.k8s.io", Resource: "certificatesigningrequests", Subresources: []string{"approval"}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"UPDATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "certificates.k8s.io", Resource: "certificatesigningrequests", Subresources: []string{"status"}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"UPDATE"}, SemanticGuard: "exact-object-transition"},
		{APIGroup: "security.fs2.nebius.ai", Resource: "publicedgenodeauthorityapprovals", Subresources: []string{""}, Namespaces: []string{"*"}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
	}
	for _, namespace := range namespaces {
		guards = append(guards,
			boundary.ResourceGuard{APIGroup: "rbac.authorization.k8s.io", Resource: "roles", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
			boundary.ResourceGuard{APIGroup: "rbac.authorization.k8s.io", Resource: "rolebindings", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition"},
			boundary.ResourceGuard{APIGroup: "", Resource: "serviceaccounts", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "inspect-controller-credential-reachability"},
			boundary.ResourceGuard{APIGroup: "", Resource: "serviceaccounts", Subresources: []string{"token"}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE"}, SemanticGuard: "exact-object-transition"},
			boundary.ResourceGuard{APIGroup: "", Resource: "secrets", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "UPDATE"}, SemanticGuard: "classify-controller-credential-secret"},
			boundary.ResourceGuard{APIGroup: "", Resource: "pods", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "inspect-controller-credential-reachability"},
			boundary.ResourceGuard{APIGroup: "", Resource: "pods", Subresources: []string{"attach", "exec", "portforward"}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CONNECT"}, SemanticGuard: "exact-object-transition"},
			boundary.ResourceGuard{APIGroup: "", Resource: "pods", Subresources: []string{"ephemeralcontainers"}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"UPDATE"}, SemanticGuard: "inspect-controller-credential-reachability"},
			boundary.ResourceGuard{APIGroup: "", Resource: "replicationcontrollers", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "inspect-controller-credential-reachability"},
			boundary.ResourceGuard{APIGroup: "apps", Resource: "deployments", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "inspect-controller-credential-reachability"},
			boundary.ResourceGuard{APIGroup: "apps", Resource: "daemonsets", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "inspect-controller-credential-reachability"},
			boundary.ResourceGuard{APIGroup: "apps", Resource: "statefulsets", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "inspect-controller-credential-reachability"},
			boundary.ResourceGuard{APIGroup: "apps", Resource: "replicasets", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "inspect-controller-credential-reachability"},
			boundary.ResourceGuard{APIGroup: "batch", Resource: "jobs", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "inspect-controller-credential-reachability"},
			boundary.ResourceGuard{APIGroup: "batch", Resource: "cronjobs", Subresources: []string{""}, Namespaces: []string{namespace}, Names: []string{"*"}, Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "inspect-controller-credential-reachability"},
		)
	}
	for _, root := range roots {
		namespace := root.Namespace
		if namespace == "" {
			namespace = "*"
		}
		guards = append(guards, boundary.ResourceGuard{
			APIGroup: root.APIGroup, Resource: root.Resource, Subresources: []string{""},
			Namespaces: []string{namespace}, Names: []string{root.Name},
			Operations: []string{"CREATE", "DELETE", "UPDATE"}, SemanticGuard: "exact-object-transition",
		})
	}
	for index := range guards {
		sort.Strings(guards[index].Subresources)
		sort.Strings(guards[index].Namespaces)
		sort.Strings(guards[index].Names)
		sort.Strings(guards[index].Operations)
	}
	sort.Slice(guards, func(left int, right int) bool {
		leftRaw, _ := json.Marshal(guards[left])
		rightRaw, _ := json.Marshal(guards[right])
		return string(leftRaw) < string(rightRaw)
	})
	result := guards[:0]
	prior := ""
	for _, guard := range guards {
		raw, _ := json.Marshal(guard)
		if string(raw) != prior {
			result = append(result, guard)
			prior = string(raw)
		}
	}
	return result
}

func mergeIdentities(collections ...[]boundary.ObjectIdentity) []boundary.ObjectIdentity {
	byKey := map[string]boundary.ObjectIdentity{}
	for _, collection := range collections {
		for _, identity := range collection {
			key := identity.APIGroup+"\x00"+identity.APIVersion+"\x00"+identity.Resource+"\x00"+identity.Namespace+"\x00"+identity.Name
			if existing, present := byKey[key]; present && existing != identity {
				continue
			}
			byKey[key] = identity
		}
	}
	result := make([]boundary.ObjectIdentity, 0, len(byKey))
	for _, identity := range byKey {
		result = append(result, identity)
	}
	return sortIdentities(result)
}

func sortIdentities(values []boundary.ObjectIdentity) []boundary.ObjectIdentity {
	result := append([]boundary.ObjectIdentity(nil), values...)
	sort.Slice(result, func(left int, right int) bool {
		first, _ := json.Marshal(result[left])
		second, _ := json.Marshal(result[right])
		return string(first) < string(second)
	})
	return result
}

func uniqueSortedStrings(values []string) []string {
	set := map[string]struct{}{}
	for _, value := range values {
		if value != "" {
			set[value] = struct{}{}
		}
	}
	result := make([]string, 0, len(set))
	for value := range set {
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}
