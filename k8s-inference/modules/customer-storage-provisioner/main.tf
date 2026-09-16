# Customer storage runs in a dedicated project with split identities. Private
# key material is created and rotated out of band and never enters Terraform
# configuration, plan output, state, or Kubernetes resources managed here.
terraform {
  required_providers {
    nebius = {
      source = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
    }
  }
}

variable "project_id" { type = string }
variable "name" {
  type    = string
  default = "fs2-customer-storage-provisioner"
}
variable "resource_public_key_pem" {
  type        = string
  sensitive   = true
  description = "Public half of the externally held resource-provisioner JWT key."
}
variable "iam_public_key_pem" {
  type        = string
  sensitive   = true
  description = "Public half of the externally held IAM-binder JWT key."
}
variable "auth_key_expires_at" {
  type        = string
  description = "RFC3339 expiry shared by both short-lived provisioner auth keys."
}

# Preserve the deployed resource addresses while replacing the credential and
# authority model. The editor permit and expiring public key update in place;
# only the new IAM-binder identity is added.
moved {
  from = nebius_iam_v1_service_account.provisioner
  to   = nebius_iam_v1_service_account.resource_provisioner
}
moved {
  from = nebius_iam_v1_group.provisioner
  to   = nebius_iam_v1_group.resource_provisioner
}
moved {
  from = nebius_iam_v1_group_membership.provisioner
  to   = nebius_iam_v1_group_membership.resource_provisioner
}
moved {
  from = nebius_iam_v1_access_permit.provisioner
  to   = nebius_iam_v1_access_permit.resource_provisioner
}
moved {
  from = nebius_iam_v1_auth_public_key.provisioner
  to   = nebius_iam_v1_auth_public_key.resource_provisioner
}

resource "nebius_iam_v1_service_account" "resource_provisioner" {
  parent_id   = var.project_id
  name        = var.name
  description = "Customer bucket, service-account and expiring access-key provisioning"
}
resource "nebius_iam_v1_group" "resource_provisioner" {
  parent_id = var.project_id
  name      = var.name
}
resource "nebius_iam_v1_group_membership" "resource_provisioner" {
  parent_id = nebius_iam_v1_group.resource_provisioner.id
  member_id = nebius_iam_v1_service_account.resource_provisioner.id
}
resource "nebius_iam_v1_access_permit" "resource_provisioner" {
  parent_id   = nebius_iam_v1_group.resource_provisioner.id
  resource_id = var.project_id
  role        = "editor"
}
resource "nebius_iam_v1_auth_public_key" "resource_provisioner" {
  parent_id  = var.project_id
  name       = var.name
  expires_at = var.auth_key_expires_at
  account = {
    service_account = { id = nebius_iam_v1_service_account.resource_provisioner.id }
  }
  data = var.resource_public_key_pem
}

resource "nebius_iam_v1_service_account" "iam_binder" {
  parent_id   = var.project_id
  name        = "${var.name}-iam-binder"
  description = "Customer storage group and membership reconciliation only"
}
resource "nebius_iam_v1_group" "iam_binder" {
  parent_id = var.project_id
  name      = "${var.name}-iam-binder"
}
resource "nebius_iam_v1_group_membership" "iam_binder" {
  parent_id = nebius_iam_v1_group.iam_binder.id
  member_id = nebius_iam_v1_service_account.iam_binder.id
}
# Nebius requires admin for group and membership mutation. This identity is
# mounted only by the isolated reconciler and scoped to the customer project.
resource "nebius_iam_v1_access_permit" "iam_binder" {
  parent_id   = nebius_iam_v1_group.iam_binder.id
  resource_id = var.project_id
  role        = "admin"
}
resource "nebius_iam_v1_auth_public_key" "iam_binder" {
  parent_id  = var.project_id
  name       = "${var.name}-iam-binder"
  expires_at = var.auth_key_expires_at
  account = {
    service_account = { id = nebius_iam_v1_service_account.iam_binder.id }
  }
  data = var.iam_public_key_pem
}

output "resource_service_account_id" { value = nebius_iam_v1_service_account.resource_provisioner.id }
output "iam_service_account_id" { value = nebius_iam_v1_service_account.iam_binder.id }
