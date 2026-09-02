import type {
  AcademicAssetReadiness,
  AcademicAssetReadinessList,
} from "../api/scientificTypes.ts";

const alphaFold3: AcademicAssetReadiness = {
  asset_id: "alphafold3",
  model_id: "alphafold3",
  backend_id: "alphafold3-native",
  display_name: "AlphaFold3 (native)",
  state: "ImageRebuildPending",
  use_authorization_status: "Granted",
  execution_authorization_status: "Authorized",
  formal_license_status: "FormalAcceptancePending",
  artifact_status: "ArtifactVerified",
  tenant_cache_status: "TenantCacheReady",
  runtime_status: "RuntimeSemanticPassedImageRebuildPending",
  deployment_status: "MissingDeployment",
  semantic_status: "MissingSemanticReadiness",
  delivery_mode: "tenant-private-volume",
  embed_in_image: false,
  mount_path: "/opt/fs2/academic/alphafold3",
  artifact_sha256: "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff",
  runtime_image_digest: "sha256:bead2e68627c1aa7d5fa80243b1164a18160f48cf3a1867090d72ff2b9270e37",
  authorization_receipt_sha256: "1b9c4f27b5c1f0a9d0a9f2f4dd0a3f6f1c2f8ba0f9a7c3e5d1b6a4c8e2f0d7b3",
  acceptance_receipt_sha256: null,
  alternative: {
    model_id: "openfold3",
    reason: "OpenFold3 is an independent open model. It is not native AlphaFold3 and is not covered by the AlphaFold3 licence.",
  },
};

const pyRosettaBindCraft: AcademicAssetReadiness = {
  asset_id: "pyrosetta-bindcraft",
  model_id: "bindcraft",
  backend_id: "bindcraft-native-pyrosetta",
  display_name: "BindCraft (native PyRosetta)",
  state: "MissingDeployment",
  use_authorization_status: "Granted",
  execution_authorization_status: "Authorized",
  formal_license_status: "FormalAcceptancePending",
  artifact_status: "ArtifactVerified",
  tenant_cache_status: "TenantCacheReady",
  runtime_status: "RuntimeReady",
  deployment_status: "MissingDeployment",
  semantic_status: "MissingSemanticReadiness",
  delivery_mode: "tenant-private-volume",
  embed_in_image: false,
  mount_path: "/opt/fs2/academic/pyrosetta-bindcraft",
  artifact_sha256: "4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242",
  runtime_image_digest: "sha256:9f2ac1d0b6e4837a5c0d1e7f83b26a4c9d5e0f71a83c2b6d4e9f10a5c7b3d820",
  authorization_receipt_sha256: "5e7b1c9a3d2f480c6b1a8e4f70d3c2b9a6f1e0d8c4b7a2f95e3d6c1b0a894f27",
  acceptance_receipt_sha256: null,
  alternative: {
    model_id: "open-binder",
    reason: "Open binder workflow is an independent model. It is not native BindCraft/PyRosetta and carries no PyRosetta licence obligation.",
  },
};

export const academicAssetReadinessFixture: AcademicAssetReadinessList = {
  tenant_id: "tenant-academic",
  institution_id: null,
  runtime_path_state: "Blocked",
  formal_license_state: "Pending",
  delivery: {
    namespace: "fs2-academic-poc",
    claim: "academic-assets-runtime-rwx",
    mount_root: "/opt/fs2/academic",
    general_shared_cache: false,
  },
  items: [alphaFold3, pyRosettaBindCraft],
};
