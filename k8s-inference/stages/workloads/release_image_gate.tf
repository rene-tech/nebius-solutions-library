resource "terraform_data" "release_image_closure_gate" {
  # Force verification for every saved-plan apply even when protected evidence
  # is published at stable filesystem paths.
  triggers_replace = [timestamp()]

  input = {
    closure_path   = abspath(var.release_image_contract.closure_path)
    plan_json_path = abspath(var.release_image_contract.plan_json_path)
    plan_root      = "stages/workloads"
  }

  provisioner "local-exec" {
    command = <<-EOT
      python3 "$FS2_RELEASE_IMAGE_VERIFIER" \
        --root "$FS2_SOURCE_ROOT" \
        --surfaces "$FS2_RELEASE_IMAGE_SURFACES" \
        --trust "$FS2_RELEASE_IMAGE_TRUST" \
        --verify-terraform-closure "$FS2_RELEASE_IMAGE_CLOSURE" \
        --plan-root "$FS2_RELEASE_IMAGE_PLAN_ROOT" \
        --terraform-plan-json-stdin < "$FS2_RELEASE_IMAGE_PLAN_JSON"
    EOT

    environment = {
      FS2_SOURCE_ROOT                 = abspath("${path.module}/../..")
      FS2_RELEASE_IMAGE_VERIFIER      = abspath("${path.module}/../../security/release_image_closure.py")
      FS2_RELEASE_IMAGE_SURFACES      = abspath("${path.module}/../../security/release-image-surfaces.json")
      FS2_RELEASE_IMAGE_TRUST         = abspath("${path.module}/../../security/image-attestation-trust.json")
      FS2_RELEASE_IMAGE_CLOSURE       = self.input.closure_path
      FS2_RELEASE_IMAGE_PLAN_JSON     = self.input.plan_json_path
      FS2_RELEASE_IMAGE_PLAN_ROOT     = self.input.plan_root
    }
  }
}
