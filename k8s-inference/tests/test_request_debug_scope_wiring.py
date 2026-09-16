"""SAI-01: the request-debug scope/expiry controls are plumbed end to end.

Without this wiring the documented activation path sets only requestDebugEnabled,
which now captures nothing (fail-closed), leaving customer debugging inoperable
without out-of-band Helm edits. These are text-contract checks over the Terraform
and Helm sources, matching the other root contract tests (no Terraform binary).
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCOPE_FIELDS = ("request_debug_tenants", "request_debug_models", "request_debug_expires_at")
CHART_KEYS = ("requestDebugTenants", "requestDebugModels", "requestDebugExpiresAt")
ENV_VARS = ("FS2_REQUEST_DEBUG_TENANTS", "FS2_REQUEST_DEBUG_MODELS", "FS2_REQUEST_DEBUG_EXPIRES_AT")


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class RequestDebugScopeWiringTests(unittest.TestCase):
    def test_root_deployment_object_declares_scope_controls(self) -> None:
        variables = _read("variables.tf")
        for field in SCOPE_FIELDS:
            self.assertIn(field, variables, f"{field} missing from deployment.observability")

    def test_root_locals_and_outputs_forward_scope_controls(self) -> None:
        locals_tf = _read("locals.tf")
        outputs_tf = _read("outputs.tf")
        for field in SCOPE_FIELDS:
            self.assertIn(
                f"{field}", locals_tf, f"{field} not forwarded to the workloads stage in locals.tf"
            )
            self.assertIn(
                f"var.deployment.observability.{field}", outputs_tf, f"{field} not surfaced in outputs.tf"
            )

    def test_workloads_stage_declares_scope_variables(self) -> None:
        variables = _read("stages/workloads/variables.tf")
        for field in SCOPE_FIELDS:
            self.assertIn(f'variable "{field}"', variables, f"{field} variable missing in workloads stage")

    def test_control_plane_config_passes_scope_controls_to_chart(self) -> None:
        control_plane = _read("stages/workloads/control_plane.tf")
        for key, field in zip(CHART_KEYS, SCOPE_FIELDS, strict=True):
            self.assertRegex(
                control_plane,
                rf"{key}\s*=\s*var\.{field}",
                f"control_plane.tf does not pass {key} from var.{field}",
            )

    def test_chart_values_and_env_expose_scope_controls(self) -> None:
        values = _read("charts/control-plane/fs2-serve-control-plane/values.yaml")
        helpers = _read("charts/control-plane/fs2-serve-control-plane/templates/_helpers.tpl")
        for key in CHART_KEYS:
            self.assertIn(key, values, f"{key} missing from chart values")
        for env in ENV_VARS:
            self.assertIn(env, helpers, f"{env} not emitted by the chart env helper")

    def test_capture_has_no_global_switch(self) -> None:
        # A regression guard: the removed capture_all footgun must not return.
        for relative in (
            "variables.tf",
            "locals.tf",
            "outputs.tf",
            "stages/workloads/variables.tf",
            "stages/workloads/control_plane.tf",
            "charts/control-plane/fs2-serve-control-plane/values.yaml",
            "charts/control-plane/fs2-serve-control-plane/templates/_helpers.tpl",
        ):
            lowered = _read(relative).lower()
            self.assertNotIn("capture_all", lowered, f"capture_all reintroduced in {relative}")
            self.assertNotIn("captureall", lowered, f"capture_all reintroduced in {relative}")


if __name__ == "__main__":
    unittest.main()
