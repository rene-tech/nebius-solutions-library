# Typed MCP release — 2026-09-09

Deployed and live-verified on the H100 cluster. Every currently exposed model/App
tool has concrete inputs and meaningful descriptions, not only models covered by
NVIDIA skills. See the [client guide](../../docs/mcp-model-tools.md) and
[live evidence](live-summary.json).

## Access and behavior

- MCP: `https://89.169.99.188/mcp`
- Admin: `https://89.169.99.188/admin/`
- Existing ordinary inference keys and model grants are unchanged. Reconnect or
  refresh a client's tool list to pick up the new schemas.
- `get_model_schema` returns authorized model-specific schemas, examples, source
  references and selected-runtime identity. Named tools accept explicit fields.
- All 19 shared tools document discovery, uploads, submissions, status, results,
  cancellation and acknowledgement. Old wrappers/generic submission tools remain
  available for compatibility; existing queues and durable results are preserved.
- Full HTTP/MCP and upstream debugging capture remains enabled. Inspect an App's
  Request log or the all-request log, including invalid requests with no run.

## Verified result

The live caller discovered 27 models/App routes and exactly 27 corresponding typed
tools: 14 native, two OpenAI-chat and 11 scientific-batch. All schemas matched
`tools/list` and `get_model_schema`, including field descriptions and provenance.
All 25 supplied examples validated; two asset/geometry-dependent models explicitly
provide no standalone example. Scientific example artifact IDs are not uploads
created for a customer.

Fresh PhenoAge, Qwen, Boltz2 and OpenFold2 requests all succeeded on attempt one,
and the client retrieved their actual results. Invalid Boltz2 MSA and unsupported
OpenFold2 alignment inputs returned structured `-32602` errors before admission,
with model, key/owner and exact body correlation in debug capture. No customer
workload was replayed. This is interface/lifecycle acceptance, not a new benchmark
or new scientific qualification of every model.

The final frozen offline regression passed 96 tests, including actual MCP HTTP
clients, tenant isolation, flat/wrapped idempotent replay, scientific envelopes,
schema/example coverage and clean-wheel packaging of all seven JSON resources.
Ruff and targeted strict mypy passed. Earlier in-development fixture/source
failures were retained in task evidence, not counted as passing runs.

## Deployment and cleanup

Repository: `rene-tech/nebius-solutions-library`, branch `main`.
Application source: `5de025fe58cc42812629c87694a1ff326e0ef00f`.
Source tree: `5c6a4cc2a65ed117ea44f0fcb098c3f2bc52e578`.

The regional control-plane image changed from
`sha256:aabc80f6ae713fa70a4850742b4fdd12ba55cd6cb3c2c50ab96d210bf2036bdb`
to `sha256:780f92b984b419aed112ced48d3df55eae1ad006091346c13f4953db23390870`.
Admin image is unchanged at
`sha256:2f7cb5a48af96ceb1c6d78f73b1bc030ad2ece0f1b493518485ba74d61fb5f2b`.

Normal Terraform deployment used only the control-plane image change in private
`terraform.tfvars`. The reviewed plan changed release metadata/bootstrap objects,
not model settings, GPU/node resources or queue capacity. Post-apply plans reported
zero managed changes in infrastructure, foundation and workloads. No quota/limit
increase was requested or performed.

Target: project `project-e00rene`, region `eu-north1`, cluster
`mk8scluster-e00j5z9te7x5dd9g6a`, context `k8s-inference-h100`.
Gateway, controller and admin were each ready 2/2 after rollout. A browser reload
rendered the Boltz2 App and Request log without UI alerts. Earlier request-logging
browser checks verified body rendering, ownership, redacted headers and actual
JSON download; transient browser network-change events and reload recovery remain
documented in the [logging evidence](../request-debug-20260909/README.md).
Test clients/admin sessions closed; existing keys, models and cluster stay running.
Raw captures, plan files and build receipts remain private; Git contains sanitized
evidence and reproducible helpers only.

## Compatibility boundary

NVIDIA BioNeMo skills can guide an agent using these typed tools, but existing
NVIDIA REST scripts are not automatically redirected to MCP and may expect
unsupported runtime features or immediate synchronous results. Portable Boltz2
requires explicit A3M; portable OpenFold2 does not accept external alignment or
template inputs. These differences are stated, not silently discarded. See the
[pinned mapping](BIONEMO-COMPATIBILITY.md). Known different runtime variants do not
inherit an incorrect portable schema. GLM remains outside this H100 deployment.
