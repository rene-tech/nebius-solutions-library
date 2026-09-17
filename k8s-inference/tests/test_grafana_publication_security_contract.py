from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_external_grafana_requires_an_explicit_bounded_operator_allowlist() -> None:
    variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
    stage_variables = (ROOT / "stages/workloads/variables.tf").read_text(
        encoding="utf-8"
    )
    root_locals = (ROOT / "locals.tf").read_text(encoding="utf-8")

    assert "allowed_source_cidrs = optional(set(string), [])" in variables
    assert "var.deployment.observability.grafana.publish_external ?" in variables
    assert "tonumber(split(\"/\", cidr)[1]) >= 8" in variables
    assert "grafana_allowed_source_cidrs" in stage_variables
    assert "universal or malformed networks are forbidden" in stage_variables
    assert (
        "grafana_allowed_source_cidrs     = "
        "sort(tolist(var.deployment.observability.grafana.allowed_source_cidrs))"
        in root_locals
    )


def test_grafana_route_is_deny_by_default_and_rate_limited_at_the_edge() -> None:
    policy = (ROOT / "stages/workloads/admin_grafana_security.tf").read_text(
        encoding="utf-8"
    )

    assert 'kind       = "SecurityPolicy"' in policy
    assert 'kind  = "HTTPRoute"' in policy
    assert 'name  = "fs2-admin-grafana"' in policy
    assert 'defaultAction = "Deny"' in policy
    assert 'action = "Allow"' in policy
    assert "clientCIDRs = sort(tolist(var.grafana_allowed_source_cidrs))" in policy
    assert '!contains(var.grafana_allowed_source_cidrs, "0.0.0.0/0")' in policy

    assert 'kind       = "BackendTrafficPolicy"' in policy
    assert 'mergeType = "StrategicMerge"' in policy
    assert "rateLimit = {" in policy
    assert "requests = 200" in policy
    assert 'unit     = "Second"' in policy
    assert policy.count("depends_on = [kubernetes_manifest.grafana_http_route]") == 2


def test_grafana_policy_resources_are_in_the_managed_address_contract() -> None:
    outputs = (ROOT / "stages/workloads/outputs.tf").read_text(encoding="utf-8")
    grafana_increment = (
        "(data.terraform_remote_state.foundation.outputs."
        "grafana_publication_contract.enabled ? 2 : 0)"
    )

    assert outputs.count(grafana_increment) == 2
