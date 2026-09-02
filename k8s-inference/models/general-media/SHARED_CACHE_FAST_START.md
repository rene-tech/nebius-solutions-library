# Shared-cache fast start for public Hugging Face models

Qwen3-8B and Cosmos3-Nano use the same inline localization protocol when their
model PVC is rendered onto the cluster shared filesystem. The protocol also
remains correct with the checked-in single-node PVC, so storage selection stays
outside the model template.

| Model | Exact revision | Verified content address |
|---|---|---|
| `qwen3-8b` | `b968826d9c46dd6066d109eabc6255188de91218` | `sha256/5b0f0f64ddb02ee2deeed4772968b9e2139a922acc9b9bb9c3488d23c678971d` |
| `cosmos3-nano` | `7a312c868bcce8e40b3eb40861300a9d0ba3fde1` | `sha256/dfa7b03382ba78d7f80703652706c3cfa777cefac48634df49345c4302af2c95` |

## First localization

The init container takes an advisory POSIX lock scoped to the model content
digest. Only that process resolves the exact Hugging Face revision. It probes
the existing revision-pinned Hugging Face cache before allowing network access;
Qwen can also adopt its prior direct-download directory after verifying it.
Regular files are hardlinked into the private staging tree when the cache and
publication path share a filesystem, avoiding a second 16 GB or 35 GB copy.
Backends that cannot hardlink use a copy fallback. Download and full SHA-256
verification use bounded parallel workers. The localizer checks the exact
path/size/SHA-256 inventory, writes a deterministic receipt, and atomically
renames the complete staging directory into its content address.

Other replicas wait for the same lock and re-check the receipt after acquiring
it. A failed writer releases the lock when its process exits and removes its
private staging directory. A content address that exists without its exact
receipt fails closed; the localizer never merges a partial tree into it.

The storage class used for multi-node scale-out must provide shared POSIX file
locking and atomic rename. Acceptance must verify those semantics on the
rendered Nebius shared-filesystem CSI/mount path. Any other RWX implementation
must pass the same check before it is used as a concurrent writer target.

## Subsequent replica starts

The receipt is
`fs2-serve.nebius.ai/shared-cache-localization-receipt/v2`. It binds the model
ID, repository, immutable revision, artifact-manifest digest, content digest,
file count, total bytes, and payload location. On a cache hit the init container
reads only this small receipt and checks each expected path's type and size. It
does not call Hugging Face and does not read the 16 GB or 35 GB payload again to
rehash it.

This fast path is safe because publication is atomic and the serving container
mounts the model volume read-only. The init container is the only writer. A
new revision or manifest necessarily selects a new content address and cannot
reuse the old receipt.

Both runtimes receive the exact local payload path. Cosmos keeps its public
`nvidia/Cosmos3-Nano` served-model name, while outbound Hugging Face and
Transformers access is disabled in the serving container. Runtime-generated
files and compiler caches remain on the separate writable runtime-cache volume.

The init log emits one of `localized`, `cache-hit`, or `cache-hit-after-wait`
with elapsed wall time. Those events distinguish initial cache fill from warm
replica startup without changing the immutable receipt.

## Performance and snapshot boundary

This removes repeated network transfer and repeated full-payload hashing. It
does not restore GPU memory, CUDA process state, compiled kernels, or KV cache.
Both models still use conventional runtime startup and weight loading. No GPU
snapshot is selected or claimed by these manifests; a snapshot path remains
disabled until its exact model, runtime image, driver, CUDA, GPU, and topology
tuple passes separate restore and semantic qualification.

Cold-start measurements must report image pull, localization, model load,
readiness, and first valid response separately. The receipt and code path are
an optimization mechanism, not evidence of a latency result.
