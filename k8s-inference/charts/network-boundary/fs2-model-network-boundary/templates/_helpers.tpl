{{- define "fs2-network-boundary.name" -}}fs2-model-network-boundary{{- end -}}
{{- define "fs2-network-boundary.labels" -}}
app.kubernetes.io/name: fs2-model-network-boundary
app.kubernetes.io/instance: fs2-model-network-boundary
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: fs2-serve
fs2-serve.nebius.ai/network-boundary-authority: "true"
fs2-serve.nebius.ai/network-boundary-object: "true"
{{- end -}}
{{- define "fs2-network-boundary.annotations" -}}
fs2-serve.nebius.ai/network-transition-writer: {{ .Values.transitionWriterUsername | quote }}
{{- if .Values.transitionHolder }}
fs2-serve.nebius.ai/network-transition-holder: {{ .Values.transitionHolder | quote }}
{{- end }}
{{- end -}}
{{- define "fs2-network-boundary.selectorLabels" -}}
app.kubernetes.io/name: fs2-model-network-boundary
app.kubernetes.io/instance: fs2-model-network-boundary
app.kubernetes.io/component: admission
{{- end -}}
