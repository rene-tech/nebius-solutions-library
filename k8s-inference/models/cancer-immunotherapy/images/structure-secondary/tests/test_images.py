from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOCK = json.loads((ROOT / "image-lock.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((ROOT / "image-lock.schema.json").read_text(encoding="utf-8"))
CONFIDENCE_SCHEMA = json.loads(
    (ROOT / "confidence.schema.json").read_text(encoding="utf-8")
)


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StructureSecondaryImageContractTests(unittest.TestCase):
    def test_lock_v2_validates_and_has_five_unique_derived_targets(self) -> None:
        jsonschema.Draft202012Validator(SCHEMA).validate(LOCK)
        self.assertEqual(LOCK["schema"], "fs2.nebius.ai/structure-secondary-image-lock/v2")
        images = LOCK["images"]
        self.assertEqual(
            [image["id"] for image in images],
            ["esmfold2", "esmfold2-fast", "protenix-v2", "alphafold3", "openfold3"],
        )
        targets = {
            f'{LOCK["registry_default"]}/{image["repository"]}:{image["tag"]}'
            for image in images
        }
        self.assertEqual(len(targets), 5)
        self.assertTrue(all("target" not in image for image in images))
        for image in images:
            contract = image["runtime_contract"]
            self.assertTrue(contract["required_mounts"])
            self.assertTrue(contract["commands"])
            self.assertTrue(
                all(command.startswith("/") for command in contract["commands"])
            )

    def test_sources_tags_and_base_images_are_immutable(self) -> None:
        expected = {
            "esmfold2": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            "esmfold2-fast": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            "protenix-v2": "2475421477ab414b571149ad4a875c390ff8a35d",
            "alphafold3": "85c4d20505fd5cef05eac22b534d4e793971ae69",
            "openfold3": "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86",
        }
        for image in LOCK["images"]:
            with self.subTest(image=image["id"]):
                revision = expected[image["id"]]
                self.assertEqual(image["source"]["revision"], revision)
                self.assertEqual(image["tag"], f"{revision}-h100-r2")
                self.assertRegex(image["source"]["tag"], r"^v[0-9]")
                for base in image["base_images"]:
                    self.assertRegex(base, r"@sha256:[0-9a-f]{64}$")

    def test_esm_variants_share_code_but_not_model_identity(self) -> None:
        images = {image["id"]: image for image in LOCK["images"]}
        full, fast = images["esmfold2"], images["esmfold2-fast"]
        self.assertEqual(full["source"], fast["source"])
        self.assertNotEqual(full["repository"], fast["repository"])
        self.assertNotEqual(
            full["build_args"]["MODEL_REPOSITORY"],
            fast["build_args"]["MODEL_REPOSITORY"],
        )
        full_artifacts = {item["id"]: item for item in full["external_artifacts"]}
        self.assertEqual(
            full_artifacts["esmfold2-trunk"]["content_digest"],
            "136a3580c01cc055ae5a1278bae056e5150a5441ddb89dfbafb9f4e88d763a0c",
        )
        for image in (full, fast):
            artifacts = {item["id"]: item for item in image["external_artifacts"]}
            self.assertIn("esmc-6b", artifacts)
            self.assertIn("esmfold2-ccd", artifacts)
            self.assertEqual(
                artifacts["esmc-6b"]["revision"],
                "45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a",
            )
            self.assertEqual(
                artifacts["esmfold2-ccd"]["sha256"],
                "9ff44b1927c6b9198e38ffe0928706827a09a350c15530beeeabebfa88038fc5",
            )
            expected_trunk = f'/models/{image["id"]}'
            trunk = next(
                artifact for artifact in artifacts.values() if artifact["id"].endswith("trunk")
            )
            self.assertEqual(trunk["mount"], expected_trunk)
            self.assertEqual(artifacts["esmc-6b"]["mount"], "/models/esmc-6b")
            self.assertEqual(artifacts["esmfold2-ccd"]["mount"], "/databases/esmfold2/ccd.pkl")

    def test_protenix_artifact_closure_and_architecture_truth(self) -> None:
        image = next(item for item in LOCK["images"] if item["id"] == "protenix-v2")
        self.assertEqual(len(image["external_artifacts"]), 1)
        artifact = image["external_artifacts"][0]
        self.assertEqual(artifact["id"], "protenix-v2")
        self.assertEqual(artifact["mount"], "/models/protenix-v2")
        self.assertEqual(artifact["checkpoint"]["bytes"], 1_859_785_497)
        self.assertEqual(
            artifact["ready_marker"],
            "/models/protenix-v2/.fs2-manifest-sha256",
        )
        self.assertEqual(
            artifact["expected_paths"],
            [
                "checkpoint/protenix-v2.pt",
                "common/components.cif",
                "common/components.cif.rdkit_mol.pkl",
                "common/clusters-by-entity-40.txt",
                "common/obsolete_release_date.csv",
                "manifest.json",
                ".fs2-manifest-sha256",
            ],
        )
        self.assertEqual(image["accelerator_support"]["blackwell"]["status"], "unsupported")
        self.assertIn("libtorch", image["accelerator_support"]["blackwell"]["scope"])
        self.assertIn("pending-exact-checkpoint", image["accelerator_support"]["h100"]["status"])
        self.assertEqual(image["runtime_contract"]["required_mounts"], ["/models/protenix-v2"])
        self.assertEqual(image["runtime_contract"]["writable_cache_mounts"], ["/cache/protenix"])
        self.assertIn("triton-jit", image["runtime_contract"]["runtime_compilation"])

    def test_protenix_composite_manifest_binds_entire_tree_without_runtime_rehash(self) -> None:
        module = load_module("run_protenix")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "checkpoint" / "protenix-v2.pt"
            checkpoint.parent.mkdir()
            with checkpoint.open("wb") as stream:
                stream.truncate(module.CHECKPOINT_BYTES)
            common = root / "common"
            common.mkdir()
            for index, relative in enumerate(module.COMMON_REQUIRED_PATHS, start=1):
                (root / relative).write_bytes(f"fixture-{index}".encode())
            files = []
            for relative in module.ARTIFACT_REQUIRED_PATHS:
                localized = root / relative
                files.append(
                    {
                        "path": relative.as_posix(),
                        "bytes": localized.stat().st_size,
                        "sha256": (
                            module.CHECKPOINT_SHA256
                            if relative == Path("checkpoint/protenix-v2.pt")
                            else hashlib.sha256(localized.read_bytes()).hexdigest()
                        ),
                    }
                )
            manifest = {
                "schema": "fs2.nebius.ai/protenix-v2-composite-artifact/v1",
                "artifact_id": module.ARTIFACT_ID,
                "revision": module.ARTIFACT_REVISION,
                "sources": {
                    "code": {"revision": module.CODE_REVISION},
                    "checkpoint": {
                        "revision": module.CHECKPOINT_REVISION,
                        "bytes": module.CHECKPOINT_BYTES,
                        "sha256": module.CHECKPOINT_SHA256,
                        "md5": module.CHECKPOINT_MD5,
                        "parameter_count": module.CHECKPOINT_PARAMETER_COUNT,
                        "verification": "third-party-mirror-verified-not-publisher-byte-compared",
                    },
                    "common": {
                        "revision": module.COMMON_DATA_REVISION,
                        "source_sha256": module.COMMON_DATA_SOURCE_SHA256,
                    },
                },
                "files": files,
            }
            manifest_path = root / "manifest.json"
            ready_path = root / ".fs2-manifest-sha256"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            ready_path.write_text(canonical_sha256(manifest) + "\n", encoding="utf-8")
            with (
                mock.patch.object(module, "PROTENIX_ROOT", root),
                mock.patch.object(module, "CHECKPOINT", checkpoint),
                mock.patch.object(module, "ARTIFACT_MANIFEST", manifest_path),
                mock.patch.object(module, "ARTIFACT_READY", ready_path),
                mock.patch.object(module, "_sha256", side_effect=AssertionError("large rehash")),
            ):
                self.assertEqual(module._validate_artifact(), canonical_sha256(manifest))
                (root / module.COMMON_REQUIRED_PATHS[-1]).unlink()
                with self.assertRaisesRegex(SystemExit, "obsolete_release_date"):
                    module._validate_artifact()

    def test_generated_protenix_argv_is_fixed_and_canonical(self) -> None:
        module = load_module("run_protenix")
        argv = module.build_pred_command(
            Path("/work/enriched.json"),
            Path("/work/results"),
            seeds="101",
            cycle=10,
            step=200,
            sample=5,
            msa_mode="none",
        )
        self.assertEqual(argv[:2], ["/opt/protenix-venv/bin/protenix", "pred"])
        joined = " ".join(argv)
        self.assertIn("--model_name protenix-v2", joined)
        self.assertIn("--use_msa false", joined)
        self.assertIn("--use_template false", joined)
        precomputed = " ".join(
            module.build_pred_command(
                Path("/work/enriched.json"),
                Path("/work/results"),
                seeds="101",
                cycle=10,
                step=200,
                sample=5,
                msa_mode="precomputed",
            )
        )
        self.assertIn("--use_msa true", precomputed)
        self.assertIn("--use_template false", precomputed)
        self.assertIn("--use_rna_msa false", precomputed)
        self.assertNotIn("--checkpoint", joined)
        source = (ROOT / "run_protenix.py").read_text(encoding="utf-8")
        self.assertNotIn("argparse.REMAINDER", source)
        self.assertNotIn("_sha256(checkpoint)", source)
        self.assertNotIn("os.execve", source)
        self.assertNotIn("REFERENCE_BUNDLE_ID", source)
        self.assertNotIn("reference_manifest_sha256", source)
        self.assertNotIn("--reference-manifest", source)
        self.assertIn("write_confidence_envelope", source)

    def test_protenix_confidence_is_deterministic_and_path_relative(self) -> None:
        module = load_module("run_protenix")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for seed in (101, 202):
                for sample in (0, 1):
                    directory = output / f"seed_{seed}"
                    directory.mkdir(parents=True, exist_ok=True)
                    prefix = f"x_seed_{seed}"
                    (directory / f"{prefix}_sample_{sample}.cif").write_text(
                        f"data_seed_{seed}_sample_{sample}\n", encoding="utf-8"
                    )
                    (directory / f"{prefix}_summary_confidence_sample_{sample}.json").write_text(
                        json.dumps(
                            {
                                "plddt": 90.0 - sample,
                                "ptm": 0.8,
                                "iptm": 0.7,
                                "ranking_score": 0.75,
                                "unbounded_per_atom": [1.0] * 100,
                            }
                        ),
                        encoding="utf-8",
                    )
            confidence = module._write_confidence(
                output, seeds=[101, 202], samples_per_seed=2
            )
            jsonschema.Draft202012Validator(CONFIDENCE_SCHEMA).validate(confidence)
            self.assertEqual(
                [result["structure"]["filename"] for result in confidence["results"]],
                [
                    "seed_101/x_seed_101_sample_0.cif",
                    "seed_101/x_seed_101_sample_1.cif",
                    "seed_202/x_seed_202_sample_0.cif",
                    "seed_202/x_seed_202_sample_1.cif",
                ],
            )
            encoded = (output / "confidence.json").read_text(encoding="utf-8")
            self.assertNotIn(str(output), encoded)
            self.assertNotIn("unbounded_per_atom", encoded)
            self.assertEqual(json.loads(encoded), confidence)
            for result in confidence["results"]:
                structure = output / result["structure"]["filename"]
                self.assertEqual(result["structure"]["bytes"], structure.stat().st_size)
                self.assertEqual(
                    result["structure"]["sha256"], hashlib.sha256(structure.read_bytes()).hexdigest()
                )
            bad = output / "seed_101" / "x_seed_101_summary_confidence_sample_0.json"
            bad.write_text(
                json.dumps({"plddt": float("nan"), "ptm": 0.8, "iptm": 0.7, "ranking_score": 0.75}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "finite scalar"):
                module._write_confidence(output, seeds=[101, 202], samples_per_seed=2)

    def test_protenix_pred_executes_generated_argv_and_collects_confidence(self) -> None:
        module = load_module("run_protenix")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "enriched.json"
            input_path.write_text("{}", encoding="utf-8")
            fake_cli = root / "protenix"
            fake_cli.write_text(
                """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
out = Path(sys.argv[sys.argv.index('--out_dir') + 1])
(out / 'argv.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')
(out / 'cache-env.json').write_text(json.dumps({
    'triton': os.environ['TRITON_CACHE_DIR'],
    'cueq': os.environ['CUEQ_TRITON_CACHE_DIR'],
}), encoding='utf-8')
seeds = [int(value) for value in sys.argv[sys.argv.index('--seeds') + 1].split(',')]
samples = int(sys.argv[sys.argv.index('--sample') + 1])
for seed in seeds:
    directory = out / f'seed_{seed}'
    directory.mkdir(parents=True, exist_ok=True)
    for sample in range(samples):
        prefix = f'fixture_seed_{seed}'
        (directory / f'{prefix}_sample_{sample}.cif').write_text('data_fixture\\n')
        (directory / f'{prefix}_summary_confidence_sample_{sample}.json').write_text(
            json.dumps({'plddt': 91.0, 'ptm': 0.8, 'iptm': 0.7, 'ranking_score': 0.75}),
            encoding='utf-8',
        )
""",
                encoding="utf-8",
            )
            fake_cli.chmod(0o755)
            output = root / "results"
            triton_cache = root / "cache" / "triton"
            cueq_cache = root / "cache" / "cueq-triton"
            triton_cache.mkdir(parents=True)
            cueq_cache.mkdir(parents=True)
            args = types.SimpleNamespace(
                input=str(input_path),
                input_marker=None,
                output_dir=str(output),
                msa_mode="none",
                seeds="101,202",
                cycle=10,
                step=200,
                sample=2,
                print_command=False,
            )
            fake_torch = types.SimpleNamespace(
                cuda=types.SimpleNamespace(
                    is_available=lambda: True,
                    get_device_capability=lambda _index: (9, 0),
                )
            )
            with (
                mock.patch.object(module, "PROTENIX_CLI", str(fake_cli)),
                mock.patch.object(module, "TRITON_CACHE", triton_cache),
                mock.patch.object(module, "CUEQ_TRITON_CACHE", cueq_cache),
                mock.patch.object(module, "_validate_artifact", return_value="a" * 64),
                mock.patch.object(
                    module,
                    "_validate_preprocessed_input",
                    return_value={"msa_mode": "none"},
                ),
                mock.patch.object(module, "_validate_installed_runtime"),
                mock.patch.dict(sys.modules, {"torch": fake_torch}),
            ):
                with redirect_stdout(io.StringIO()):
                    module._pred(args)
            argv = json.loads((output / "argv.json").read_text(encoding="utf-8"))
            self.assertEqual(argv[:1], ["pred"])
            self.assertEqual(argv[argv.index("--model_name") + 1], "protenix-v2")
            self.assertEqual(argv[argv.index("--use_msa") + 1], "false")
            self.assertEqual(argv[argv.index("--use_template") + 1], "false")
            self.assertEqual(argv[argv.index("--use_rna_msa") + 1], "false")
            cache_env = json.loads((output / "cache-env.json").read_text())
            self.assertEqual(cache_env, {"triton": str(triton_cache), "cueq": str(cueq_cache)})
            envelope = json.loads((output / "confidence.json").read_text())
            jsonschema.Draft202012Validator(CONFIDENCE_SCHEMA).validate(envelope)
            self.assertEqual(envelope["seeds"], [101, 202])
            self.assertEqual(len(envelope["results"]), 4)

    def test_generated_alphafold3_argv_separates_cpu_and_gpu_stages(self) -> None:
        wrapper = ROOT / "run_alphafold3.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir, db_dir, output = root / "models", root / "databases", root / "out"
            model_dir.mkdir()
            db_dir.mkdir()
            request = root / "input.json"
            request.write_text('{"name":"fixture","modelSeeds":[999]}', encoding="utf-8")
            processed = root / "handoff" / "processed.json"
            data = subprocess.run(
                [
                    sys.executable,
                    str(wrapper),
                    "data",
                    "--input-json",
                    str(request),
                    "--output-dir",
                    str(output),
                    "--processed-json-output",
                    str(processed),
                    "--seeds",
                    "101,202",
                    "--model-dir",
                    str(model_dir),
                    "--db-dir",
                    str(db_dir),
                    "--print-command",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            data_argv = json.loads(data.stdout)["argv"]
            self.assertIn("--run_data_pipeline", data_argv)
            self.assertIn("--norun_inference", data_argv)
            self.assertIn("--force_output_dir", data_argv)
            staged_input = Path(
                next(value.split("=", 1)[1] for value in data_argv if value.startswith("--json_path="))
            )
            self.assertEqual(
                json.loads(staged_input.read_text(encoding="utf-8"))["modelSeeds"],
                [101, 202],
            )
            with (model_dir / "af3.bin.zst").open("wb") as stream:
                stream.truncate(1_020_545_840)
            processed.parent.mkdir(parents=True, exist_ok=True)
            processed.write_text(
                '{"name":"fixture","modelSeeds":[101,202]}', encoding="utf-8"
            )
            marker = {
                "schema": "fs2.nebius.ai/alphafold3-processed-input/v1",
                "input_json_sha256": "0" * 64,
                "processed_json_sha256": hashlib.sha256(processed.read_bytes()).hexdigest(),
                "model_seeds": [101, 202],
                "database_artifact_id": "alphafold3-public-databases-v3.0",
                "database_revision": "fetch_databases.sh@231efc9bb9c13b45cc59e43f7107869084ee9624",
            }
            Path(f"{processed}.fs2.json").write_text(json.dumps(marker), encoding="utf-8")
            inference = subprocess.run(
                [
                    sys.executable,
                    str(wrapper),
                    "inference",
                    "--processed-json",
                    str(processed),
                    "--output-dir",
                    str(output),
                    "--seeds",
                    "101,202",
                    "--model-dir",
                    str(model_dir),
                    "--db-dir",
                    str(db_dir),
                    "--print-command",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            inference_argv = json.loads(inference.stdout)["argv"]
            self.assertIn("--norun_data_pipeline", inference_argv)
            self.assertIn("--run_inference", inference_argv)
            self.assertIn("--num_diffusion_samples=5", inference_argv)
            self.assertIn(f"--model_dir={model_dir}", inference_argv)
            self.assertIn(f"--db_dir={db_dir}", inference_argv)
            rejected = subprocess.run(
                [sys.executable, str(wrapper), "data", "--json_path=/tmp/override"],
                check=False,
                text=True,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(rejected.returncode, 0)
        self.assertNotIn("argparse.REMAINDER", wrapper.read_text(encoding="utf-8"))
        self.assertNotIn("os.execve", wrapper.read_text(encoding="utf-8"))

    def test_af3_two_seed_two_sample_confidence_binds_structures_and_bounds(self) -> None:
        module = load_module("run_alphafold3")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for seed in (101, 202):
                for sample in (0, 1):
                    directory = output / "job" / f"seed-{seed}_sample-{sample}"
                    directory.mkdir(parents=True)
                    (directory / "result_model.cif").write_text(
                        f"data_af3_{seed}_{sample}\n", encoding="utf-8"
                    )
                    (directory / "summary_confidences.json").write_text(
                        json.dumps(
                            {
                                "ptm": 0.8,
                                "iptm": 0.7,
                                "fraction_disordered": 0.1,
                                "ranking_score": 0.75,
                                "chain_pair_iptm": [[0.7]],
                            }
                        ),
                        encoding="utf-8",
                    )
            envelope = module._write_confidence(
                output, seeds=[101, 202], samples_per_seed=2
            )
            jsonschema.Draft202012Validator(CONFIDENCE_SCHEMA).validate(envelope)
            self.assertEqual(envelope["schema"], "fs2.nebius.ai/structure-confidence/v1")
            self.assertEqual(len(envelope["results"]), 4)
            self.assertNotIn(
                "chain_pair_iptm",
                (output / "confidence.json").read_text(encoding="utf-8"),
            )
            for result in envelope["results"]:
                structure = output / result["structure"]["filename"]
                self.assertEqual(result["structure"]["bytes"], structure.stat().st_size)
                self.assertEqual(result["structure"]["sha256"], hashlib.sha256(structure.read_bytes()).hexdigest())
            overflow = output / "job" / "seed-101_sample-2"
            overflow.mkdir()
            (overflow / "result_model.cif").write_text("data_overflow\n", encoding="utf-8")
            (overflow / "summary_confidences.json").write_text(
                '{"ptm":0.8,"ranking_score":0.75}', encoding="utf-8"
            )
            with self.assertRaisesRegex(SystemExit, "(bounded seed/sample product|valid bounded structure sample)"):
                module._write_confidence(output, seeds=[101, 202], samples_per_seed=2)

    def test_generated_openfold_argv_is_offline_and_public_ccd_api_is_used(self) -> None:
        wrapper = ROOT / "run_openfold3.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            query = root / "query.json"
            query.write_text('{"queries":{"q":{"chains":[]}}}', encoding="utf-8")
            checkpoint = root / "of3.pt"
            with checkpoint.open("wb") as stream:
                stream.truncate(2_287_872_989)
            ccd = root / "components.bcif"
            with ccd.open("wb") as stream:
                stream.truncate(63_393_643)
            runner_yaml = root / "runner.yaml"
            runner_yaml.write_text(
                json.dumps({"experiment_settings": {"seeds": [101, 202]}}),
                encoding="utf-8",
            )
            prepared_marker = root / "prepared-query.fs2.json"
            prepared_marker.write_text(
                json.dumps(
                    {
                        "schema": "fs2.nebius.ai/openfold3-prepared-query/v1",
                        "query_sha256": hashlib.sha256(query.read_bytes()).hexdigest(),
                        "msa_mode": "none",
                        "model_seeds": [101, 202],
                        "runner_yaml_sha256": hashlib.sha256(runner_yaml.read_bytes()).hexdigest(),
                        "ccd_sha256": "473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c",
                        "network_policy": "offline",
                    }
                ),
                encoding="utf-8",
            )
            output = root / "out"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(wrapper),
                    "--query-json",
                    str(query),
                    "--output-dir",
                    str(output),
                    "--checkpoint",
                    str(checkpoint),
                    "--ccd-path",
                    str(ccd),
                    "--runner-yaml",
                    str(runner_yaml),
                    "--prepared-marker",
                    str(prepared_marker),
                    "--seeds",
                    "101,202",
                    "--print-command",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            argv = json.loads(completed.stdout)["argv"]
            self.assertEqual(argv[:2], ["run_openfold", "predict"])
            self.assertIn("--inference-ckpt-path", argv)
            self.assertEqual(argv[argv.index("--use-msa-server") + 1], "false")
            self.assertEqual(argv[argv.index("--use-templates") + 1], "false")
            self.assertEqual(argv[argv.index("--runner-yaml") + 1], str(runner_yaml))
            self.assertNotIn("--num-model-seeds", argv)
        source = wrapper.read_text(encoding="utf-8")
        self.assertIn("ccd.set_ccd_path(ccd_path)", source)
        self.assertNotIn("._CCD_FILE", source)
        self.assertIn("standalone_mode=False", source)

    def test_openfold_two_seed_two_sample_confidence_binds_structures_and_bounds(self) -> None:
        module = load_module("run_openfold3")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for seed in (101, 202):
                directory = output / "query" / f"seed_{seed}"
                directory.mkdir(parents=True)
                for sample in (1, 2):
                    prefix = f"query_seed_{seed}_sample_{sample}"
                    (directory / f"{prefix}_model.cif").write_text(
                        f"data_of3_{seed}_{sample}\n", encoding="utf-8"
                    )
                    (directory / f"{prefix}_confidences_aggregated.json").write_text(
                        json.dumps(
                            {
                                "avg_plddt": 88.0,
                                "gpde": 4.0,
                                "ptm": 0.8,
                                "iptm": 0.7,
                                "sample_ranking_score": 0.75,
                                "per_atom": [1.0] * 100,
                            }
                        ),
                        encoding="utf-8",
                    )
            envelope = module._write_confidence(
                output, seeds=[101, 202], samples_per_seed=2
            )
            jsonschema.Draft202012Validator(CONFIDENCE_SCHEMA).validate(envelope)
            self.assertEqual(envelope["schema"], "fs2.nebius.ai/structure-confidence/v1")
            self.assertEqual(len(envelope["results"]), 4)
            encoded = (output / "confidence.json").read_text(encoding="utf-8")
            self.assertNotIn("per_atom", encoded)
            self.assertNotIn(str(output), encoded)
            bad = output / "query" / "seed_101" / "query_seed_101_sample_1_confidences_aggregated.json"
            bad.write_text(
                json.dumps({"avg_plddt": 88.0, "gpde": float("inf")}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "finite scalar"):
                module._write_confidence(output, seeds=[101, 202], samples_per_seed=2)

    def test_esm_production_defaults_and_non_hopper_flash_disable(self) -> None:
        source = (ROOT / "run_esmfold2.py").read_text(encoding="utf-8")
        self.assertIn('default=20', source)
        self.assertIn('default=200', source)
        self.assertIn('"--smoke"', source)
        self.assertIn("esmfold2_layers.FLASH_ATTN_AVAILABLE = False", source)
        self.assertIn('attention = "flash_attention_2" if args.hardware_mode == "h100" else "sdpa"', source)
        self.assertIn('confidence_path = output.parent / "confidence.json"', source)
        self.assertIn('"plddt_mean"', source)
        self.assertIn("write_confidence_envelope", source)
        self.assertNotIn(".cpu().tolist()", source)

    def test_dockerfiles_are_exact_external_and_do_not_duplicate_large_chmod_layers(self) -> None:
        for image in LOCK["images"]:
            dockerfile = (ROOT / image["dockerfile"]).read_text(encoding="utf-8")
            with self.subTest(image=image["id"]):
                self.assertIn(image["source"]["revision"], dockerfile)
                self.assertIn('test "$(git rev-parse HEAD)" = "${SOURCE_REVISION}"', dockerfile)
                self.assertIn("git describe --tags --exact-match HEAD", dockerfile)
                self.assertIn('ai.nebius.fs2.artifact.policy="external-only"', dockerfile)
                self.assertIn("USER 10001:10001", dockerfile)
                self.assertIn("result_contract.py /usr/local/bin/result_contract.py", dockerfile)
                self.assertIn("confidence.schema.json /opt/fs2/confidence.schema.json", dockerfile)
                artifact_copies = [
                    line
                    for line in dockerfile.splitlines()
                    if line.startswith(("COPY ", "ADD "))
                    and re.search(r"(af3\.bin|model\.safetensors|[^ ]+\.pt)\b", line)
                ]
                self.assertEqual(artifact_copies, [])
                self.assertLessEqual(dockerfile.count("chmod -R a-w"), 1)
        protenix = (ROOT / "Dockerfile.protenix-v2").read_text(encoding="utf-8")
        runtime_stage = protenix.split("FROM ${BASE_IMAGE}", maxsplit=1)[1]
        self.assertIn("WORKDIR /opt/fs2/runtime", runtime_stage)
        self.assertNotIn("/opt/protenix-build", runtime_stage)
        self.assertIn("! command -v nvcc", runtime_stage)
        self.assertIn('unsupported-pinned-pytorch-cu126', protenix)
        af3 = (ROOT / "Dockerfile.alphafold3").read_text(encoding="utf-8")
        self.assertIn("FS2_MODEL_DIR=/models", af3)
        self.assertIn("FS2_DATABASE_DIR=/databases", af3)
        self.assertNotIn("/opt/fs2/academic/alphafold3", af3)
        of3 = (ROOT / "Dockerfile.openfold3").read_text(encoding="utf-8")
        self.assertIn("prepare_openfold3.py /opt/fs2/prepare_openfold3.py", of3)

    def test_protenix_fast_layernorm_is_prebuilt_but_triton_jit_is_truthful(self) -> None:
        compiler = (ROOT / "protenix-torch-ext-compile.py").read_text(encoding="utf-8")
        self.assertIn('"arch=compute_90,code=sm_90"', compiler)
        self.assertIn('"arch=compute_90,code=compute_90"', compiler)
        self.assertNotRegex(compiler, r"compute_(70|80|86|89|100)")
        dockerfile = (ROOT / "Dockerfile.protenix-v2").read_text(encoding="utf-8")
        self.assertIn('rm -rf "${package_root}/model/layer_norm/kernel"', dockerfile)
        self.assertIn('"${package_root}/model/layer_norm/torch_ext_compile.py"', dockerfile)
        self.assertIn("env -C / /opt/protenix-venv/bin/python -c", dockerfile)
        self.assertIn("TRITON_CACHE_DIR=/cache/protenix/triton", dockerfile)
        self.assertIn("CUEQ_TRITON_CACHE_DIR=/cache/protenix/cueq-triton", dockerfile)
        smoke = (ROOT / "image_smoke.py").read_text(encoding="utf-8")
        self.assertIn("active-triton-jit-first-shape-then-cache", smoke)
        self.assertNotIn('result["runtime_jit"] = "disabled"', smoke)

    def test_cross_contract_wrappers_reject_unbound_seed_handoffs(self) -> None:
        af3 = load_module("run_alphafold3")
        self.assertEqual(af3._parse_seeds("101,202"), [101, 202])
        with self.assertRaisesRegex(SystemExit, "unique"):
            af3._parse_seeds("101,101")
        protenix = load_module("run_protenix")
        self.assertEqual(protenix._canonical_seeds("001,202"), "1,202")
        with self.assertRaisesRegex(SystemExit, "unique"):
            protenix._canonical_seeds("101,101")
        patch_source = (ROOT / "patch_protenix_source.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("FS2_MSA_MODE")', patch_source)
        self.assertIn("use_msa=False", patch_source)
        self.assertIn("use_template=False", patch_source)
        self.assertIn("use_rna_msa=False", patch_source)
        self.assertNotIn("FS2_PUBLIC_MSA_OPT_IN", patch_source)

    def test_openfold_preparation_binds_offline_msa_mode(self) -> None:
        module = load_module("prepare_openfold3")
        document = {
            "queries": {
                "fixture": {
                    "chains": [
                        {
                            "molecule_type": "protein",
                            "chain_ids": "A",
                            "sequence": "ACDE",
                        }
                    ]
                }
            }
        }
        module._bind_msa_mode(document, "none")
        chain = document["queries"]["fixture"]["chains"][0]
        self.assertIs(chain["use_msas"], False)
        with self.assertRaisesRegex(SystemExit, "main_msa_file_paths"):
            module._bind_msa_mode(
                {
                    "queries": {
                        "fixture": {
                            "chains": [
                                {
                                    "molecule_type": "protein",
                                    "chain_ids": "A",
                                    "sequence": "ACDE",
                                }
                            ]
                        }
                    }
                },
                "precomputed",
            )

    def test_smoke_defaults_to_semantic_and_build_mode_is_explicit(self) -> None:
        source = (ROOT / "image_smoke.py").read_text(encoding="utf-8")
        self.assertIn("--build-only", source)
        self.assertIn("exact-artifact-h100-semantic", source)
        self.assertIn("semantic smoke requires --semantic-request", source)
        publisher = (ROOT / "build-and-publish.sh").read_text(encoding="utf-8")
        self.assertIn("fs2-image-smoke --build-only", publisher)

    def test_publisher_consumes_v2_repository_tag_and_configurable_registry(self) -> None:
        script = (ROOT / "build-and-publish.sh").read_text(encoding="utf-8")
        self.assertIn("--registry-root", script)
        self.assertIn("FS2_REGISTRY_ROOT", script)
        self.assertIn(".repository", script)
        self.assertIn(".tag", script)
        self.assertNotIn("'.target'", script)
        self.assertGreaterEqual(script.count('target_state "$target"'), 2)
        self.assertIn("refusing to overwrite existing target", script)
        self.assertIn("refusing raced overwrite", script)
        self.assertNotIn(":latest", script)
        self.assertNotIn("docker login", script)

    def test_shell_entrypoints_are_syntactically_valid(self) -> None:
        for name in ("entrypoint.sh", "entrypoint-openfold3.sh", "build-and-publish.sh"):
            completed = subprocess.run(
                ["bash", "-n", str(ROOT / name)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)

    def test_preliminary_publications_remain_explicitly_non_deployable(self) -> None:
        superseded = LOCK["superseded_publications"]
        self.assertEqual(len(superseded), 4)
        self.assertTrue(all(item["deployable"] is False for item in superseded))
        self.assertTrue(all(item["digest"].startswith("sha256:") for item in superseded))


if __name__ == "__main__":
    unittest.main()
