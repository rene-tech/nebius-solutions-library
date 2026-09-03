"""The scientific onboarding budget: a new model costs tens of lines, not hundreds."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import compile_model  # noqa: E402
import expand_declaration  # noqa: E402

BUDGET_LINES = 50
DECLARATIONS = ROOT / "declarations"


class ExpandDeclarationTests(unittest.TestCase):
    def declarations(self) -> list[Path]:
        return sorted(DECLARATIONS.glob("*.json"))

    def test_every_declaration_is_within_the_onboarding_budget(self) -> None:
        for path in self.declarations():
            with self.subTest(path.name):
                lines = len(path.read_text(encoding="utf-8").splitlines())
                self.assertLessEqual(
                    lines, BUDGET_LINES,
                    f"{path.name} costs {lines} lines; onboarding hundreds of models "
                    f"requires each to stay within {BUDGET_LINES}")

    def test_expansion_is_accepted_by_the_existing_compiler(self) -> None:
        """No second onboarding path: the output is an ordinary model declaration."""
        for path in self.declarations():
            with self.subTest(path.name):
                expanded = expand_declaration.expand(json.loads(path.read_text()))
                compile_model._validate_declaration_value(expanded)

    def test_expansion_is_deterministic(self) -> None:
        for path in self.declarations():
            short = json.loads(path.read_text())
            with self.subTest(path.name):
                self.assertEqual(expand_declaration.expand(short),
                                 expand_declaration.expand(short))

    def test_stage_graph_is_derived_from_the_stage_list(self) -> None:
        short = json.loads((DECLARATIONS / "boltzgen.json").read_text())
        stages = expand_declaration.expand(short)["batch"]["stages"]
        self.assertEqual([s["id"] for s in stages], short["batch"]["gpu_stages"])
        self.assertEqual(stages[0]["needs"], [])
        for earlier, later in zip(stages, stages[1:]):
            self.assertEqual(later["needs"], [earlier["id"]])
            self.assertEqual(later["resource_class"], "gpu")

    def test_a_declaration_states_only_what_differs(self) -> None:
        """Derived sections must not need repeating per model."""
        short = json.loads((DECLARATIONS / "boltzgen.json").read_text())
        for derived in ("resources", "placement", "serving", "execution_mode",
                        "schema_version"):
            self.assertNotIn(derived, short)
        for derived in ("protocol", "request_schema", "result_schema", "service_classes",
                        "access_profile", "access_state", "retry", "cancellation",
                        "stages"):
            self.assertNotIn(derived, short["batch"])

    def test_an_unknown_kind_or_size_is_refused(self) -> None:
        short = json.loads((DECLARATIONS / "boltzgen.json").read_text())
        for field in ("kind", "size_class"):
            with self.subTest(field):
                broken = dict(short, **{field: "no-such-value"})
                with self.assertRaises(expand_declaration.ExpansionError):
                    expand_declaration.expand(broken)

    def test_a_declaration_without_a_gpu_stage_is_refused(self) -> None:
        short = json.loads((DECLARATIONS / "boltzgen.json").read_text())
        short["batch"] = {k: v for k, v in short["batch"].items() if k != "gpu_stages"}
        with self.assertRaises(expand_declaration.ExpansionError):
            expand_declaration.expand(short)


if __name__ == "__main__":
    unittest.main()
