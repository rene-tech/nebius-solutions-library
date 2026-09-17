locals {
  nim_admission_required_subjects = toset(flatten([
    for model_id in local.selected_model_ids : [
      "NIMCache/${model_id}",
      "NIMService/${model_id}",
    ] if try(local.catalog_models[model_id].runtime.kind, null) == "nim"
  ]))
  nim_admission_required = length(local.nim_admission_required_subjects) > 0
  nim_admission_policy_contract = {
    schema         = "fs2-serve.nebius.ai/nim-admission-policy/v8"
    name           = "fs2-serve-control-plane-nim-admission"
    namespace      = "fs2-models"
    failure_policy = "Fail"
    match_policy   = "Equivalent"
    side_effects   = "None"
    timeout_seconds = 30
    admission_review_versions = ["v1"]
    operations     = ["CREATE", "UPDATE"]
    resources = [
      "apps.nvidia.com/v1alpha1/nimcaches",
      "apps.nvidia.com/v1alpha1/nimservices",
      "apps/v1/deployments",
      "apps/v1/replicasets",
      "apps/v1/statefulsets",
      "batch/v1/jobs",
      "v1/pods",
      "v1/pods/ephemeralcontainers",
    ]
    service = {
      namespace = "fs2-system"
      name      = "fs2-serve-control-plane-nim-admission"
      path      = "/admit"
      port      = 8443
    }
    ca_bundle_sha256 = local.nim_admission_required ? sha256(base64decode(var.nim_operator_admission.ca_bundle)) : ""
    tls = {
      secret_name            = var.nim_operator_admission.tls_secret_name
      secret_uid             = var.nim_operator_admission.tls_secret_uid
      secret_resource_version = var.nim_operator_admission.tls_secret_resource_version
      secret_type            = var.nim_operator_admission.tls_secret_type
      generation_sha256      = var.nim_operator_admission.tls_generation_sha256
      certificate_sha256     = var.nim_operator_admission.tls_certificate_sha256
      public_key_spki_sha256 = var.nim_operator_admission.tls_public_key_spki_sha256
      ca_bundle_sha256       = local.nim_admission_required ? sha256(base64decode(var.nim_operator_admission.ca_bundle)) : ""
      service_dns_name       = "fs2-serve-control-plane-nim-admission.fs2-system.svc"
    }
    owner_resolution = "live-read-through-exact-uid-chain"
    owner_lookup_namespaces = local.nim_admission_owner_lookup_namespaces
    network_policy = {
      webhook_source_cidrs = sort(distinct(var.nim_operator_admission.webhook_source_cidrs))
      kubernetes_api_cidrs  = sort(tolist(local.kubernetes_api_egress_cidrs))
    }
    root_enrollment = {
      namespace   = "fs2-system"
      name_prefix = "fs2-nim-root-"
      storage_kind = "immutable-configmap-create-once"
      reconciler = "persisted-root-readback"
      reconcile_interval_seconds = 30
      admission_behavior = "verify-existing-deny-until-enrolled"
    }
    security_boundary = {
      name                      = var.nim_operator_admission.security_boundary.name
      policy_uid                = var.nim_operator_admission.security_boundary.policy_uid
      policy_resource_version   = var.nim_operator_admission.security_boundary.policy_resource_version
      binding_uid               = var.nim_operator_admission.security_boundary.binding_uid
      binding_resource_version  = var.nim_operator_admission.security_boundary.binding_resource_version
      subject_sha256            = var.nim_operator_admission.security_boundary.subject_sha256
      authorization_id          = var.nim_operator_admission.security_boundary.authorization_id
      cluster_uid               = var.nim_operator_admission.security_boundary.cluster_uid
      provider_authorization_id = var.nim_operator_admission.security_boundary.provider_authorization_id
      installation_receipt_authorization_id = var.nim_operator_admission.installation_receipt.authorization_id
      owner_lookup_namespaces   = var.nim_operator_admission.security_boundary.owner_lookup_namespaces
      principal_epoch           = var.nim_operator_admission.security_boundary.principal_epoch
      provider_renewal          = var.nim_operator_admission.security_boundary.provider_renewal
    }
  }
  # Python's shared canonical_bytes() contract terminates canonical JSON with
  # one LF. Bind Terraform to those exact bytes instead of a look-alike hash
  # over jsonencode() without the terminator.
  nim_admission_policy_sha256 = sha256("${jsonencode(local.nim_admission_policy_contract)}\n")
  nim_admission_entries = [
    for entry_id in sort(keys(var.nim_operator_admission.entries)) : {
      resource_kind = var.nim_operator_admission.entries[entry_id].resource_kind
      model_id       = var.nim_operator_admission.entries[entry_id].model_id
      security_envelope = {
        subject            = var.nim_operator_admission.entries[entry_id].subject
        subject_sha256     = var.nim_operator_admission.entries[entry_id].subject_sha256
        attestation        = local.verified_runtime_security_authorizations[var.nim_operator_admission.entries[entry_id].authorization_id].attestation
        attestation_sha256 = local.verified_runtime_security_authorizations[var.nim_operator_admission.entries[entry_id].authorization_id].attestation_sha256
      }
    }
  ]
  nim_non_nim_controller_exemptions = [
    for exemption_id in sort(keys(var.nim_operator_admission.non_nim_controller_exemptions)) : {
      subject            = var.nim_operator_admission.non_nim_controller_exemptions[exemption_id].subject
      subject_sha256     = var.nim_operator_admission.non_nim_controller_exemptions[exemption_id].subject_sha256
      authorization_id   = var.nim_operator_admission.non_nim_controller_exemptions[exemption_id].authorization_id
      evidence_sha256    = local.verified_runtime_security_authorizations[var.nim_operator_admission.non_nim_controller_exemptions[exemption_id].authorization_id].evidence_sha256
      attestation        = local.verified_runtime_security_authorizations[var.nim_operator_admission.non_nim_controller_exemptions[exemption_id].authorization_id].attestation
      attestation_sha256 = local.verified_runtime_security_authorizations[var.nim_operator_admission.non_nim_controller_exemptions[exemption_id].authorization_id].attestation_sha256
    }
  ]
  nim_admission_config = {
    schema              = "fs2-serve.nebius.ai/nim-operator-admission-config/v6"
    namespace           = "fs2-models"
    security_session_id = local.runtime_security_authority_session_id
    trusted_attestors   = local.runtime_security_trusted_attestors
    admission_policy    = local.nim_admission_policy_contract
    admission_policy_sha256 = local.nim_admission_policy_sha256
    security_boundary_envelope = local.nim_admission_required ? {
      subject_sha256     = var.nim_operator_admission.security_boundary.subject_sha256
      evidence_sha256    = local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.authorization_id].evidence_sha256
      attestation        = local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.authorization_id].attestation
      attestation_sha256 = local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.authorization_id].attestation_sha256
      provider_custody = {
        subject            = var.nim_operator_admission.security_boundary.provider_subject
        subject_sha256     = var.nim_operator_admission.security_boundary.provider_authorization_sha256
        evidence_sha256    = local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.provider_authorization_id].evidence_sha256
        attestation        = local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.provider_authorization_id].attestation
        attestation_sha256 = local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.provider_authorization_id].attestation_sha256
      }
    } : {}
    non_nim_controller_exemptions = local.nim_non_nim_controller_exemptions
    entries             = local.nim_admission_entries
  }
  nim_admission_config_json = jsonencode(local.nim_admission_config)
  nim_admission_config_name = format(
    "fs2-nim-admission-%s",
    substr(sha256(local.nim_admission_config_json), 0, 16),
  )
  nim_admission_security_chart_sources = {
    boundary = [
      "charts/security/fs2-platform-security-boundary/Chart.yaml",
      "charts/security/fs2-platform-security-boundary/values.yaml",
      "charts/security/fs2-platform-security-boundary/values.schema.json",
      "charts/security/fs2-platform-security-boundary/templates/guard.yaml",
    ]
    backend = [
      "charts/security/fs2-nim-admission-security/Chart.yaml",
      "charts/security/fs2-nim-admission-security/values.yaml",
      "charts/security/fs2-nim-admission-security/values.schema.json",
      "charts/security/fs2-nim-admission-security/templates/_helpers.tpl",
      "charts/security/fs2-nim-admission-security/templates/backend.yaml",
    ]
    receipt = [
      "charts/security/fs2-nim-admission-installation-receipt/Chart.yaml",
      "charts/security/fs2-nim-admission-installation-receipt/values.yaml",
      "charts/security/fs2-nim-admission-installation-receipt/values.schema.json",
      "charts/security/fs2-nim-admission-installation-receipt/templates/receipt.yaml",
    ]
    static_generation = [
      "charts/security/fs2-nim-admission-static-generation/Chart.yaml",
      "charts/security/fs2-nim-admission-static-generation/values.yaml",
      "charts/security/fs2-nim-admission-static-generation/values.schema.json",
      "charts/security/fs2-nim-admission-static-generation/templates/generation.yaml",
    ]
    provider_envelope = [
      "charts/security/fs2-nim-admission-provider-envelope/Chart.yaml",
      "charts/security/fs2-nim-admission-provider-envelope/values.yaml",
      "charts/security/fs2-nim-admission-provider-envelope/values.schema.json",
      "charts/security/fs2-nim-admission-provider-envelope/templates/envelope.yaml",
    ]
    provider_checkpoint = [
      "charts/security/fs2-nim-admission-provider-checkpoint/Chart.yaml",
      "charts/security/fs2-nim-admission-provider-checkpoint/values.yaml",
      "charts/security/fs2-nim-admission-provider-checkpoint/values.schema.json",
      "charts/security/fs2-nim-admission-provider-checkpoint/templates/checkpoint.yaml",
    ]
    provider_head = [
      "charts/security/fs2-nim-admission-provider-head/Chart.yaml",
      "charts/security/fs2-nim-admission-provider-head/values.yaml",
      "charts/security/fs2-nim-admission-provider-head/values.schema.json",
      "charts/security/fs2-nim-admission-provider-head/templates/head.yaml",
    ]
  }
  nim_admission_security_chart_tree_sha256 = {
    for chart, paths in local.nim_admission_security_chart_sources : chart => sha256("${jsonencode({
      for path in paths : path => filesha256("${local.fs2_root}/${path}")
    })}\n")
  }
  nim_admission_projection_contract = {
    schema        = "fs2-serve.nebius.ai/nim-admission-desired-object-projection/v1"
    serialization = "canonical-json-lf/v1"
    fields = ["all top-level desired fields", "metadata desired fields"]
    secret_fields = ["all top-level desired fields except data/stringData", "metadata desired fields"]
    excluded_live_fields = [
      "metadata.creationTimestamp",
      "metadata.deletionGracePeriodSeconds",
      "metadata.deletionTimestamp",
      "metadata.generation",
      "metadata.managedFields",
      "metadata.resourceVersion",
      "metadata.selfLink",
      "metadata.uid",
      "status",
    ]
    secret_data_handling  = "excluded-public-digests-verified-in-process"
    readiness_handling    = "separate-receipt-boolean-and-live-generation"
  }
  nim_admission_security_handoff_subject = {
    schema = "fs2-serve.nebius.ai/nim-admission-security-handoff/v2"
    release = {
      chart       = "charts/security/fs2-nim-admission-security"
      name        = "fs2-nim-admission-security"
      namespace   = "fs2-system"
      principal   = var.nim_operator_admission.security_boundary.principal_epoch.active_principal
      principal_epoch = var.nim_operator_admission.security_boundary.principal_epoch
      workload_release_mode = "observe-only"
      boundary_chart = "charts/security/fs2-platform-security-boundary"
      boundary_release_name = "fs2-platform-security-boundary"
      installation_receipt_chart = "charts/security/fs2-nim-admission-installation-receipt"
      installation_receipt_release_name = "fs2-nim-installation-<subject-sha256-prefix>"
      append_only_generation_charts = {
        static_generation   = "charts/security/fs2-nim-admission-static-generation"
        provider_envelope   = "charts/security/fs2-nim-admission-provider-envelope"
        provider_checkpoint = "charts/security/fs2-nim-admission-provider-checkpoint"
        provider_head       = "charts/security/fs2-nim-admission-provider-head"
        installation_receipt = "charts/security/fs2-nim-admission-installation-receipt"
      }
      immutable_generation_lifecycle = "unique-content-addressed-release-plus-helm-resource-policy-keep"
    }
    backend = {
      image                    = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
      config_map_name          = local.nim_admission_config_name
      config_sha256            = sha256(local.nim_admission_config_json)
      admission_policy_sha256  = local.nim_admission_policy_sha256
      tls_secret_name          = var.nim_operator_admission.tls_secret_name
      tls_secret_uid           = var.nim_operator_admission.tls_secret_uid
      tls_secret_resource_version = var.nim_operator_admission.tls_secret_resource_version
      tls_secret_type          = var.nim_operator_admission.tls_secret_type
      tls_generation_sha256    = var.nim_operator_admission.tls_generation_sha256
      tls_certificate_sha256   = var.nim_operator_admission.tls_certificate_sha256
      tls_public_key_spki_sha256 = var.nim_operator_admission.tls_public_key_spki_sha256
      ca_bundle_sha256         = local.nim_admission_policy_contract.ca_bundle_sha256
      owner_lookup_namespaces  = local.nim_admission_owner_lookup_namespaces
      network_policy           = local.nim_admission_policy_contract.network_policy
    }
    custody = {
      boundary_authorization_sha256 = var.nim_operator_admission.security_boundary.subject_sha256
      provider_authorization_sha256 = var.nim_operator_admission.security_boundary.provider_authorization_sha256
      principal_epoch = var.nim_operator_admission.security_boundary.principal_epoch
      provider_renewal = var.nim_operator_admission.security_boundary.provider_renewal
      owner_lookup_namespaces = local.nim_admission_owner_lookup_namespaces
      webhook_source_cidrs = local.nim_admission_policy_contract.network_policy.webhook_source_cidrs
      protected_release_objects = [
        "ConfigMap/${local.nim_admission_config_name}",
        "ConfigMap/${var.nim_operator_admission.security_boundary.provider_renewal.activation_envelope_name}",
        "ConfigMap/${var.nim_operator_admission.security_boundary.provider_renewal.head_name}",
        "ConfigMap/${var.nim_operator_admission.security_boundary.provider_renewal.checkpoint_name_prefix}${var.nim_operator_admission.security_boundary.provider_renewal.activation_generation}-${substr(var.nim_operator_admission.security_boundary.provider_renewal.activation_subject_sha256, 0, 16)}",
        "ConfigMap/fs2-nim-admission-tls-receipt-${substr(var.nim_operator_admission.tls_generation_sha256, 0, 16)}",
        "Deployment/fs2-serve-control-plane-nim-admission",
        "NetworkPolicy/fs2-serve-control-plane-nim-admission",
        "PodDisruptionBudget/fs2-serve-control-plane-nim-admission",
        "Secret/${var.nim_operator_admission.tls_secret_name}",
        "Service/fs2-serve-control-plane-nim-admission",
        "ServiceAccount/fs2-serve-control-plane-nim-admission",
        "ValidatingAdmissionPolicy/fs2-platform-security-admission-guard",
        "ValidatingAdmissionPolicyBinding/fs2-platform-security-admission-guard",
        "ValidatingAdmissionPolicy/fs2-nim-admission-apply-fence",
        "ValidatingAdmissionPolicyBinding/fs2-nim-admission-apply-fence",
        "Lease/fs2-nim-admission-apply-fence",
        "ConfigMap/fs2-nim-apply-authorization-<nonce-sha256-prefix>",
        "ValidatingWebhookConfiguration/fs2-serve-control-plane-nim-admission",
      ]
    }
    release_artifacts = {
      projection_contract = local.nim_admission_projection_contract
      expected_projections = var.nim_operator_admission.security_release_artifacts.expected_projections
      charts = {
        boundary = {
          source_tree_sha256          = local.nim_admission_security_chart_tree_sha256.boundary
          package_sha256              = var.nim_operator_admission.security_release_artifacts.boundary_package_sha256
          rendered_projection_sha256  = var.nim_operator_admission.security_release_artifacts.boundary_rendered_projection_sha256
        }
        backend = {
          source_tree_sha256          = local.nim_admission_security_chart_tree_sha256.backend
          package_sha256              = var.nim_operator_admission.security_release_artifacts.backend_package_sha256
          rendered_projection_sha256  = var.nim_operator_admission.security_release_artifacts.backend_rendered_projection_sha256
        }
        receipt = {
          source_tree_sha256          = local.nim_admission_security_chart_tree_sha256.receipt
          package_sha256              = var.nim_operator_admission.security_release_artifacts.receipt_package_sha256
          # The receipt's signed subject contains the handoff digest. Binding a
          # rendered receipt digest here would require an impossible SHA-256
          # fixed point. The reviewed receipt source/package is bound here;
          # each full live receipt is authenticated by its post-handoff
          # signature and exact expected-object projection map below.
          rendered_projection_binding = "post-handoff-signed-receipt/v1"
        }
        static_generation = {
          source_tree_sha256 = local.nim_admission_security_chart_tree_sha256.static_generation
          package_sha256     = var.nim_operator_admission.security_release_artifacts.static_generation_package_sha256
          lifecycle          = "unique-release-retained-resource/v1"
        }
        provider_envelope = {
          source_tree_sha256 = local.nim_admission_security_chart_tree_sha256.provider_envelope
          package_sha256     = var.nim_operator_admission.security_release_artifacts.provider_envelope_package_sha256
          lifecycle          = "stage-observe-retain/v1"
        }
        provider_checkpoint = {
          source_tree_sha256 = local.nim_admission_security_chart_tree_sha256.provider_checkpoint
          package_sha256     = var.nim_operator_admission.security_release_artifacts.provider_checkpoint_package_sha256
          lifecycle          = "post-observation-signed-retain/v1"
        }
        provider_head = {
          source_tree_sha256 = local.nim_admission_security_chart_tree_sha256.provider_head
          package_sha256     = var.nim_operator_admission.security_release_artifacts.provider_head_package_sha256
          lifecycle          = "security-controller-cas-current-and-previous/v1"
        }
      }
    }
  }
  nim_admission_security_handoff_sha256 = sha256("${jsonencode(local.nim_admission_security_handoff_subject)}\n")
  nim_admission_owner_lookup_namespaces = sort(distinct(concat(
    ["fs2-models", "fs2-system"],
    flatten([
      for entry in values(var.nim_operator_admission.entries) : [
        for identity in values(try(entry.subject.actor_identities, {})) : identity.namespace
        if try(identity.kind, "") == "pod-bound-service-account"
      ]
    ]),
  )))
  nim_admission_protected_security_objects = [
    for encoded in sort([
      for protected in concat(
        [
          { api_group = "", resource = "configmaps", scope = "namespace:fs2-system", names = [], name_prefixes = ["fs2-nim-admission-", "fs2-nim-installation-", "fs2-nim-apply-authorization-", "fs2-nim-root-"] },
          { api_group = "", resource = "secrets", scope = "namespace:fs2-system", names = [var.nim_operator_admission.tls_secret_name], name_prefixes = [] },
          { api_group = "", resource = "serviceaccounts", scope = "namespace:fs2-system", names = ["fs2-serve-control-plane-nim-admission"], name_prefixes = [] },
          { api_group = "", resource = "services", scope = "namespace:fs2-system", names = ["fs2-serve-control-plane-nim-admission"], name_prefixes = [] },
          { api_group = "admissionregistration.k8s.io", resource = "validatingadmissionpolicies", scope = "cluster", names = ["fs2-platform-security-admission-guard", "fs2-nim-admission-apply-fence", "fs2-scientific-cache-controller-chain", "fs2-scientific-runtime-cache-writer-fence"], name_prefixes = [] },
          { api_group = "admissionregistration.k8s.io", resource = "validatingadmissionpolicybindings", scope = "cluster", names = ["fs2-platform-security-admission-guard", "fs2-nim-admission-apply-fence", "fs2-scientific-cache-controller-chain", "fs2-scientific-runtime-cache-writer-fence"], name_prefixes = [] },
          { api_group = "admissionregistration.k8s.io", resource = "validatingwebhookconfigurations", scope = "cluster", names = ["fs2-serve-control-plane-nim-admission"], name_prefixes = [] },
          { api_group = "apps", resource = "deployments", scope = "namespace:fs2-system", names = ["fs2-serve-control-plane-nim-admission"], name_prefixes = [] },
          { api_group = "apps", resource = "replicasets", scope = "namespace:fs2-system", names = [], name_prefixes = ["fs2-serve-control-plane-nim-admission-"] },
          { api_group = "", resource = "pods", scope = "namespace:fs2-system", names = [], name_prefixes = ["fs2-serve-control-plane-nim-admission-"] },
          { api_group = "discovery.k8s.io", resource = "endpointslices", scope = "namespace:fs2-system", names = [], name_prefixes = ["fs2-serve-control-plane-nim-admission-"] },
          { api_group = "coordination.k8s.io", resource = "leases", scope = "namespace:fs2-system", names = ["fs2-nim-admission-apply-fence"], name_prefixes = [] },
          { api_group = "networking.k8s.io", resource = "networkpolicies", scope = "namespace:fs2-system", names = ["fs2-serve-control-plane-nim-admission"], name_prefixes = [] },
          { api_group = "policy", resource = "poddisruptionbudgets", scope = "namespace:fs2-system", names = ["fs2-serve-control-plane-nim-admission"], name_prefixes = [] },
          { api_group = "rbac.authorization.k8s.io", resource = "clusterroles", scope = "cluster", names = ["fs2-serve-control-plane-nim-policy-reader"], name_prefixes = [] },
          { api_group = "rbac.authorization.k8s.io", resource = "clusterrolebindings", scope = "cluster", names = ["fs2-serve-control-plane-nim-policy-reader"], name_prefixes = [] },
          { api_group = "rbac.authorization.k8s.io", resource = "roles", scope = "namespace:fs2-models", names = ["fs2-serve-control-plane-nim-root-inventory"], name_prefixes = [] },
          { api_group = "rbac.authorization.k8s.io", resource = "rolebindings", scope = "namespace:fs2-models", names = ["fs2-serve-control-plane-nim-root-inventory"], name_prefixes = [] },
          { api_group = "rbac.authorization.k8s.io", resource = "roles", scope = "namespace:fs2-system", names = ["fs2-serve-control-plane-nim-root-enrollment"], name_prefixes = [] },
          { api_group = "rbac.authorization.k8s.io", resource = "rolebindings", scope = "namespace:fs2-system", names = ["fs2-serve-control-plane-nim-root-enrollment"], name_prefixes = [] },
        ],
        flatten([
          for namespace in local.nim_admission_owner_lookup_namespaces : [
            { api_group = "rbac.authorization.k8s.io", resource = "roles", scope = "namespace:${namespace}", names = ["fs2-serve-control-plane-nim-owner-reader"], name_prefixes = [] },
            { api_group = "rbac.authorization.k8s.io", resource = "rolebindings", scope = "namespace:${namespace}", names = ["fs2-serve-control-plane-nim-owner-reader"], name_prefixes = [] },
          ]
        ]),
      ) : jsonencode(protected)
    ]) : jsondecode(encoded)
  ]
  nim_admission_expected_installation_objects = toset(concat(
    [
      "v1|ConfigMap|fs2-system|${local.nim_admission_config_name}",
      "v1|ConfigMap|fs2-system|${var.nim_operator_admission.security_boundary.provider_renewal.activation_envelope_name}",
      "v1|ConfigMap|fs2-system|${var.nim_operator_admission.security_boundary.provider_renewal.checkpoint_name_prefix}${var.nim_operator_admission.security_boundary.provider_renewal.activation_generation}-${substr(var.nim_operator_admission.security_boundary.provider_renewal.activation_subject_sha256, 0, 16)}",
      "v1|ConfigMap|fs2-system|fs2-nim-admission-tls-receipt-${substr(var.nim_operator_admission.tls_generation_sha256, 0, 16)}",
      "v1|Secret|fs2-system|${var.nim_operator_admission.tls_secret_name}",
      "v1|ServiceAccount|fs2-system|fs2-serve-control-plane-nim-admission",
      "v1|Service|fs2-system|fs2-serve-control-plane-nim-admission",
      "apps/v1|Deployment|fs2-system|fs2-serve-control-plane-nim-admission",
      "admissionregistration.k8s.io/v1|ValidatingAdmissionPolicy||fs2-platform-security-admission-guard",
      "admissionregistration.k8s.io/v1|ValidatingAdmissionPolicyBinding||fs2-platform-security-admission-guard",
      "admissionregistration.k8s.io/v1|ValidatingAdmissionPolicy||fs2-nim-admission-apply-fence",
      "admissionregistration.k8s.io/v1|ValidatingAdmissionPolicyBinding||fs2-nim-admission-apply-fence",
      "admissionregistration.k8s.io/v1|ValidatingWebhookConfiguration||fs2-serve-control-plane-nim-admission",
      "coordination.k8s.io/v1|Lease|fs2-system|fs2-nim-admission-apply-fence",
      "rbac.authorization.k8s.io/v1|ClusterRole||fs2-serve-control-plane-nim-policy-reader",
      "rbac.authorization.k8s.io/v1|ClusterRoleBinding||fs2-serve-control-plane-nim-policy-reader",
      "rbac.authorization.k8s.io/v1|Role|fs2-models|fs2-serve-control-plane-nim-root-inventory",
      "rbac.authorization.k8s.io/v1|RoleBinding|fs2-models|fs2-serve-control-plane-nim-root-inventory",
      "rbac.authorization.k8s.io/v1|Role|fs2-system|fs2-serve-control-plane-nim-root-enrollment",
      "rbac.authorization.k8s.io/v1|RoleBinding|fs2-system|fs2-serve-control-plane-nim-root-enrollment",
      "networking.k8s.io/v1|NetworkPolicy|fs2-system|fs2-serve-control-plane-nim-admission",
      "policy/v1|PodDisruptionBudget|fs2-system|fs2-serve-control-plane-nim-admission",
    ],
    flatten([
      for namespace in local.nim_admission_owner_lookup_namespaces : [
        "rbac.authorization.k8s.io/v1|Role|${namespace}|fs2-serve-control-plane-nim-owner-reader",
        "rbac.authorization.k8s.io/v1|RoleBinding|${namespace}|fs2-serve-control-plane-nim-owner-reader",
      ]
    ]),
  ))
  nim_admission_installation_receipt_subject = try(var.nim_operator_admission.installation_receipt.subject, {})
  nim_admission_installation_receipt_sha256 = try(var.nim_operator_admission.installation_receipt.subject_sha256, "")
  nim_admission_installation_receipt_name = "fs2-nim-installation-${substr(local.nim_admission_installation_receipt_sha256, 0, 16)}"
  nim_admission_installation_receipt_json = jsonencode(local.nim_admission_installation_receipt_subject)
  nim_admission_chart_values = {
    enabled         = true
    required        = local.nim_admission_required
    manageSecurityBackend = false
    securityCustody = "external-platform-security"
    replicaCount    = 2
    namespace       = "fs2-models"
    configMapName   = local.nim_admission_required ? local.nim_admission_config_name : ""
    configKey       = "admission.json"
    tlsSecretName   = var.nim_operator_admission.tls_secret_name
    caBundle        = var.nim_operator_admission.ca_bundle
    admissionPolicySha256 = local.nim_admission_required ? local.nim_admission_policy_sha256 : ""
    ownerLookupNamespaces = local.nim_admission_required ? local.nim_admission_owner_lookup_namespaces : []
    service         = { port = 8443 }
    resources = {
      requests = { cpu = "25m", memory = "64Mi" }
      limits   = { cpu = "250m", memory = "256Mi" }
    }
    nodeSelector = {
      "workload.fs2.nebius/system" = "true"
      "capacity.fs2.nebius/type"   = "regular"
      "capacity.fs2.nebius/pool"   = "system"
    }
    tolerations = []
    affinity    = {}
  }
}

resource "terraform_data" "nim_admission_contract" {
  input = {
    required      = local.nim_admission_required
    config_sha256 = sha256(local.nim_admission_config_json)
    entries       = [for entry in local.nim_admission_entries : "${entry.resource_kind}/${entry.model_id}"]
  }

  lifecycle {
    precondition {
      condition = !local.nim_admission_required || (
        local.runtime_security_authority_consistent &&
        length(local.runtime_security_trusted_attestors) > 0 &&
        can(regex("^fs2-nim-admission-tls-[a-f0-9]{16}$", var.nim_operator_admission.tls_secret_name)) &&
        can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", var.nim_operator_admission.tls_secret_uid)) &&
        can(regex("^[1-9][0-9]*$", var.nim_operator_admission.tls_secret_resource_version)) &&
        var.nim_operator_admission.tls_secret_type == "kubernetes.io/tls" &&
        can(regex("^[a-f0-9]{64}$", var.nim_operator_admission.tls_generation_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.nim_operator_admission.tls_certificate_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.nim_operator_admission.tls_public_key_spki_sha256)) &&
        var.nim_operator_admission.tls_generation_sha256 == sha256("${var.nim_operator_admission.tls_certificate_sha256}\n${var.nim_operator_admission.tls_public_key_spki_sha256}\n${local.nim_admission_policy_contract.ca_bundle_sha256}\n") &&
        var.nim_operator_admission.tls_secret_name == "fs2-nim-admission-tls-${substr(var.nim_operator_admission.tls_generation_sha256, 0, 16)}" &&
        length(var.nim_operator_admission.ca_bundle) >= 1 &&
        can(base64decode(var.nim_operator_admission.ca_bundle)) &&
        try(var.nim_operator_admission.security_boundary.name, "") == "fs2-platform-security-admission-guard" &&
        can(regex("^[0-9a-f-]{36}$", try(var.nim_operator_admission.security_boundary.policy_uid, ""))) &&
        can(regex("^[1-9][0-9]*$", try(var.nim_operator_admission.security_boundary.policy_resource_version, ""))) &&
        can(regex("^[0-9a-f-]{36}$", try(var.nim_operator_admission.security_boundary.binding_uid, ""))) &&
        can(regex("^[1-9][0-9]*$", try(var.nim_operator_admission.security_boundary.binding_resource_version, ""))) &&
        can(regex("^[a-f0-9]{64}$", try(var.nim_operator_admission.security_boundary.subject_sha256, ""))) &&
        can(regex("^[0-9a-f-]{36}$", try(var.nim_operator_admission.security_boundary.cluster_uid, ""))) &&
        can(regex("^[a-f0-9]{64}$", try(var.nim_operator_admission.security_boundary.provider_authorization_sha256, ""))) &&
        sha256("${jsonencode(var.nim_operator_admission.security_boundary.provider_subject)}\n") == var.nim_operator_admission.security_boundary.provider_authorization_sha256 &&
        var.nim_operator_admission.security_boundary.owner_lookup_namespaces == local.nim_admission_owner_lookup_namespaces &&
        try(var.nim_operator_admission.security_boundary.provider_subject.owner_lookup_namespaces, []) == local.nim_admission_owner_lookup_namespaces &&
        try(var.nim_operator_admission.security_boundary.principal_epoch.schema, "") == "fs2-serve.nebius.ai/platform-security-principal-epoch/v1" &&
        try(var.nim_operator_admission.security_boundary.principal_epoch.generation, 0) >= 1 &&
        can(regex("^[a-f0-9]{64}$", try(var.nim_operator_admission.security_boundary.principal_epoch.epoch_id, ""))) &&
        try(var.nim_operator_admission.security_boundary.principal_epoch.active_principal, "") == "fs2-platform-security-external-automation-${try(var.nim_operator_admission.security_boundary.principal_epoch.epoch_id, "")}" &&
        can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", try(var.nim_operator_admission.security_boundary.principal_epoch.activated_at, ""))) &&
        try(var.nim_operator_admission.security_boundary.principal_epoch.max_overlap_seconds, 0) >= 60 &&
        try(var.nim_operator_admission.security_boundary.principal_epoch.max_overlap_seconds, 0) <= 900 &&
        length(try(var.nim_operator_admission.security_boundary.principal_epoch.overlap_principals, [])) <= 1 &&
        length(try(var.nim_operator_admission.security_boundary.principal_epoch.retained_predecessors, [])) == try(var.nim_operator_admission.security_boundary.principal_epoch.generation, 0) - 1 &&
        try(var.nim_operator_admission.security_boundary.principal_epoch.predecessor_chain_sha256, "") == sha256("${jsonencode(try(var.nim_operator_admission.security_boundary.principal_epoch.retained_predecessors, []))}\n") &&
        alltrue([
          for index, predecessor in try(var.nim_operator_admission.security_boundary.principal_epoch.retained_predecessors, []) :
          predecessor.generation == index + 1 &&
          contains(["denied", "expired"], predecessor.disposition) &&
          predecessor.principal == "fs2-platform-security-external-automation-${predecessor.epoch_id}" &&
          can(regex("^[a-f0-9]{64}$", predecessor.epoch_id)) &&
          can(regex("^[a-f0-9]{64}$", predecessor.evidence_sha256)) &&
          can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", predecessor.effective_at))
        ]) &&
        alltrue([
          for overlap in try(var.nim_operator_admission.security_boundary.principal_epoch.overlap_principals, []) :
          overlap.generation == var.nim_operator_admission.security_boundary.principal_epoch.generation + 1 &&
          overlap.principal == "fs2-platform-security-external-automation-${overlap.epoch_id}" &&
          can(regex("^[a-f0-9]{64}$", overlap.epoch_id)) &&
          can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", overlap.not_after))
        ]) &&
        !contains([
          for predecessor in try(var.nim_operator_admission.security_boundary.principal_epoch.retained_predecessors, []) : predecessor.principal
        ], try(var.nim_operator_admission.security_boundary.principal_epoch.active_principal, "")) &&
        alltrue([
          for overlap in try(var.nim_operator_admission.security_boundary.principal_epoch.overlap_principals, []) :
          !contains([
            for predecessor in try(var.nim_operator_admission.security_boundary.principal_epoch.retained_predecessors, []) : predecessor.principal
          ], overlap.principal)
        ]) &&
        try(var.nim_operator_admission.security_boundary.provider_subject.principal_epoch, {}) == var.nim_operator_admission.security_boundary.principal_epoch &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.schema, "") == "fs2-serve.nebius.ai/provider-custody-renewal/v2" &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.namespace, "") == "fs2-system" &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.envelope_name_prefix, "") == "fs2-nim-admission-provider-envelope-" &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.checkpoint_name_prefix, "") == "fs2-nim-admission-provider-renewal-checkpoint-" &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.receipt_name_prefix, "") == "fs2-nim-installation-" &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.head_name, "") == "fs2-nim-admission-provider-head" &&
        can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", try(var.nim_operator_admission.security_boundary.provider_renewal.head_uid, ""))) &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.activation_head_generation, 0) >= 1 &&
        can(regex("^[a-f0-9]{64}$", try(var.nim_operator_admission.security_boundary.provider_renewal.activation_head_subject_sha256, ""))) &&
        can(regex("^[a-f0-9]{64}$", try(var.nim_operator_admission.security_boundary.provider_renewal.activation_head_chain_sha256, ""))) &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.activation_generation, 0) >= 1 &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.activation_subject_sha256, "") == var.nim_operator_admission.security_boundary.provider_authorization_sha256 &&
        can(regex("^(|[a-f0-9]{64})$", try(var.nim_operator_admission.security_boundary.provider_renewal.activation_previous_subject_sha256, ""))) &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.activation_envelope_name, "") == "fs2-nim-admission-provider-envelope-${var.nim_operator_admission.security_boundary.provider_renewal.activation_generation}-${substr(var.nim_operator_admission.security_boundary.provider_renewal.activation_subject_sha256, 0, 16)}" &&
        can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", try(var.nim_operator_admission.security_boundary.provider_renewal.activation_envelope_uid, ""))) &&
        can(regex("^[1-9][0-9]*$", try(var.nim_operator_admission.security_boundary.provider_renewal.activation_envelope_resource_version, ""))) &&
        can(regex("^[a-f0-9]{64}$", try(var.nim_operator_admission.security_boundary.provider_renewal.activation_envelope_projection_sha256, ""))) &&
        length(try(var.nim_operator_admission.security_boundary.provider_renewal.controller_resource_id, "")) > 0 &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.refresh_interval_seconds, 0) >= 30 &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.refresh_interval_seconds, 0) <= 300 &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.max_observation_age_seconds, 0) >= 120 &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.max_observation_age_seconds, 0) <= 900 &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.max_clock_skew_seconds, 0) >= 0 &&
        try(var.nim_operator_admission.security_boundary.provider_renewal.max_clock_skew_seconds, 0) <= 300 &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.schema, "") == "fs2-serve.nebius.ai/provider-custody-renewal/v2" &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.namespace, "") == var.nim_operator_admission.security_boundary.provider_renewal.namespace &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.envelope_name_prefix, "") == var.nim_operator_admission.security_boundary.provider_renewal.envelope_name_prefix &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.checkpoint_name_prefix, "") == var.nim_operator_admission.security_boundary.provider_renewal.checkpoint_name_prefix &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.receipt_name_prefix, "") == var.nim_operator_admission.security_boundary.provider_renewal.receipt_name_prefix &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.head_name, "") == var.nim_operator_admission.security_boundary.provider_renewal.head_name &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.head_uid, "") == var.nim_operator_admission.security_boundary.provider_renewal.head_uid &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.activation_head_generation, 0) == var.nim_operator_admission.security_boundary.provider_renewal.activation_head_generation &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.activation_head_subject_sha256, "") == var.nim_operator_admission.security_boundary.provider_renewal.activation_head_subject_sha256 &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.activation_head_chain_sha256, "") == var.nim_operator_admission.security_boundary.provider_renewal.activation_head_chain_sha256 &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.controller_resource_id, "") == var.nim_operator_admission.security_boundary.provider_renewal.controller_resource_id &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.refresh_interval_seconds, 0) == var.nim_operator_admission.security_boundary.provider_renewal.refresh_interval_seconds &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.max_observation_age_seconds, 0) == var.nim_operator_admission.security_boundary.provider_renewal.max_observation_age_seconds &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.max_clock_skew_seconds, 0) == var.nim_operator_admission.security_boundary.provider_renewal.max_clock_skew_seconds &&
        toset(keys(try(var.nim_operator_admission.provider_head.envelope, {}))) == toset(["subject", "subject_sha256", "evidence_sha256", "attestation", "attestation_sha256"]) &&
        toset(keys(try(var.nim_operator_admission.provider_head.envelope.subject, {}))) == toset(["schema", "cluster_uid", "security_session_id", "security_handoff_sha256", "generation", "previous_head_subject_sha256", "updated_at", "valid_until", "max_age_seconds", "current", "previous", "anchor"]) &&
        try(var.nim_operator_admission.provider_head.envelope.subject.schema, "") == "fs2-serve.nebius.ai/provider-custody-renewal-head/v1" &&
        try(var.nim_operator_admission.provider_head.envelope.subject.cluster_uid, "") == var.nim_operator_admission.security_boundary.cluster_uid &&
        try(var.nim_operator_admission.provider_head.envelope.subject.security_handoff_sha256, "") == local.nim_admission_security_handoff_sha256 &&
        try(var.nim_operator_admission.provider_head.envelope.subject.generation, 0) == var.nim_operator_admission.security_boundary.provider_renewal.activation_head_generation &&
        try(var.nim_operator_admission.provider_head.envelope.subject.anchor, {}) == {
          generation     = var.nim_operator_admission.security_boundary.provider_renewal.activation_head_generation
          subject_sha256 = var.nim_operator_admission.security_boundary.provider_renewal.activation_head_subject_sha256
          chain_sha256   = var.nim_operator_admission.security_boundary.provider_renewal.activation_head_chain_sha256
        } &&
        try(var.nim_operator_admission.provider_head.envelope.subject_sha256, "") == sha256("${jsonencode(var.nim_operator_admission.provider_head.envelope.subject)}\n") &&
        try(var.nim_operator_admission.provider_head.envelope_sha256, "") == sha256("${jsonencode(var.nim_operator_admission.provider_head.envelope)}\n") &&
        try(var.nim_operator_admission.provider_head.envelope.attestation_sha256, "") == sha256("${jsonencode(var.nim_operator_admission.provider_head.envelope.attestation)}\n") &&
        (
          (try(var.nim_operator_admission.provider_head.head_uid, "") == "" && try(var.nim_operator_admission.provider_head.head_resource_version, "") == "") ||
          (
            try(var.nim_operator_admission.provider_head.head_uid, "") == var.nim_operator_admission.security_boundary.provider_renewal.head_uid &&
            can(regex("^[1-9][0-9]*$", try(var.nim_operator_admission.provider_head.head_resource_version, "")))
          )
        ) &&
        try(var.nim_operator_admission.security_boundary.provider_subject.renewal.generation, 0) >= 1 &&
        can(regex("^(|[a-f0-9]{64})$", try(var.nim_operator_admission.security_boundary.provider_subject.renewal.previous_subject_sha256, ""))) &&
        can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", try(var.nim_operator_admission.security_boundary.provider_subject.renewal.refreshed_at, ""))) &&
        can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", try(var.nim_operator_admission.security_boundary.provider_subject.renewal.refresh_deadline, ""))) &&
        contains(try(var.nim_operator_admission.security_boundary.provider_subject.collector.resource_ids, []), var.nim_operator_admission.security_boundary.provider_renewal.controller_resource_id) &&
        try(var.nim_operator_admission.security_boundary.provider_subject.webhook_source_cidrs, []) == local.nim_admission_policy_contract.network_policy.webhook_source_cidrs &&
        length(local.nim_admission_policy_contract.network_policy.webhook_source_cidrs) > 0 &&
        alltrue([for cidr in concat(local.nim_admission_policy_contract.network_policy.webhook_source_cidrs, local.nim_admission_policy_contract.network_policy.kubernetes_api_cidrs) : can(cidrhost(cidr, 0)) && cidr == cidrsubnet(cidr, 0, 0) && !contains(["0.0.0.0/0", "::/0"], cidr)]) &&
        try(var.nim_operator_admission.security_boundary.provider_subject.protected_objects, []) == local.nim_admission_protected_security_objects &&
        contains(keys(local.verified_runtime_security_authorizations), try(var.nim_operator_admission.security_boundary.provider_authorization_id, "")) &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.provider_authorization_id].kind == "provider-admission-custody" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.provider_authorization_id].model_id == "platform" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.provider_authorization_id].subject_schema == "fs2-serve.nebius.ai/platform-security-provider-custody/v2" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.provider_authorization_id].subject_sha256 == var.nim_operator_admission.security_boundary.provider_authorization_sha256 &&
        contains(keys(local.verified_runtime_security_authorizations), try(var.nim_operator_admission.security_boundary.authorization_id, "")) &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.authorization_id].kind == "platform-admission-boundary" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.authorization_id].model_id == "platform" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.authorization_id].subject_schema == "fs2-serve.nebius.ai/platform-security-admission-boundary/v3" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_boundary.authorization_id].subject_sha256 == var.nim_operator_admission.security_boundary.subject_sha256 &&
        var.nim_operator_admission.security_handoff.subject == local.nim_admission_security_handoff_subject &&
        var.nim_operator_admission.security_handoff.subject_sha256 == local.nim_admission_security_handoff_sha256 &&
        contains(keys(local.verified_runtime_security_authorizations), var.nim_operator_admission.security_handoff.authorization_id) &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_handoff.authorization_id].kind == "nim-admission-security-handoff" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_handoff.authorization_id].model_id == "platform" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_handoff.authorization_id].subject_schema == "fs2-serve.nebius.ai/nim-admission-security-handoff/v2" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.security_handoff.authorization_id].subject_sha256 == local.nim_admission_security_handoff_sha256 &&
        var.nim_operator_admission.security_release_artifacts.projection_schema == local.nim_admission_projection_contract.schema &&
        toset(keys(var.nim_operator_admission.security_release_artifacts.expected_projections)) == local.nim_admission_expected_installation_objects &&
        alltrue([
          for digest in values(var.nim_operator_admission.security_release_artifacts.expected_projections) :
          can(regex("^[a-f0-9]{64}$", digest))
        ]) &&
        alltrue([
          for digest in [
            var.nim_operator_admission.security_release_artifacts.boundary_package_sha256,
            var.nim_operator_admission.security_release_artifacts.boundary_rendered_projection_sha256,
            var.nim_operator_admission.security_release_artifacts.backend_package_sha256,
            var.nim_operator_admission.security_release_artifacts.backend_rendered_projection_sha256,
            var.nim_operator_admission.security_release_artifacts.receipt_package_sha256,
            var.nim_operator_admission.security_release_artifacts.static_generation_package_sha256,
            var.nim_operator_admission.security_release_artifacts.provider_envelope_package_sha256,
            var.nim_operator_admission.security_release_artifacts.provider_checkpoint_package_sha256,
            var.nim_operator_admission.security_release_artifacts.provider_head_package_sha256,
          ] : can(regex("^[a-f0-9]{64}$", digest))
        ]) &&
        try(local.nim_admission_installation_receipt_subject.schema, "") == "fs2-serve.nebius.ai/nim-admission-installation-receipt/v5" &&
        toset(keys(local.nim_admission_installation_receipt_subject)) == toset(["schema", "cluster_uid", "security_handoff_sha256", "installed_at", "owner_lookup_namespaces", "network_policy", "tls", "principal_epoch", "provider_head", "apply_fence", "objects"]) &&
        try(local.nim_admission_installation_receipt_subject.cluster_uid, "") == var.nim_operator_admission.security_boundary.cluster_uid &&
        try(local.nim_admission_installation_receipt_subject.security_handoff_sha256, "") == local.nim_admission_security_handoff_sha256 &&
        try(local.nim_admission_installation_receipt_subject.owner_lookup_namespaces, []) == local.nim_admission_owner_lookup_namespaces &&
        try(local.nim_admission_installation_receipt_subject.network_policy, {}) == local.nim_admission_policy_contract.network_policy &&
        try(local.nim_admission_installation_receipt_subject.tls, {}) == local.nim_admission_policy_contract.tls &&
        try(local.nim_admission_installation_receipt_subject.principal_epoch, {}) == var.nim_operator_admission.security_boundary.principal_epoch &&
        try(local.nim_admission_installation_receipt_subject.provider_head, {}) == {
          name                = var.nim_operator_admission.security_boundary.provider_renewal.head_name
          uid                 = var.nim_operator_admission.security_boundary.provider_renewal.head_uid
          schema              = "fs2-serve.nebius.ai/provider-custody-renewal-head/v1"
          update_policy       = "external-cas-monotonic-signed"
          bounded_generations = 2
        } &&
        try(local.nim_admission_installation_receipt_subject.apply_fence, {}) == {
          lease_name                = "fs2-nim-admission-apply-fence"
          policy_name               = "fs2-nim-admission-apply-fence"
          binding_name              = "fs2-nim-admission-apply-fence"
          authorization_name_prefix = "fs2-nim-apply-authorization-"
          authorization_schema      = "fs2-serve.nebius.ai/nim-admission-apply-fence/v1"
          lifecycle                 = "immutable-generation-retained-no-delete"
        } &&
        can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", try(local.nim_admission_installation_receipt_subject.installed_at, ""))) &&
        toset([
          for object in try(local.nim_admission_installation_receipt_subject.objects, []) :
          "${object.api_version}|${object.kind}|${object.namespace}|${object.name}"
        ]) == local.nim_admission_expected_installation_objects &&
        length(try(local.nim_admission_installation_receipt_subject.objects, [])) == length(local.nim_admission_expected_installation_objects) &&
        alltrue([
          for object in try(local.nim_admission_installation_receipt_subject.objects, []) :
          toset(keys(object)) == toset(["api_version", "kind", "namespace", "name", "uid", "resource_version", "generation", "projection_sha256"]) &&
          can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", object.uid)) &&
          can(regex("^[1-9][0-9]*$", object.resource_version)) &&
          length(object.generation) > 0 &&
          can(regex("^[a-f0-9]{64}$", object.projection_sha256)) &&
          object.projection_sha256 == try(var.nim_operator_admission.security_release_artifacts.expected_projections["${object.api_version}|${object.kind}|${object.namespace}|${object.name}"], "")
        ]) &&
        local.nim_admission_installation_receipt_sha256 == sha256("${local.nim_admission_installation_receipt_json}\n") &&
        contains(keys(local.verified_runtime_security_authorizations), var.nim_operator_admission.installation_receipt.authorization_id) &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.installation_receipt.authorization_id].kind == "nim-admission-installation-receipt" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.installation_receipt.authorization_id].model_id == "platform" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.installation_receipt.authorization_id].subject_schema == "fs2-serve.nebius.ai/nim-admission-installation-receipt/v5" &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.installation_receipt.authorization_id].subject_sha256 == local.nim_admission_installation_receipt_sha256 &&
        local.verified_runtime_security_authorizations[var.nim_operator_admission.installation_receipt.authorization_id].attestation.issued_at == local.nim_admission_installation_receipt_subject.installed_at &&
        toset([
          for entry in local.nim_admission_entries : "${entry.resource_kind}/${entry.model_id}"
        ]) == local.nim_admission_required_subjects &&
        alltrue([
          for entry in local.nim_admission_entries :
          try(entry.security_envelope.subject.admission_policy, "") == local.nim_admission_policy_contract.name &&
          try(entry.security_envelope.subject.admission_policy_sha256, "") == local.nim_admission_policy_sha256
        ]) &&
        alltrue([
          for exemption in values(var.nim_operator_admission.non_nim_controller_exemptions) :
          try(exemption.subject.schema, "") == "fs2-serve.nebius.ai/non-nim-controller-exemption/v1" &&
          try(exemption.subject.reason, "") == "cold-start-compatibility" &&
          exemption.subject_sha256 == sha256("${jsonencode(exemption.subject)}\n") &&
          contains(keys(local.verified_runtime_security_authorizations), exemption.authorization_id) &&
          local.verified_runtime_security_authorizations[exemption.authorization_id].kind == "non-nim-controller-exemption" &&
          local.verified_runtime_security_authorizations[exemption.authorization_id].model_id == exemption.subject.model_id &&
          local.verified_runtime_security_authorizations[exemption.authorization_id].subject_schema == "fs2-serve.nebius.ai/non-nim-controller-exemption/v1" &&
          local.verified_runtime_security_authorizations[exemption.authorization_id].subject_sha256 == exemption.subject_sha256
        ]) &&
        length(local.nim_admission_config_json) <= 900000
      )
      error_message = "NIM admission requires exactly one externally verified NIMCache and NIMService envelope for every selected NIM model under the fixed runtime-security authority."
    }
  }
}

# Phase zero is intentionally smaller than the backend proposal. It contains no
# policy/binding UID or resourceVersion and can therefore install the external
# boundary without placeholder live identities.
resource "terraform_data" "nim_admission_boundary_prepare_contract" {
  input = {
    schema = "fs2-serve.nebius.ai/nim-admission-boundary-prepare/v1"
    boundary_chart = "charts/security/fs2-platform-security-boundary"
    boundary_chart_tree_sha256 = local.nim_admission_security_chart_tree_sha256.boundary
    fixed_release = {
      name      = "fs2-platform-security-boundary"
      namespace = "fs2-system"
      custody   = "external-platform-security"
    }
    external_inputs = [
      "cluster UID",
      "epoch-unique principal and credential-expiry evidence",
      "complete native provider IAM and Kubernetes authorization evidence",
      "dynamic owner lookup namespaces",
    ]
    forbidden_inputs = [
      "signed authorization",
      "policy or binding UID/resourceVersion",
      "TLS Secret identity",
      "installation receipt",
      "Kubernetes data source",
    ]
  }
}

# Phase one is evaluated only after Platform Security has installed and observed
# the boundary. It binds the exact policy/binding UID/resourceVersion into the
# config generation and unsigned backend handoff. It deliberately does not read
# the later installation receipt; phases two and three install the backend,
# observe immutable identities, sign the receipt, and prove runtime readiness.
resource "terraform_data" "nim_admission_prepare_contract" {
  input = {
    schema                  = "fs2-serve.nebius.ai/nim-admission-security-prepare/v1"
    config_sha256           = sha256(local.nim_admission_config_json)
    policy_sha256           = local.nim_admission_policy_sha256
    proposed_handoff        = local.nim_admission_security_handoff_subject
    proposed_handoff_sha256 = local.nim_admission_security_handoff_sha256
    chart_tree_sha256       = local.nim_admission_security_chart_tree_sha256
  }

  lifecycle {
    precondition {
      condition = !local.nim_admission_required || (
        local.runtime_security_authority_consistent &&
        var.nim_operator_admission.security_release_artifacts.projection_schema == local.nim_admission_projection_contract.schema &&
        toset(keys(var.nim_operator_admission.security_release_artifacts.expected_projections)) == local.nim_admission_expected_installation_objects &&
        alltrue([
          for digest in concat(
            values(local.nim_admission_security_chart_tree_sha256),
            values(var.nim_operator_admission.security_release_artifacts.expected_projections),
            [
              var.nim_operator_admission.security_release_artifacts.boundary_package_sha256,
              var.nim_operator_admission.security_release_artifacts.boundary_rendered_projection_sha256,
              var.nim_operator_admission.security_release_artifacts.backend_package_sha256,
              var.nim_operator_admission.security_release_artifacts.backend_rendered_projection_sha256,
              var.nim_operator_admission.security_release_artifacts.receipt_package_sha256,
              var.nim_operator_admission.security_release_artifacts.static_generation_package_sha256,
              var.nim_operator_admission.security_release_artifacts.provider_envelope_package_sha256,
              var.nim_operator_admission.security_release_artifacts.provider_checkpoint_package_sha256,
              var.nim_operator_admission.security_release_artifacts.provider_head_package_sha256,
            ],
          ) : can(regex("^[a-f0-9]{64}$", digest))
        ])
      )
      error_message = "The phase-one backend prepare requires externally observed boundary identities and complete reviewed static chart/package/projection inputs, but never a signed handoff or installation receipt."
    }
  }
}

data "kubernetes_config_map_v1" "nim_admission" {
  count = local.nim_admission_required ? 1 : 0

  metadata {
    name      = local.nim_admission_config_name
    namespace = "fs2-system"
  }

  lifecycle {
    postcondition {
      condition = (
        self.immutable == true &&
        try(self.metadata[0].labels["app.kubernetes.io/managed-by"], "") == "platform-security" &&
        try(self.metadata[0].labels["fs2-serve.nebius.ai/immutable-security-boundary"], "") == "true" &&
        try(self.data["admission.json"], "") == local.nim_admission_config_json
      )
      error_message = "The workload release only observes the immutable Platform Security-owned NIM admission ConfigMap; its exact content and custody labels must already exist."
    }
  }
  depends_on = [terraform_data.nim_admission_contract]
}

data "kubernetes_config_map_v1" "nim_admission_installation_receipt" {
  count = local.nim_admission_required ? 1 : 0

  metadata {
    name      = local.nim_admission_installation_receipt_name
    namespace = "fs2-system"
  }

  lifecycle {
    postcondition {
      condition = (
        self.immutable == true &&
        try(self.metadata[0].labels["app.kubernetes.io/managed-by"], "") == "platform-security" &&
        try(self.metadata[0].labels["fs2-serve.nebius.ai/immutable-security-boundary"], "") == "true" &&
        try(self.data["subject.json"], "") == local.nim_admission_installation_receipt_json &&
        try(self.data["subject.sha256"], "") == local.nim_admission_installation_receipt_sha256 &&
        try(self.data["attestation.json"], "") == jsonencode(local.verified_runtime_security_authorizations[var.nim_operator_admission.installation_receipt.authorization_id].attestation) &&
        try(self.data["attestation.sha256"], "") == local.verified_runtime_security_authorizations[var.nim_operator_admission.installation_receipt.authorization_id].attestation_sha256
      )
      error_message = "NIM workloads require the exact immutable Platform Security-signed live installation/readiness receipt before any NIM resource can be created."
    }
  }

  depends_on = [terraform_data.nim_admission_contract]
}

# Enforcement phase only: re-read every non-secret object named by the fresh
# signed receipt immediately before workload mutation. Secret payload bytes are
# deliberately never imported into Terraform state; their exact generation,
# UID/resourceVersion and public digests are attested in the receipt, protected
# by the external boundary, and continuously verified from the mounted files by
# the admission process.
locals {
  nim_admission_receipt_objects_by_key = {
    for object in try(local.nim_admission_installation_receipt_subject.objects, []) :
    "${object.api_version}|${object.kind}|${object.namespace}|${object.name}" => object
  }
}

data "kubernetes_resource" "nim_admission_live_object" {
  for_each = local.nim_admission_required ? {
    for key, object in local.nim_admission_receipt_objects_by_key : key => object
    if object.kind != "Secret"
  } : {}

  api_version = each.value.api_version
  kind        = each.value.kind
  metadata {
    name      = each.value.name
    namespace = each.value.namespace == "" ? null : each.value.namespace
  }

  depends_on = [data.kubernetes_config_map_v1.nim_admission_installation_receipt]
}

data "kubernetes_resources" "nim_admission_endpoint_slices" {
  count          = local.nim_admission_required ? 1 : 0
  api_version    = "discovery.k8s.io/v1"
  kind           = "EndpointSlice"
  namespace      = "fs2-system"
  label_selector = "kubernetes.io/service-name=fs2-serve-control-plane-nim-admission"

  depends_on = [data.kubernetes_config_map_v1.nim_admission_installation_receipt]
}

locals {
  nim_admission_live_projections = {
    for key, live in data.kubernetes_resource.nim_admission_live_object : key => merge(
      {
        for field, value in live.object : field => value
        if !contains(["metadata", "status"], field)
      },
      {
        metadata = {
          for field, value in live.object.metadata : field => value
          if !contains([
            "creationTimestamp",
            "deletionGracePeriodSeconds",
            "deletionTimestamp",
            "generation",
            "managedFields",
            "resourceVersion",
            "selfLink",
            "uid",
          ], field)
        }
      },
    )
  }
  nim_admission_ready_endpoint_addresses = local.nim_admission_required ? flatten([
    for slice in data.kubernetes_resources.nim_admission_endpoint_slices[0].objects : flatten([
      for endpoint in try(slice.endpoints, []) : try(endpoint.addresses, [])
      if try(endpoint.conditions.ready, false) == true
    ])
  ]) : []
}

resource "terraform_data" "nim_admission_live_installation_gate" {
  input = {
    required       = local.nim_admission_required
    receipt_sha256 = local.nim_admission_installation_receipt_sha256
    live_object_resource_versions = {
      for key, live in data.kubernetes_resource.nim_admission_live_object :
      key => try(live.object.metadata.resourceVersion, "")
    }
  }

  lifecycle {
    precondition {
      condition = !local.nim_admission_required || (
        length(data.kubernetes_resource.nim_admission_live_object) == length(local.nim_admission_expected_installation_objects) - 1 &&
        alltrue([
          for key, live in data.kubernetes_resource.nim_admission_live_object :
          try(live.object.metadata.uid, "") == local.nim_admission_receipt_objects_by_key[key].uid &&
          try(live.object.metadata.resourceVersion, "") == local.nim_admission_receipt_objects_by_key[key].resource_version &&
          tostring(try(live.object.metadata.generation, 0)) == local.nim_admission_receipt_objects_by_key[key].generation &&
          sha256("${jsonencode(local.nim_admission_live_projections[key])}\n") == local.nim_admission_receipt_objects_by_key[key].projection_sha256 &&
          local.nim_admission_receipt_objects_by_key[key].projection_sha256 == var.nim_operator_admission.security_release_artifacts.expected_projections[key]
        ])
      )
      error_message = "NIM planning requires the long-lived signed installation receipt joined to exact live non-secret object bytes. Apply-time freshness and attributed two-endpoint activation are enforced outside this plan by the fenced NIM-root stage and the admission runtime."
    }
  }

  depends_on = [
    data.kubernetes_config_map_v1.nim_admission,
    data.kubernetes_config_map_v1.nim_admission_installation_receipt,
    data.kubernetes_resource.nim_admission_live_object,
    data.kubernetes_resources.nim_admission_endpoint_slices,
  ]
}
