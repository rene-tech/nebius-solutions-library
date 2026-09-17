"""Repository-wide inheritance contract for Envoy Gateway security policies."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path


SOLUTION_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SOLUTION_ROOT.parent
GIT = shutil.which("git")
if GIT is None:
    raise RuntimeError("git is required for security-policy composition tests")


def terraform_resource_blocks(source: str) -> list[str]:
    """Return balanced kubernetes_manifest resource blocks without evaluating HCL."""

    starts = list(re.finditer(r'resource\s+"kubernetes_manifest"\s+"[^"]+"\s*\{', source))
    blocks: list[str] = []
    for start in starts:
        depth = 0
        quoted = False
        escaped = False
        for index in range(start.end() - 1, len(source)):
            character = source[index]
            if escaped:
                escaped = False
                continue
            if quoted and character == "\\":
                escaped = True
                continue
            if character == '"':
                quoted = not quoted
                continue
            if quoted:
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(source[start.start() : index + 1])
                    break
        else:
            raise ValueError("unterminated kubernetes_manifest resource")
    return blocks


def route_security_policy_without_parent_merge(source: str) -> list[str]:
    findings: list[str] = []
    for block in terraform_resource_blocks(source):
        if not re.search(r'kind\s*=\s*"SecurityPolicy"', block):
            continue
        if not re.search(r'kind\s*=\s*"HTTPRoute"', block):
            continue
        if not re.search(r'mergeType\s*=\s*"StrategicMerge"', block):
            name = re.search(r'resource\s+"kubernetes_manifest"\s+"([^"]+)"', block)
            findings.append(name.group(1) if name else "unnamed")
    return findings


def yaml_route_security_policy_without_parent_merge(source: str) -> list[str]:
    findings: list[str] = []
    for index, document in enumerate(re.split(r"^---\s*$", source, flags=re.MULTILINE)):
        active = "\n".join(line for line in document.splitlines() if not line.lstrip().startswith("#"))
        if not re.search(r"^\s*kind:\s*SecurityPolicy\s*$", active, flags=re.MULTILINE):
            continue
        if not re.search(r"^\s*kind:\s*HTTPRoute(?:Rule)?\s*$", active, flags=re.MULTILINE):
            continue
        if not re.search(r"^\s*mergeType:\s*StrategicMerge\s*$", active, flags=re.MULTILINE):
            findings.append(f"document-{index + 1}")
    return findings


class SecurityPolicyCompositionTests(unittest.TestCase):
    def test_every_route_security_policy_source_merges_parent_security(self) -> None:
        result = subprocess.run(  # noqa: S603 - fixed read-only Git inventory.
            [GIT, "-C", str(REPOSITORY_ROOT), "ls-files", "-z", "--", "k8s-inference"],
            check=True,
            capture_output=True,
        )
        findings: list[str] = []
        for encoded in result.stdout.split(b"\0"):
            if not encoded:
                continue
            path = REPOSITORY_ROOT / encoded.decode()
            if path.suffix == ".tf":
                names = route_security_policy_without_parent_merge(path.read_text(encoding="utf-8"))
            elif path.suffix in {".yaml", ".yml"}:
                names = yaml_route_security_policy_without_parent_merge(path.read_text(encoding="utf-8"))
            else:
                names = []
            findings.extend(f"{path.relative_to(REPOSITORY_ROOT)}:{name}" for name in names)
        self.assertEqual([], findings)

    def test_unmerged_route_policy_is_rejected_by_composition_contract(self) -> None:
        policy = '''
resource "kubernetes_manifest" "unsafe_child" {
  manifest = {
    kind = "SecurityPolicy"
    spec = { targetRefs = [{ kind = "HTTPRoute" }] }
  }
}
'''
        self.assertEqual(["unsafe_child"], route_security_policy_without_parent_merge(policy))

    def test_strategically_merged_route_policy_is_accepted_by_composition_contract(self) -> None:
        policy = '''
resource "kubernetes_manifest" "safe_child" {
  manifest = {
    kind = "SecurityPolicy"
    spec = {
      mergeType = "StrategicMerge"
      targetRefs = [{ kind = "HTTPRoute" }]
    }
  }
}
'''
        self.assertEqual([], route_security_policy_without_parent_merge(policy))

    def test_yaml_route_policy_requires_the_same_merge_contract(self) -> None:
        unmerged = """
kind: SecurityPolicy
spec:
  targetRefs:
    - kind: HTTPRoute
"""
        merged = unmerged + "  mergeType: StrategicMerge\n"
        self.assertEqual(["document-1"], yaml_route_security_policy_without_parent_merge(unmerged))
        self.assertEqual([], yaml_route_security_policy_without_parent_merge(merged))


if __name__ == "__main__":
    unittest.main()
