def fs2_private_cidrs:
  [
    .status.ipv4_private_pools[]?.cidrs[]?,
    .status.ipv4_private_cidrs[]?
  ] | unique;

[
  (.items // [])[]
  | select(
      .metadata.name == $target.subnet_name and
      .status.state == "READY" and
      (fs2_private_cidrs == [$target.private_subnet_cidr])
    )
] | length == 1
