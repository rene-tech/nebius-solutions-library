{{- define "fs2-serve.name" -}}
fs2-serve-control-plane
{{- end -}}

{{- define "fs2-serve.fullname" -}}
{{- printf "%s" (include "fs2-serve.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "fs2-serve.labels" -}}
app.kubernetes.io/name: {{ include "fs2-serve.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "fs2-serve.selectorLabels" -}}
app.kubernetes.io/name: {{ include "fs2-serve.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "fs2-serve.runtimeSelectorLabels" -}}
{{ include "fs2-serve.selectorLabels" . }}
app.kubernetes.io/component: gateway
{{- end -}}

{{- define "fs2-serve.adminConsoleFullname" -}}
{{- printf "%s-admin-console" (include "fs2-serve.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "fs2-serve.adminConsoleSelectorLabels" -}}
{{ include "fs2-serve.selectorLabels" . }}
app.kubernetes.io/component: admin-console
{{- end -}}

{{- define "fs2-serve.maintenanceSelectorLabels" -}}
{{ include "fs2-serve.selectorLabels" . }}
app.kubernetes.io/component: maintenance
{{- end -}}

{{- define "fs2-serve.migrationSelectorLabels" -}}
{{ include "fs2-serve.selectorLabels" . }}
app.kubernetes.io/component: migration
{{- end -}}

{{- define "fs2-serve.bootstrapAccessSelectorLabels" -}}
{{ include "fs2-serve.selectorLabels" . }}
app.kubernetes.io/component: bootstrap-access
{{- end -}}

{{- define "fs2-serve.modelControllerSelectorLabels" -}}
{{ include "fs2-serve.selectorLabels" . }}
app.kubernetes.io/component: model-controller
{{- end -}}

{{- define "fs2-serve.serviceAccountName" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $account := index $root.Values.serviceAccounts $component -}}
{{- if $account.create -}}
{{- default (printf "%s-%s" (include "fs2-serve.fullname" $root) $component) $account.name -}}
{{- else -}}
{{- required (printf "serviceAccounts.%s.name is required when create=false" $component) $account.name -}}
{{- end -}}
{{- end -}}

{{- define "fs2-serve.image" -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- end -}}

{{- define "fs2-serve.adminConsoleImage" -}}
{{- printf "%s@%s" .Values.adminConsole.image.repository .Values.adminConsole.image.digest -}}
{{- end -}}

{{- define "fs2-serve.databaseEnv" -}}
- name: FS2_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.database.name }}
      key: {{ .Values.secrets.database.key }}
{{- end -}}

{{- define "fs2-serve.migrationDatabaseEnv" -}}
- name: FS2_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.migrationsDatabase.name }}
      key: {{ .Values.secrets.migrationsDatabase.key }}
{{- end -}}

{{- define "fs2-serve.maintenanceDatabaseEnv" -}}
- name: FS2_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.maintenanceDatabase.name }}
      key: {{ .Values.secrets.maintenanceDatabase.key }}
{{- end -}}

{{- define "fs2-serve.scientificArtifactsEnv" -}}
{{- if .Values.scientificArtifacts.enabled }}
- name: FS2_SCIENTIFIC_ARTIFACTS_ENABLED
  value: "true"
- name: FS2_ARTIFACT_STORE_ENDPOINT
  value: {{ required "scientificArtifacts.endpoint is required" .Values.scientificArtifacts.endpoint | quote }}
- name: FS2_ARTIFACT_STORE_BUCKET
  value: {{ required "scientificArtifacts.bucket is required" .Values.scientificArtifacts.bucket | quote }}
- name: FS2_ARTIFACT_STORE_REGION
  value: {{ required "scientificArtifacts.region is required" .Values.scientificArtifacts.region | quote }}
- name: FS2_ARTIFACT_STORE_ADDRESSING_STYLE
  value: {{ .Values.scientificArtifacts.addressingStyle | quote }}
- name: FS2_ARTIFACT_STORE_VERIFY_TLS
  value: {{ .Values.scientificArtifacts.verifyTls | quote }}
- name: FS2_ARTIFACT_STORE_CREDENTIALS_FILE
  value: /var/run/secrets/fs2-serve/artifact-store/credentials.json
- name: FS2_ARTIFACT_HANDLE_TTL_SECONDS
  value: {{ .Values.scientificArtifacts.handleTtlSeconds | int64 | quote }}
- name: FS2_ARTIFACT_MAX_BYTES
  value: {{ .Values.scientificArtifacts.maxBytes | int64 | quote }}
- name: FS2_ARTIFACT_RETENTION_SECONDS
  value: {{ .Values.scientificArtifacts.retentionSeconds | int64 | quote }}
- name: FS2_ARTIFACT_MEDIA_TYPES
  value: {{ join "," .Values.scientificArtifacts.mediaTypes | quote }}
{{- end }}
{{- end -}}

{{- define "fs2-serve.scientificArtifactsVolumes" -}}
{{- if .Values.scientificArtifacts.enabled }}
- name: artifact-store
  secret:
    secretName: {{ .Values.secrets.artifactStore.name }}
    defaultMode: 0400
    items:
      - key: {{ .Values.secrets.artifactStore.key }}
        path: credentials.json
{{- end }}
{{- end -}}

{{- define "fs2-serve.scientificArtifactsVolumeMounts" -}}
{{- if .Values.scientificArtifacts.enabled }}
- name: artifact-store
  mountPath: /var/run/secrets/fs2-serve/artifact-store
  readOnly: true
{{- end }}
{{- end -}}

{{- define "fs2-serve.cryptoEnv" -}}
- name: FS2_PAYLOAD_KEYRING_FILE
  value: /var/run/secrets/fs2-serve/crypto/payload-keyring.json
- name: FS2_LEDGER_HMAC_KEYRING_FILE
  value: /var/run/secrets/fs2-serve/crypto/ledger-hmac-keyring.json
{{- end -}}

{{- define "fs2-serve.payloadEnv" -}}
- name: FS2_PAYLOAD_TTL_SECONDS
  value: {{ .Values.config.payloadTtlSeconds | quote }}
{{- end -}}

{{- define "fs2-serve.retentionEnv" -}}
- name: FS2_OPERATION_RETENTION_SECONDS
  value: {{ .Values.config.operationRetentionSeconds | quote }}
- name: FS2_PAT_RETENTION_SECONDS
  value: {{ .Values.config.patRetentionSeconds | quote }}
- name: FS2_AUDIT_RETENTION_SECONDS
  value: {{ .Values.config.auditRetentionSeconds | quote }}
- name: FS2_USAGE_RETENTION_SECONDS
  value: {{ .Values.config.usageRetentionSeconds | quote }}
{{- end -}}

{{- define "fs2-serve.runtimeEnv" -}}
{{ include "fs2-serve.databaseEnv" . }}
{{ include "fs2-serve.cryptoEnv" . }}
{{ include "fs2-serve.payloadEnv" . }}
{{- include "fs2-serve.scientificArtifactsEnv" . }}
- name: FS2_CATALOG_DIR
  value: {{ ternary .Values.catalog.imagePath "/etc/fs2-serve/catalog" (eq .Values.catalog.delivery "image") | quote }}
{{- if eq .Values.catalog.delivery "image" }}
- name: FS2_REPO_ROOT
  value: "/opt/fs2/catalog/repository"
{{- end }}
- name: FS2_BINDINGS_FILE
  value: /etc/fs2-serve/bindings/serving-bindings.json
- name: FS2_VARIANT_PROMOTIONS_FILE
  value: /etc/fs2-serve/bindings/model-variant-promotions.json
{{- if .Values.catalog.leanRoutes.enabled }}
- name: FS2_LEAN_ROUTES_FILE
  value: /etc/fs2-serve/lean-routes/lean-routes.json
{{- end }}
- name: FS2_EVIDENCE_ROOT
  value: /etc/fs2-serve/evidence
- name: FS2_FEDERATION_ROUTES_FILE
  value: /var/run/secrets/fs2-serve/federation/{{ .Values.federation.routesKey }}
- name: FS2_FEDERATION_SECRET_DIR
  value: /var/run/secrets/fs2-serve/federation
- name: FS2_TOKEN_PEPPER_FILE
  value: /var/run/secrets/fs2-serve/token-pepper
- name: FS2_ROUTE_ATTESTORS_FILE
  value: /var/run/secrets/fs2-serve/attestors/route-attestors.json
- name: FS2_ADMIN_TOKEN_FILE
  value: /var/run/secrets/fs2-serve/admin-token
- name: FS2_ADMIN_SESSION_TTL_SECONDS
  value: {{ .Values.config.adminSessionTtlSeconds | quote }}
{{- if .Values.adminConfiguration.enabled }}
- name: FS2_ADMIN_CONFIGURATION_FILE
  value: /etc/fs2-serve/admin/{{ .Values.adminConfiguration.key }}
{{- if .Values.adminConfiguration.receiptKey }}
- name: FS2_ADMIN_CONFIGURATION_RECEIPT_FILE
  value: /etc/fs2-serve/admin/{{ .Values.adminConfiguration.receiptKey }}
{{- end }}
{{- end }}
- name: FS2_PUBLIC_BASE_URL
  value: {{ .Values.config.publicBaseUrl | quote }}
- name: FS2_PUBLIC_AUTHORITY_MODE
  value: {{ .Values.config.publicAuthorityMode | quote }}
- name: FS2_AUTHORIZATION_SERVER_URL
  value: {{ .Values.config.authorizationServerUrl | quote }}
- name: FS2_ALLOW_NON_CLUSTER_URLS
  value: {{ .Values.config.allowNonClusterUrls | quote }}
- name: FS2_OTLP_ENDPOINT
  value: {{ .Values.config.otlpEndpoint | quote }}
- name: FS2_MAX_REQUEST_BYTES
  value: {{ .Values.config.maxRequestBytes | quote }}
- name: FS2_MAX_RESPONSE_BYTES
  value: {{ .Values.config.maxResponseBytes | quote }}
- name: FS2_SYNC_WAIT_SECONDS
  value: {{ .Values.config.syncWaitSeconds | quote }}
- name: FS2_MAX_SYNC_WAIT_SECONDS
  value: {{ .Values.config.maxSyncWaitSeconds | quote }}
- name: FS2_MAX_SYNC_WAITERS
  value: {{ .Values.config.maxSyncWaiters | quote }}
- name: FS2_WAIT_POLL_INITIAL_SECONDS
  value: {{ .Values.config.waitPollInitialSeconds | quote }}
- name: FS2_WAIT_POLL_MAX_SECONDS
  value: {{ .Values.config.waitPollMaxSeconds | quote }}
{{- if .Values.config.activationTimeoutSeconds }}
- name: FS2_ACTIVATION_TIMEOUT_SECONDS
  value: {{ .Values.config.activationTimeoutSeconds | quote }}
{{- end }}
- name: FS2_WORKER_CONCURRENCY
  value: {{ .Values.config.workerConcurrency | quote }}
- name: FS2_WORKER_POLL_SECONDS
  value: {{ .Values.config.workerPollSeconds | quote }}
- name: FS2_WORKER_LEASE_SECONDS
  value: {{ .Values.config.workerLeaseSeconds | quote }}
- name: FS2_ROUTE_REVALIDATION_INTERVAL_SECONDS
  value: {{ .Values.config.routeRevalidationIntervalSeconds | quote }}
- name: FS2_SHUTDOWN_GRACE_SECONDS
  value: {{ .Values.config.shutdownGraceSeconds | quote }}
- name: FS2_MAX_ATTEMPTS
  value: {{ .Values.config.maxAttempts | quote }}
- name: FS2_MAX_GPU_SECONDS_PER_ATTEMPT
  value: {{ .Values.config.maxGpuSecondsPerAttempt | quote }}
{{- if .Values.adminReadAdapters.capacity.enabled }}
- name: FS2_ADMIN_CAPACITY_ENABLED
  value: "true"
- name: FS2_ADMIN_KUBERNETES_API_URL
  value: {{ .Values.adminReadAdapters.capacity.kubernetesApiUrl | quote }}
- name: FS2_ADMIN_KUBERNETES_TOKEN_FILE
  value: /var/run/secrets/fs2-serve/admin-kubernetes/token
- name: FS2_ADMIN_KUBERNETES_CA_FILE
  value: /var/run/secrets/fs2-serve/admin-kubernetes/ca.crt
- name: FS2_ADMIN_KUBERNETES_MODEL_NAMESPACE
  value: {{ .Values.adminReadAdapters.capacity.modelNamespace | quote }}
- name: FS2_ADMIN_KUBERNETES_SYSTEM_NAMESPACE
  value: {{ .Values.adminReadAdapters.capacity.systemNamespace | quote }}
- name: FS2_ADMIN_KUEUE_API_VERSION
  value: {{ .Values.adminReadAdapters.capacity.kueueApiVersion | quote }}
- name: FS2_ADMIN_KUBERNETES_CACHE_TTL_SECONDS
  value: {{ .Values.adminReadAdapters.kubernetesCacheTtlSeconds | quote }}
{{- if .Values.adminReadAdapters.capacity.nodeScalerProvider }}
- name: FS2_ADMIN_NODE_SCALER_PROVIDER
  value: {{ .Values.adminReadAdapters.capacity.nodeScalerProvider | quote }}
{{- end }}
{{- if .Values.modelController.enabled }}
- name: FS2_MODEL_CONTROLLER_ENABLED
  value: "true"
- name: FS2_MODEL_CONTROLLER_WRITES_ENABLED
  value: {{ .Values.modelController.writesEnabled | quote }}
- name: FS2_MODEL_CONTROLLER_NAMESPACE
  value: {{ .Values.modelController.modelNamespace | quote }}
- name: FS2_MODEL_CONTROLLER_ENVELOPE_FILE
  value: /etc/fs2-serve/model-controller/infrastructure-envelope.json
- name: FS2_MODEL_CONTROLLER_BUNDLES_FILE
  value: /etc/fs2-serve/model-controller/renderer-bundles.json
- name: FS2_MODEL_CONTROLLER_PROMETHEUS_SERVER_ADDRESS
  value: {{ .Values.modelController.prometheusServerAddress | quote }}
- name: FS2_MODEL_CONTROLLER_POLL_SECONDS
  value: {{ .Values.modelController.pollSeconds | quote }}
- name: FS2_MODEL_CONTROLLER_API_TIMEOUT_SECONDS
  value: {{ .Values.modelController.apiTimeoutSeconds | quote }}
{{- end }}
{{- end }}
{{- if .Values.adminReadAdapters.observability.enabled }}
- name: FS2_ADMIN_PROMETHEUS_URL
  value: {{ .Values.adminReadAdapters.observability.prometheusUrl | quote }}
- name: FS2_ADMIN_OBSERVABILITY_CONFIG_FILE
  value: /etc/fs2-serve/admin-observability/config.json
{{- end }}
{{- if .Values.scientificBatch.enabled }}
- name: FS2_SCIENTIFIC_BATCH_ENABLED
  value: "true"
- name: FS2_SCIENTIFIC_BATCH_WRITES_ENABLED
  value: {{ .Values.scientificBatch.writesEnabled | quote }}
- name: FS2_SCIENTIFIC_BATCH_NAMESPACE
  value: {{ .Values.scientificBatch.namespace | quote }}
- name: FS2_SCIENTIFIC_BATCH_CONTROLLER_ID
  valueFrom:
    fieldRef:
      fieldPath: metadata.uid
- name: FS2_SCIENTIFIC_BATCH_KUBERNETES_API_URL
  value: {{ .Values.scientificBatch.kubernetesApiUrl | quote }}
- name: FS2_SCIENTIFIC_BATCH_KUBERNETES_TOKEN_FILE
  value: /var/run/secrets/fs2-scientific-batch/token
- name: FS2_SCIENTIFIC_BATCH_KUBERNETES_CA_FILE
  value: /var/run/secrets/fs2-scientific-batch/ca.crt
- name: FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_FILE
  value: /etc/fs2-scientific-batch/{{ .Values.scientificBatch.schedulingContractKey }}
- name: FS2_SCIENTIFIC_BATCH_EXECUTION_MAP_FILE
  value: /etc/fs2-scientific-batch/{{ .Values.scientificBatch.executionMapKey }}
- name: FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE
  value: {{ include "fs2-serve.image" . }}
- name: FS2_SCIENTIFIC_BATCH_INTERNAL_API_URL
  value: http://{{ include "fs2-serve.fullname" . }}.{{ .Release.Namespace }}.svc:{{ .Values.service.port }}
- name: FS2_SCIENTIFIC_BATCH_WORKERS
  value: {{ .Values.scientificBatch.workers | quote }}
- name: FS2_SCIENTIFIC_BATCH_POLL_SECONDS
  value: {{ .Values.scientificBatch.pollSeconds | quote }}
- name: FS2_SCIENTIFIC_BATCH_LEASE_SECONDS
  value: {{ .Values.scientificBatch.leaseSeconds | quote }}
- name: FS2_SCIENTIFIC_BATCH_API_TIMEOUT_SECONDS
  value: {{ .Values.scientificBatch.apiTimeoutSeconds | quote }}
{{- end }}
- name: FS2_ADMIN_ADAPTER_TIMEOUT_SECONDS
  value: {{ .Values.adminReadAdapters.adapterTimeoutSeconds | quote }}
- name: FS2_ADMIN_SOURCE_MAX_AGE_SECONDS
  value: {{ .Values.adminReadAdapters.sourceMaxAgeSeconds | quote }}
{{- if and .Values.adminReadAdapters.context.project .Values.adminReadAdapters.context.cluster .Values.adminReadAdapters.context.region }}
- name: FS2_ADMIN_CONTEXT_PROJECT
  value: {{ .Values.adminReadAdapters.context.project | quote }}
- name: FS2_ADMIN_CONTEXT_CLUSTER
  value: {{ .Values.adminReadAdapters.context.cluster | quote }}
- name: FS2_ADMIN_CONTEXT_REGION
  value: {{ .Values.adminReadAdapters.context.region | quote }}
- name: FS2_ADMIN_CONTEXT_LABEL
  value: {{ default .Values.adminReadAdapters.context.cluster .Values.adminReadAdapters.context.label | quote }}
{{- end }}
{{- end -}}

{{- define "fs2-serve.migrationEnv" -}}
{{ include "fs2-serve.migrationDatabaseEnv" . }}
- name: FS2_REPORTING_DATABASE_ROLE
  value: {{ .Values.migration.reportingDatabaseRole | quote }}
- name: FS2_RUNTIME_DATABASE_ROLE
  value: {{ .Values.migration.runtimeDatabaseRole | quote }}
- name: FS2_MAINTENANCE_DATABASE_ROLE
  value: {{ .Values.migration.maintenanceDatabaseRole | quote }}
- name: FS2_ACTIVATION_DATABASE_ROLE
  value: {{ .Values.migration.activationDatabaseRole | quote }}
{{- end -}}

{{- define "fs2-serve.schemaWaitEnv" -}}
{{ include "fs2-serve.databaseEnv" . }}
- name: FS2_SCHEMA_WAIT_SECONDS
  value: {{ .Values.config.schemaWaitSeconds | quote }}
{{- end -}}

{{- define "fs2-serve.maintenanceEnv" -}}
{{ include "fs2-serve.maintenanceDatabaseEnv" . }}
{{ include "fs2-serve.retentionEnv" . }}
{{- end -}}

{{- define "fs2-serve.cryptoVolumeMounts" -}}
- name: crypto-keyrings
  mountPath: /var/run/secrets/fs2-serve/crypto
  readOnly: true
{{- end -}}

{{- define "fs2-serve.databaseCaVolumeMount" -}}
- name: database-ca
  mountPath: /tls
  readOnly: true
{{- end -}}

{{- define "fs2-serve.databaseCaVolume" -}}
- name: database-ca
  secret:
    secretName: {{ .secret.name }}
    defaultMode: 0400
    items:
      - key: {{ .secret.caKey }}
        path: ca.crt
{{- end -}}

{{- define "fs2-serve.runtimeVolumeMounts" -}}
{{ include "fs2-serve.cryptoVolumeMounts" . }}
{{- include "fs2-serve.scientificArtifactsVolumeMounts" . }}
{{ include "fs2-serve.databaseCaVolumeMount" . }}
{{- if eq .Values.catalog.delivery "pvc" }}
- name: catalog
  mountPath: /etc/fs2-serve/catalog
  readOnly: true
{{- end }}
- name: bindings
  mountPath: /etc/fs2-serve/bindings/serving-bindings.json
  subPath: serving-bindings.json
  readOnly: true
- name: bindings
  mountPath: /etc/fs2-serve/bindings/model-variant-promotions.json
  subPath: model-variant-promotions.json
  readOnly: true
{{- if .Values.catalog.leanRoutes.enabled }}
- name: lean-routes
  mountPath: /etc/fs2-serve/lean-routes
  readOnly: true
{{- end }}
- name: evidence
  mountPath: /etc/fs2-serve/evidence
  readOnly: true
- name: token-pepper
  mountPath: /var/run/secrets/fs2-serve/token-pepper
  subPath: token-pepper.json
  readOnly: true
- name: route-attestors
  mountPath: /var/run/secrets/fs2-serve/attestors
  readOnly: true
- name: admin-token
  mountPath: /var/run/secrets/fs2-serve/admin-token
  subPath: token
  readOnly: true
- name: federation
  mountPath: /var/run/secrets/fs2-serve/federation
  readOnly: true
{{- if .Values.adminReadAdapters.capacity.enabled }}
- name: admin-kubernetes
  mountPath: /var/run/secrets/fs2-serve/admin-kubernetes
  readOnly: true
{{- end }}
{{- if .Values.adminReadAdapters.observability.enabled }}
- name: admin-observability
  mountPath: /etc/fs2-serve/admin-observability
  readOnly: true
{{- end }}
{{- if .Values.adminConfiguration.enabled }}
- name: admin-configuration
  mountPath: /etc/fs2-serve/admin/{{ .Values.adminConfiguration.key }}
  subPath: {{ .Values.adminConfiguration.key }}
  readOnly: true
{{- if .Values.adminConfiguration.receiptKey }}
- name: admin-configuration
  mountPath: /etc/fs2-serve/admin/{{ .Values.adminConfiguration.receiptKey }}
  subPath: {{ .Values.adminConfiguration.receiptKey }}
  readOnly: true
{{- end }}
{{- end }}
{{- if .Values.modelController.enabled }}
- name: model-controller-envelope
  mountPath: /etc/fs2-serve/model-controller/infrastructure-envelope.json
  subPath: {{ .Values.modelController.infrastructureEnvelopeKey }}
  readOnly: true
- name: model-controller-bundles
  mountPath: /etc/fs2-serve/model-controller/renderer-bundles.json
  subPath: {{ .Values.modelController.rendererBundlesKey }}
  readOnly: true
{{- end }}
{{- if .Values.scientificBatch.enabled }}
- name: scientific-batch-kubernetes
  mountPath: /var/run/secrets/fs2-scientific-batch
  readOnly: true
- name: scientific-batch-scheduling
  mountPath: /etc/fs2-scientific-batch/{{ .Values.scientificBatch.schedulingContractKey }}
  subPath: {{ .Values.scientificBatch.schedulingContractKey }}
  readOnly: true
- name: scientific-batch-execution
  mountPath: /etc/fs2-scientific-batch/{{ .Values.scientificBatch.executionMapKey }}
  subPath: {{ .Values.scientificBatch.executionMapKey }}
  readOnly: true
{{- end }}
{{- end -}}

{{- define "fs2-serve.cryptoVolumes" -}}
- name: crypto-keyrings
  projected:
    defaultMode: 0400
    sources:
      - secret:
          name: {{ .Values.secrets.payloadKeyring.name }}
          items:
            - key: {{ .Values.secrets.payloadKeyring.key }}
              path: payload-keyring.json
      - secret:
          name: {{ .Values.secrets.ledgerHmacKeyring.name }}
          items:
            - key: {{ .Values.secrets.ledgerHmacKeyring.key }}
              path: ledger-hmac-keyring.json
{{- end -}}

{{- define "fs2-serve.runtimeVolumes" -}}
{{ include "fs2-serve.cryptoVolumes" . }}
{{- include "fs2-serve.scientificArtifactsVolumes" . }}
{{ include "fs2-serve.databaseCaVolume" (dict "secret" .Values.secrets.database) }}
{{- if eq .Values.catalog.delivery "pvc" }}
- name: catalog
  persistentVolumeClaim:
    claimName: {{ required "catalog.persistentVolumeClaimName is required" .Values.catalog.persistentVolumeClaimName }}
    readOnly: true
{{- end }}
- name: bindings
  configMap:
    name: {{ .Values.catalog.bindingsConfigMapName }}
    items:
      - key: {{ .Values.catalog.bindingsKey }}
        path: serving-bindings.json
      - key: {{ .Values.catalog.variantPromotionsKey }}
        path: model-variant-promotions.json
{{- if .Values.catalog.leanRoutes.enabled }}
- name: lean-routes
  configMap:
    name: {{ .Values.catalog.leanRoutes.configMapName }}
    items:
      - key: {{ .Values.catalog.leanRoutes.key }}
        path: lean-routes.json
{{- end }}
- name: evidence
{{- if .Values.catalog.leanRoutes.enabled }}
  emptyDir: {}
{{- else }}
  persistentVolumeClaim:
    claimName: {{ required "catalog.evidencePersistentVolumeClaimName is required" .Values.catalog.evidencePersistentVolumeClaimName }}
    readOnly: true
{{- end }}
- name: token-pepper
  secret:
    secretName: {{ .Values.secrets.tokenPepper.name }}
    defaultMode: 0400
    items:
      - key: {{ .Values.secrets.tokenPepper.key }}
        path: token-pepper.json
- name: route-attestors
  secret:
    secretName: {{ .Values.secrets.routeAttestors.name }}
    defaultMode: 0400
    items:
      - key: {{ .Values.secrets.routeAttestors.key }}
        path: route-attestors.json
- name: admin-token
  secret:
    secretName: {{ .Values.secrets.admin.name }}
    defaultMode: 0400
    items:
      - key: {{ .Values.secrets.admin.key }}
        path: token
- name: federation
  secret:
    secretName: {{ .Values.federation.secretName }}
    optional: true
    defaultMode: 0400
{{- if .Values.adminReadAdapters.capacity.enabled }}
- name: admin-kubernetes
  projected:
    defaultMode: 0400
    sources:
      - serviceAccountToken:
          expirationSeconds: {{ .Values.adminReadAdapters.capacity.tokenExpirationSeconds }}
          path: token
      - configMap:
          name: kube-root-ca.crt
          items:
            - key: ca.crt
              path: ca.crt
{{- end }}
{{- if .Values.adminReadAdapters.observability.enabled }}
- name: admin-observability
  configMap:
    name: {{ include "fs2-serve.fullname" . }}-admin-observability
    items:
      - key: config.json
        path: config.json
{{- end }}
{{- if .Values.adminConfiguration.enabled }}
- name: admin-configuration
  configMap:
    name: {{ .Values.adminConfiguration.configMapName }}
    defaultMode: 0444
    items:
      - key: {{ .Values.adminConfiguration.key }}
        path: {{ .Values.adminConfiguration.key }}
      {{- if .Values.adminConfiguration.receiptKey }}
      - key: {{ .Values.adminConfiguration.receiptKey }}
        path: {{ .Values.adminConfiguration.receiptKey }}
      {{- end }}
{{- end }}
{{- if .Values.modelController.enabled }}
- name: model-controller-envelope
  configMap:
    name: {{ .Values.modelController.infrastructureEnvelopeConfigMapName }}
    items:
      - key: {{ .Values.modelController.infrastructureEnvelopeKey }}
        path: {{ .Values.modelController.infrastructureEnvelopeKey }}
- name: model-controller-bundles
  configMap:
    name: {{ .Values.modelController.rendererBundlesConfigMapName }}
    items:
      - key: {{ .Values.modelController.rendererBundlesKey }}
        path: {{ .Values.modelController.rendererBundlesKey }}
{{- end }}
{{- if .Values.scientificBatch.enabled }}
- name: scientific-batch-kubernetes
  projected:
    defaultMode: 0400
    sources:
      - serviceAccountToken:
          audience: kubernetes.default.svc
          expirationSeconds: {{ .Values.scientificBatch.tokenExpirationSeconds }}
          path: token
      - configMap:
          name: kube-root-ca.crt
          items:
            - key: ca.crt
              path: ca.crt
- name: scientific-batch-scheduling
  configMap:
    name: {{ .Values.scientificBatch.schedulingContractConfigMapName }}
    items:
      - key: {{ .Values.scientificBatch.schedulingContractKey }}
        path: {{ .Values.scientificBatch.schedulingContractKey }}
- name: scientific-batch-execution
  configMap:
    name: {{ .Values.scientificBatch.executionMapConfigMapName }}
    items:
      - key: {{ .Values.scientificBatch.executionMapKey }}
        path: {{ .Values.scientificBatch.executionMapKey }}
{{- end }}
{{- end -}}
