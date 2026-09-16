output "release_name" {
  value = helm_release.workshop.name
}

output "namespace" {
  value = var.namespace
}

output "workshop_url" {
  value = "${var.public_origin}/workshop"
}

output "workshop_api_url" {
  value = "${var.public_origin}/v1/workshop"
}

output "gateway_api_url" {
  value = "${var.public_origin}/v1/mindeval"
}
