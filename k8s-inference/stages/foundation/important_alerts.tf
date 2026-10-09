# The installed Prometheus operator owns delivery/retries/grouping. Secret
# material is provisioned separately; Terraform only carries its name and key.
resource "helm_release" "important_alerts" {
  count = var.alertmanager.enabled && var.alertmanager.email.enabled ? 1 : 0

  name      = "fs2-important-alerts"
  namespace = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
  chart     = "${path.module}/../../charts/addons/important-alerts"
  atomic    = true
  wait      = true
  timeout   = 300

  values = [yamlencode({
    email = {
      to             = var.alertmanager.email.to
      from           = var.alertmanager.email.from
      smarthost      = var.alertmanager.email.smarthost
      username       = var.alertmanager.email.username
      passwordSecret = var.alertmanager.email.password_secret
      passwordKey    = var.alertmanager.email.password_key
    }
    adminUrl = var.alertmanager.email.admin_url
    ruleLabels = {
      release = "fs2-${var.run_id}-monitoring"
    }
  })]

  depends_on = [helm_release.monitoring]
}
