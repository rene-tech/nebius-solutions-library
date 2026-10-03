"""Offline checks for the internal acceptance runner, not platform qualification."""

import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

SPEC = importlib.util.spec_from_file_location("concurrency", Path(__file__).with_name("verify_concurrency.py"))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
SUMMARY_SPEC = importlib.util.spec_from_file_location("summary", Path(__file__).with_name("summarize.py"))
summary = importlib.util.module_from_spec(SUMMARY_SPEC)
SUMMARY_SPEC.loader.exec_module(summary)


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_read_accepts_only_exact_system_qa(self):
        from types import SimpleNamespace

        key = {"id": runner.QA_KEY_ID, "tenant_id": "lynx", "principal_id": "lynx", "expires_at": None}
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": {"items": [key]}})
        admin = SimpleNamespace(get=AsyncMock(return_value=response))
        run = runner.Run(None, None)
        with self.assertRaises(ValueError):
            await run.key_metadata(admin)
        key.update(tenant_id="system", principal_id="qa")
        self.assertEqual(await run.key_metadata(admin), key)
        key["expires_at"] = "2026-10-04T00:00:00Z"
        with self.assertRaises(ValueError):
            await run.key_metadata(admin)


class FixtureTests(unittest.TestCase):
    def test_seed_and_length_are_reproducible_and_topology_is_unchanged(self):
        # The input is the installed, immutable system QA starter pack; no cloud
        # request and no customer data are involved in this offline check.
        fixture = Path("/home/tux/secure-handoff/fs2-agent-reliability-20261001/reliability-r7/workspace/examples/v3/molecular-dynamics/alanine-quickstart/gromacs")
        if not fixture.exists():
            self.skipTest("Installed immutable QA fixture is unavailable")
        content, protocol = runner.make_input(fixture, 2001, 1000)
        again, _ = runner.make_input(fixture, 2001, 1000)
        different, _ = runner.make_input(fixture, 2010, 1000)
        self.assertEqual(content, again)
        self.assertNotEqual(content, different)
        self.assertEqual(protocol["production_seed"], 2003)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as changed, tarfile.open(fixture / "input.tar.gz", "r:gz") as original:
            for name in ("system.gro", "system.top", "topology-audit.json", "master-manifest.json"):
                self.assertEqual(changed.extractfile(name).read(), original.extractfile(name).read())
            production = changed.extractfile("production.mdp").read().decode()
            self.assertIn("nsteps = 500000", production)
            self.assertIn("gen-seed = 2003", production)
            self.assertIn("nstxout-compressed = 5000", production)
            self.assertEqual(len(changed.getmembers()), 10)

    def test_energy_parser_uses_explicit_legends_and_rejects_missing_density_or_nan(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "energy.xvg"
            columns = ("Potential", "Total Energy", "Temperature", "Pressure")
            legends = "\n".join(f'@ s{i} legend "{name}"' for i, name in enumerate(columns)) + "\n"
            path.write_text(legends + "0 -1 -0.5 300 1\n1 -1 -0.5 301 2\n")
            self.assertEqual(len(summary.xvg(path, density=False)), 2)
            with self.assertRaises(ValueError):
                summary.xvg(path, density=True)
            path.write_text(legends + "0 -1 -0.5 nan 1\n")
            with self.assertRaises(ValueError):
                summary.xvg(path, density=False)


if __name__ == "__main__":
    unittest.main()
