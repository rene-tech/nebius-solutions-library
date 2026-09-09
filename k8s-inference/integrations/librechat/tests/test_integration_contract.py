from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "scientific-gateway"


def test_librechat_mcp_uses_per_user_bearer_auth() -> None:
    config = yaml.safe_load((ROOT / "librechat.example.yaml").read_text(encoding="utf-8"))
    server = config["mcpServers"]["bionemo-models"]

    assert server["type"] == "streamable-http"
    assert server["url"] == "${SCIENTIFIC_MODELS_MCP_URL}"
    assert server["startup"] is False
    assert server["requiresOAuth"] is False
    assert server["serverInstructions"] is True
    assert server["headers"]["Authorization"] == "Bearer {{SCIENTIFIC_MODELS_API_KEY}}"
    assert server["customUserVars"]["SCIENTIFIC_MODELS_API_KEY"]["sensitive"] is True


def test_skill_package_has_current_typed_operation_contract() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    reference = (SKILL / "references" / "client-contract.md").read_text(encoding="utf-8")

    assert "name: scientific-gateway" in text
    for required in (
        "get_model_schema",
        "model_input_validation",
        "get_operation_result",
        "get_scientific_result",
        "begin_scientific_artifact_upload",
        "acknowledge_operation",
    ):
        assert required in text
    assert "intentionally generic" not in text
    assert "No public per-model parameter-schema" not in text
    assert "just under 12 MiB" in reference


def test_handover_and_agent_instructions_do_not_embed_private_keys() -> None:
    paths = [
        ROOT / "HANDOVER.md",
        ROOT / "AGENT_INSTRUCTIONS.md",
        ROOT / "librechat.example.yaml",
        SKILL / "SKILL.md",
        SKILL / "references" / "client-contract.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert not re.search(r"(?:fs2_pat_|nvapi-)[A-Za-z0-9_-]{8,}", combined)
    assert "scientific_models__get_model_schema" in combined  # documented as obsolete only
    assert "get_model_schema_mcp_bionemo-models" in combined
    assert "clawbio_upload_create" in combined  # documented as an incompatible legacy bridge
