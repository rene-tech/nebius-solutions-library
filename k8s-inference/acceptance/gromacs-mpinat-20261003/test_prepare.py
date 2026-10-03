import importlib.util
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from prepare import parameters, Links


class PreparationTests(unittest.TestCase):
    def test_immutable_physics_and_repetitions(self):
        request = parameters({"id": "benchmem"})
        steps = request["jobs"][0]["steps"]
        self.assertEqual(steps[0]["command"], "convert-tpr")
        self.assertEqual(steps[0]["args"], ["-s", "original.tpr", "-o", "benchmark.tpr", "-nsteps", "10000"])
        md = [step for step in steps if step["command"] == "mdrun"]
        self.assertEqual(len(md), 3)
        for step in md:
            self.assertNotIn("-resethway", step["args"])
            self.assertNotIn("-resetstep", step["args"])
            self.assertEqual(step["args"][step["args"].index("-update") + 1], "cpu")

    def test_peph_offload_is_not_copied_to_mpi(self):
        single = parameters({"id": "benchpep-h"})["jobs"][0]["steps"][1]["args"]
        mpi = parameters({"id": "benchpep-h"}, mpi_nodes=2)
        self.assertIn("-update", single)
        self.assertNotIn("-update", mpi["jobs"][0]["steps"][1]["args"])
        self.assertEqual(mpi["nodes"], 2)
        self.assertEqual(mpi["jobs"][0]["id"], "gang")

    def test_url_scope(self):
        parser = Links()
        with self.assertRaises(ValueError):
            parser.feed('<a title="benchMEM" href="https://unrelated.invalid/test.zip">')

    def test_all_parameters_match_runtime_contract(self):
        runtime = HERE.parents[1] / "models/molecular-dynamics/gromacs/runtime"
        sys.path.insert(0, str(runtime))
        from fs2_gromacs.contracts import normalize
        for case in ("benchmem", "benchpep-h", "benchsfi"):
            normalize(parameters({"id": case}))
            normalize(parameters({"id": case}, mpi_nodes=2), mpi=True)


if __name__ == "__main__":
    unittest.main()
