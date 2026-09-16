variable "kubeconfig_path" {
  description = "Path to an existing kubeconfig; no credential bytes are copied into this module."
  type        = string
}

variable "kube_context" {
  type = string
}

variable "namespace" {
  type    = string
  default = "fs2-system"
}

variable "workshop_image" {
  type = string
}

variable "public_origin" {
  type = string
}

variable "values" {
  type    = list(string)
  default = []
}
