# Scientific profile discovery and startup canary

Scientific discovery is an admission projection, not a list of candidates.
`list_scientific_models` and `/admin/api/v1/scientific-models` expose a profile
only when all of these statements are true for the requesting tenant:

- the canonical profile is `qualified` or `active`, is route-exposed, and has
  complete immutable runtime, artifact, execution, access, and semantic state;
- the caller has `catalog.read`, `inference.invoke`, and model-policy access
  (MCP), or an explicitly authorized tenant context (admin);
- the exact execution map binds the model variant, namespace, and every stage
  collector, including any tenant-bound academic authorization;
- every advertised service class can freeze a Kueue scheduling decision for
  the profile's minimum legal plan.

Failure of any check hides the profile. A candidate is never returned with a
status that invites a predictably failing submission. The admin endpoint takes
an optional `tenant_id`; tenant-scoped operators are pinned to their own tenant,
while a global operator must select a tenant before any model is advertised.

## Empty-catalog startup

An enabled controller may start with zero public profiles. Before workers are
created it runs `fs2-internal-scientific-cpu-v1`, a fixed in-process CPU vector
that validates the packaged artifact-manifest schema, hashes a fixed FASTA
input, performs a bounded amino-acid transform, and checks the exact output
digest. This canary consumes no Kubernetes, Kueue, GPU, model artifact, or
customer resource. It is not a `ScientificWorkloadProfile`, never appears in
discovery, and cannot be submitted by a caller.

The Helm execution map may therefore contain `models: []`. That is a
fail-closed state: startup plumbing is verified, discovery is empty, and all
scientific submissions remain unavailable until a real qualified profile and
matching execution map are delivered.

## Qualification boundary

The profile schema accepts qualified profiles but makes the transition
one-way and explicit: route exposure, `qualified-input` source classification,
immutable artifact and execution digests, MCP parity, usable access state, and
semantic qualification must arrive together. Candidate profiles retain the
opposite constraints.

As of the 2026-09-03 controller handoff, the checked-in BoltzGen and
Proteina-Complexa profiles remain candidates. BoltzGen's H100 receipt qualifies
its configure/design slice but explicitly excludes the later route stages;
Proteina-Complexa's receipt likewise excludes its complete controller route.
Neither is promoted or advertised by this change. A later integration must add
the full-route evidence and exact execution map before changing either profile.

## Verification without a shared deploy

The chart test renders scientific batch enabled with an empty schema-v3 map,
and the controller test constructs that renderer after the CPU canary passes.
Qualified-profile tests then prove positive discovery plus fail-closed model
policy, missing invoke scope, scheduler mismatch, tenant context, and disabled
service behavior. Live rollout belongs to the integrated controller release;
this isolated lane must not replace the shared control plane.
