variable "namespace" {
  description = "Namespace containing the existing workshop, database and Token Factory Secrets."
  type        = string
  default     = "fs2-system"
}

variable "release_name" {
  type    = string
  default = "fs2-mindeval-workshop"
}

variable "workshop_image" {
  description = "Published workshop image addressed by immutable digest."
  type        = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.workshop_image))
    error_message = "workshop_image must use an immutable sha256 image digest."
  }
}

variable "gateway_image" {
  description = "Calibrated gateway image, addressed by immutable digest."
  type        = string
  default     = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/mindeval-gateway@sha256:4d7c32408cdc7a8bb5df55b73408146d6fe5aee759927f9211790b0874ef2db7"
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.gateway_image))
    error_message = "gateway_image must use an immutable sha256 image digest."
  }
}

variable "public_origin" {
  description = "HTTPS origin already served by the platform Gateway."
  type        = string
  validation {
    condition     = can(regex("^https://[^/]+$", var.public_origin))
    error_message = "public_origin must be an HTTPS origin without a trailing slash."
  }
}

variable "values" {
  description = "Additional non-secret Helm YAML values. Credentials are existing Secret references only. Explicit image/origin variables take precedence."
  type        = list(string)
  default     = []
}

variable "timeout_seconds" {
  type    = number
  default = 600
}
