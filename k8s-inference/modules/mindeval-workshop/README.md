# MindEval workshop Terraform module

An additive Helm release for an existing Scientific AI platform. It does not
create cloud/GPU resources, alter platform releases, create credential values or
change the platform migration list. See the sibling chart README for the
existing Secret, database and Gateway prerequisites.

```hcl
module "mindeval_workshop" {
  source         = "./modules/mindeval-workshop"
  namespace      = "fs2-system"
  workshop_image = var.published_workshop_image
  public_origin  = var.public_origin
  values         = [file("workshop-values.yaml")]
}
```

Inputs accept the release namespace/name, immutable workshop/gateway images,
public origin and optional non-secret Helm YAML. Explicit image/origin variables
take precedence over YAML overrides. Credential bytes belong in existing Secret
resources, never Helm values or Terraform inputs. The Helm provider is inherited
from the caller; configure the exact existing kubeconfig/context there.

Outputs are `workshop_url`, `workshop_api_url`, `gateway_api_url`, `release_name`
and `namespace`. Helm waits for the schema hook and ready pods with atomic
rollback. The gateway PVC is retained by the chart on uninstall. Changing the
chart's singleton scheduler semantics requires implementing distributed rate
accounting first.

This module and the standalone `examples/mindeval-workshop` root are validated
using `terraform init -backend=false` and `terraform validate`. No Terraform
apply, state import or backend migration is performed during validation.
