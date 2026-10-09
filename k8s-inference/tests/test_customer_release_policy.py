"""Keep the restored shared-service requirements alongside customer-key isolation."""

import unittest
from pathlib import Path


class CustomerReleasePolicyTests(unittest.TestCase):
    def test_shared_service_and_internal_test_identity_requirements_are_retained(self):
        policy = (Path(__file__).resolve().parents[1] / "CUSTOMER_RELEASE_POLICY.md").read_text()
        for requirement in (
            "## Shared-model concurrency, capacity and scaling",
            "**Isolation and scheduling:**",
            "**Measured capacity envelope:**",
            "**Saturation and headroom:**",
            "**Scaling and failure:**",
            "`system/qa` or `system/development` inference identity",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, policy)


if __name__ == "__main__":
    unittest.main()
