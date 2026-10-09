"""CPU-only negative gates; the actual device-buffer probe is a separate gate."""

import importlib.util
from pathlib import Path
import unittest


RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
SPEC = importlib.util.spec_from_file_location(
    "verify_mpi_cuda_build", RUNTIME / "cuda-aware/verify_build.py"
)
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class BuildCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.evidence = [
            "#define OPAL_CUDA_SUPPORT 1\n",
            "mca:opal:base:param:opal_built_with_cuda_support:value:true\n"
            "mca:accelerator:cuda:version:mca:2.1.0\n",
            "#define MPIX_CUDA_AWARE_SUPPORT 1\n",
            "MPI_SUPPORTS_CUDA_AWARE_DETECTION:INTERNAL=1\n",
            "                 U MPIX_Query_cuda_support\n",
            "00000000000123 D mca_accelerator_cuda_component\n",
        ]

    def test_positive_build_evidence(self):
        VERIFY.check_capability(*self.evidence)

    def test_original_silent_cuda_disable_is_rejected(self):
        self.evidence[0] = "#define OPAL_CUDA_SUPPORT 0\n"
        with self.assertRaisesRegex(ValueError, "configured CUDA"):
            VERIFY.check_capability(*self.evidence)

    def test_ucx_cuda_or_extension_name_is_not_mpi_support(self):
        self.evidence[1] = (
            "MPI extensions: cuda\nTransport: cuda_copy\nTransport: cuda_ipc\n"
        )
        with self.assertRaisesRegex(ValueError, "compiled capability"):
            VERIFY.check_capability(*self.evidence)

    def test_each_missing_assertion_fails_closed(self):
        for index in range(len(self.evidence)):
            with self.subTest(index=index):
                evidence = self.evidence.copy()
                evidence[index] = ""
                with self.assertRaises(ValueError):
                    VERIFY.check_capability(*evidence)

    def test_disabled_extension_macro_is_rejected(self):
        self.evidence[2] = "#define MPIX_CUDA_AWARE_SUPPORT 0\n"
        with self.assertRaisesRegex(ValueError, "extension CUDA"):
            VERIFY.check_capability(*self.evidence)

    def test_cuda_accelerator_component_is_required(self):
        self.evidence[5] = ""
        with self.assertRaisesRegex(ValueError, "component"):
            VERIFY.check_capability(*self.evidence)

    def test_cpu_builder_need_not_load_driver_dependent_component(self):
        self.evidence[1] = self.evidence[1].splitlines()[0] + "\n"
        VERIFY.check_capability(*self.evidence)


class RecipeTests(unittest.TestCase):
    def test_pins_and_explicit_single_cuda_library_directory(self):
        recipe = (RUNTIME / "Containerfile.mpi-cuda-aware").read_text()
        self.assertIn(
            "--with-cuda-libdir=/usr/local/cuda/targets/x86_64-linux/lib/stubs", recipe
        )
        self.assertIn(
            "7e531c7b97c8dc7c0aea115396ff2f63f04acf9f0ae00593fa0a9a5ed9369945", recipe
        )
        self.assertIn(
            "c6c353e55deade8c8fe7a2f02e68bcdb639c7680c8435b665e72815d0213c9ea", recipe
        )
        self.assertIn(
            "f891ddf2dab3b604f2521d0569615bc1c07a1ac86a295ac543e059aecb303621", recipe
        )
        self.assertIn("'#define OPAL_CUDA_SUPPORT 1'", recipe)
        self.assertIn("opal_built_with_cuda_support:value:true$", recipe)
        self.assertNotIn("GMX_FORCE_GPU_AWARE_MPI=", recipe)
        self.assertNotIn("LD_LIBRARY_PATH=", recipe)

    def test_final_worker_overlay_does_not_replace_gromacs_or_ucx(self):
        candidate = (
            (RUNTIME / "Containerfile.mpi-cuda-aware")
            .read_text()
            .split(" AS candidate\n", 1)[1]
        )
        self.assertIn("COPY --from=mpi_build /opt/ompi /opt/ompi", candidate)
        self.assertNotIn("COPY fs2_gromacs", candidate)
        self.assertNotIn("/opt/ucx", candidate)
        self.assertNotIn("/opt/gromacs-mpi", candidate)
        self.assertTrue(candidate.rstrip().endswith("USER 10001:10001"))

    def test_probe_queries_after_init_and_passes_real_device_buffers(self):
        source = (RUNTIME / "cuda-aware/mpi_device_probe.c").read_text()
        body = source.split("int main(", 1)[1]
        self.assertLess(
            body.index("MPI_Init("), body.index("MPIX_Query_cuda_support()")
        )
        self.assertIn("query != 1", body)
        self.assertIn("cudaMalloc((void **)&send_device", body)
        self.assertIn("cudaMalloc((void **)&recv_device", body)
        self.assertIn("MPI_Sendrecv(send_device", body)
        self.assertIn("MPI_Isend(send_device", body)
        self.assertIn("MPI_Irecv(recv_device", body)
        self.assertIn("MPI_Allreduce(send_device, recv_device", body)
        self.assertIn("two ranks mapped to the same physical GPU", body)
        self.assertIn('getenv("GMX_FORCE_GPU_AWARE_MPI")', body)


if __name__ == "__main__":
    unittest.main()
