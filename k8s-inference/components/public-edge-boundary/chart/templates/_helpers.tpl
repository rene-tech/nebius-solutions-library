{{- define "fs2-boundary.image" -}}
{{- $repository := required "image.repository is owner-enrolled" .Values.image.repository -}}
{{- $digest := required "image.digest is owner-enrolled" .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $digest) -}}{{ fail "image.digest must be sha256:<64 lowercase hex>" }}{{- end -}}
{{ printf "%s@%s" $repository $digest }}
{{- end -}}

{{- define "fs2-boundary.requiredInputs" -}}
{{- range $name, $secret := .Values.inputSecrets -}}{{- $_ := required (printf "inputSecrets.%s is owner-enrolled" $name) $secret -}}{{- end -}}
{{- range $name, $claim := .Values.storage -}}{{- $_ := required (printf "storage.%s is owner-enrolled" $name) $claim -}}{{- end -}}
{{- $_ := required "webhook.caBundle is owner-enrolled" .Values.webhook.caBundle -}}
{{- $_ := required "webhook.clientConfigEvidenceSHA256 is owner-enrolled" .Values.webhook.clientConfigEvidenceSHA256 -}}
{{- $_ := required "controlPlaneHandoff.admissionConfigurationSHA256 is owner-enrolled" .Values.controlPlaneHandoff.admissionConfigurationSHA256 -}}
{{- $_ := required "controlPlaneHandoff.webhookKubeconfigSHA256 is owner-enrolled" .Values.controlPlaneHandoff.webhookKubeconfigSHA256 -}}
{{- $_ := required "controlPlaneHandoff.apiServerClientSPKISHA256Current is owner-enrolled" .Values.controlPlaneHandoff.apiServerClientSPKISHA256Current -}}
{{- end -}}

{{- define "fs2-boundary.podSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: {drop: ["ALL"]}
seccompProfile: {type: RuntimeDefault}
{{- end -}}

{{- define "fs2-boundary.spread" -}}
- maxSkew: 1
  topologyKey: kubernetes.io/hostname
  whenUnsatisfiable: DoNotSchedule
  labelSelector:
    matchLabels: {app.kubernetes.io/name: {{ . | quote }}}
{{- end -}}
