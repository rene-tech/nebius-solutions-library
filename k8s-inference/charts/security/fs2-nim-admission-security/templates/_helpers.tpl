{{- define "fs2-nim-admission-security.labels" -}}
app.kubernetes.io/name: fs2-nim-admission-security
app.kubernetes.io/instance: fs2-nim-admission-security
app.kubernetes.io/managed-by: platform-security
app.kubernetes.io/component: nim-admission
app.kubernetes.io/part-of: fs2-serve
fs2-serve.nebius.ai/immutable-security-boundary: "true"
{{- end -}}
