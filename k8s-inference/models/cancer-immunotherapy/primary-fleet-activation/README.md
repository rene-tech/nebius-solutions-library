# Primary scientific fleet activation inputs

This directory validates model-owned activation inputs for Proteina-Complexa,
Mosaic, BindCraft, and RFdiffusion. The inputs are deliberately not the shared
scientific workload-profile or execution-map aggregates. The serialized
integration owner can consume them without resolving merge conflicts in those
shared files.

Each fragment pins the accepted source revision, immutable image, H100 semantic
evidence, candidate workload profile, exact stage placement capabilities, and
the artifact generation `subPath` needed by each stage. Placement names only
logical FS2 capabilities and eligible pools; it never embeds a Nebius resource
ID, node name, or GPU UUID. The fragments intentionally leave CPU, memory, and
GPU request/limit envelopes to the shared scheduler integration owner: this
handoff changes no limits and does not invent sizes absent from accepted model
evidence.

The activation state is fail closed:

* Proteina-Complexa and BindCraft carry exact current-main generation bindings.
  BindCraft also carries the adapter's exact deployment-authorization overlay;
  those controller-only fields are intentionally outside the public profile
  projection because the current public schema cannot express them.
* Mosaic and RFdiffusion have canonical public generations. Their activation
  fragments and serialized candidate profiles pin the split-root v6 and r13
  images that passed their bounded H100 image-level workflows. Public routes
  remain closed until the same identities pass controller submission,
  collection, and scheduler qualification.
* Every projected profile is `candidate-unqualified`, every route and MCP
  invocation flag remains off, and a real public controller run is required
  before promotion.

Mosaic and RFdiffusion now have canonical localization generations, reviewed
controller compilers/collectors, and exact successor-image H100 evidence. The
historical accepted-image mismatch and remaining public promotion gates are recorded in
[`CONTROLLER_INTEGRATION_GAPS.md`](CONTROLLER_INTEGRATION_GAPS.md). They remain
route-disabled; image-level semantic evidence is not a public companion run.

Every fragment also records the exact integration source revision and complete
runtime-recipe path set. `fs2-path-set-sha256-v1` is byte-for-byte the control
plane recipe algorithm: sorted repository path, NUL, byte length, NUL, file
bytes. The shared path closure includes the adapter registry, so a registry or
controller change invalidates all four candidate identities instead of leaving
stale values that merely agree with their result fixtures. Proteina-Complexa's
path set is the canonical control-plane registration. The three models without
a complete canonical recipe registration record their current model-owned
adapter/runtime path set explicitly; integration must replace that candidate
set if it adds new schema or adapter-shim inputs.

`validate_fragments.py` validates the custom generator-input schema, the
canonical profile/request/result schemas, referenced evidence, source/image
identity, artifact/stage joins, placement portability, and fail-closed route
state. It independently recomputes runtime recipes, workload recipes, artifact
identities, and public-result execution identities from the pinned source. It
also emits deterministic integration material without writing shared
aggregates:

```bash
python3 models/cancer-immunotherapy/primary-fleet-activation/validate_fragments.py
python3 models/cancer-immunotherapy/primary-fleet-activation/validate_fragments.py --render proteina-complexa
```

The integration owner must reconcile BindCraft's `adapter_profile_overlay`
with the controller/catalog access-state seam before making the route
invocable. The focused adapter check applies that overlay only in memory and
proves that the model-owned request compiles; it does not weaken or rewrite the
public profile schema.

The public result files are schema fixtures, not live receipts. They use
synthetic workload identifiers and record a terminal `ACTIVATION_NOT_ENABLED`
failure so they cannot be mistaken for a successful platform run. The accepted
H100 evidence remains solely in the paths pinned by each fragment.
