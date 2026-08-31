.metadata.name == $contract.project_name and
.metadata.parent_id == $tenant and
((.status.container_state // .status.project_state // "") == "ACTIVE")
