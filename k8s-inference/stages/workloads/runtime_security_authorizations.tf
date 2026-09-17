data "external" "runtime_security_authorization" {
  for_each = var.model_runtime_security_authorizations

  program = [
    "python3",
    "${path.module}/scripts/verify_runtime_security_authorization.py",
  ]

  query = {
    authorization_id       = each.key
    kind                   = each.value.kind
    model_id               = each.value.model_id
    subject_schema         = each.value.subject_schema
    subject_sha256         = each.value.subject_sha256
    evidence_path          = abspath("${path.module}/../../${each.value.evidence_path}")
    evidence_sha256        = each.value.evidence_sha256
    attestation_path       = abspath("${path.module}/../../${each.value.attestation_path}")
    attestation_sha256     = each.value.attestation_sha256
  }

  lifecycle {
    postcondition {
      condition = (
        self.result.verified == "true" &&
        self.result.authorization_id == each.key &&
        self.result.subject_sha256 == each.value.subject_sha256 &&
        self.result.evidence_sha256 == each.value.evidence_sha256 &&
        self.result.attestation_sha256 == each.value.attestation_sha256
      )
      error_message = "Runtime security authorization did not verify against the external Ed25519 trust root."
    }
  }
}

locals {
  verified_runtime_security_authorizations = {
    for authorization_id, authorization in var.model_runtime_security_authorizations :
    authorization_id => merge(authorization, {
      evidence = jsondecode(data.external.runtime_security_authorization[authorization_id].result.evidence_json)
      attestation = jsondecode(data.external.runtime_security_authorization[authorization_id].result.attestation_json)
      verified_key_id = data.external.runtime_security_authorization[authorization_id].result.key_id
      session_id       = data.external.runtime_security_authorization[authorization_id].result.session_id
      authority_sha256 = data.external.runtime_security_authorization[authorization_id].result.authority_sha256
      trusted_attestors = jsondecode(data.external.runtime_security_authorization[authorization_id].result.trusted_attestors_json)
    })
  }
  runtime_security_authority_sessions = toset([
    for authorization in values(local.verified_runtime_security_authorizations) : authorization.session_id
  ])
  runtime_security_authority_digests = toset([
    for authorization in values(local.verified_runtime_security_authorizations) : authorization.authority_sha256
  ])
  runtime_security_authority_roots = toset([
    for authorization in values(local.verified_runtime_security_authorizations) : jsonencode(authorization.trusted_attestors)
  ])
  runtime_security_authority_consistent = (
    length(local.verified_runtime_security_authorizations) == 0 || (
      length(local.runtime_security_authority_sessions) == 1 &&
      length(local.runtime_security_authority_digests) == 1 &&
      length(local.runtime_security_authority_roots) == 1
    )
  )
  runtime_security_authority_session_id = (
    length(local.runtime_security_authority_sessions) == 1 ? one(local.runtime_security_authority_sessions) : ""
  )
  runtime_security_trusted_attestors = (
    length(local.runtime_security_authority_roots) == 1 ?
    jsondecode(one(local.runtime_security_authority_roots)) : {}
  )
}

resource "terraform_data" "runtime_security_authority" {
  input = {
    authority_path   = "/run/fs2-runtime-security/platform-security/authority.json"
    authority_sha256 = length(local.runtime_security_authority_digests) == 1 ? one(local.runtime_security_authority_digests) : ""
    session_id       = local.runtime_security_authority_session_id
  }

  lifecycle {
    precondition {
      condition     = local.runtime_security_authority_consistent
      error_message = "Every runtime-security authorization must resolve through one active platform-security authority contract."
    }
  }
}
