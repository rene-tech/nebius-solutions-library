# Scientific AI naming and compatibility

Use the same customer-facing vocabulary in Terraform outputs, the admin console,
MCP metadata, client configuration, skills and handovers.

| Concept | Canonical name or identifier |
| --- | --- |
| Product | **Nebius Scientific AI** |
| Deployable/catalog resource | **Scientific AI App** |
| Protocol service | **Scientific AI MCP server** |
| MCP `serverInfo.name` and maintained LibreChat connection | `scientific-ai-apps` |
| MCP display title | **Nebius Scientific AI Apps** |
| Customer assistant | **Nebius Scientific AI Agent** |
| Conversational model provider | **Nebius Token Factory** |
| NVIDIA model ecosystem | **NVIDIA BioNeMo**, only where that lineage is true |

An App can be an NVIDIA BioNeMo model, another NVIDIA model, a community model,
an internally packaged runtime or a multi-step scientific workflow. BioNeMo is
therefore a model ecosystem and compatibility boundary, not the name of the
complete platform, catalog, MCP server or customer client.

## Stable implementation identifiers

The following are not customer-facing brands and remain stable to avoid an
unrelated infrastructure/data migration:

- Python package and CLI `fs2_serve` / `fs2-serve`;
- Kubernetes names, labels and namespaces beginning with `fs2`;
- database tables and versioned `fs2-serve.nebius.ai` schemas;
- model source directories such as `models/bionemo` when their contents are
  specifically derived from NVIDIA BioNeMo;
- deployment variables `SCIENTIFIC_MODELS_MCP_URL`,
  `SCIENTIFIC_MODELS_API_BASE_URL` and `SCIENTIFIC_MODELS_API_KEY`.

These may appear in operator diagnostics, manifests and source paths. UI labels,
MCP metadata and customer instructions must use the canonical names above.

## Legacy client migration

Older LibreChat configurations named the same MCP endpoint `bionemo-models`.
New clients use only `scientific-ai-apps`; do not configure both names because
LibreChat would expose duplicate copies of every tool. The rename changes
LibreChat-generated tool IDs from `_mcp_bionemo-models` to
`_mcp_scientific-ai-apps`. Update the saved agent's `mcpServerNames`, wildcard
tool attachment and deferred-tool suffix together.

Preserve old tool IDs in historical conversations and evidence as provenance.
They are not valid names for new calls after migration. A client-local MCP
connection name does not alter the HTTPS endpoint, API key, App identity,
operation ID or server-side authorization.

Use “BioNeMo” only for a real NVIDIA BioNeMo model/source/toolkit statement.
Use “Scientific AI App,” “Scientific AI MCP server,” or “Nebius Scientific AI”
for shared platform behavior.
