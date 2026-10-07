"""Render the real chart, including Prometheus selection and delivery controls."""

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_terraform_retains_the_independently_qualified_collector():
    assert 'tools_image = optional(string, "")' in (ROOT / "variables.tf").read_text()
    assert (
        "tools_image              = var.deployment.scientific_batch.tools_image"
        in (ROOT / "locals.tf").read_text()
    )
    assert (
        "toolsImage                      = var.scientific_batch.tools_image"
        in (ROOT / "stages/workloads/scientific_artifacts.tf").read_text()
    )
    assert (
        "var.scientific_batch.tools_image"
        in (ROOT / "stages/workloads/variables.tf").read_text()
    )


def test_rules_are_selected_and_email_is_important_only():
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "fs2-important-alerts",
            str(ROOT / "charts/addons/important-alerts"),
            "--namespace",
            "fs2-observability",
            "-f",
            str(Path(__file__).with_name("important-alerts.values.yaml")),
        ],
        text=True,
    )
    documents = {item["kind"]: item for item in yaml.safe_load_all(rendered) if item}
    rules = documents["PrometheusRule"]
    assert rules["metadata"]["labels"]["release"] == "fs2-r927c465c6d-monitoring"
    assert len(rules["spec"]["groups"][0]["rules"]) == 4
    for rule in rules["spec"]["groups"][0]["rules"]:
        assert rule["labels"]["fs2_notification"] == "important"
    delivery = documents["AlertmanagerConfig"]["spec"]
    assert delivery["route"]["repeatInterval"] == "24h"
    assert delivery["route"]["matchers"] == [
        {"name": "fs2_notification", "matchType": "=", "value": "important"}
    ]
    email = delivery["receivers"][0]["emailConfigs"][0]
    assert email["to"] == "rene@nebius.com"
    assert email["sendResolved"] is False
    assert email["authPassword"] == {
        "name": "fs2-important-alert-mail",
        "key": "smtp-password",
    }


def test_actual_prometheus_rules(tmp_path):
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "fs2-important-alerts",
            str(ROOT / "charts/addons/important-alerts"),
            "--namespace",
            "fs2-observability",
            "-f",
            str(Path(__file__).with_name("important-alerts.values.yaml")),
        ],
        text=True,
    )
    rules = next(
        item["spec"]
        for item in yaml.safe_load_all(rendered)
        if item["kind"] == "PrometheusRule"
    )
    (tmp_path / "rendered-rules.yaml").write_text(yaml.safe_dump(rules))
    (tmp_path / "test-alert-rules.yaml").write_text(
        Path(__file__).with_name("test_alert_rules.yaml").read_text()
    )
    subprocess.run(
        ["promtool", "check", "rules", "rendered-rules.yaml"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["promtool", "test", "rules", "test-alert-rules.yaml"], cwd=tmp_path, check=True
    )
