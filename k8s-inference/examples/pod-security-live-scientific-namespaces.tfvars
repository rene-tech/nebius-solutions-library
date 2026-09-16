# Overlay for the retained scientific namespace inventory. Apply only in the
# final enforce phase, after every namespace is confirmed empty or its Pods
# have passed Baseline admission checks.
pod_security_rollout_phase = "enforce"
pod_security_existing_scientific_namespaces = [
  "fs2-bioir-boltz2",
  "fs2-bioir-coverage",
  "fs2-bioir-openfold",
  "fs2-bioir-protenix",
  "fs2-bioir-snapshot",
]
