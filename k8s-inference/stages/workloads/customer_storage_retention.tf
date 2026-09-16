# The deployed fixed-name SAI-08 compatibility objects must remain live while
# the provider-enforced v2 reconciler is added beside them. These declarations
# remove only Terraform ownership; `destroy = false` forbids a delete plan.
# The externally signed handoff records their live UIDs/spec digests before a
# future state transition. No state action is authorized by this source change.
removed {
  from = kubernetes_config_map_v1.customer_storage_egress_contract

  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_manifest.customer_storage_egress_admission_policy

  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_manifest.customer_storage_egress_admission_binding

  lifecycle {
    destroy = false
  }
}
