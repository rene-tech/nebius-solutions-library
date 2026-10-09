from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
NETWORK_POLICY_RUNTIME_LABELS = {
    "app.kubernetes.io/component": "model-runtime",
    "app.kubernetes.io/part-of": "fs2-serve",
}


@pytest.mark.parametrize("model_id", ("cellpose-cpsam-v2", "scvi-scanvi"))
def test_visual_science_pods_match_control_plane_runtime_network_policy(model_id: str) -> None:
    path = ROOT / "models" / "visual-science" / "k8s" / f"{model_id}.yaml"
    documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert labels | NETWORK_POLICY_RUNTIME_LABELS == labels
