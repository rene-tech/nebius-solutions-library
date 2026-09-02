# Tenant-private delivery of licensed academic assets.
#
# The implementation lives in a reusable module so the claim-lifecycle contract
# can be exercised with provider-mocked plan, state and destroy tests without
# standing up the whole workloads stage. See modules/academic-assets/tests.

module "academic_assets" {
  source = "../../modules/academic-assets"

  academic_assets = var.academic_assets
}
