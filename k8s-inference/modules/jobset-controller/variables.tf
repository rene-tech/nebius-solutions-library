variable "enabled" {
  description = "Install and qualify JobSet for scientific true-gang stages."
  type        = bool
}

variable "run_id" {
  type = string
}

variable "namespace" {
  type    = string
  default = "jobset-system"
}

variable "kubeconfig_path" {
  type = string
}

variable "kube_context" {
  type = string
}

variable "run_root" {
  type = string
}

variable "cluster_id" {
  type = string
}

variable "kubernetes_version" {
  description = "Managed Kubernetes version from deployment.cluster.kubernetes_version. JobSet v0.12.0's own end-to-end matrix covers Kubernetes 1.32-1.34; patch upgrades within those minors are accepted. No wider minor is claimed, because nothing upstream tests one."
  type        = string
  default     = "1.34"

  validation {
    condition = try(
      tonumber(split(".", trimprefix(var.kubernetes_version, "v"))[0]) == 1 &&
      contains([32, 33, 34], tonumber(split(".", trimprefix(var.kubernetes_version, "v"))[1])) &&
      length(split(".", trimprefix(var.kubernetes_version, "v"))) >= 2 &&
      length(split(".", trimprefix(var.kubernetes_version, "v"))) <= 3 &&
      alltrue([
        for component in split(".", trimprefix(var.kubernetes_version, "v")) :
        tostring(tonumber(component)) == component
      ]),
      false,
    )
    error_message = "JobSet v0.12.0 requires a numeric Kubernetes 1.<minor>[.<patch>] version in its upstream-tested 1.32-1.34 minor set."
  }
}

variable "labels" {
  type    = map(string)
  default = {}
}
