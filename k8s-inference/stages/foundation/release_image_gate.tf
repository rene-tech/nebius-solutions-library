resource "terraform_data" "release_image_closure_gate" {
  # Force verification for every saved-plan apply even when protected evidence
  # is published at stable filesystem paths.
  triggers_replace = [timestamp()]

  input = {
    closure_path   = abspath(var.release_image_contract.closure_path)
    toolchain_path = abspath(var.release_image_contract.toolchain_path)
    bootstrap_path = abspath(var.release_image_contract.bootstrap_path)
    external_trust_path = abspath(var.release_image_contract.external_trust_path)
    plan_root      = "stages/foundation"
  }

  provisioner "local-exec" {
    interpreter = [self.input.bootstrap_path, "python-entry", "--external-trust", self.input.external_trust_path, "--toolchain", self.input.toolchain_path, "--source-root", abspath("${path.module}/../.."), "--entry", "security/terraform_apply_gate_entrypoint.py", "--"]
    command = jsonencode({
      closure_path  = self.input.closure_path
      plan_root     = self.input.plan_root
      source_root   = abspath("${path.module}/../..")
      surfaces_path = abspath("${path.module}/../../security/release-image-surfaces.json")
      toolchain_path = self.input.toolchain_path
      external_trust_path = self.input.external_trust_path
      trust_path    = abspath("${path.module}/../../security/image-attestation-trust.json")
    })
  }
}
