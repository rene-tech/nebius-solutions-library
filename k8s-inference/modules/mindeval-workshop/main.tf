resource "helm_release" "workshop" {
  name             = var.release_name
  namespace        = var.namespace
  chart            = abspath("${path.module}/../../charts/addons/mindeval-workshop")
  create_namespace = false
  atomic           = true
  wait             = true
  wait_for_jobs    = true
  timeout          = var.timeout_seconds
  values = concat(var.values, [yamlencode({
    gateway = { image = var.gateway_image }
    workshop = {
      image        = var.workshop_image
      publicOrigin = var.public_origin
    }
  })])
}
