import copy
import unittest

from jsonschema.exceptions import ValidationError

from pme_control import control_request
from pme_control_report import MEM_PARAMETERS, native_parameters


class PMEControlTests(unittest.TestCase):
    def log(self, gpu=True):
        text = "\n".join(f"   {key} = {value}" for key, value in MEM_PARAMETERS.items())
        text += "\nPP tasks will do non-perturbed short-ranged interactions on the GPU\n"
        text += "PP task will update and constrain coordinates on the CPU\n"
        return text + ("PME tasks will do all aspects on the GPU\n" if gpu else "")

    def test_native_cutoff_grid_and_gpu_dispatch_required(self):
        self.assertEqual(native_parameters(self.log(), "gpu")["errors"], [])
        self.assertIn("rcoulomb", native_parameters(self.log().replace("rcoulomb = 1", "rcoulomb = 1.65"), "gpu")["errors"])
        self.assertTrue(native_parameters(self.log(False), "gpu")["errors"])
        self.assertEqual(native_parameters(self.log(False), "cpu")["errors"], [])

    def test_native_tuning_changes_rejected_and_neighbor_adjustment_disclosed(self):
        text = self.log() + "Changing nstlist from 10 to 100, rlist from 1 to 1.133\n"
        observed = native_parameters(text, "gpu")
        self.assertEqual(len(observed["neighbor_list_adjustments"]), 1)
        self.assertEqual(observed["errors"], [])
        self.assertTrue(native_parameters(text + "PP/PME load balancing changed", "gpu")["errors"])

    def request(self):
        return {"schema": "fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1", "nodes": 1,
                "gpus_per_node": 1, "jobs": [{"id": "gang", "steps": [
                    {"id": f"repeat-{i}", "command": "mdrun", "args": ["-s", "same.tpr", "-deffnm", f"repeat{i}",
                     "-nb", "gpu", "-pme", "cpu", "-npme", "0", "-update", "cpu"]} for i in (1, 2, 3)]}]}

    def test_exact_one_rank_gpu_pme_is_not_a_separate_rank(self):
        request = self.request()
        before = copy.deepcopy(request)
        value = control_request(request, 1, "gpu")
        args = value["jobs"][0]["steps"][0]["args"]
        self.assertEqual(args[args.index("-npme") + 1], "0")
        self.assertEqual(args[args.index("-pmefft") + 1], "gpu")
        self.assertEqual(args[args.index("-bonded") + 1], "cpu")
        self.assertIn("-notunepme", args)
        self.assertEqual(request, before)

    def test_multiple_ranks_use_only_one_gpu_pme_rank(self):
        for nodes, gpus in ((1, 2), (1, 4), (1, 8), (2, 8)):
            args = control_request(self.request(), gpus, "gpu", nodes)["jobs"][0]["steps"][0]["args"]
            self.assertEqual(args[args.index("-npme") + 1], "1")
            self.assertEqual(args.count("-pme"), 1)

    def test_cpu_control_keeps_all_ranks_pp_pme(self):
        args = control_request(self.request(), 2, "cpu")["jobs"][0]["steps"][0]["args"]
        self.assertEqual(args[args.index("-npme") + 1], "0")
        self.assertEqual(args[args.index("-pmefft") + 1], "cpu")
        self.assertEqual(args[args.index("-s") + 1], "same.tpr")

    def test_old_reset_and_incomplete_controls_rejected(self):
        request = self.request()
        request["jobs"][0]["steps"][0]["args"].append("-resethway")
        with self.assertRaises(ValueError):
            control_request(request, 1, "gpu")
        request = self.request()
        request["jobs"][0]["steps"].pop()
        with self.assertRaises(ValueError):
            control_request(request, 1, "gpu")

    def test_unsupported_shape_and_mode_rejected(self):
        with self.assertRaises(ValidationError):
            control_request(self.request(), 8, "gpu", 3)
        with self.assertRaises(ValueError):
            control_request(self.request(), 1, "auto")


if __name__ == "__main__":
    unittest.main()
