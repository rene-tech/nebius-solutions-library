# Model metrics in a mixed scientific and general-serving cluster

The general-model admin page projects only its selected model IDs. Prometheus
also retains scientific and historical model series. Its grouped queries must
therefore filter the same selected IDs before the bounded result parser runs.

On the H100 cluster at2026-09-06T21:52:10Z, the unfiltered request-rate query
returned twelve models for a page expecting Qwen and Cosmos. The parser rejected
that response as exceeding its two-series bound, leaving every per-model value
unavailable even though Prometheus had valid measurements. The scoped query
returned both expected model rates successfully. Private live evidence is retained
in the readiness run's `admin-metrics-query-proof.json`.

The fix preserves six grouped queries regardless of model count, escapes literal
model IDs in the PromQL regex, and retains the response label/cardinality checks.
Aggregate platform metrics remain platform-wide; per-model values are scoped to
the requested projection. Missing observations remain unavailable rather than
being invented as zero. Regression tests cover the query scope and model-ID
escaping. Verify the deployed admin response after the next combined release.
