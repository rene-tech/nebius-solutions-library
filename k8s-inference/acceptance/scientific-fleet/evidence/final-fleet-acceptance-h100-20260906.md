# Final H100 scientific fleet acceptance — 6 September 2026

All ten scientific profiles passed real public requests on runtime source
`8bb53aabc6231c5c29e29d265e7df776750b6c7c`, with the original strict
request, semantic-result and artifact validators. Five primary and five
secondary profiles succeeded; none failed. The unmodified runner used a
maximum of eight concurrent workers. Its ESMFold2-Fast submission was deferred
until the independent admin policy test restored the original policy; the
other models progressed in parallel.

| Public model ID | Semantic result | Accepted to completed |
| --- | --- | ---: |
| `alphafold3` | Passed | 70.792 s |
| `bindcraft` | Passed | 618.703 s |
| `boltzgen` | Passed | 802.182 s |
| `esmfold2` | Passed | 143.403 s |
| `esmfold2-fast` | Passed | 82.031 s |
| `mosaic` | Passed | 583.329 s |
| `openfold3-openbind` | Passed | 643.290 s |
| `proteina-complexa` | Passed | 696.834 s |
| `protenix-v2` | Passed | 123.526 s |
| `rfdiffusion` | Passed | 183.047 s |

Times use durable operation timestamps, rounded to millisecond resolution.
They include queueing, CPU preparation, image/cache state, model work and
result publication under the concurrent campaign. Different fixtures do not
form a model-speed ranking. These are single functional-acceptance samples,
not cold-start percentiles, isolated weight-loading measurements or maximum
throughput. The standard receipt's isolated cold-start field is unavailable;
no snapshot was used and no missing duration is replaced with zero.

Separate [varied-input and burst evidence](customer-readiness-h100-20260906.md)
covers customer/bulk contention, 17 simultaneous GPU admissions and preempted
work retry. [Repeated optimized stages](optimized-stages-h100-29b7e01a-20260906.md)
separate the initial cold CPU-capacity request from warm RFdiffusion/mosaic
repetitions. [Real admin controls](customer-access-policy-h100-8bb53aab-20260906.md)
prove scientific-key scope/revocation, durable pause/resume, cap-one dispatch,
artifact delivery and restored configuration. Those records also reconcile
application GPU occupied/active/idle intervals; the legacy fleet receipt's
missing aggregate GPU field must not be mistaken for absent underlying
lifecycle accounting or for measured zero utilization.

## Qualification and reproducibility

Campaign: `final-scientific-release-8bb53aab-20260906T224216Z`.

Execution map SHA-256:
`cafe5eaa551f213a4919b79e3c782da58e381838dee50b984eba32c5004b2e09`.

Aggregate SHA-256:
`0fcb8b38233467f9ae06fa63c3aebb92be09efdd18ed7dbf3e9e43b04b89d635`.

The exact runtime checkout was prepared offline before collection so the
runner could pin its input activation fragments. It was never deployed in
the intermediate active state. New scheduler eligibility receipts bind the
successful runs to the unchanged execution identities; historical receipts
remain intact. Final deployment identity is available in the Terraform access
outputs; later admin/MCP changes do not change these scientific execution
recipes.

Raw private receipts remain together under the campaign directory in the
release owner's `final-scientific-release-qualification` evidence store.
Individual receipt digests follow:

| Model | Receipt SHA-256 |
| --- | --- |
| `alphafold3` | `533836c49169edaadbcce9dc587e1b40aafb196bab4165e5cb2fc893aea6058c` |
| `bindcraft` | `5e105c7c53f129a7a7719ae720811c14f167217db5d9c55b780e3f77737cf189` |
| `boltzgen` | `7c116e90853b0e0edfe0d6451f327813338716dde285af2239d506d0c3c9f5cd` |
| `esmfold2` | `1f3b9ef318fdaa3a00108a772dcced96d65490fd3dae00a241388a76e16905a3` |
| `esmfold2-fast` | `d970dad8f184c15f09d1c6556dfa2ac5492f6b66eea9b7f92babc7e671b1571b` |
| `mosaic` | `df57f86a7e936b9dc693879c3af084121e591f7c8febc9b7e0600e7c6fe3ff5f` |
| `openfold3-openbind` | `0ae693f45e04c06f14de8ad7b2c36a09568a11b88bd8c5f57ce2c83046de67db` |
| `proteina-complexa` | `89a7b449264d6a3b1ff8087c9331fd82b6e65732985d5cd160ccc101651a21db` |
| `protenix-v2` | `07578f238ccb4a2ecc540b083ec0c1609f66369acd5d49db13aeaf80200386d1` |
| `rfdiffusion` | `80c3759306c6d10817ca9f31876607b02426a66732cb54d163d93e1f3f12bd4a` |

These receipts do not grant commercial rights to academic-restricted models.
AlphaFold3 and BindCraft/PyRosetta remain subject to the configured tenant's
existing entitlement. They also do not qualify untested GPU families, new
model versions, production GPU snapshots, or larger scientific workloads.

