locals {
  grafana_admin_session_publication      = data.terraform_remote_state.foundation.outputs.grafana_admin_session_publication_contract
  grafana_admin_session_enabled          = local.grafana_admin_session_publication.phase != "disabled"
  grafana_admin_session_attach           = local.grafana_admin_session_publication.phase == "attach"
  grafana_admin_session_route_name       = "fs2-admin-grafana"
  grafana_admin_session_deny_filter_name = "fs2-admin-grafana-prepare-deny"
  grafana_admin_session_login_rule       = "grafana-login"
  grafana_admin_session_main_rule        = "grafana"
  grafana_admin_session_login_path       = "${local.grafana_admin_session_publication.path}/login"
  grafana_admin_session_login_limit = {
    requests = 5
    unit     = "Minute"
  }
  grafana_admin_session_labels = merge(local.common_labels, {
    "app.kubernetes.io/component"                       = "admin-observability"
    "fs2-serve.nebius.ai/authentication"                = "admin-session-external-authorization"
    "fs2-serve.nebius.ai/public-backend"                = "grafana-only"
    "fs2-serve.nebius.ai/publication-phase"             = local.grafana_admin_session_publication.phase
    "fs2-serve.nebius.ai/required-policy-statuses"      = "Accepted-True"
    "fs2-serve.nebius.ai/required-route-parent-statuses" = "Accepted-True_ResolvedRefs-True"
  })
  grafana_admin_session_parent_ref = {
    group       = "gateway.networking.k8s.io"
    kind        = "Gateway"
    name        = local.grafana_admin_session_publication.gateway_name
    namespace   = local.grafana_admin_session_publication.route_namespace
    sectionName = local.grafana_admin_session_publication.listener_name
  }
  grafana_admin_session_backend_ref = {
    group     = ""
    kind      = "Service"
    name      = local.grafana_admin_session_publication.service_name
    namespace = local.grafana_admin_session_publication.namespace
    port      = local.grafana_admin_session_publication.service_port
    weight    = 1
  }
  grafana_admin_session_route_rules = [
    {
      name = local.grafana_admin_session_login_rule
      matches = [{
        path = {
          type  = "Exact"
          value = local.grafana_admin_session_login_path
        }
      }]
      backendRefs = [local.grafana_admin_session_backend_ref]
    },
    {
      name = local.grafana_admin_session_main_rule
      matches = [{
        path = {
          type  = "PathPrefix"
          value = local.grafana_admin_session_publication.path
        }
      }]
      backendRefs = [local.grafana_admin_session_backend_ref]
    },
  ]
  grafana_admin_session_deny_filter_ref = {
    group = "gateway.envoyproxy.io"
    kind  = "HTTPRouteFilter"
    name  = local.grafana_admin_session_deny_filter_name
  }
  grafana_admin_session_quarantine_rules = [
    {
      name = local.grafana_admin_session_login_rule
      matches = [{
        path = {
          type  = "Exact"
          value = local.grafana_admin_session_login_path
        }
      }]
      filters = [{
        type         = "ExtensionRef"
        extensionRef = local.grafana_admin_session_deny_filter_ref
      }]
    },
    {
      name = local.grafana_admin_session_main_rule
      matches = [{
        path = {
          type  = "PathPrefix"
          value = local.grafana_admin_session_publication.path
        }
      }]
      filters = [{
        type         = "ExtensionRef"
        extensionRef = local.grafana_admin_session_deny_filter_ref
      }]
    },
  ]
  grafana_admin_session_deny_filter_spec = {
    directResponse = {
      contentType = "text/plain"
      statusCode  = 403
      body = {
        type   = "Inline"
        inline = "Forbidden"
      }
    }
  }
  grafana_admin_session_reference_grant_spec = {
    from = [{
      group     = "gateway.networking.k8s.io"
      kind      = "HTTPRoute"
      namespace = local.grafana_admin_session_publication.route_namespace
    }]
    to = [{
      group = ""
      kind  = "Service"
      name  = local.grafana_admin_session_publication.service_name
    }]
  }
  grafana_admin_session_security_policy_spec = {
    targetRefs = [{
      group = "gateway.networking.k8s.io"
      kind  = "HTTPRoute"
      name  = local.grafana_admin_session_route_name
    }]
    extAuth = {
      failOpen         = false
      statusOnError    = 403
      timeout          = "2s"
      headersToExtAuth = ["cookie"]
      http = {
        pathOverride = local.grafana_admin_session_publication.ext_auth.path_override
        backendRefs = [{
          group = ""
          kind  = "Service"
          name  = local.grafana_admin_session_publication.ext_auth.service_name
          port  = local.grafana_admin_session_publication.ext_auth.service_port
        }]
      }
    }
  }
  grafana_admin_session_rate_policy_spec = {
    mergeType = "StrategicMerge"
    targetRefs = [{
      group       = "gateway.networking.k8s.io"
      kind        = "HTTPRoute"
      name        = local.grafana_admin_session_route_name
      sectionName = local.grafana_admin_session_login_rule
    }]
    rateLimit = {
      local = {
        rules = [{ limit = local.grafana_admin_session_login_limit }]
      }
    }
  }

  grafana_admin_session_observed_route = try(
    data.kubernetes_resource.grafana_admin_session_route[0].object,
    null,
  )
  grafana_admin_session_observed_reference_grant = try(
    data.kubernetes_resource.grafana_admin_session_reference_grant[0].object,
    null,
  )
  grafana_admin_session_observed_deny_filter = try(
    data.kubernetes_resource.grafana_admin_session_deny_filter[0].object,
    null,
  )
  grafana_admin_session_observed_security_policy = try(
    data.kubernetes_resource.grafana_admin_session_security_policy[0].object,
    null,
  )
  grafana_admin_session_observed_rate_policy = try(
    data.kubernetes_resource.grafana_admin_session_rate_policy[0].object,
    null,
  )
  grafana_admin_session_observed_policy_conditions = {
    security = flatten([
      for ancestor in try(local.grafana_admin_session_observed_security_policy.status.ancestors, []) :
      try(
        ancestor.ancestorRef.group == local.grafana_admin_session_parent_ref.group &&
        ancestor.ancestorRef.kind == local.grafana_admin_session_parent_ref.kind &&
        ancestor.ancestorRef.name == local.grafana_admin_session_parent_ref.name &&
        coalesce(try(ancestor.ancestorRef.namespace, null), local.grafana_admin_session_parent_ref.namespace) == local.grafana_admin_session_parent_ref.namespace &&
        coalesce(try(ancestor.ancestorRef.sectionName, null), local.grafana_admin_session_parent_ref.sectionName) == local.grafana_admin_session_parent_ref.sectionName,
        false,
      ) ? try(ancestor.conditions, []) : []
    ])
    rate_limit = flatten([
      for ancestor in try(local.grafana_admin_session_observed_rate_policy.status.ancestors, []) :
      try(
        ancestor.ancestorRef.group == local.grafana_admin_session_parent_ref.group &&
        ancestor.ancestorRef.kind == local.grafana_admin_session_parent_ref.kind &&
        ancestor.ancestorRef.name == local.grafana_admin_session_parent_ref.name &&
        coalesce(try(ancestor.ancestorRef.namespace, null), local.grafana_admin_session_parent_ref.namespace) == local.grafana_admin_session_parent_ref.namespace &&
        coalesce(try(ancestor.ancestorRef.sectionName, null), local.grafana_admin_session_parent_ref.sectionName) == local.grafana_admin_session_parent_ref.sectionName,
        false,
      ) ? try(ancestor.conditions, []) : []
    ])
  }
  grafana_admin_session_observed_policy_generations = {
    security   = try(local.grafana_admin_session_observed_security_policy.metadata.generation, -1)
    rate_limit = try(local.grafana_admin_session_observed_rate_policy.metadata.generation, -1)
  }
  grafana_admin_session_policy_status = {
    for policy_name, conditions in local.grafana_admin_session_observed_policy_conditions :
    policy_name => {
      Accepted = anytrue([
        for condition in conditions :
        try(
          condition.type == "Accepted" &&
          condition.status == "True" &&
          condition.observedGeneration == local.grafana_admin_session_observed_policy_generations[policy_name],
          false,
        )
      ])
    }
  }
  grafana_admin_session_observed_route_parents = [
    for parent in try(local.grafana_admin_session_observed_route.status.parents, []) : parent
    if try(
      parent.parentRef.group == local.grafana_admin_session_parent_ref.group &&
      parent.parentRef.kind == local.grafana_admin_session_parent_ref.kind &&
      parent.parentRef.name == local.grafana_admin_session_parent_ref.name &&
      parent.parentRef.namespace == local.grafana_admin_session_parent_ref.namespace &&
      parent.parentRef.sectionName == local.grafana_admin_session_parent_ref.sectionName,
      false,
    )
  ]
  grafana_admin_session_observed_route_ready = anytrue([
    for parent in local.grafana_admin_session_observed_route_parents :
    alltrue([
      for condition_type in ["Accepted", "ResolvedRefs"] : anytrue([
        for condition in try(parent.conditions, []) :
        try(
          condition.type == condition_type &&
          condition.status == "True" &&
          condition.observedGeneration == local.grafana_admin_session_observed_route.metadata.generation,
          false,
        )
      ])
    ])
  ])
  grafana_admin_session_observed_route_has_parent = (
    length(try(local.grafana_admin_session_observed_route.spec.parentRefs, [])) == 1 &&
    jsonencode(try(local.grafana_admin_session_observed_route.spec.parentRefs[0], {})) == jsonencode(local.grafana_admin_session_parent_ref)
  )
  grafana_admin_session_observed_route_prepared = (
    local.grafana_admin_session_observed_route_has_parent &&
    local.grafana_admin_session_observed_route_ready &&
    jsonencode(try(local.grafana_admin_session_observed_route.spec.rules, [])) == jsonencode(local.grafana_admin_session_quarantine_rules)
  )
  grafana_admin_session_observed_route_attached = (
    local.grafana_admin_session_observed_route_has_parent &&
    local.grafana_admin_session_observed_route_ready &&
    jsonencode(try(local.grafana_admin_session_observed_route.spec.rules, [])) == jsonencode(local.grafana_admin_session_route_rules)
  )
  grafana_admin_session_observed_contract_exact = (
    (local.grafana_admin_session_observed_route_prepared || local.grafana_admin_session_observed_route_attached) &&
    jsonencode(try(local.grafana_admin_session_observed_reference_grant.spec, {})) == jsonencode(local.grafana_admin_session_reference_grant_spec) &&
    jsonencode(try(local.grafana_admin_session_observed_deny_filter.spec, {})) == jsonencode(local.grafana_admin_session_deny_filter_spec) &&
    jsonencode(try(local.grafana_admin_session_observed_security_policy.spec, {})) == jsonencode(local.grafana_admin_session_security_policy_spec) &&
    jsonencode(try(local.grafana_admin_session_observed_rate_policy.spec, {})) == jsonencode(local.grafana_admin_session_rate_policy_spec)
  )
}

moved {
  from = kubernetes_manifest.grafana_reference_grant[0]
  to   = kubernetes_manifest.grafana_admin_session_reference_grant[0]
}

moved {
  from = kubernetes_manifest.grafana_http_route[0]
  to   = kubernetes_manifest.grafana_admin_session_http_route[0]
}

moved {
  from = kubernetes_manifest.grafana_security_policy[0]
  to   = kubernetes_manifest.grafana_admin_session_security_policy[0]
}

moved {
  from = kubernetes_manifest.grafana_backend_traffic_policy[0]
  to   = kubernetes_manifest.grafana_admin_session_backend_traffic_policy[0]
}

data "kubernetes_resource" "grafana_admin_session_route" {
  count       = local.grafana_admin_session_attach ? 1 : 0
  api_version = "gateway.networking.k8s.io/v1"
  kind        = "HTTPRoute"
  metadata {
    name      = local.grafana_admin_session_route_name
    namespace = local.grafana_admin_session_publication.route_namespace
  }
}

data "kubernetes_resource" "grafana_admin_session_reference_grant" {
  count       = local.grafana_admin_session_attach ? 1 : 0
  api_version = "gateway.networking.k8s.io/v1beta1"
  kind        = "ReferenceGrant"
  metadata {
    name      = local.grafana_admin_session_route_name
    namespace = local.grafana_admin_session_publication.namespace
  }
}

data "kubernetes_resource" "grafana_admin_session_deny_filter" {
  count       = local.grafana_admin_session_attach ? 1 : 0
  api_version = "gateway.envoyproxy.io/v1alpha1"
  kind        = "HTTPRouteFilter"
  metadata {
    name      = local.grafana_admin_session_deny_filter_name
    namespace = local.grafana_admin_session_publication.route_namespace
  }
}

data "kubernetes_resource" "grafana_admin_session_security_policy" {
  count       = local.grafana_admin_session_attach ? 1 : 0
  api_version = "gateway.envoyproxy.io/v1alpha1"
  kind        = "SecurityPolicy"
  metadata {
    name      = "fs2-admin-grafana-client-cidrs"
    namespace = local.grafana_admin_session_publication.route_namespace
  }
}

data "kubernetes_resource" "grafana_admin_session_rate_policy" {
  count       = local.grafana_admin_session_attach ? 1 : 0
  api_version = "gateway.envoyproxy.io/v1alpha1"
  kind        = "BackendTrafficPolicy"
  metadata {
    name      = "fs2-admin-grafana-edge-limit"
    namespace = local.grafana_admin_session_publication.route_namespace
  }
}

resource "terraform_data" "grafana_admin_session_pre_attach_receipt" {
  count = local.grafana_admin_session_attach ? 1 : 0

  input = {
    schema = "fs2-serve.nebius.ai/grafana-admin-session-pre-attach-receipt/v1"
    route = {
      uid              = try(local.grafana_admin_session_observed_route.metadata.uid, null)
      resource_version = try(local.grafana_admin_session_observed_route.metadata.resourceVersion, null)
      generation       = try(local.grafana_admin_session_observed_route.metadata.generation, null)
      state = (
        local.grafana_admin_session_observed_route_prepared ?
        "prepared-direct-403" : "attached-accepted"
      )
    }
    reference_grant_uid = try(local.grafana_admin_session_observed_reference_grant.metadata.uid, null)
    deny_filter_uid      = try(local.grafana_admin_session_observed_deny_filter.metadata.uid, null)
    policy_status        = local.grafana_admin_session_policy_status
    policy_generations   = local.grafana_admin_session_observed_policy_generations
    exact_contract       = local.grafana_admin_session_observed_contract_exact
  }

  lifecycle {
    precondition {
      condition = (
        local.grafana_admin_session_publication.schema == "fs2-serve.nebius.ai/grafana-admin-session-publication/v2" &&
        var.grafana_admin_session_publication_phase == "attach" &&
        local.grafana_admin_session_publication.phase == var.grafana_admin_session_publication_phase &&
        local.grafana_admin_session_publication.authentication == "fs2-admin-session+grafana-native" &&
        !local.grafana_admin_session_publication.ext_auth.fail_open &&
        !local.grafana_admin_session_publication.raw_backends_public
      )
      error_message = "Grafana attachment requires the exact v2 fail-closed admin-session publication contract."
    }

    precondition {
      condition     = local.grafana_admin_session_observed_contract_exact
      error_message = "Grafana attachment is forbidden until the live direct-403 route, HTTPRouteFilter, ReferenceGrant, ext-auth SecurityPolicy, and login-only rate policy exactly match the prepared contract."
    }

    precondition {
      condition = (
        local.grafana_admin_session_policy_status.security.Accepted &&
        local.grafana_admin_session_policy_status.rate_limit.Accepted
      )
      error_message = "Grafana attachment is forbidden until both policies report current-generation Accepted=True."
    }

    precondition {
      condition = (
        local.grafana_admin_session_observed_route_prepared ||
        local.grafana_admin_session_observed_route_attached
      )
      error_message = "Grafana attachment requires either the direct-403 quarantine rules or already attached Grafana rules with current-generation route Accepted=True and ResolvedRefs=True."
    }
  }
}

resource "kubernetes_manifest" "grafana_admin_session_reference_grant" {
  count = local.grafana_admin_session_enabled ? 1 : 0

  manifest = {
    apiVersion = "gateway.networking.k8s.io/v1beta1"
    kind       = "ReferenceGrant"
    metadata = {
      name      = local.grafana_admin_session_route_name
      namespace = local.grafana_admin_session_publication.namespace
      labels    = local.grafana_admin_session_labels
    }
    spec = local.grafana_admin_session_reference_grant_spec
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-admin-grafana"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "grafana_admin_session_security_policy" {
  count = local.grafana_admin_session_enabled ? 1 : 0

  manifest = {
    apiVersion = "gateway.envoyproxy.io/v1alpha1"
    kind       = "SecurityPolicy"
    metadata = {
      # The legacy name is intentionally retained so the state move updates the
      # live object in place; the spec contains no source-IP authorization.
      name      = "fs2-admin-grafana-client-cidrs"
      namespace = local.grafana_admin_session_publication.route_namespace
      labels    = local.grafana_admin_session_labels
      annotations = {
        "fs2-serve.nebius.ai/sai-26-successor" = "admin-session-ext-auth-v2"
      }
    }
    spec = local.grafana_admin_session_security_policy_spec
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-admin-grafana-security"
  }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        local.grafana_admin_session_publication.ext_auth.service_name == "fs2-serve-control-plane" &&
        local.grafana_admin_session_publication.ext_auth.service_port == 8080 &&
        local.grafana_admin_session_publication.ext_auth.path_override == "/admin/api/v1/grafana-authorization" &&
        !local.grafana_admin_session_publication.ext_auth.fail_open
      )
      error_message = "Grafana SecurityPolicy must use the exact in-namespace, fail-closed admin-session authorization endpoint."
    }
  }

  depends_on = [helm_release.control_plane]
}

resource "kubernetes_manifest" "grafana_admin_session_deny_filter" {
  count = local.grafana_admin_session_enabled ? 1 : 0

  manifest = {
    apiVersion = "gateway.envoyproxy.io/v1alpha1"
    kind       = "HTTPRouteFilter"
    metadata = {
      name      = local.grafana_admin_session_deny_filter_name
      namespace = local.grafana_admin_session_publication.route_namespace
      labels    = local.grafana_admin_session_labels
    }
    spec = local.grafana_admin_session_deny_filter_spec
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-admin-grafana-prepare-deny"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "grafana_admin_session_backend_traffic_policy" {
  count = local.grafana_admin_session_enabled ? 1 : 0

  manifest = {
    apiVersion = "gateway.envoyproxy.io/v1alpha1"
    kind       = "BackendTrafficPolicy"
    metadata = {
      name      = "fs2-admin-grafana-edge-limit"
      namespace = local.grafana_admin_session_publication.route_namespace
      labels    = local.grafana_admin_session_labels
      annotations = {
        "fs2-serve.nebius.ai/sai-26-successor" = "grafana-login-only-v2"
      }
    }
    spec = local.grafana_admin_session_rate_policy_spec
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-admin-grafana-rate-limit"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_manifest" "grafana_admin_session_http_route" {
  count = local.grafana_admin_session_enabled ? 1 : 0

  manifest = {
    apiVersion = "gateway.networking.k8s.io/v1"
    kind       = "HTTPRoute"
    metadata = {
      name      = local.grafana_admin_session_route_name
      namespace = local.grafana_admin_session_publication.route_namespace
      labels    = local.grafana_admin_session_labels
      annotations = {
        "fs2-serve.nebius.ai/attachment-gate" = "direct-403 route accepted before Grafana backends"
        "fs2-serve.nebius.ai/sai-26-successor" = "admin-session-ext-auth-v2"
      }
    }
    spec = {
      parentRefs = [local.grafana_admin_session_parent_ref]
      rules = (
        local.grafana_admin_session_attach ?
        local.grafana_admin_session_route_rules :
        local.grafana_admin_session_quarantine_rules
      )
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-admin-grafana"
  }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        local.grafana_admin_session_publication.external_url == "${local.public_base_url}/admin/observability/grafana" &&
        local.grafana_admin_session_publication.root_url == "${local.public_base_url}/admin/observability/grafana/"
      )
      error_message = "Grafana root_url and public link must use the exact current HTTPS Gateway authority and retained subpath."
    }
  }

  timeouts {
    create = "10m"
    update = "10m"
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_manifest.grafana_admin_session_deny_filter,
    kubernetes_manifest.grafana_admin_session_reference_grant,
    terraform_data.grafana_admin_session_pre_attach_receipt,
  ]
}

resource "terraform_data" "grafana_admin_session_attachment_receipt" {
  count = local.grafana_admin_session_attach ? 1 : 0

  input = {
    schema               = "fs2-serve.nebius.ai/grafana-admin-session-attachment-receipt/v1"
    route                = "${local.grafana_admin_session_publication.route_namespace}/${local.grafana_admin_session_route_name}"
    security_policy      = "${local.grafana_admin_session_publication.route_namespace}/fs2-admin-grafana-client-cidrs"
    rate_limit_policy    = "${local.grafana_admin_session_publication.route_namespace}/fs2-admin-grafana-edge-limit"
    pre_attach_receipt   = terraform_data.grafana_admin_session_pre_attach_receipt[0].output
    status_gate_sha256   = filesha256("${path.module}/scripts/wait-for-grafana-admin-session-publication.py")
    desired_route_sha256 = sha256(jsonencode(kubernetes_manifest.grafana_admin_session_http_route[0].manifest))
  }

  triggers_replace = {
    cluster_id           = var.cluster_id
    kube_system_uid      = var.kube_system_uid
    pre_attach_receipt   = sha256(jsonencode(terraform_data.grafana_admin_session_pre_attach_receipt[0].output))
    status_gate_sha256   = filesha256("${path.module}/scripts/wait-for-grafana-admin-session-publication.py")
    desired_route_sha256 = sha256(jsonencode(kubernetes_manifest.grafana_admin_session_http_route[0].manifest))
  }

  provisioner "local-exec" {
    command = "python3 \"${path.module}/scripts/wait-for-grafana-admin-session-publication.py\""
    quiet   = true

    environment = {
      FS2_GATE_KUBECONFIG        = abspath(var.kubeconfig_path)
      FS2_GATE_KUBE_CONTEXT      = var.kube_context
      FS2_GATE_CLUSTER_ID        = var.cluster_id
      FS2_GATE_CLUSTER_NAME      = var.cluster_name
      FS2_GATE_KUBE_SYSTEM_UID   = var.kube_system_uid
      FS2_GATE_NAMESPACE         = local.grafana_admin_session_publication.route_namespace
      FS2_GATE_ROUTE_NAME        = local.grafana_admin_session_route_name
      FS2_GATE_GATEWAY_NAME      = local.grafana_admin_session_publication.gateway_name
      FS2_GATE_LISTENER_NAME     = local.grafana_admin_session_publication.listener_name
      FS2_GATE_GRAFANA_NAMESPACE = local.grafana_admin_session_publication.namespace
      FS2_GATE_GRAFANA_SERVICE   = local.grafana_admin_session_publication.service_name
      FS2_GATE_GRAFANA_PORT      = tostring(local.grafana_admin_session_publication.service_port)
      FS2_GATE_TIMEOUT_SECONDS   = "180"
      FS2_GATE_RETRY_SECONDS     = "2"
    }
  }

  depends_on = [
    kubernetes_manifest.grafana_admin_session_http_route,
    kubernetes_manifest.grafana_admin_session_deny_filter,
    kubernetes_manifest.grafana_admin_session_security_policy,
    kubernetes_manifest.grafana_admin_session_backend_traffic_policy,
  ]
}

output "grafana_admin_session_security_contract" {
  description = "Source and deploy-time receipts for the staged, fail-closed SAI-26 successor."
  value = local.grafana_admin_session_enabled ? {
    schema         = local.grafana_admin_session_publication.schema
    phase          = local.grafana_admin_session_publication.phase
    authentication = local.grafana_admin_session_publication.authentication
    ext_auth = {
      endpoint       = local.grafana_admin_session_publication.ext_auth.path_override
      fail_open      = local.grafana_admin_session_publication.ext_auth.fail_open
      forwarded_only = ["cookie"]
      policy         = "fs2-system/fs2-admin-grafana-client-cidrs"
    }
    login_rate_limit = {
      policy       = "fs2-system/fs2-admin-grafana-edge-limit"
      route_rule   = local.grafana_admin_session_login_rule
      requests     = local.grafana_admin_session_login_limit.requests
      unit         = local.grafana_admin_session_login_limit.unit
      defense_role = "secondary-to-admin-session"
    }
    route = {
      name                       = "fs2-system/${local.grafana_admin_session_route_name}"
      attached                   = local.grafana_admin_session_attach
      pre_attach_receipt         = try(terraform_data.grafana_admin_session_pre_attach_receipt[0].output, null)
      attachment_receipt         = try(terraform_data.grafana_admin_session_attachment_receipt[0].output, null)
      required_policy_conditions = local.grafana_admin_session_publication.required_policy_conditions
      required_route_conditions  = local.grafana_admin_session_publication.required_route_conditions
    }
    raw_backends_public = false
  } : null
}
