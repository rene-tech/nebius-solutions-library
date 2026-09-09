# LibreChat client integration

This directory contains a deployable client contract for using the inference
solution from LibreChat:

- `librechat.example.yaml`: per-user MCP authentication and agent capabilities;
- `AGENT_INSTRUCTIONS.md`: instructions to install on the saved workbench agent;
- `skills/scientific-gateway/`: the replacement shared deployment skill;
- `HANDOVER.md`: deployment, identity, file-bridge and acceptance requirements.

Set the non-secret deployment variable:

```text
SCIENTIFIC_MODELS_MCP_URL=https://89.169.99.188/mcp
```

Each LibreChat user supplies their own ordinary inference key through MCP
Settings. No key is stored in this repository. For another installation, use
the Terraform `mcp` output instead of the example address.

The current public endpoint was checked on 2026-09-09 with normal TLS
verification: `/mcp` was reachable and returned `401` without credentials, as
expected. No inference was submitted by this documentation check.
