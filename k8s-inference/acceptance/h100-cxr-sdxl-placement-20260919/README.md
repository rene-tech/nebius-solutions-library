# Exact H100 placement metadata for SDXL and NV-Reason-CXR

The canonical records describe B300 resources, while the actual operator-owned
deployments and retained public calls run these exact images on one H100. This
caused false `unsupported_accelerator_placement` warnings and could block later
activation or placement changes. A digest-pinned additive receipt records only
the already demonstrated H100 compatibility; it does not rewrite either canonical
record or its immutable artifact identity.

| App | Public successful calls | Conservatively bracketed hardware witnesses |
| --- | ---: | ---: |
| SDXL | 36 | 35 |
| NV-Reason-CXR-3B | 20 | 18 |

The three unmatched completions remain in `retained-public-proof.json`; they are
not used as GPU qualification evidence. Each accepted witness joins an immutable
operation Pod UID, node UID and GPU UUID with observations before and after the
call. It requires the same running container, actual image, model revision and
content identity, driver `580.159.04-1ubuntu1`, provider SKU `gpu-h100-sxm` and GPU
class `nvidia-h100-sxm5-80gb`. Downloaded output artifacts are checked against the
public reference's size/hash, or inline output against its retained response.

Scope is runtime and bounded public-output semantic compatibility. This is **not**
clinical efficacy, SDXL prompt fidelity, full scientific correctness, cold-start
latency, scale-out or snapshot qualification. It grants no L40S or other GPU
support. Existing snapshots and owner policies remain unchanged.

## Reproduce without model calls

Run from the control-plane environment, with a new protected output directory:

```sh
uv run --frozen python ../../acceptance/h100-cxr-sdxl-placement-20260919/prepare.py \
  --root /path/to/retained-campaign \
  --baseline /path/to/backend-baseline177 \
  --output /path/to/new-proof
```

The retained cohorts are `general-r1` and
`cxr-schema-attribution-repaired-r1`. Minute observations, original request/result
hashes and canonical records are joined without any network call. The portable
proof omits request bodies and credentials. Raw weights are not rehashed here;
the source manifest, unchanged canonical identity and observed content annotation
are retained as distinct evidence.

The source receipt and packaged control-plane copy must remain byte-identical.
The approved package hash is pinned in `configuration.py`. Altered runtime,
revision, manifest, GPU count/topology, unreviewed receipt bytes and duplicate
qualification identities cannot extend placement support. This is a metadata-only
control-plane release; it needs no owner, four-map, resource or limit mutation.
