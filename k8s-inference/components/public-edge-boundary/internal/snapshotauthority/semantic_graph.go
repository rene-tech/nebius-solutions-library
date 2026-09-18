package snapshotauthority

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"sort"
	"strconv"
	"strings"

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
		!gatewayHasListeners(gateway.value, policy.HTTPListenerName, policy.HTTPSListenerName) {
		return nil, errors.New("public Gateway does not expose the exact accepted HTTP and HTTPS listeners")
	}
	httpPolicy, err := inventory.require(ObjectReference{"apiserver-client-traffic-policies", policy.Namespace, policy.HTTPClientTrafficPolicyName})
	if err != nil || validateClientTrafficPolicy(httpPolicy.value, policy.GatewayName, policy.HTTPListenerName) != nil {
		return nil, errors.New("public HTTP listener ClientTrafficPolicy is absent or unsafe")
	}
	httpsPolicy, err := inventory.require(ObjectReference{"apiserver-client-traffic-policies", policy.Namespace, policy.HTTPSClientTrafficPolicyName})
	if err != nil || validateClientTrafficPolicy(httpsPolicy.value, policy.GatewayName, policy.HTTPSListenerName) != nil {
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
		{"apiserver-poddisruptionbudgets", policy.Namespace, policy.ControlPlanePDBName},
		{"apiserver-poddisruptionbudgets", policy.RateLimitPDBNamespace, policy.RateLimitPDBName},
		{"apiserver-poddisruptionbudgets", policy.RedisPDBNamespace, policy.RedisPDBName},
		{"apiserver-deployments", policy.RateLimitWorkloadNamespace, policy.RateLimitWorkloadName},
		{"apiserver-statefulsets", policy.RedisWorkloadNamespace, policy.RedisWorkloadName},
		{"apiserver-services", policy.RateLimitServiceNamespace, policy.RateLimitServiceName},
		{"apiserver-services", policy.RedisServiceNamespace, policy.RedisServiceName},
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
	// Fixed entries 7..9 are PDBs and 6,10,11 are their exact workloads.
	if err := validatePDBSelectsWorkload(objects[7].value, objects[6].value); err != nil {
		return nil, err
	}
	if err := validatePDBSelectsWorkload(objects[8].value, objects[10].value); err != nil {
		return nil, err
	}
	if err := validatePDBSelectsWorkload(objects[9].value, objects[11].value); err != nil {
		return nil, err
	}
	if replicas(objects[10].value) < 2 || replicas(objects[11].value) < 3 {
		return nil, errors.New("rate-limit or Redis workload lacks its accepted availability replica floor")
	}
	if !serviceSelectsWorkload(objects[12].value, objects[10].value) || !serviceSelectsWorkload(objects[13].value, objects[11].value) {
		return nil, errors.New("rate-limit or Redis Service selector does not select its exact workload")
	}
	if err := validateServicePort(objects[12].value, policy.RateLimitServicePort); err != nil {
		return nil, fmt.Errorf("rate-limit Service: %w", err)
	}
	if err := validateServicePort(objects[13].value, policy.RedisServicePort); err != nil {
		return nil, fmt.Errorf("Redis Service: %w", err)
	}
	if err := validateRateLimitRedisBinding(objects[10], objects[11], policy); err != nil {
		return nil, err
	}
	baseCount := 14
	routeObjects := objects[baseCount : baseCount+len(policy.PublicRoutes)]
	backendObjects := objects[baseCount+len(policy.PublicRoutes) : baseCount+len(policy.PublicRoutes)+len(policy.PublicBackendServices)]
	networkObjects := objects[baseCount+len(policy.PublicRoutes)+len(policy.PublicBackendServices):]
	if err := validatePublicRoutes(inventory, routeObjects, backendObjects, policy); err != nil {
		return nil, err
	}
	if err := validateNoUnaccountedBackendRoutes(inventory, routeObjects, backendObjects); err != nil { return nil, err }
	if err := validateEdgeNetworkPolicies(inventory, networkObjects, policy.NetworkPolicySpecs, objects[6], objects[10], objects[11]); err != nil {
		return nil, err
	}
	identities := make([]boundary.ObjectIdentity, 0, len(objects))
	for _, item := range objects {
		identities = append(identities, item.identity)
	}
	return sortIdentities(identities), nil
}

func gatewayHasListeners(value map[string]any, expected ...string) bool {
	spec, _ := object(value["spec"])
	actual := map[string]bool{}
	for _, raw := range array(spec["listeners"]) {
		listener, _ := object(raw)
		actual[stringValue(listener["name"])] = true
	}
	for _, name := range expected {
		if name == "" || !actual[name] {
			return false
		}
	}
	return true
}

func validateClientTrafficPolicy(value map[string]any, gateway string, section string) error {
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
	if !valid || limitValue < 1 || !hopsValid || hops < 1 || !streamsValid || streams < 1 ||
		!requestsValid || requests < 1 || requests > 1_000_000 || stringValue(connection["maxConnectionDuration"]) == "" {
		return errors.New("ClientTrafficPolicy connection boundary is incomplete")
	}
	return nil
}

func validateBackendTrafficPolicy(value map[string]any, gateway string, httpListener string, httpsListener string, providerID string) (int64, error) {
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
	if len(rules) < 2 {
		return 0, errors.New("BackendTrafficPolicy omits public or admin rate limits")
	}
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
		selector, _ := object(selectors[0])
		sourceCIDR, _ := object(selector["sourceCIDR"])
		if stringValue(sourceCIDR["type"]) != "Distinct" || stringValue(sourceCIDR["value"]) == "" {
			return 0, errors.New("BackendTrafficPolicy does not isolate source CIDRs")
		}
	}
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
	for _, route := range routes {
		spec, _ := object(route.value["spec"])
		parents := array(spec["parentRefs"])
		parentMatched := false
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
			}
		}
		if !parentMatched { return fmt.Errorf("route %s/%s is not attached to an exact public Gateway listener", route.identity.Namespace, route.identity.Name) }
		backendCount := 0
		for _, rawRule := range array(spec["rules"]) {
			rule, _ := object(rawRule)
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
	return nil
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

func validateServicePort(service map[string]any, expected int64) error {
	if expected < 1 || expected > 65535 { return errors.New("accepted Service port is invalid") }
	spec, _ := object(service["spec"])
	matches := 0
	for _, raw := range array(spec["ports"]) {
		port, _ := object(raw)
		value, valid := number(port["port"])
		if valid && value == expected { matches++ }
	}
	if matches != 1 { return errors.New("Service does not expose one exact accepted port") }
	return nil
}

func validateRateLimitRedisBinding(rateLimit inventoryObject, redis inventoryObject, policy EdgeGraphPolicy) error {
	podSpec, err := workloadPodSpec(rateLimit)
	if err != nil { return err }
	expectedAddress := policy.RedisServiceName+"."+policy.RedisServiceNamespace+".svc:"+strconv.FormatInt(policy.RedisServicePort, 10)
	addressBound := false
	tlsBound := false
	for _, rawContainer := range array(podSpec["containers"]) {
		container, _ := object(rawContainer)
		for _, rawEnv := range array(container["env"]) {
			env, _ := object(rawEnv)
			if stringValue(env["name"]) == "REDIS_ADDRESS" && stringValue(env["value"]) == expectedAddress { addressBound = true }
			if stringValue(env["name"]) == "REDIS_TLS_ENABLED" && stringValue(env["value"]) == "true" { tlsBound = true }
		}
	}
	secretBound := false
	for _, rawVolume := range array(podSpec["volumes"]) {
		volume, _ := object(rawVolume)
		secret, _ := object(volume["secret"])
		if stringValue(secret["secretName"]) == policy.RedisTLSSecretName { secretBound = true }
	}
	if !addressBound || !tlsBound || !secretBound || replicas(redis.value) < 3 {
		return errors.New("rate-limit workload does not bind the exact TLS Redis service and availability floor")
	}
	return nil
}

func validateEdgeNetworkPolicies(inventory *semanticInventory, policies []inventoryObject, contracts []NetworkPolicySpecContract, workloads ...inventoryObject) error {
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
	selectedPolicies := map[string]bool{}
	for _, workload := range workloads {
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
	if len(selectedPolicies) != len(accepted) { return errors.New("accepted NetworkPolicy union includes an unrelated non-edge selector") }
	return nil
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
