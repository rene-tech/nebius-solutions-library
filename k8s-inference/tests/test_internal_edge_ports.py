from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "stages"
    / "workloads"
    / "scripts"
    / "internal_edge_acceptance.py"
)
SPEC = importlib.util.spec_from_file_location(
    "internal_edge_acceptance_under_test", SCRIPT_PATH
)
assert SPEC and SPEC.loader
ACCEPTANCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCEPTANCE)


class InternalEdgePortTests(unittest.TestCase):
    def tearDown(self) -> None:
        ACCEPTANCE.configure_local_ports(18080, 18081, 18082)

    def test_offset_tuple_configures_all_loopback_routes(self) -> None:
        ACCEPTANCE.configure_local_ports(28080, 28081, 28082)

        self.assertEqual(ACCEPTANCE.CONTROL_PORT, 28080)
        self.assertEqual(ACCEPTANCE.ADMIN_PORT, 28081)
        self.assertEqual(ACCEPTANCE.PROXY_PORT, 28082)
        self.assertEqual(ACCEPTANCE.APPLICATION_ORIGIN, "http://localhost:28082")
        self.assertEqual(ACCEPTANCE.upstream_port("/mcp"), 28080)
        self.assertEqual(ACCEPTANCE.upstream_port("/admin/"), 28081)

    def test_tuple_rejects_privileged_or_colliding_ports(self) -> None:
        for ports in ((443, 28081, 28082), (28080, 28080, 28082)):
            with self.subTest(ports=ports), self.assertRaises(ValueError):
                ACCEPTANCE.configure_local_ports(*ports)


if __name__ == "__main__":
    unittest.main()
