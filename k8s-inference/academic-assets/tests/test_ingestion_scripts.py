"""End-to-end tests for the supported operator entrypoints.

Unit tests over the status projection cannot catch drift between the shell
entrypoints and the contract they are supposed to drive, so these run the real
scripts. The cluster-touching staging step stays opt-in and is not exercised
here; its delivery-contract validation is checked directly instead.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from test_academic_assets import AcademicAssetTestCase  # noqa: E402

ASSET_ROOT = Path(__file__).resolve().parents[1]
INGEST = ASSET_ROOT / "scripts" / "ingest-approved-assets.sh"
STAGE = ASSET_ROOT / "scripts" / "stage-private-cache.sh"


class IngestEntrypointTests(AcademicAssetTestCase):
    def run_ingest(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.update(
            {
                "FS2_ACADEMIC_ASSET_STATE_DIR": str(self.state_dir),
                "FS2_ACADEMIC_GENERATION": "script-e2e",
                "FS2_AF3_FILE": str(self.af3_path),
                "FS2_PYROSETTA_WHEEL_FILE": str(self.wheel_path),
            }
        )
        # Run the real entrypoint, pointed at the tiny structurally-real fixture
        # contract so the test is hermetic without forking the script. The
        # authorizations must match that contract's digests, which is itself the
        # binding the validator enforces.
        environment["FS2_ACADEMIC_CONTRACT"] = str(self.contract_path)
        environment["FS2_AF3_AUTHORIZATION"] = str(self.authorization("alphafold3"))
        environment["FS2_PYROSETTA_AUTHORIZATION"] = str(self.authorization("pyrosetta-bindcraft"))
        environment.update(overrides)
        return subprocess.run(
            ["bash", str(INGEST)], env=environment, capture_output=True, text=True, check=False
        )

    def test_entrypoint_reproduces_the_authorized_poc_from_scratch(self) -> None:
        result = self.run_ingest()
        self.assertEqual(0, result.returncode, result.stderr)
        projection = json.loads(result.stdout)
        self.assert_schema_valid(projection)
        self.assertEqual("script-e2e", projection["generation"])
        for item in projection["assets"]:
            with self.subTest(asset=item["asset_id"]):
                # Authorization comes from the committed receipts by default.
                self.assertEqual("Granted", item["use_authorization_status"])
                self.assertEqual("Authorized", item["execution_authorization_status"])
                self.assertEqual("ArtifactVerified", item["artifact_status"])
                self.assertEqual("MissingTenantCache", item["state"])
                # The formal axis stays untouched by the operational entrypoint.
                self.assertEqual("FormalAcceptancePending", item["formal_license_status"])
        self.assertEqual("Pending", projection["formal_license_state"])

    def test_entrypoint_requires_the_exact_pinned_wheel(self) -> None:
        """The old conda archive must not be accepted by the wheel entrypoint."""

        wrong = self.sources / "pyrosetta-2025.24+release.8e1e5e54f0-py310_0.conda"
        wrong.write_bytes(b"PK\x03\x04not-a-wheel")
        result = self.run_ingest(FS2_PYROSETTA_WHEEL_FILE=str(wrong))
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("ArtifactVerified", result.stdout)

    def test_entrypoint_fails_closed_without_required_environment(self) -> None:
        environment = dict(os.environ)
        for key in ("FS2_ACADEMIC_ASSET_STATE_DIR", "FS2_AF3_FILE", "FS2_PYROSETTA_WHEEL_FILE"):
            environment.pop(key, None)
        environment["FS2_ACADEMIC_GENERATION"] = "unset-env"
        result = subprocess.run(
            ["bash", str(INGEST)], env=environment, capture_output=True, text=True, check=False
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("FS2_ACADEMIC_ASSET_STATE_DIR", result.stderr)

    def test_entrypoint_records_optional_formal_acceptance_on_its_own_axis(self) -> None:
        acceptance = self.acceptance("alphafold3")
        result = self.run_ingest(FS2_AF3_ACCEPTANCE=str(acceptance))
        self.assertEqual(0, result.returncode, result.stderr)
        projection = json.loads(result.stdout)
        self.assert_schema_valid(projection)
        alphafold3 = self.asset(projection, "alphafold3")
        pyrosetta = self.asset(projection, "pyrosetta-bindcraft")
        self.assertEqual("FormalAcceptanceRecorded", alphafold3["formal_license_status"])
        self.assertEqual("FormalAcceptancePending", pyrosetta["formal_license_status"])
        # One recorded acceptance must not flip the whole platform axis.
        self.assertEqual("Pending", projection["formal_license_state"])

    def test_entrypoint_refuses_an_authorization_for_a_different_artifact(self) -> None:
        """An authorization is bound to the exact artifact and licence digests it names."""

        mismatched = self.authorization(
            "alphafold3",
            artifact_reference={
                "filename": "af3.bin.zst",
                "version": "Google object generation 1780568696389861",
                "sha256": "a" * 64,
                "source_url": "https://storage.googleapis.com/alphafold3/af3.bin.zst",
            },
        )
        result = self.run_ingest(FS2_AF3_AUTHORIZATION=str(mismatched))
        self.assertNotEqual(0, result.returncode)

    def test_entrypoint_help_documents_the_supported_contract(self) -> None:
        result = subprocess.run(
            ["bash", str(INGEST), "--help"], capture_output=True, text=True, check=False
        )
        self.assertEqual(0, result.returncode)
        for expected in (
            "FS2_PYROSETTA_WHEEL_FILE",
            "FS2_AF3_AUTHORIZATION",
            "FS2_AF3_ACCEPTANCE",
        ):
            self.assertIn(expected, result.stdout)
        self.assertNotIn("conda", result.stdout.lower())


class VolumeBootstrapAndInstallTests(unittest.TestCase):
    """Executed against real directories, including an unwritable fresh root."""

    def setUp(self) -> None:
        import install_tree

        self.install_tree = install_tree
        self.workspace = Path(tempfile.mkdtemp(prefix="academic-install-test-"))
        self.addCleanup(self._cleanup)
        self.root = self.workspace / "volume-root"
        self.root.mkdir()
        self.gid = os.getgid()

    def _cleanup(self) -> None:
        for path in sorted(self.workspace.rglob("*"), reverse=True):
            try:
                path.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(self.workspace, ignore_errors=True)

    def build_wheel(self, name: str = "fs2demo", version: str = "1.2.3") -> Path:
        import zipfile

        wheel = self.workspace / f"{name}-{version}-py3-none-any.whl"
        dist_info = f"{name}-{version}.dist-info"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(f"{name}/__init__.py", "VALUE = 42\n")
            archive.writestr(
                f"{dist_info}/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\nbody\n",
            )
            archive.writestr(
                f"{dist_info}/WHEEL",
                "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr(f"{dist_info}/RECORD", f"{dist_info}/METADATA,,\n")
        return wheel

    def test_fresh_unwritable_root_blocks_the_installer_until_bootstrapped(self) -> None:
        """A freshly provisioned claim root is not writable by the non-root installer."""

        wheel = self.build_wheel()
        self.root.chmod(0o555)
        destination = self.root / "site-packages"
        with self.assertRaises(PermissionError):
            (self.root / "probe").mkdir()

        self.root.chmod(0o755)
        report = self.install_tree.prepare_volume_root(self.root, gid=self.gid)
        self.assertEqual("prepared-empty-root", report["action"])
        self.assertTrue(report["group_writable"])
        self.assertFalse(int(report["mode"], 8) & 0o007)

        result = self.install_tree.install_wheel(
            wheel, destination, file_mode="0440", directory_mode="0550", gid=self.gid
        )
        self.assertTrue(result["atomic_promotion"])
        self.assertGreater(result["file_count"], 0)
        self.assertTrue((destination / "fs2demo" / "__init__.py").is_file())

    def test_bootstrap_tightens_an_over_permissive_populated_root(self) -> None:
        """Removing access from a populated root is safe; adding it never happens."""

        existing = self.root / "alphafold3"
        existing.mkdir()
        payload = existing / "af3.bin.zst"
        payload.write_bytes(b"licensed")
        payload.chmod(0o440)
        existing.chmod(0o750)
        self.root.chmod(0o2775)

        report = self.install_tree.prepare_volume_root(self.root, gid=self.gid, mode="2770")
        self.assertEqual("tightened-existing-root", report["action"])
        self.assertFalse(report["world_accessible"])
        self.assertEqual(0o440, stat.S_IMODE(payload.lstat().st_mode))
        self.assertEqual(0o750, stat.S_IMODE(existing.lstat().st_mode))

    def test_bootstrap_never_touches_a_populated_root(self) -> None:
        existing = self.root / "alphafold3"
        existing.mkdir()
        payload = existing / "af3.bin.zst"
        payload.write_bytes(b"licensed")
        payload.chmod(0o440)
        existing.chmod(0o550)

        self.root.chmod(0o2770)
        report = self.install_tree.prepare_volume_root(self.root, gid=self.gid, mode="2770")
        self.assertEqual("verified-existing", report["action"])
        self.assertEqual(1, report["entries"])
        # The already-staged asset keeps its restrictive modes.
        self.assertEqual(0o440, stat.S_IMODE(payload.lstat().st_mode))
        self.assertEqual(0o550, stat.S_IMODE(existing.lstat().st_mode))

    def test_installed_tree_is_group_readable_and_never_world_readable(self) -> None:
        wheel = self.build_wheel()
        destination = self.root / "site-packages"
        self.install_tree.prepare_volume_root(self.root, gid=self.gid)
        self.install_tree.install_wheel(
            wheel, destination, file_mode="0440", directory_mode="0550", gid=self.gid
        )
        for path in destination.rglob("*"):
            mode = stat.S_IMODE(path.lstat().st_mode)
            with self.subTest(path=path.name):
                self.assertFalse(mode & 0o007, "world access")
                self.assertFalse(mode & 0o022, "writable")
                self.assertTrue(mode & 0o040, "group readable")

    def test_verification_imports_from_the_tree_and_pins_the_version(self) -> None:
        wheel = self.build_wheel()
        destination = self.root / "site-packages"
        self.install_tree.prepare_volume_root(self.root, gid=self.gid)
        self.install_tree.install_wheel(
            wheel, destination, file_mode="0440", directory_mode="0550", gid=self.gid
        )
        report = self.install_tree.verify_installed_tree(
            destination,
            distribution="fs2demo",
            version="1.2.3",
            file_mode="0440",
            directory_mode="0550",
            gid=self.gid,
            functional_proof=False,
        )
        self.assertTrue(report["import_verified"])
        self.assertFalse(report["world_readable"])
        self.assertEqual("1.2.3", report["installed_distribution_version"])
        self.assertEqual(64, len(report["evidence_digest"]))

        with self.assertRaises(self.install_tree.InstallError):
            self.install_tree.verify_installed_tree(
                destination,
                distribution="fs2demo",
                version="9.9.9",
                file_mode="0440",
                directory_mode="0550",
                gid=self.gid,
                functional_proof=False,
            )

    def test_reinstall_is_atomic_and_leaves_no_staging_directory(self) -> None:
        destination = self.root / "site-packages"
        self.install_tree.prepare_volume_root(self.root, gid=self.gid)
        self.install_tree.install_wheel(
            self.build_wheel(version="1.2.3"), destination, file_mode="0440", directory_mode="0550", gid=self.gid
        )
        self.install_tree.install_wheel(
            self.build_wheel(version="2.0.0"), destination, file_mode="0440", directory_mode="0550", gid=self.gid
        )
        report = self.install_tree.verify_installed_tree(
            destination,
            distribution="fs2demo",
            version="2.0.0",
            file_mode="0440",
            directory_mode="0550",
            gid=self.gid,
            functional_proof=False,
        )
        self.assertEqual("2.0.0", report["installed_distribution_version"])
        leftovers = [p.name for p in self.root.iterdir() if p.name.startswith(".site-packages")]
        self.assertEqual([], leftovers)

    def test_unsafe_modes_and_root_group_are_refused(self) -> None:
        wheel = self.build_wheel()
        destination = self.root / "site-packages"
        self.install_tree.prepare_volume_root(self.root, gid=self.gid)
        for file_mode, directory_mode in (("0444", "0550"), ("0460", "0550"), ("0440", "0540")):
            with self.subTest(file_mode=file_mode, directory_mode=directory_mode):
                with self.assertRaises(self.install_tree.InstallError):
                    self.install_tree.install_wheel(
                        wheel, destination, file_mode=file_mode, directory_mode=directory_mode, gid=self.gid
                    )
        with self.assertRaises(self.install_tree.InstallError):
            self.install_tree.prepare_volume_root(self.root, gid=0)

    def test_every_script_is_shellcheck_clean(self) -> None:
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:  # pragma: no cover - environment guard
            self.skipTest("shellcheck is unavailable")
        for script in sorted((ASSET_ROOT / "scripts").glob("*.sh")):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [shellcheck, str(script)], capture_output=True, text=True, check=False
                )
                self.assertEqual(0, result.returncode, result.stdout)

    def test_loader_pod_never_sets_fsgroup(self) -> None:
        """kubelet fsGroup management rewrites the tree to 0660 files and 2775 dirs."""

        template = json.loads(
            (ASSET_ROOT / "kubernetes" / "private-cache-loader.template.json").read_text()
        )
        security = template["spec"]["securityContext"]
        self.assertNotIn("fsGroup", security)
        self.assertNotIn("fsGroupChangePolicy", security)
        self.assertIs(True, security["runAsNonRoot"])


class StagingScriptExecutionTests(AcademicAssetTestCase):
    """Runs the real staging script so producer and validator cannot drift apart.

    The cluster is stubbed, not the script: every kubectl call the script makes is
    answered by a fake that reports the modes, group and digest a correct upload
    would produce, and the resulting receipt is recorded through the real
    validator.
    """

    PVC_UID = "4d074d97-3391-46b9-9eea-ae57abc85d06"
    VOLUME = "pvc-4d074d97-3391-46b9-9eea-ae57abc85d06"

    def setUp(self) -> None:
        super().setUp()
        self.bin_dir = self.workspace / "bin"
        self.bin_dir.mkdir()
        self.kubeconfig = self.workspace / "kubeconfig"
        self.kubeconfig.write_text("stub\n")
        self.marker = self.workspace / "uploaded"

    def write_stub_kubectl(self, *, file_mode: str, directory_mode: str, gid: int, sha256: str, size: int) -> None:
        stub = self.bin_dir / "kubectl"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            f"marker = pathlib.Path({str(self.marker)!r})\n"
            "args = sys.argv[1:]\n"
            "joined = ' '.join(args)\n"
            "if 'get pod' in joined:\n"
            "    sys.exit(1)\n"
            "if 'create' in args and '-f' in args:\n"
            "    sys.exit(0)\n"
            "if 'wait' in args:\n"
            "    sys.exit(0)\n"
            "if 'exec' in args:\n"
            "    if '-i' in args:\n"
            "        sys.stdin.buffer.read()\n"
            "        marker.write_text('uploaded')\n"
            "        sys.exit(0)\n"
            "    if not marker.exists():\n"
            "        print(json.dumps({'exists': False})); sys.exit(0)\n"
            "    print(json.dumps({'exists': True,\n"
            f"                      'mode': {file_mode!r},\n"
            f"                      'gid': {gid},\n"
            f"                      'parent_mode': {directory_mode!r},\n"
            f"                      'parent_gid': {gid},\n"
            f"                      'sha256': {sha256!r},\n"
            f"                      'size_bytes': {size}}}))\n"
            "    sys.exit(0)\n"
            "if 'get' in args and 'pv' in args:\n"
            f"    print({self.VOLUME!r}); sys.exit(0)\n"
            "if 'get' in args and 'pvc' in args:\n"
            "    if 'volumeName' in joined:\n"
            f"        print({self.VOLUME!r})\n"
            "    elif 'filesystem-id' in joined:\n"
            "        print('')\n"
            "    else:\n"
            f"        print({self.PVC_UID!r})\n"
            "    sys.exit(0)\n"
            "sys.exit(0)\n"
        )
        stub.chmod(0o755)

    def run_staging(self, asset_id: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": f"{self.bin_dir}:{environment['PATH']}",
                "FS2_ACADEMIC_CONTRACT": str(self.contract_path),
                "FS2_ACADEMIC_ASSET_STATE_DIR": str(self.state_dir),
                "FS2_ACADEMIC_ASSET_ID": asset_id,
                "FS2_ACADEMIC_KUBECONFIG": str(self.kubeconfig),
                "FS2_ACADEMIC_KUBE_CONTEXT": "stub-context",
                "FS2_ACADEMIC_PROJECT_ID": "project-test",
                "FS2_ACADEMIC_REGION": "eu-north1",
                "FS2_ACADEMIC_CLUSTER_ID": "mk8scluster-test",
            }
        )
        environment.update(overrides)
        return subprocess.run(
            ["bash", str(STAGE)], env=environment, capture_output=True, text=True, check=False
        )

    def test_real_script_produces_a_receipt_the_validator_accepts(self) -> None:
        self.ingest("staging-e2e")
        spec = self.contract_document["assets"]["alphafold3"]
        self.write_stub_kubectl(
            file_mode=spec["delivery"]["file_mode"],
            directory_mode=spec["delivery"]["directory_mode"],
            gid=spec["delivery"]["asset_gid"],
            sha256=spec["artifact"]["sha256"],
            size=spec["artifact"]["size_bytes"],
        )
        result = self.run_staging("alphafold3")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("TenantCacheReady", result.stdout)

        code, projection = self.run_cli("status", "--state-dir", str(self.state_dir))
        self.assertEqual(0, code)
        self.assert_schema_valid(projection)
        alphafold3 = self.asset(projection, "alphafold3")
        self.assertEqual("TenantCacheReady", alphafold3["tenant_cache_status"])

        recorded = json.loads(
            (
                self.state_dir / "generations" / "staging-e2e" / "receipts" / "alphafold3" / "cache.json"
            ).read_text()
        )
        # The producer must emit exactly what the validator requires, including the
        # observed volume identity and the delivery ownership evidence.
        self.assertIsNone(recorded["filesystem_id"])
        self.assertEqual(self.VOLUME, recorded["volume_handle"])
        self.assertEqual(self.PVC_UID, recorded["pvc_uid"])
        self.assertEqual(spec["delivery"]["asset_gid"], recorded["asset_gid"])
        self.assertEqual(spec["delivery"]["file_mode"], recorded["file_mode"])
        self.assertEqual(spec["delivery"]["directory_mode"], recorded["directory_mode"])
        self.assertEqual("project-test", recorded["project_id"])

        from jsonschema import Draft202012Validator

        schema = json.loads((ASSET_ROOT / "schemas" / "stage-receipt.schema.json").read_text())
        self.assertEqual([], [str(e) for e in Draft202012Validator(schema).iter_errors(recorded)])

    def test_real_script_rejects_a_mode_that_differs_from_the_contract(self) -> None:
        self.ingest("staging-bad-mode")
        spec = self.contract_document["assets"]["alphafold3"]
        self.write_stub_kubectl(
            file_mode="0444",
            directory_mode=spec["delivery"]["directory_mode"],
            gid=spec["delivery"]["asset_gid"],
            sha256=spec["artifact"]["sha256"],
            size=spec["artifact"]["size_bytes"],
        )
        result = self.run_staging("alphafold3")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("differs from the delivery contract", result.stderr)

    def test_real_script_rejects_a_wrong_group(self) -> None:
        self.ingest("staging-bad-gid")
        spec = self.contract_document["assets"]["alphafold3"]
        self.write_stub_kubectl(
            file_mode=spec["delivery"]["file_mode"],
            directory_mode=spec["delivery"]["directory_mode"],
            gid=4242,
            sha256=spec["artifact"]["sha256"],
            size=spec["artifact"]["size_bytes"],
        )
        result = self.run_staging("alphafold3")
        self.assertNotEqual(0, result.returncode)

    def test_real_script_requires_observed_deployment_identity(self) -> None:
        self.ingest("staging-no-env")
        result = self.run_staging("alphafold3", FS2_ACADEMIC_PROJECT_ID="")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("FS2_ACADEMIC_PROJECT_ID", result.stderr)


if __name__ == "__main__":
    unittest.main()


class AdoptionBackendTests(unittest.TestCase):
    """Adoption must bind to one exact state before it reads or writes it.

    Reading state before `init` can inspect a stale or empty local state and reach
    the wrong conclusion, and `-backend-config` is only valid on `init`. Both are
    verified by recording every terraform invocation the script makes.
    """

    ADOPT = ASSET_ROOT / "scripts" / "adopt-live-resources.sh"

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="academic-adopt-test-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.calls = self.workspace / "terraform-calls.log"
        self.bin_dir = self.workspace / "bin"
        self.bin_dir.mkdir()
        self.data_dir = self.workspace / "tfdata"
        self.state = self.workspace / "isolated.tfstate"
        self.state.write_text(json.dumps({"version": 4, "resources": []}))
        self._write_stub("terraform", exit_for_state_show=1)
        self._write_stub("kubectl", exit_for_state_show=0)

    def _write_stub(self, name: str, *, exit_for_state_show: int) -> None:
        stub = self.bin_dir / name
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            f"log = pathlib.Path({str(self.calls)!r})\n"
            f"if {name!r} == 'terraform':\n"
            "    with log.open('a') as handle:\n"
            "        handle.write('|'.join(sys.argv[1:]) + ' TF_DATA_DIR=' + os.environ.get('TF_DATA_DIR','') + '\\n')\n"
            "    if 'state' in sys.argv:\n"
            f"        sys.exit({exit_for_state_show})\n"
            "    sys.exit(0)\n"
            "sys.exit(0)\n"
        )
        stub.chmod(0o755)

    def run_adopt(self, *extra: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PATH"] = f"{self.bin_dir}:{environment['PATH']}"
        environment.pop("TF_DATA_DIR", None)
        return subprocess.run(
            ["bash", str(self.ADOPT), "--chdir", str(self.workspace), *extra],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def recorded(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_backend_is_initialised_before_any_state_read(self) -> None:
        result = self.run_adopt(
            "--data-dir", str(self.data_dir),
            "--backend-config", f"path={self.state}",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        calls = self.recorded()
        self.assertTrue(calls, "the script made no terraform calls")
        # Match argv tokens, not substrings: a backend path can contain "tfstate".
        self.assertIn("init", calls[0].split("|"))
        state_calls = [index for index, call in enumerate(calls) if "state" in call.split("|")]
        self.assertTrue(state_calls, "no state inspection happened")
        self.assertLess(0, min(state_calls), "state was read before init")

    def test_backend_config_reaches_init_only(self) -> None:
        self.run_adopt(
            "--data-dir", str(self.data_dir),
            "--backend-config", f"path={self.state}",
            "--var-file", "example.tfvars",
        )
        for call in self.recorded():
            tokens = call.split("|")
            if "init" in tokens:
                self.assertTrue(any(t.startswith("-backend-config=") for t in tokens))
            else:
                # backend-config is not a valid argument to state or import.
                self.assertFalse(any(t.startswith("-backend-config=") for t in tokens))

    def test_var_file_reaches_import_but_never_state_show(self) -> None:
        """`terraform state show` rejects -var-file, so it must never receive it."""

        self._write_stub("terraform", exit_for_state_show=1)
        self.run_adopt(
            "--data-dir", str(self.data_dir),
            "--var-file", "example.tfvars",
            "--state", str(self.state),
            "--apply",
        )
        state_calls = [c for c in self.recorded() if "state" in c.split("|")]
        import_calls = [c for c in self.recorded() if "import" in c.split("|")]
        self.assertTrue(state_calls, "no state inspection happened")
        self.assertTrue(import_calls, "no import happened")
        for call in state_calls:
            tokens = call.split("|")
            self.assertFalse(any(t.startswith("-var-file=") for t in tokens), call)
            self.assertTrue(any(t.startswith("-state=") for t in tokens), call)
        for call in import_calls:
            tokens = call.split("|")
            self.assertTrue(any(t.startswith("-var-file=") for t in tokens), call)
            self.assertTrue(any(t.startswith("-state=") for t in tokens), call)

    def test_every_call_is_bound_to_the_requested_data_directory(self) -> None:
        self.run_adopt("--data-dir", str(self.data_dir))
        calls = self.recorded()
        self.assertTrue(calls)
        for call in calls:
            self.assertIn(f"TF_DATA_DIR={self.data_dir}", call)

    def test_import_addresses_the_selected_lifecycle_resource(self) -> None:
        """Retained and disposable claims are separate resources; import must name the live one."""

        for lifecycle in ("retained", "disposable"):
            with self.subTest(lifecycle=lifecycle):
                self.calls.unlink(missing_ok=True)
                self._write_stub("terraform", exit_for_state_show=1)
                result = self.run_adopt(
                    "--data-dir", str(self.data_dir),
                    "--runtime-lifecycle", lifecycle,
                    "--legacy-lifecycle", lifecycle,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                # printf %q escapes the index brackets; compare on the unescaped text.
                printed = result.stdout.replace("\\", "")
                self.assertIn(
                    f"module.academic_assets.kubernetes_persistent_volume_claim_v1."
                    f"academic_assets_runtime_{lifecycle}[0]",
                    printed,
                )
                other = "disposable" if lifecycle == "retained" else "retained"
                self.assertNotIn(f"academic_assets_runtime_{other}[0]", printed)

    def test_module_prefix_can_be_dropped_when_targeting_the_module_directly(self) -> None:
        self._write_stub("terraform", exit_for_state_show=1)
        result = self.run_adopt("--data-dir", str(self.data_dir), "--module-prefix", "")
        self.assertEqual(0, result.returncode, result.stderr)
        printed = result.stdout.replace("\\", "")
        self.assertIn("kubernetes_persistent_volume_claim_v1.academic_assets_runtime_retained[0]", printed)
        self.assertNotIn("module.academic_assets.", printed)

    def test_an_unknown_lifecycle_is_refused(self) -> None:
        result = self.run_adopt("--data-dir", str(self.data_dir), "--runtime-lifecycle", "permanent")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("retained or disposable", result.stderr)

    def test_backend_config_without_an_explicit_data_dir_is_refused(self) -> None:
        result = self.run_adopt("--backend-config", f"path={self.state}")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("explicit --data-dir", result.stderr)
        self.assertEqual([], self.recorded(), "nothing may run before the binding is settled")

    def test_adoption_is_idempotent_when_state_already_manages_the_address(self) -> None:
        # A state that already knows every address must import nothing.
        self._write_stub("terraform", exit_for_state_show=0)
        result = self.run_adopt("--data-dir", str(self.data_dir), "--apply")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("4 already managed", result.stdout)
        self.assertNotIn("importing", result.stdout)
        self.assertEqual([], [call for call in self.recorded() if "import" in call.split("|")])
