# Only the control plane receives this project-scoped provisioning identity.
# Customer S3 identities have no project role: access comes from bucket policy.
terraform {
  required_providers {
    nebius = {
      source = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.1"
    }
  }
}

variable "project_id" {
  type = string
}

variable "name" {
  type    = string
  default = "fs2-customer-storage-provisioner"
}

resource "nebius_iam_v1_service_account" "provisioner" {
  parent_id   = var.project_id
  name        = var.name
  description = "Scientific AI bucket and customer S3 credential provisioning"
}

resource "nebius_iam_v1_group" "provisioner" {
  parent_id = var.project_id
  name      = var.name
}

resource "nebius_iam_v1_group_membership" "provisioner" {
  parent_id = nebius_iam_v1_group.provisioner.id
  member_id = nebius_iam_v1_service_account.provisioner.id
}

# Creating IAM groups, memberships and access keys requires admin, not editor.
# Scope is this project, never the encompassing Nebius tenant.
resource "nebius_iam_v1_access_permit" "provisioner" {
  parent_id   = nebius_iam_v1_group.provisioner.id
  resource_id = var.project_id
  role        = "admin"
}

resource "tls_private_key" "provisioner" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "nebius_iam_v1_auth_public_key" "provisioner" {
  parent_id = var.project_id
  name      = var.name
  account = {
    service_account = { id = nebius_iam_v1_service_account.provisioner.id }
  }
  data = tls_private_key.provisioner.public_key_pem
}

# This long-lived provisioner key is sensitive Terraform state. Protect and
# retain the backend accordingly. Customer S3 secrets are NOT Terraform state;
# they are created dynamically and encrypted in the platform's PostgreSQL DB.
output "credentials_json" {
  sensitive = true
  value = jsonencode({
    subject-credentials = {
      type        = "JWT"
      alg         = "RS256"
      private-key = tls_private_key.provisioner.private_key_pem
      kid         = nebius_iam_v1_auth_public_key.provisioner.id
      iss         = nebius_iam_v1_service_account.provisioner.id
      sub         = nebius_iam_v1_service_account.provisioner.id
    }
  })
  depends_on = [nebius_iam_v1_access_permit.provisioner]
}
