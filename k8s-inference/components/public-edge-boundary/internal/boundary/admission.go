package boundary

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"time"
)

type TransitionConsumer interface {
	Consume(snapshot Snapshot, transition Transition, admissionUID string, requestSHA256 string, now time.Time) error
}

func (r *Runtime) Review(raw []byte, now time.Time, consumer TransitionConsumer) AdmissionReview {
	requestReview := AdmissionReview{}
	if err := decodeAdmissionJSON(raw, &requestReview); err != nil || requestReview.Request == nil {
		return deniedReview("", "MalformedRequest", "admission request is not valid JSON")
	}
	request := requestReview.Request
	if requestReview.APIVersion != "admission.k8s.io/v1" || requestReview.Kind != "AdmissionReview" || request.UID == "" {
		return deniedReview(request.UID, "MalformedRequest", "only admission.k8s.io/v1 AdmissionReview is accepted")
	}
	if !validAdmissionIdentifiers(request) {
		return deniedReview(request.UID, "MalformedRequest", "admission identity contains an empty or control-bearing field")
	}
	if !r.Current(now) {
		return deniedReview(request.UID, "Forbidden", "public-edge authority snapshot is stale or expired")
	}
	transition, protected, reason := r.authorize(request)
	allowed := reason == ""
	if !allowed {
		return deniedReview(request.UID, "Forbidden", reason)
	}
	if protected && !transition.DryRun {
		canonicalRequest, err := json.Marshal(request)
		if err != nil || consumer == nil || consumer.Consume(r.Snapshot, transition, request.UID, digestHex(canonicalRequest), now) != nil {
			return deniedReview(request.UID, "Forbidden", "exact transition consumption could not be durably settled")
		}
	}
	return AdmissionReview{
		APIVersion: "admission.k8s.io/v1",
		Kind:       "AdmissionReview",
		Response: &AdmissionResponse{
			UID:     request.UID,
			Allowed: true,
		},
	}
}

func validAdmissionIdentifiers(request *AdmissionRequest) bool {
	return safeText(request.UID, false) && safeText(request.Operation, false) &&
		safeText(request.Resource.Group, true) && safeText(request.Resource.Version, false) &&
		safeText(request.Resource.Resource, false) && safeText(request.Subresource, true) &&
		validGroupVersionKind(request.Kind, false) && validOriginalRequestTuple(request.RequestKind, request.RequestResource, request.RequestSubresource) &&
		safeText(request.Namespace, true) && (safeText(request.Name, false) || request.Operation == "CREATE" && request.Name == "") &&
		safeText(request.UserInfo.Username, false) && safeText(request.UserInfo.UID, true)
}

func (r *Runtime) authorize(request *AdmissionRequest) (Transition, bool, string) {
	key := identityKey(
		request.Resource.Group,
		request.Resource.Version,
		request.Resource.Resource,
		request.Namespace,
		request.Name,
	)
	if _, root := r.protectedRoots[key]; root && request.Operation == "DELETE" {
		return Transition{}, true, "protected public-edge root deletion requires the external break-glass authority"
	}

	objectRaw := request.Object

	requiresTransition := false
	if _, root := r.protectedRoots[key]; root {
		requiresTransition = true
	}
	if _, credentialObject := r.credentialItems[key]; credentialObject {
		requiresTransition = true
	}
	_, credentialNamespace := r.credentialNS[request.Namespace]
	if credentialNamespace && request.Resource.Group == "" && request.Resource.Resource == "secrets" && (request.Operation == "CREATE" || request.Operation == "UPDATE") {
		requiresTransition = true
	}
	guards := r.matchingResourceGuards(request)
	for _, guard := range guards {
		if guard.SemanticGuard != "inspect-controller-credential-reachability" {
			requiresTransition = true
		}
	}
	if credentialNamespace && isWorkloadResource(request.Resource.Group, request.Resource.Resource) && (request.Operation == "CREATE" || request.Operation == "UPDATE") {
			sensitive, inspectErr := r.workloadReachesCredential(request.Namespace, request.Resource.Resource, objectRaw)
			if inspectErr != nil {
				return Transition{}, true, "workload credential reachability cannot be determined"
		}
		requiresTransition = requiresTransition || sensitive
	}
	for _, guard := range guards {
		if guard.SemanticGuard != "inspect-controller-credential-reachability" {
			continue
		}
			sensitive, inspectErr := r.workloadReachesCredential(request.Namespace, request.Resource.Resource, objectRaw)
			if inspectErr != nil {
				return Transition{}, true, "guarded workload credential reachability cannot be determined"
		}
		requiresTransition = requiresTransition || sensitive
	}
	if !requiresTransition {
		return Transition{}, false, ""
	}

	actor := normalizedAdmissionActor(request.UserInfo)
	requestObjectSHA256, err := canonicalOptionalObjectSHA256(request.Object)
	if err != nil {
		return Transition{}, true, "admission request object cannot be canonically reconstructed"
	}
	optionsSHA256, err := canonicalOptionalObjectSHA256(request.Options)
	if err != nil {
		return Transition{}, true, "admission options cannot be canonically reconstructed"
	}
	oldObject := AdmissionObjectBinding{}
	newObject := AdmissionObjectBinding{}
	serviceAccountToken := request.Operation == "CREATE" && request.Resource.Group == "" &&
		request.Resource.Resource == "serviceaccounts" && request.Subresource == "token"
	tokenAudiences := []string(nil)
	var tokenExpirationSeconds int64
	if serviceAccountToken {
		tokenAudiences, tokenExpirationSeconds, err = serviceAccountTokenRequestScope(request.Object)
		if err != nil {
			return Transition{}, true, "TokenRequest audiences or expiration are malformed or outside the bounded contract"
		}
	}
	if request.Operation == "UPDATE" || request.Operation == "DELETE" {
		oldObject, err = admissionObjectBinding(request.OldObject)
		if err != nil || bindingEmpty(oldObject) {
			return Transition{}, true, "old admission object lacks exact UID/resourceVersion identity"
		}
	}
	if (request.Operation == "CREATE" && !serviceAccountToken) || request.Operation == "UPDATE" {
		newObject, err = admissionObjectBinding(request.Object)
		if err != nil || bindingEmpty(newObject) {
			return Transition{}, true, "new admission object lacks exact UID/resourceVersion identity"
		}
	}
	generateName := ""
	if request.Operation == "CREATE" && !serviceAccountToken {
		objectName, objectGenerateName, nameErr := admissionObjectNames(request.Object)
		if nameErr != nil || request.Name != objectName ||
			(request.Name == "" && objectGenerateName == "") ||
			(request.Name != "" && objectGenerateName != "") {
			return Transition{}, true, "CREATE name/generateName identity is ambiguous or inconsistent"
		}
		generateName = objectGenerateName
	}
	targetObject := AdmissionObjectBinding{}
	if serviceAccountToken {
		identity, exists := r.authorityItems[key]
		if !exists {
			return Transition{}, true, "TokenRequest target ServiceAccount is absent from the signed authority object closure"
		}
		targetObject = AdmissionObjectBinding{UID: identity.UID, ResourceVersion: identity.ResourceVersion, ObjectSHA256: identity.ObjectSHA256}
		oldObject = AdmissionObjectBinding{}
		newObject = AdmissionObjectBinding{}
	}
	if request.Operation == "CONNECT" {
		identity, exists := r.authorityItems[key]
		if !exists {
			return Transition{}, true, "CONNECT target is absent from the signed authority object closure"
		}
		targetObject = AdmissionObjectBinding{UID: identity.UID, ResourceVersion: identity.ResourceVersion, ObjectSHA256: identity.ObjectSHA256}
		oldObject = AdmissionObjectBinding{}
		newObject = AdmissionObjectBinding{}
	}
	dryRun := request.DryRun != nil && *request.DryRun
	transition := Transition{
		Operation:    request.Operation,
		DryRunSpecified: request.DryRun != nil,
		DryRun:       dryRun,
		Kind:         request.Kind,
		APIGroup:     request.Resource.Group,
		APIVersion:   request.Resource.Version,
		Resource:     request.Resource.Resource,
		Subresource:  request.Subresource,
		RequestKind: request.RequestKind,
		RequestResource: request.RequestResource,
		RequestSubresource: request.RequestSubresource,
		Namespace:    request.Namespace,
		Name:         request.Name,
		GenerateName: generateName,
		RequestObjectSHA256: requestObjectSHA256,
		OptionsSHA256: optionsSHA256,
		OldObject: oldObject,
		NewObject: newObject,
		TargetObject: targetObject,
		TokenAudiences: tokenAudiences,
		TokenExpirationSeconds: tokenExpirationSeconds,
		Actor:        actor,
	}
	expected, exists := r.transitions[transitionKey(transition)]
	if !exists || !actorsEqual(expected.Actor, actor) {
		return Transition{}, true, "no independently signed exact-object transition authorizes this actor"
	}
	return expected, true, ""
}

func serviceAccountTokenRequestScope(raw []byte) ([]string, int64, error) {
	var object map[string]any
	if err := decodeAdmissionJSON(raw, &object); err != nil {
		return nil, 0, err
	}
	spec, ok := objectMap(object["spec"])
	if !ok {
		return nil, 0, errors.New("TokenRequest spec is absent")
	}
	audiences := []string{}
	for _, value := range objectSlice(spec["audiences"]) {
		audience, ok := value.(string)
		if !ok || !safeText(audience, false) {
			return nil, 0, errors.New("TokenRequest audience is invalid")
		}
		audiences = append(audiences, audience)
	}
	sort.Strings(audiences)
	if !sortedUnique(audiences) {
		return nil, 0, errors.New("TokenRequest audiences are empty or repeated")
	}
	expiration, ok := spec["expirationSeconds"].(json.Number)
	if !ok {
		return nil, 0, errors.New("TokenRequest expirationSeconds is absent")
	}
	expirationSeconds, err := expiration.Int64()
	if err != nil || expirationSeconds < 1 || expirationSeconds > 600 {
		return nil, 0, errors.New("TokenRequest expirationSeconds exceeds the ten-minute authority bound")
	}
	return audiences, expirationSeconds, nil
}

func admissionObjectNames(raw []byte) (string, string, error) {
	var object map[string]any
	if err := decodeAdmissionJSON(raw, &object); err != nil {
		return "", "", err
	}
	metadata, ok := objectMap(object["metadata"])
	if !ok {
		return "", "", errors.New("admission object metadata is absent")
	}
	name, _ := metadata["name"].(string)
	generateName, _ := metadata["generateName"].(string)
	if !safeText(name, true) || !safeText(generateName, true) {
		return "", "", errors.New("admission object name identity is invalid")
	}
	return name, generateName, nil
}

func canonicalOptionalObjectSHA256(raw []byte) (string, error) {
	if len(bytes.TrimSpace(raw)) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return "", nil
	}
	return canonicalObjectSHA256(raw)
}

func admissionObjectBinding(raw []byte) (AdmissionObjectBinding, error) {
	objectSHA256, err := canonicalOptionalObjectSHA256(raw)
	if err != nil || objectSHA256 == "" {
		return AdmissionObjectBinding{}, err
	}
	var object map[string]any
	if err := decodeAdmissionJSON(raw, &object); err != nil {
		return AdmissionObjectBinding{}, err
	}
	metadata, ok := objectMap(object["metadata"])
	if !ok {
		return AdmissionObjectBinding{}, errors.New("admission object metadata is absent")
	}
	uid, _ := metadata["uid"].(string)
	resourceVersion, _ := metadata["resourceVersion"].(string)
	if !safeText(uid, true) || !safeText(resourceVersion, true) {
		return AdmissionObjectBinding{}, errors.New("admission object metadata identity is invalid")
	}
	return AdmissionObjectBinding{UID: uid, ResourceVersion: resourceVersion, ObjectSHA256: objectSHA256}, nil
}

func (r *Runtime) matchingResourceGuards(request *AdmissionRequest) []ResourceGuard {
	matched := []ResourceGuard{}
	for _, guard := range r.resourceGuards {
		if guard.APIGroup != request.Resource.Group || guard.Resource != request.Resource.Resource ||
			!containsExact(guard.Subresources, request.Subresource) ||
			!containsExact(guard.Operations, request.Operation) ||
			!containsSelector(guard.Namespaces, request.Namespace) ||
			!containsSelector(guard.Names, request.Name) {
			continue
		}
		matched = append(matched, guard)
	}
	return matched
}

func containsExact(values []string, expected string) bool {
	index := sort.SearchStrings(values, expected)
	return index < len(values) && values[index] == expected
}

func containsSelector(values []string, expected string) bool {
	return containsExact(values, "*") || containsExact(values, expected)
}

func isWorkloadResource(group string, resource string) bool {
	switch group {
	case "":
		return resource == "pods" || resource == "replicationcontrollers" || resource == "serviceaccounts"
	case "apps":
		return resource == "daemonsets" || resource == "deployments" || resource == "replicasets" || resource == "statefulsets"
	case "batch":
		return resource == "cronjobs" || resource == "jobs"
	default:
		return false
	}
}

func (r *Runtime) workloadReachesCredential(namespace string, resource string, raw []byte) (bool, error) {
	var object map[string]any
	if err := decodeAdmissionJSON(raw, &object); err != nil {
		return false, err
	}
	if resource == "serviceaccounts" {
		metadata, _ := objectMap(object["metadata"])
		name, _ := metadata["name"].(string)
		key := identityKey("", "v1", "serviceaccounts", namespace, name)
		if _, protected := r.credentialItems[key]; protected {
			return true, nil
		}
		for _, reference := range append(objectSlice(object["secrets"]), objectSlice(object["imagePullSecrets"])...) {
			item, ok := objectMap(reference)
			if !ok {
				return false, errors.New("ServiceAccount secret reference is malformed")
			}
			name, _ := item["name"].(string)
			secretKey := identityKey("", "v1", "secrets", namespace, name)
			if _, protected := r.credentialItems[secretKey]; protected {
				return true, nil
			}
		}
		return false, nil
	}
	spec, ok := objectMap(object["spec"])
	if !ok {
		return false, errors.New("workload spec is absent")
	}
	if resource == "cronjobs" {
		jobTemplate, ok := objectMap(spec["jobTemplate"])
		if !ok {
			return false, errors.New("CronJob jobTemplate is absent")
		}
		spec, ok = objectMap(jobTemplate["spec"])
		if !ok {
			return false, errors.New("CronJob job spec is absent")
		}
	}
	if resource != "pods" {
		template, ok := objectMap(spec["template"])
		if !ok {
			return false, errors.New("Pod template is absent")
		}
		spec, ok = objectMap(template["spec"])
		if !ok {
			return false, errors.New("Pod template spec is absent")
		}
	}
	serviceAccount, _ := spec["serviceAccountName"].(string)
	if serviceAccount == "" {
		serviceAccount = "default"
	}
	key := identityKey("", "v1", "serviceaccounts", namespace, serviceAccount)
	if _, protected := r.credentialItems[key]; protected {
		return true, nil
	}
	secrets, claims, err := podCredentialReferences(spec)
	if err != nil {
		return false, err
	}
	for _, secret := range secrets {
		key := identityKey("", "v1", "secrets", namespace, secret)
		if _, protected := r.credentialItems[key]; protected {
			return true, nil
		}
	}
	for _, claim := range claims {
		key := identityKey("", "v1", "persistentvolumeclaims", namespace, claim)
		if _, protected := r.credentialItems[key]; protected {
			return true, nil
		}
	}
	return false, nil
}

func podCredentialReferences(spec map[string]any) ([]string, []string, error) {
	result := map[string]struct{}{}
	claims := map[string]struct{}{}
	for _, value := range objectSlice(spec["imagePullSecrets"]) {
		if item, ok := objectMap(value); ok {
			addString(result, item["name"])
		}
	}
	for _, value := range objectSlice(spec["volumes"]) {
		volume, ok := objectMap(value)
		if !ok {
			return nil, nil, errors.New("Pod volume is malformed")
		}
		known := map[string]struct{}{
			"name": {}, "awsElasticBlockStore": {}, "azureDisk": {}, "configMap": {},
			"downwardAPI": {}, "emptyDir": {}, "fc": {}, "gcePersistentDisk": {},
			"gitRepo": {}, "glusterfs": {}, "hostPath": {}, "iscsi": {}, "nfs": {},
			"photonPersistentDisk": {}, "portworxVolume": {}, "quobyte": {},
			"vsphereVolume": {},
		}
		if secret, ok := objectMap(volume["secret"]); ok {
			addString(result, secret["secretName"])
			known["secret"] = struct{}{}
		}
		if projected, ok := objectMap(volume["projected"]); ok {
			known["projected"] = struct{}{}
			for _, sourceValue := range objectSlice(projected["sources"]) {
				if source, ok := objectMap(sourceValue); ok {
					if secret, ok := objectMap(source["secret"]); ok {
						addString(result, secret["name"])
					}
				}
			}
		}
		if csi, ok := objectMap(volume["csi"]); ok {
			known["csi"] = struct{}{}
			if secret, ok := objectMap(csi["nodePublishSecretRef"]); ok {
				addString(result, secret["name"])
			}
		}
		for _, field := range []string{"cephfs", "flexVolume", "rbd", "scaleIO", "storageos"} {
			if source, ok := objectMap(volume[field]); ok {
				known[field] = struct{}{}
				if secret, ok := objectMap(source["secretRef"]); ok {
					addString(result, secret["name"])
				}
			}
		}
		if azureFile, ok := objectMap(volume["azureFile"]); ok {
			known["azureFile"] = struct{}{}
			addString(result, azureFile["secretName"])
		}
		if claim, ok := objectMap(volume["persistentVolumeClaim"]); ok {
			known["persistentVolumeClaim"] = struct{}{}
			addString(claims, claim["claimName"])
		}
		for field := range volume {
			if _, supported := known[field]; !supported {
				return nil, nil, fmt.Errorf("Pod volume source %q is not classified by the signed credential-reachability policy", field)
			}
		}
	}
	containers := append(objectSlice(spec["initContainers"]), objectSlice(spec["containers"])...)
	containers = append(containers, objectSlice(spec["ephemeralContainers"])...)
	for _, value := range containers {
		container, ok := objectMap(value)
		if !ok {
			continue
		}
		for _, envValue := range objectSlice(container["env"]) {
			if env, ok := objectMap(envValue); ok {
				if valueFrom, ok := objectMap(env["valueFrom"]); ok {
					if secret, ok := objectMap(valueFrom["secretKeyRef"]); ok {
						addString(result, secret["name"])
					}
				}
			}
		}
		for _, envFromValue := range objectSlice(container["envFrom"]) {
			if envFrom, ok := objectMap(envFromValue); ok {
				if secret, ok := objectMap(envFrom["secretRef"]); ok {
					addString(result, secret["name"])
				}
			}
		}
	}
	values := make([]string, 0, len(result))
	for value := range result {
		values = append(values, value)
	}
	sort.Strings(values)
	claimValues := make([]string, 0, len(claims))
	for value := range claims {
		claimValues = append(claimValues, value)
	}
	sort.Strings(claimValues)
	return values, claimValues, nil
}

func canonicalObjectSHA256(raw []byte) (string, error) {
	if len(bytes.TrimSpace(raw)) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return "", errors.New("object is absent")
	}
	if err := rejectDuplicateKeys(raw); err != nil {
		return "", err
	}
	var value any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return "", err
	}
	if err := ensureJSONEOF(decoder); err != nil {
		return "", err
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return digestHex(canonical), nil
}

func admissionTargetSHA256(request *AdmissionRequest) string {
	canonical, _ := json.Marshal(map[string]string{
		"api_group":   request.Resource.Group,
		"api_version": request.Resource.Version,
		"name":        request.Name,
		"namespace":   request.Namespace,
		"resource":    request.Resource.Resource,
		"subresource": request.Subresource,
	})
	return digestHex(canonical)
}

func decodeAdmissionJSON(raw []byte, destination any) error {
	if len(raw) > 8*1024*1024 {
		return errors.New("admission body exceeds 8 MiB")
	}
	if err := rejectDuplicateKeys(raw); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	return ensureJSONEOF(decoder)
}

func normalizedAdmissionActor(user AdmissionUser) Actor {
	groups := append([]string(nil), user.Groups...)
	sort.Strings(groups)
	extra := map[string][]string{}
	for key, values := range user.Extra {
		copyValues := append([]string(nil), values...)
		sort.Strings(copyValues)
		extra[key] = copyValues
	}
	return Actor{Username: user.Username, UID: user.UID, Groups: groups, Extra: extra}
}

func actorsEqual(left Actor, right Actor) bool {
	leftRaw, _ := json.Marshal(left)
	rightRaw, _ := json.Marshal(right)
	return bytes.Equal(leftRaw, rightRaw)
}

func objectMap(value any) (map[string]any, bool) {
	result, ok := value.(map[string]any)
	return result, ok
}

func objectSlice(value any) []any {
	result, _ := value.([]any)
	return result
}

func addString(target map[string]struct{}, value any) {
	if text, ok := value.(string); ok && text != "" {
		target[text] = struct{}{}
	}
}

func deniedReview(uid string, reason string, message string) AdmissionReview {
	return AdmissionReview{
		APIVersion: "admission.k8s.io/v1",
		Kind:       "AdmissionReview",
		Response: &AdmissionResponse{
			UID:     uid,
			Allowed: false,
			Status: &Status{
				Code:    403,
				Reason:  reason,
				Message: message,
			},
		},
}

func (r *Runtime) Ready(now time.Time) error {
	if !r.Current(now) {
		return fmt.Errorf("boundary snapshot is not current")
	}
	return nil
}
