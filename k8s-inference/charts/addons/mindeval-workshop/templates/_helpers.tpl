{{- define "mindeval.labels" -}}
app.kubernetes.io/part-of: mindeval-workshop
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
{{- define "mindeval.workshopImage" -}}
{{- required "workshop.image must be a published workshop image digest" .Values.workshop.image -}}
{{- end -}}
