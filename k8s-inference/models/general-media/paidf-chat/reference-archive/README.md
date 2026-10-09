# Temporary hosted Qwen reference baseline

2026-09-21: the user explicitly authorized a new temporary caption verification
deployment. Only the original Qwen3.6-27B-FP8 declaration is projected into the
catalog for `paidf-caption-check-20260921`; the retired original App identity
and Qwen2.5 remain retired. This is a diagnostic deployment, not a change to the
public Token Factory production target. Remove its runtime, owned cache and
active catalog projection after the separately recorded verification.

These declarations and exact weight inventories are retained evidence, outside
the active model catalog. The user requested hosted Qwen only to double-check
NVIDIA's reference implementation, then removal from Scientific AI and use of
explicitly declared public Token Factory models. Do not re-register these Apps
as part of normal platform bootstrap or the video workbench deployment.

The original `prepare_registration.py` and `build_catalog.py` in the parent
directory describe the historical baseline acquisition. Their original source
and receipts are retained for reproducibility, not as current deployment steps.

Retirement retains revision/audit/result evidence. The two dedicated weight
caches can be deleted after drain, archive-backed App retirement, and verified
absence of Pod references; the inventory here permits exact re-download for a
separately authorised future reference check.
