# Native AlphaFold3 adapter

`alphafold3` means the native Google DeepMind code and parameters under their
academic terms. It is never an alias for OpenFold3. The canonical profile puts
the CPU data pipeline before GPU inference. A caller may select a reusable
enriched input only by logical artifact name in the canonical input manifest;
the controller resolves its `artifact-pointer/v1` without exposing storage.

The exact parameter bytes are `staged-quarantined`. Academic/non-commercial PoC
authorization is operator-owned deployment metadata, not a per-request tenant
or receipt gate. The adapter fails closed unless the profile records the PoC
authorization and restricted-quarantine policy and the controller resolves the
exact parameter and reference-bundle identities with live localization
readiness. The private PVC file is mounted read-only at `/models/af3.bin.zst`
from `alphafold3/af3.bin.zst`; authorization actor data never enters the public
request or generated argv.

The public database identity is deliberately two-part: immutable database-tree
content SHA-256 and localization-manifest SHA-256 are different fields and must
not be equated. The CPU argv receives both as
`--expected-db-content-sha256`/`--expected-db-manifest-sha256`; its relocatable
handoff binds both as `reference_content_sha256` and
`reference_manifest_sha256`; GPU inference receives them again as
`--expected-reference-content-sha256` and
`--expected-reference-manifest-sha256`. The exact release remains
`v3.0-paper-snapshot-2022-09-28`. Production routing stays disabled until both
independently promoted identities exist; deterministic test digests do not
claim production readiness.

Both stages run only in the deployment-owned `fs2-academic-poc` execution
target with intended ServiceAccount `fs2-academic-runner` and LocalQueue
`academic-scientific`. These resources, the LocalQueue route to
`inference-accelerators`, and the cross-namespace RoleBinding for controller
ServiceAccount `fs2-system/fs2-serve-control-plane-runtime` are deployment
prerequisites owned by the academic-assets companion module; their absence is
not bypassed or recreated by the adapter. The runtime rejects an execution map
whose controller subject differs from that configured deployment identity. The
landed companion creates the LocalQueue object but does not, by itself, add its
identity/route to `module.kueue_scheduling.contract`. Terraform integration
must publish exact `local_queues` and `local_queue_routes` entries for
`fs2-academic-poc/academic-scientific -> inference-accelerators` in the
content-addressed immutable scheduling ConfigMap. Controller startup validates
that cross-contract binding, so AF3 remains non-deployable while it is absent.
The CPU stage receives the narrowly allowlisted, read-only operator
host root `/mnt/fs2-reference-data/data`, but only through the promoted safe
dataset subPath
`datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/sha256/<tree_sha256>`
mounted at `/databases`. The aggregate-tree localization carries no truncated
per-file list: it binds the tree/content SHA, independent manifest SHA, exact
relative path/URI, total file count, and node-accessibility receipt. The Pod is
restricted by the deployment-owned and attested
`storage.fs2.nebius/reference-data=true` selector, which is revalidated at apply
and conjoined with Kueue ResourceFlavor placement without node or pool IDs;
the runtime rejects explicit node names. The wrapper verifies
`/databases/.fs2-manifest-sha256`. The Pod keeps the H100 class selector, tolerates only the dedicated
inference taint, and receives supplemental group `1000` to traverse the
`1000:1000` mode-`0770` filesystem root without ownership mutation. Inference receives PVC
`academic-assets-runtime-rwx` from the same namespace with
the exact 1,020,545,840-byte `alphafold3/af3.bin.zst` mounted read-only at
`/models/af3.bin.zst` with supplemental group `65532`. Namespace, queue, claim,
host path, placement, groups, and service account are never request fields. The
raw adapter deliberately leaves groups empty; the controller binds `1000` or
`65532` only from the exact trusted execution-map source and renders neither
`fsGroup` nor `fsGroupChangePolicy`.

Only the GPU `inference` stage receives the deployment-owned writable PVC
`scientific-alphafold3-cache` from `fs2-academic-poc`, mounted at
`/cache/alphafold3`. The trusted execution map fixes
`FS2_AF3_CACHE_ROOT=/cache/alphafold3`, JAX compilation storage at
`/cache/alphafold3/jax`, Triton storage at `/cache/alphafold3/triton`, and XDG
storage at `/cache/alphafold3/xdg`; neither the request nor adapter can select a
claim or cache path. This is an auxiliary L1+ compiler/JIT cache, not L2. The
profile stays `requested_level=Off`, `maximum_level=L1`,
`qualified_level=Off`, and `candidate-unqualified` until paired first-compile
and warm-compile H100 measurements pass. L2 requires an actual GPU/process
snapshot on shared storage; no L2/L3/L4 snapshot or residency claim is made.

The checked-in database requirement remains `supply_state=unresolved` with no
content digest, manifest digest, aggregate path, or item inventory. A deployment
may substitute the safe concrete subPath only after promotion supplies every
independent identity and node-access receipt required by the aggregate-tree
contract; until then `route_exposed=false` remains mandatory.
