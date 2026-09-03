from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

import jsonschema
import yaml


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
    ).encode("utf-8") + b"\n"
    return hashlib.sha256(encoded).hexdigest()


def write_runtime_localization_marker(
    path: Path,
    *,
    model_id: str,
    variant_id: str,
    stage_id: str,
    artifacts: list[dict[str, object]],
    authorization_receipt_sha256: str | None = None,
) -> Path:
    receipt = "d" * 64
    marker = {
        "schema": "fs2-serve.nebius.ai/runtime-localization-marker/v1",
        "operation_id": "00000000-0000-4000-8000-000000000010",
        "attempt_id": "00000000-0000-4000-8000-000000000011",
        "tenant_id": "test-tenant",
        "model_id": model_id,
        "variant_id": variant_id,
        "stage_id": stage_id,
        "artifacts": [
            {
                "artifact_id": artifact["artifact_id"],
                "mount_path": artifact["mount_path"],
                "content_digest": f"sha256:{artifact['content_sha256']}",
                "localization_receipt_digest": f"sha256:{receipt}",
                "sub_path": artifact.get("sub_path"),
                "expected_manifest_sha256": artifact.get(
                    "expected_manifest_sha256"
                ),
                "readiness_receipt_sha256": receipt,
                "authorization_receipt_sha256": authorization_receipt_sha256,
            }
            for artifact in artifacts
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


class StructureSecondaryImageContractTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("FS2_ARTIFACT_GENERATOR"),
        "set FS2_ARTIFACT_GENERATOR to the exact 37239176 catalog generator",
    )
    def test_lock_mounts_match_exact_artifact_worker_runtime_integration(self) -> None:
        generator_path = Path(os.environ["FS2_ARTIFACT_GENERATOR"]).resolve()
        spec = importlib.util.spec_from_file_location(
            "artifact_worker_mounts_37239176", generator_path
        )
        assert spec is not None and spec.loader is not None
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        integration = next(
            value
            for path, value in worker.documents().items()
            if path.name == "runtime-integration.json"
        )["consumers"]
        for model_id in ("esmfold2", "esmfold2-fast"):
            with self.subTest(model_id=model_id, contract="candidate-only"):
                self.assertEqual(
                    integration[model_id]["accelerator_compatibility"],
                    "binary-compatible-hopper-candidate-sm90-no-ptx",
                )
                self.assertEqual(
                    integration[model_id]["qualification_state"],
                    "pending-exact-image-h100-semantic-test",
                )
        images = {image["id"]: image for image in LOCK["images"]}
        runtime_path_keys = {
            "esmfold2": {"esmfold2-ccd": "ccd_path"},
            "esmfold2-fast": {"esmfold2-ccd": "ccd_path"},
            "openfold3": {
                "openfold3-openbind-0": "checkpoint",
                "openfold3-components-bcif": "components_bcif",
            },
        }
        for model_id, artifact_paths in runtime_path_keys.items():
            locked = {
                item["id"]: item for item in images[model_id]["external_artifacts"]
            }
            for artifact_id, path_key in artifact_paths.items():
                with self.subTest(model_id=model_id, artifact_id=artifact_id):
                    worker_artifact = integration[model_id]["artifacts"][artifact_id]
                    self.assertEqual(locked[artifact_id]["mount"], worker_artifact["mount_path"])
                    self.assertEqual(
                        locked[artifact_id]["content_digest"],
                        worker_artifact["content_digest_sha256"],
                    )
                    self.assertEqual(
                        locked[artifact_id]["runtime_path"],
                        integration[model_id]["runtime_paths"][path_key],
                    )

    @unittest.skipUnless(
        os.environ.get("FS2_ARTIFACT_GENERATOR"),
        "set FS2_ARTIFACT_GENERATOR to the exact 37239176 catalog generator",
    )
    def test_exact_artifact_worker_manifest_is_accepted_with_newline_hash(self) -> None:
        generator_path = Path(os.environ["FS2_ARTIFACT_GENERATOR"]).resolve()
        self.assertEqual(
            hashlib.sha256(generator_path.read_bytes()).hexdigest(),
            "e7ec850a96daaf7d9463d953490d263069406ff4f1b125d400d75390372994b8",
        )
        spec = importlib.util.spec_from_file_location(
            "artifact_worker_37239176", generator_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        worker = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(worker)

        outputs = worker.documents()
        generated_output_sha256 = {
            path.name: hashlib.sha256(worker.pretty_json(value)).hexdigest()
            for path, value in outputs.items()
            if path.name in {"manifest-protenix-v2.json", "runtime-integration.json"}
        }
        self.assertEqual(
            generated_output_sha256,
            {
                "manifest-protenix-v2.json": "8c48d5c50b14dfa1fff0e55614ce85c79750e672db8904bea843e36ccedcc19f",
                "runtime-integration.json": "ebc007e94c60154e34b8d87d877d25e5d0bdbfb1b97a17c411be236cbc5a7f0b",
            },
        )
        source_manifest = next(
            value
            for path, value in outputs.items()
            if path.name == "manifest-protenix-v2.json"
        )
        integration = next(
            value
            for path, value in outputs.items()
            if path.name == "runtime-integration.json"
        )["consumers"]["protenix-v2"]
        exact_manifest = worker.protenix_localization_manifest(
            source_manifest["content"]["files"]
        )
        exact_digest = integration["localization_manifest_sha256"]
        self.assertEqual(
            exact_digest,
            "a093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7",
        )
        self.assertEqual(
            hashlib.sha256(worker.canonical(exact_manifest)).hexdigest(), exact_digest
        )
        without_newline = json.dumps(
            exact_manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(without_newline).hexdigest(),
            "dea84c5e3f2e87de99813b616f70ddd53576b72a42a0313e9c89876b02070565",
        )

        runtime = load_module("run_protenix")
        self.assertEqual(runtime._canonical_json_sha256(exact_manifest), exact_digest)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for entry in exact_manifest["files"]:
                destination = root / entry["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as stream:
                    stream.truncate(entry["bytes"])
            manifest_path = root / "manifest.json"
            manifest_path.write_bytes(worker.pretty_json(exact_manifest))
            ready_path = root / ".fs2-manifest-sha256"
            ready_path.write_text(exact_digest + "\n", encoding="ascii")
            with (
                mock.patch.object(runtime, "PROTENIX_ROOT", root),
                mock.patch.object(
                    runtime,
                    "CHECKPOINT",
                    root / "checkpoint/protenix-v2.pt",
                ),
                mock.patch.object(runtime, "ARTIFACT_MANIFEST", manifest_path),
                mock.patch.object(runtime, "ARTIFACT_READY", ready_path),
            ):
                self.assertEqual(runtime._validate_artifact(), exact_digest)

    @unittest.skipUnless(
        os.environ.get("FS2_PROTENIX_SOURCE"),
        "set FS2_PROTENIX_SOURCE to an exact v2.0.0 checkout for the patch gate",
    )
    def test_exact_protenix_v200_source_accepts_one_offline_prep_patch(self) -> None:
        source = Path(os.environ["FS2_PROTENIX_SOURCE"]).resolve()
        revision = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        self.assertEqual(revision, "2475421477ab414b571149ad4a875c390ff8a35d")
        upstream = source / "runner/batch_inference.py"
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            destination = checkout / "runner/batch_inference.py"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(upstream.read_bytes())
            subprocess.run(
                [sys.executable, str(ROOT / "patch_protenix_source.py")],
                cwd=checkout,
                check=True,
            )
            patched = destination.read_text(encoding="utf-8")
            self.assertEqual(patched.count('os.environ.get("FS2_MSA_MODE")'), 1)
            offline_call = (
                "return preprocess_input(\n"
                "        input_json=input,\n"
                "        out_dir=out_dir,\n"
                "        use_msa=False,\n"
                "        use_template=False,\n"
                "        use_rna_msa=False,"
            )
            self.assertEqual(patched.count(offline_call), 1)
            rejected = subprocess.run(
                [sys.executable, str(ROOT / "patch_protenix_source.py")],
                cwd=checkout,
                check=False,
                text=True,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("was not found once", rejected.stderr)

    @unittest.skipUnless(
        os.environ.get("FS2_OPENFOLD3_SOURCE"),
        "set FS2_OPENFOLD3_SOURCE to an exact v0.5.0 checkout for the pinned parser gate",
    )
    def test_pinned_openfold3_v050_parser_accepts_prepared_query(self) -> None:
        source = Path(os.environ["FS2_OPENFOLD3_SOURCE"]).resolve()
        revision = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        self.assertEqual(revision, "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86")
        sys.path.insert(0, str(source))
        try:
            from openfold3.projects.of3_all_atom.config.inference_query_format import (
                InferenceQuerySet,
            )

            module = load_module("run_openfold3")
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                raw = root / "input.json"
                raw.write_text(
                    json.dumps(
                        {
                            "queries": {
                                "query_ubiquitin": {
                                    "chains": [
                                        {
                                            "molecule_type": "protein",
                                            "chain_ids": ["A"],
                                            "sequence": "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG",
                                        }
                                    ]
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                base = ROOT / "openfold3-runner-base.yaml"
                prepared = root / "prepared"
                args = types.SimpleNamespace(
                    input_manifest=str(raw),
                    query_json=str(prepared / "query.json"),
                    base_runner_yaml=str(base),
                    runner_yaml=str(prepared / "runner.yaml"),
                    provenance_marker=str(prepared / "provenance.json"),
                    handoff_tar=str(root / "handoff.tar.zst"),
                    output_artifact_id="openfold-stage-1",
                    raw_input_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
                    msa_mode="none",
                    model_seeds="101,202",
                    offline=True,
                )
                with (
                    mock.patch.object(module, "CANONICAL_BASE_RUNNER", base),
                    mock.patch.object(
                        module,
                        "BASE_RUNNER_SHA256",
                        hashlib.sha256(base.read_bytes()).hexdigest(),
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    module._prepare(args)
                parsed = InferenceQuerySet.from_json(prepared / "query.json")
                query = parsed.queries["query_ubiquitin"]
                self.assertEqual(parsed.seeds, [101, 202])
                self.assertFalse(query.use_msas)
                self.assertFalse(query.use_main_msas)
                self.assertFalse(query.use_paired_msas)
        finally:
            sys.path.remove(str(source))

    def test_lock_v2_validates_and_has_four_unique_derived_targets(self) -> None:
        jsonschema.Draft202012Validator(SCHEMA).validate(LOCK)
        self.assertEqual(LOCK["schema"], "fs2.nebius.ai/structure-secondary-image-lock/v2")
        images = LOCK["images"]
        self.assertEqual(
            [image["id"] for image in images],
            ["esmfold2", "esmfold2-fast", "protenix-v2", "openfold3"],
        )
        self.assertEqual(
            set(CONFIDENCE_SCHEMA["properties"]["runtime_id"]["enum"]),
            {image["id"] for image in images},
        )
        expected_repositories = {
            "esmfold2": "cancer-immunotherapy/esmfold2",
            "esmfold2-fast": "cancer-immunotherapy/esmfold2-fast",
            "protenix-v2": "cancer-immunotherapy/protenix-v2",
            "openfold3": "cancer-immunotherapy/openfold3-upstream",
        }
        self.assertEqual(
            {image["id"]: image["repository"] for image in images},
            expected_repositories,
        )
        targets = {
            f'{LOCK["registry_default"]}/{image["repository"]}:{image["tag"]}'
            for image in images
        }
        self.assertEqual(len(targets), 4)
        self.assertTrue(all("target" not in image for image in images))
        artifact_command_indexes = {
            "esmfold2": (1,),
            "esmfold2-fast": (1,),
            "protenix-v2": (0, 1),
            "openfold3": (1,),
        }
        for image in images:
            contract = image["runtime_contract"]
            self.assertTrue(contract["required_mounts"])
            self.assertTrue(contract["commands"])
            self.assertTrue(
                all(command.startswith("/") for command in contract["commands"])
            )
            for index in artifact_command_indexes[image["id"]]:
                self.assertIn(
                    "--runtime-localization-marker",
                    contract["commands"][index],
                )
        self.assertFalse((ROOT / "Dockerfile.alphafold3").exists())
        self.assertFalse((ROOT / "run_alphafold3.py").exists())

    def test_runtime_localization_marker_is_strict_and_receipt_bound(self) -> None:
        runtime = load_module("runtime_localization")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = write_runtime_localization_marker(
                root / "runtime-localization.json",
                model_id="protenix-v2",
                variant_id="upstream-v2-0-0",
                stage_id="sample-structure",
                artifacts=[
                    {
                        "artifact_id": "protenix-v2",
                        "mount_path": "/models/protenix-v2",
                        "content_sha256": "5" * 64,
                        "expected_manifest_sha256": "a" * 64,
                    }
                ],
            )
            expected = (
                runtime.RuntimeArtifactExpectation(
                    "protenix-v2",
                    "/models/protenix-v2",
                    "5" * 64,
                    expected_manifest_sha256="a" * 64,
                ),
            )
            environment = {
                "FS2_OPERATION_ID": "00000000-0000-4000-8000-000000000010",
                "FS2_ATTEMPT_ID": "00000000-0000-4000-8000-000000000011",
                "FS2_TENANT_ID": "test-tenant",
                "FS2_VARIANT_ID": "upstream-v2-0-0",
                "FS2_STAGE_ID": "sample-structure",
                "FS2_RUNTIME_LOCALIZATION_MARKER": str(path),
                "FS2_ARTIFACT_ACCESS_RECEIPT_DIGEST": "",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                marker = runtime.validate_runtime_localization(
                    path,
                    model_id="protenix-v2",
                    variant_id="upstream-v2-0-0",
                    stage_id="sample-structure",
                    artifacts=expected,
                )
                self.assertEqual(marker["artifacts"][0]["readiness_receipt_sha256"], "d" * 64)
                for field, value in (
                    ("content_digest", "sha256:" + "0" * 64),
                    ("mount_path", "/models/substitute"),
                    ("expected_manifest_sha256", "0" * 64),
                    ("readiness_receipt_sha256", "0" * 64),
                    ("authorization_receipt_sha256", "0" * 64),
                ):
                    with self.subTest(field=field):
                        changed = json.loads(path.read_text(encoding="utf-8"))
                        changed["artifacts"][0][field] = value
                        path.write_text(json.dumps(changed), encoding="utf-8")
                        with self.assertRaisesRegex(
                            SystemExit, "does not bind exact artifact"
                        ):
                            runtime.validate_runtime_localization(
                                path,
                                model_id="protenix-v2",
                                variant_id="upstream-v2-0-0",
                                stage_id="sample-structure",
                                artifacts=expected,
                            )
                        write_runtime_localization_marker(
                            path,
                            model_id="protenix-v2",
                            variant_id="upstream-v2-0-0",
                            stage_id="sample-structure",
                            artifacts=[
                                {
                                    "artifact_id": "protenix-v2",
                                    "mount_path": "/models/protenix-v2",
                                    "content_sha256": "5" * 64,
                                    "expected_manifest_sha256": "a" * 64,
                                }
                            ],
                        )
                with mock.patch.dict(
                    os.environ, {"FS2_TENANT_ID": "another-tenant"}, clear=False
                ):
                    with self.assertRaisesRegex(SystemExit, "FS2_TENANT_ID"):
                        runtime.validate_runtime_localization(
                            path,
                            model_id="protenix-v2",
                            variant_id="upstream-v2-0-0",
                            stage_id="sample-structure",
                            artifacts=expected,
                        )

    def test_sources_tags_and_base_images_are_immutable(self) -> None:
        expected = {
            "esmfold2": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            "esmfold2-fast": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            "protenix-v2": "2475421477ab414b571149ad4a875c390ff8a35d",
            "openfold3": "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86",
        }
        for image in LOCK["images"]:
            with self.subTest(image=image["id"]):
                revision = expected[image["id"]]
                self.assertEqual(image["source"]["revision"], revision)
                self.assertEqual(image["tag"], f"{revision}-h100-r3")
                self.assertRegex(image["source"]["tag"], r"^v[0-9]")
                for base in image["base_images"]:
                    self.assertRegex(base, r"@sha256:[0-9a-f]{64}$")

    def test_candidate_contract_does_not_claim_unearned_qualification(self) -> None:
        reviewed_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT / "README.md",
                ROOT / "run_protenix.py",
                ROOT / "run_openfold3.py",
                ROOT / "patch_protenix_source.py",
                ROOT / "protenix-torch-ext-compile.py",
            )
        ).lower()
        for phrase in (
            "canonical checkpoint",
            "currently qualified",
            "qualified lane",
            "qualified semantic boundary",
            "qualified localization identity",
            "specifically qualified",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, reviewed_text)

        self.assertEqual(
            LOCK["publication_policy"],
            "explicit-non-overwriting-build-checked-tags",
        )
        expected_h100_states = {
            "esmfold2": "pending-exact-artifact-semantic-test",
            "esmfold2-fast": "pending-exact-artifact-semantic-test",
            "protenix-v2": "pending-exact-checkpoint-semantic-test",
            "openfold3": "pending-exact-artifact-semantic-test",
        }
        for image in LOCK["images"]:
            with self.subTest(image=image["id"]):
                self.assertIsNone(image["published_digest"])
                self.assertEqual(
                    image["accelerator_support"]["h100"]["status"],
                    expected_h100_states[image["id"]],
                )

        protenix = next(image for image in LOCK["images"] if image["id"] == "protenix-v2")
        checkpoint_artifact = protenix["external_artifacts"][0]
        self.assertEqual(
            checkpoint_artifact["state"],
            "third-party-mirror-verified-not-publisher-byte-compared",
        )

    def test_mandatory_check_uses_reachable_generator_in_fresh_object_store(self) -> None:
        source = (ROOT / "check.sh").read_text(encoding="utf-8")
        self.assertIn(
            "artifact_worker_revision=372391764b7c514d015f9b33cd3dcba9f3119f73",
            source,
        )
        self.assertIn(
            "artifact_generator_sha256=e7ec850a96daaf7d9463d953490d263069406ff4f1b125d400d75390372994b8",
            source,
        )
        self.assertIn(
            "artifact_worker_ref=refs/heads/agent/fs2-cancer-model-artifact-cache-ingestion-r20260902",
            source,
        )
        self.assertIn("checkout_exact_ref", source)
        self.assertIn("--no-tags --depth=1", source)
        self.assertIn('artifact_worker_source="${temporary_root}/artifact-worker"', source)
        self.assertIn('"${superseded_unreachable_revision}^{commit}"', source)
        self.assertNotIn('git -C "$runtime_dir" show', source)
        self.assertNotIn('git -C "$runtime_dir" cat-file', source)

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
            self.assertEqual(artifacts["esmfold2-ccd"]["mount"], "/databases/esmfold2")
            self.assertEqual(
                artifacts["esmfold2-ccd"]["runtime_path"],
                "/databases/esmfold2/ccd.pkl",
            )
            self.assertEqual(
                artifacts["esmfold2-ccd"]["content_digest"],
                "b1c2fe19204c57f7a7cca6ab4cb0cb420b99312fff424ef2e405fc8234b7616e",
            )

    def test_fast_start_cache_contracts_are_auxiliary_and_numbered_level_is_l1(self) -> None:
        images = {image["id"]: image for image in LOCK["images"]}
        expected_level_states = {
            "L1": "candidate-pending-regional-image-cache-evidence",
            "L2": "unavailable-no-shared-storage-gpu-process-snapshot",
            "L3": "unavailable-no-local-disk-cached-snapshot",
            "L4": "unavailable-no-system-ram-retained-model",
        }
        for image in images.values():
            fast_start = image["fast_start"]
            self.assertEqual(fast_start["maximum_candidate_level"], "L1")
            self.assertIsNone(fast_start["qualified_level"])
            self.assertEqual(
                fast_start["qualification_state"],
                "pending-regional-image-cache-evidence",
            )
            self.assertEqual(fast_start["level_states"], expected_level_states)

        for model_id in ("esmfold2", "esmfold2-fast"):
            contract = images[model_id]["runtime_contract"]
            self.assertNotIn("writable_cache_mounts", contract)
            self.assertNotIn("cache_environment", contract)
            self.assertEqual(
                images[model_id]["fast_start"]["compiler_cache_optimization"],
                "none-proven-l1-image-plus-external-artifacts",
            )

        expected = {
            "protenix-v2": (
                "/cache/protenix",
                {
                    "TRITON_CACHE_DIR": "/cache/protenix/triton",
                    "CUEQ_TRITON_CACHE_DIR": "/cache/protenix/cueq-triton",
                    "TORCH_EXTENSIONS_DIR": "/cache/protenix/torch-extensions",
                    "XDG_CACHE_HOME": "/cache/protenix/xdg",
                },
            ),
            "openfold3": (
                "/cache/openfold3",
                {
                    "TRITON_CACHE_DIR": "/cache/openfold3/triton",
                    "TORCH_EXTENSIONS_DIR": "/cache/openfold3/torch-extensions",
                    "XDG_CACHE_HOME": "/cache/openfold3/xdg",
                },
            ),
        }
        for model_id, (mount, environment) in expected.items():
            contract = images[model_id]["runtime_contract"]
            self.assertEqual(contract["writable_cache_mounts"], [mount])
            self.assertEqual(contract["cache_environment"], environment)
            self.assertEqual(
                images[model_id]["fast_start"]["compiler_cache_optimization"],
                "auxiliary-l1-plus-persistent-cache-pending-first-vs-warm-h100-timing",
            )

        openfold = (ROOT / "Dockerfile.openfold3").read_text(encoding="utf-8")
        for value in expected["openfold3"][1].values():
            self.assertGreaterEqual(openfold.count(value), 2)
        self.assertIn("mkdir -p /opt/fs2/runtime /models/openfold3 /databases/openfold3 /outputs /cache/openfold3/triton /cache/openfold3/torch-extensions /cache/openfold3/xdg", openfold)
        self.assertIn("chown -R 10001:10001 /models /databases /outputs /cache/openfold3", openfold)
        self.assertNotIn("TRITON_CACHE_DIR=/tmp", openfold)
        self.assertNotIn("TORCH_EXTENSIONS_DIR=/tmp", openfold)
        self.assertNotIn("XDG_CACHE_HOME=/tmp", openfold)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("L1: regional image cache", readme)
        self.assertIn("L2: a real GPU/process snapshot restored from", readme)
        self.assertIn("L3: that snapshot cached on local disk", readme)
        self.assertIn("L4: the model retained in system RAM for GPU swap", readme)
        self.assertIn("cannot qualify L2", readme)
        self.assertIn("maximum_candidate_level: L1", readme)
        self.assertIn("L2, L3, and L4 are explicitly", readme)

    def test_build_cache_smoke_contract_exactly_matches_image_lock(self) -> None:
        smoke = load_module("image_smoke")
        expected = {
            image["id"]: {
                "mount_roots": image["runtime_contract"]["writable_cache_mounts"],
                "environment": image["runtime_contract"]["cache_environment"],
            }
            for image in LOCK["images"]
            if "writable_cache_mounts" in image["runtime_contract"]
            or "cache_environment" in image["runtime_contract"]
        }
        self.assertEqual(smoke.BUILD_CACHE_CONTRACTS, expected)

    def test_build_cache_smoke_probes_every_path_as_final_nonroot_user(self) -> None:
        smoke = load_module("image_smoke")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache" / "runtime"
            first = root / "compiler"
            second = root / "xdg"
            first.mkdir(parents=True)
            second.mkdir()
            contract = {
                "fixture": {
                    "mount_roots": [str(root)],
                    "environment": {
                        "FIRST_CACHE_DIR": str(first),
                        "SECOND_CACHE_DIR": str(second),
                    },
                }
            }
            with (
                mock.patch.object(smoke, "BUILD_CACHE_CONTRACTS", contract),
                mock.patch.object(smoke.os, "geteuid", return_value=10001),
                mock.patch.object(smoke.os, "getegid", return_value=10001),
                mock.patch.dict(
                    os.environ,
                    {
                        "FIRST_CACHE_DIR": str(first),
                        "SECOND_CACHE_DIR": str(second),
                    },
                    clear=False,
                ),
            ):
                evidence = smoke._validate_build_cache_contract("fixture")

            self.assertEqual(evidence["effective_uid"], 10001)
            self.assertEqual(evidence["effective_gid"], 10001)
            self.assertEqual(evidence["scope"], "built-image-filesystem-only")
            self.assertEqual(
                evidence["deployment_persistent_mount_readiness"], "not-tested"
            )
            self.assertEqual(
                {item["path"] for item in evidence["directories"]},
                {str(root), str(first), str(second)},
            )
            self.assertTrue(
                all(
                    item["probe"] == "bounded-create-read-remove-passed"
                    and item["probe_bytes"] == len(smoke.CACHE_WRITE_PROBE)
                    for item in evidence["directories"]
                )
            )
            self.assertEqual(list(root.rglob(".fs2-cache-smoke.*")), [])

    def test_build_cache_smoke_fails_closed_on_identity_environment_and_path(self) -> None:
        smoke = load_module("image_smoke")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache"
            root.mkdir()
            contract = {
                "fixture": {
                    "mount_roots": [str(root)],
                    "environment": {"CACHE_DIR": str(root)},
                }
            }
            with (
                mock.patch.object(smoke, "BUILD_CACHE_CONTRACTS", contract),
                mock.patch.object(smoke.os, "geteuid", return_value=0),
                mock.patch.object(smoke.os, "getegid", return_value=0),
                self.assertRaisesRegex(RuntimeError, "final image user 10001:10001"),
            ):
                smoke._validate_build_cache_contract("fixture")
            with (
                mock.patch.object(smoke, "BUILD_CACHE_CONTRACTS", contract),
                mock.patch.object(smoke.os, "geteuid", return_value=10001),
                mock.patch.object(smoke.os, "getegid", return_value=10001),
                mock.patch.dict(os.environ, {"CACHE_DIR": str(root / "wrong")}),
                self.assertRaisesRegex(RuntimeError, "does not exactly match"),
            ):
                smoke._validate_build_cache_contract("fixture")
            with (
                mock.patch.object(smoke, "BUILD_CACHE_CONTRACTS", contract),
                mock.patch.object(smoke.os, "geteuid", return_value=10001),
                mock.patch.object(smoke.os, "getegid", return_value=10001),
                mock.patch.dict(os.environ, {"CACHE_DIR": str(root)}),
                mock.patch.object(smoke.os, "access", return_value=False),
                self.assertRaisesRegex(RuntimeError, "not writable by UID 10001"),
            ):
                smoke._validate_build_cache_contract("fixture")
            for bad_path, message in (
                ("relative-cache", "must be absolute"),
                (str(root / "missing"), "does not exist"),
            ):
                with self.subTest(path=bad_path):
                    bad_contract = {
                        "fixture": {
                            "mount_roots": [bad_path],
                            "environment": {"CACHE_DIR": bad_path},
                        }
                    }
                    with (
                        mock.patch.object(
                            smoke, "BUILD_CACHE_CONTRACTS", bad_contract
                        ),
                        mock.patch.object(smoke.os, "geteuid", return_value=10001),
                        mock.patch.object(smoke.os, "getegid", return_value=10001),
                        mock.patch.dict(os.environ, {"CACHE_DIR": bad_path}),
                        self.assertRaisesRegex(RuntimeError, message),
                    ):
                        smoke._validate_build_cache_contract("fixture")

    def test_protenix_artifact_closure_and_architecture_truth(self) -> None:
        image = next(item for item in LOCK["images"] if item["id"] == "protenix-v2")
        self.assertEqual(len(image["external_artifacts"]), 1)
        artifact = image["external_artifacts"][0]
        self.assertEqual(artifact["id"], "protenix-v2")
        self.assertEqual(artifact["mount"], "/models/protenix-v2")
        self.assertEqual(
            artifact["localized_content_digest_sha256"],
            "5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48",
        )
        self.assertEqual(
            artifact["localization_manifest_sha256"],
            "a093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7",
        )
        self.assertEqual(artifact["checkpoint"]["bytes"], 1_859_785_497)
        self.assertEqual(
            artifact["ready_marker"],
            "/models/protenix-v2/.fs2-manifest-sha256",
        )
        self.assertEqual(
            artifact["common"],
            {
                "artifact_id": "protenix-v2-inference-data-2026-01-29",
                "revision": "tos-common-2026-01-29",
                "archive_url": "https://protenix.tos-cn-beijing.volces.com/common.tar.gz",
                "archive_bytes": 475_085_654,
                "archive_sha256": "08ea594f429df35494c062e3dfcacaf48fa761e4ea4a8bcb6d5107d211e64dbd",
            },
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
        self.assertIn("system-gcc-launcher", image["runtime_contract"]["runtime_compilation"])
        self.assertIn("no-nvcc", image["runtime_contract"]["runtime_compilation"])

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
            for relative, (byte_count, _digest) in module.COMMON_FILE_IDENTITIES.items():
                with (root / relative).open("wb") as stream:
                    stream.truncate(byte_count)
            files = [
                {
                    "path": "checkpoint/protenix-v2.pt",
                    "bytes": module.CHECKPOINT_BYTES,
                    "sha256": module.CHECKPOINT_SHA256,
                },
                *[
                    {"path": relative, "bytes": identity[0], "sha256": identity[1]}
                    for relative, identity in sorted(module.COMMON_FILE_IDENTITIES.items())
                ],
            ]
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
                        "archive_url": module.COMMON_DATA_ARCHIVE_URL,
                        "archive_bytes": module.COMMON_DATA_ARCHIVE_BYTES,
                        "archive_sha256": module.COMMON_DATA_ARCHIVE_SHA256,
                    },
                },
                "files": files,
            }
            manifest_path = root / "manifest.json"
            ready_path = root / ".fs2-manifest-sha256"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(
                canonical_sha256(manifest), module.LOCALIZATION_MANIFEST_SHA256
            )
            ready_path.write_text(
                module.LOCALIZATION_MANIFEST_SHA256 + "\n", encoding="utf-8"
            )
            with (
                mock.patch.object(module, "PROTENIX_ROOT", root),
                mock.patch.object(module, "CHECKPOINT", checkpoint),
                mock.patch.object(module, "ARTIFACT_MANIFEST", manifest_path),
                mock.patch.object(module, "ARTIFACT_READY", ready_path),
            ):
                self.assertEqual(
                    module._validate_artifact(), module.LOCALIZATION_MANIFEST_SHA256
                )
                (root / module.COMMON_REQUIRED_PATHS[-1]).unlink()
                with self.assertRaisesRegex(SystemExit, "obsolete_release_date"):
                    module._validate_artifact()
                with (root / module.COMMON_REQUIRED_PATHS[-1]).open("wb") as stream:
                    stream.truncate(
                        module.COMMON_FILE_IDENTITIES[
                            module.COMMON_REQUIRED_PATHS[-1].as_posix()
                        ][0]
                    )
                substituted = json.loads(json.dumps(manifest))
                substituted["files"][-1]["sha256"] = "f" * 64
                manifest_path.write_text(json.dumps(substituted), encoding="utf-8")
                ready_path.write_text(
                    canonical_sha256(substituted) + "\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(SystemExit, "exact common file|localization acceptance"):
                    module._validate_artifact()

    def test_generated_protenix_argv_is_fixed_and_canonical(self) -> None:
        module = load_module("run_protenix")
        argv = module.build_pred_command(
            Path("/work/enriched.json"),
            Path("/work/results"),
            seeds=[101, 202],
            sample_count=5,
        )
        self.assertEqual(argv[:2], ["/opt/protenix-venv/bin/protenix", "pred"])
        joined = " ".join(argv)
        self.assertIn("--model_name protenix-v2", joined)
        self.assertIn("--use_msa false", joined)
        self.assertIn("--use_template false", joined)
        self.assertIn("--use_rna_msa false", joined)
        self.assertEqual(argv[argv.index("--seeds") + 1], "101,202")
        self.assertNotIn("--checkpoint", joined)
        source = (ROOT / "run_protenix.py").read_text(encoding="utf-8")
        self.assertNotIn("argparse.REMAINDER", source)
        self.assertNotIn("_sha256(checkpoint)", source)
        self.assertNotIn("os.execve", source)
        self.assertNotIn("REFERENCE_BUNDLE_ID", source)
        self.assertNotIn("reference_manifest_sha256", source)
        self.assertIn("--reference-manifest", source)
        self.assertIn('choices=("none",)', source)
        self.assertIn("write_confidence_envelope", source)

    def test_protenix_confidence_is_deterministic_and_path_relative(self) -> None:
        module = load_module("run_protenix")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for seed in (101, 202):
                for sample in (0, 1):
                    directory = output / "dataset" / "fixture" / f"seed_{seed}" / "predictions"
                    directory.mkdir(parents=True, exist_ok=True)
                    prefix = "fixture"
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
                output, seeds=[202, 101], samples_per_seed=2
            )
            jsonschema.Draft202012Validator(CONFIDENCE_SCHEMA).validate(confidence)
            self.assertEqual(
                [result["structure"]["filename"] for result in confidence["results"]],
                [
                    "dataset/fixture/seed_202/predictions/fixture_sample_0.cif",
                    "dataset/fixture/seed_202/predictions/fixture_sample_1.cif",
                    "dataset/fixture/seed_101/predictions/fixture_sample_0.cif",
                    "dataset/fixture/seed_101/predictions/fixture_sample_1.cif",
                ],
            )
            self.assertEqual(confidence["seeds"], [202, 101])
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
            missing = output / "dataset/fixture/seed_202/predictions/fixture_summary_confidence_sample_1.json"
            missing.unlink()
            with self.assertRaisesRegex(SystemExit, "exact seed/sample product"):
                module._write_confidence(output, seeds=[202, 101], samples_per_seed=2)
            missing.write_text(
                json.dumps({"plddt": 89.0, "ptm": 0.8, "iptm": 0.7, "ranking_score": 0.75}),
                encoding="utf-8",
            )
            bad = output / "dataset/fixture/seed_101/predictions/fixture_summary_confidence_sample_0.json"
            bad.write_text(
                json.dumps({"plddt": float("nan"), "ptm": 0.8, "iptm": 0.7, "ranking_score": 0.75}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "finite scalar"):
                module._write_confidence(output, seeds=[202, 101], samples_per_seed=2)

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
    directory = out / 'dataset' / 'fixture' / f'seed_{seed}' / 'predictions'
    directory.mkdir(parents=True, exist_ok=True)
    for sample in range(samples):
        prefix = 'fixture'
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
            marker_path = root / "provenance.json"
            marker_path.write_text(
                json.dumps(
                    {
                        "schema": module.PROTENIX_HANDOFF_SCHEMA,
                        "artifact_id": "prepared-1",
                        "member": "processed.json",
                        "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                        "raw_input_sha256": "b" * 64,
                        "msa_mode": "none",
                        "composite_artifact_id": module.ARTIFACT_ID,
                        "composite_artifact_revision": module.ARTIFACT_REVISION,
                        "localized_content_digest_sha256": module.LOCALIZED_CONTENT_DIGEST_SHA256,
                        "composite_manifest_sha256": module.LOCALIZATION_MANIFEST_SHA256,
                        "source_revision": module.CODE_REVISION,
                    }
                ),
                encoding="utf-8",
            )
            runtime_marker = write_runtime_localization_marker(
                root / "runtime-localization-pred.json",
                model_id="protenix-v2",
                variant_id=module.VARIANT_ID,
                stage_id="sample-structure",
                artifacts=[
                    {
                        "artifact_id": module.ARTIFACT_ID,
                        "mount_path": str(module.PROTENIX_ROOT),
                        "content_sha256": module.LOCALIZED_CONTENT_DIGEST_SHA256,
                        "expected_manifest_sha256": module.LOCALIZATION_MANIFEST_SHA256,
                    }
                ],
            )
            args = types.SimpleNamespace(
                input=str(input_path),
                input_marker=str(marker_path),
                input_artifact_id="prepared-1",
                output_dir=str(output),
                msa_mode="none",
                seeds="101,202",
                sample_count=2,
                checkpoint=str(module.CHECKPOINT),
                common_dir=str(module.COMMON_DIR),
                disable_templates=True,
                disable_rna_msa=True,
                runtime_localization_marker=str(runtime_marker),
            )
            fake_torch = types.SimpleNamespace(
                cuda=types.SimpleNamespace(
                    is_available=lambda: True,
                    get_device_capability=lambda _index: (9, 0),
                )
            )
            with (
                mock.patch.object(module, "PROTENIX_CLI", str(fake_cli)),
                mock.patch.object(
                    module,
                    "_validate_artifact",
                    return_value=module.LOCALIZATION_MANIFEST_SHA256,
                ),
                mock.patch.object(module, "_validate_installed_runtime"),
                mock.patch.dict(sys.modules, {"torch": fake_torch}),
                mock.patch.dict(
                    "os.environ",
                    {
                        "TRITON_CACHE_DIR": str(triton_cache),
                        "CUEQ_TRITON_CACHE_DIR": str(cueq_cache),
                    },
                    clear=False,
                ),
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

    def test_protenix_pred_rejects_cross_artifact_or_msa_handoff_reuse(self) -> None:
        module = load_module("run_protenix")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            processed = root / "processed.json"
            processed.write_text("{}", encoding="utf-8")
            marker = {
                "schema": module.PROTENIX_HANDOFF_SCHEMA,
                "artifact_id": "prepared-1",
                "member": "processed.json",
                "sha256": hashlib.sha256(processed.read_bytes()).hexdigest(),
                "raw_input_sha256": "a" * 64,
                "msa_mode": "none",
                "composite_artifact_id": module.ARTIFACT_ID,
                "composite_artifact_revision": module.ARTIFACT_REVISION,
                "localized_content_digest_sha256": module.LOCALIZED_CONTENT_DIGEST_SHA256,
                "composite_manifest_sha256": module.LOCALIZATION_MANIFEST_SHA256,
                "source_revision": module.CODE_REVISION,
            }
            provenance = root / "provenance.json"
            runtime_marker = write_runtime_localization_marker(
                root / "runtime-localization-pred.json",
                model_id="protenix-v2",
                variant_id=module.VARIANT_ID,
                stage_id="sample-structure",
                artifacts=[
                    {
                        "artifact_id": module.ARTIFACT_ID,
                        "mount_path": str(module.PROTENIX_ROOT),
                        "content_sha256": module.LOCALIZED_CONTENT_DIGEST_SHA256,
                        "expected_manifest_sha256": module.LOCALIZATION_MANIFEST_SHA256,
                    }
                ],
            )
            values = {
                "input": str(processed),
                "input_marker": str(provenance),
                "input_artifact_id": "prepared-1",
                "output_dir": str(root / "out"),
                "msa_mode": "none",
                "seeds": "1,2",
                "sample_count": 2,
                "checkpoint": str(module.CHECKPOINT),
                "common_dir": str(module.COMMON_DIR),
                "disable_templates": True,
                "disable_rna_msa": True,
                "runtime_localization_marker": str(runtime_marker),
            }
            for field, bad_value in (
                ("msa_mode", "precomputed"),
                ("composite_manifest_sha256", "c" * 64),
                ("localized_content_digest_sha256", "d" * 64),
                ("composite_artifact_revision", "wrong-revision"),
            ):
                with self.subTest(field=field):
                    candidate = {**marker, field: bad_value}
                    provenance.write_text(json.dumps(candidate), encoding="utf-8")
                    with (
                        mock.patch.object(
                            module,
                            "_validate_artifact",
                            return_value=module.LOCALIZATION_MANIFEST_SHA256,
                        ),
                        self.assertRaisesRegex(SystemExit, "does not bind"),
                    ):
                        module._pred(types.SimpleNamespace(**values))

    def test_wrapper_parser_surfaces_accept_documented_argv_shapes(self) -> None:
        """Parser-only fixtures; real adapter tuples are verified by the external gate."""
        protenix = load_module("run_protenix")
        prep = [
            "prep", "--input", "/work/prep/input.json", "--output-dir", "/work/prep/prepared",
            "--processed-json", "/work/prep/prepared/processed.json",
            "--provenance-marker", "/work/prep/prepared/provenance.json",
            "--handoff-tar", "/work/prep/prepared.tar.zst", "--output-artifact-id", "protenix-stage-1",
            "--msa-mode", "none", "--reference-root", "/models/protenix-v2",
            "--reference-manifest", "/models/protenix-v2/manifest.json",
            "--runtime-localization-marker", "/work/prep/.fs2/runtime-localization.json",
        ]
        pred = [
            "pred", "--input", "/work/pred/input/processed.json", "--input-marker", "/work/pred/input/provenance.json",
            "--input-artifact-id", "protenix-stage-1", "--output-dir", "/work/pred/outputs",
            "--checkpoint", "/models/protenix-v2/checkpoint/protenix-v2.pt",
            "--common-dir", "/models/protenix-v2/common", "--msa-mode", "none", "--seeds", "101,202",
            "--sample-count", "2", "--disable-templates", "--disable-rna-msa",
            "--runtime-localization-marker", "/work/pred/.fs2/runtime-localization.json",
        ]
        with mock.patch.object(protenix, "_prep") as handler:
            protenix.main(prep)
            self.assertEqual(handler.call_args.args[0].msa_mode, "none")
        with mock.patch.object(protenix, "_pred") as handler:
            protenix.main(pred)
            parsed = handler.call_args.args[0]
            self.assertEqual((parsed.seeds, parsed.sample_count), ("101,202", 2))
            self.assertTrue(parsed.disable_templates and parsed.disable_rna_msa)

    def test_relocatable_archive_has_only_runtime_owned_canonical_members(self) -> None:
        handoff = load_module("handoff_contract")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            processed = root / "export" / "processed.json"
            marker = root / "export" / "provenance.json"
            archive = root / "handoff.tar.zst"
            processed.parent.mkdir(parents=True)
            processed.write_text('{"modelSeeds":[11,29]}\n', encoding="utf-8")
            marker.write_text(
                '{"schema":"runtime-specific-provenance-fixture"}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(
                SystemExit, "distinct from every archive member"
            ):
                handoff.write_archive(
                    processed,
                    {"processed.json": processed, "provenance.json": marker},
                )
            self.assertEqual(
                processed.read_text(encoding="utf-8"),
                '{"modelSeeds":[11,29]}\n',
            )
            handoff.write_archive(
                archive,
                {"processed.json": processed, "provenance.json": marker},
            )
            raw_tar = root / "handoff.tar"
            subprocess.run(["zstd", "-q", "-d", "-o", str(raw_tar), str(archive)], check=True)
            with tarfile.open(raw_tar) as bundle:
                self.assertEqual(bundle.getnames(), ["processed.json", "provenance.json"])
                self.assertNotIn(str(root), " ".join(bundle.getnames()))
            relocated = root / "relocated"
            relocated.mkdir()
            with tarfile.open(raw_tar) as bundle:
                bundle.extractall(relocated, filter="data")
            self.assertEqual(
                (relocated / "processed.json").read_bytes(), processed.read_bytes()
            )
            self.assertFalse(hasattr(handoff, "write_handoff"))
            self.assertFalse(hasattr(handoff, "validate_handoff"))

    def test_openfold_prepare_emits_exact_materialized_query_handoff(self) -> None:
        module = load_module("run_openfold3")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "input.json"
            raw.write_text(
                json.dumps({
                    "queries": {"fixture": {"chains": [{
                        "molecule_type": "protein", "chain_ids": "A", "sequence": "ACDE"
                    }]}}
                }),
                encoding="utf-8",
            )
            base = root / "runner-base.yaml"
            base.write_text(
                "experiment_settings:\n"
                "  mode: predict\n"
                "  seeds: [42]\n"
                "  use_msa_server: false\n"
                "  use_templates: false\n",
                encoding="utf-8",
            )
            prepared = root / "prepared"
            archive = root / "prepared.tar.zst"
            args = types.SimpleNamespace(
                input_manifest=str(raw),
                query_json=str(prepared / "query.json"),
                base_runner_yaml=str(base),
                runner_yaml=str(prepared / "runner.yaml"),
                provenance_marker=str(prepared / "provenance.json"),
                handoff_tar=str(archive),
                output_artifact_id="openfold-stage-1",
                raw_input_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
                msa_mode="none",
                model_seeds="7,9",
                offline=True,
            )
            with (
                mock.patch.object(module, "CANONICAL_BASE_RUNNER", base),
                mock.patch.object(module, "BASE_RUNNER_SHA256", hashlib.sha256(base.read_bytes()).hexdigest()),
                redirect_stdout(io.StringIO()),
            ):
                module._prepare(args)
            marker = json.loads((prepared / "provenance.json").read_text())
            self.assertEqual(marker["member"], "query.json")
            self.assertEqual(marker["model_seeds"], [7, 9])
            self.assertEqual(marker["raw_input_sha256"], hashlib.sha256(raw.read_bytes()).hexdigest())
            self.assertEqual(marker["lane_id"], "openfold3-openbind-0-none")
            query = json.loads((prepared / "query.json").read_text())
            self.assertEqual(query["seeds"], [7, 9])
            self.assertFalse(query["queries"]["fixture"]["use_msas"])
            self.assertFalse(query["queries"]["fixture"]["use_main_msas"])
            self.assertFalse(query["queries"]["fixture"]["use_paired_msas"])
            chain = query["queries"]["fixture"]["chains"][0]
            self.assertNotIn("use_msas", chain)
            self.assertEqual(
                yaml.safe_load((prepared / "runner.yaml").read_text())["experiment_settings"]["seeds"],
                [7, 9],
            )
            raw_tar = root / "prepared.tar"
            subprocess.run(["zstd", "-q", "-d", "-o", str(raw_tar), str(archive)], check=True)
            with tarfile.open(raw_tar) as bundle:
                self.assertEqual(bundle.getnames(), ["query.json", "provenance.json"])

    def test_generated_openfold_argv_is_offline_and_public_ccd_api_is_used(self) -> None:
        wrapper = ROOT / "run_openfold3.py"
        module = load_module("run_openfold3")
        image = next(item for item in LOCK["images"] if item["id"] == "openfold3")
        self.assertNotIn(
            "database-dir", image["runtime_contract"]["commands"][0]
        )
        prepare_argv = [
            "prepare", "--input-manifest", "/work/input.json",
            "--query-json", "/work/prepared/query.json",
            "--base-runner-yaml", "/opt/fs2/runtime/openfold3/runner-base.yaml",
            "--runner-yaml", "/work/prepared/runner.yaml", "--msa-mode", "none",
            "--provenance-marker", "/work/prepared/provenance.json",
            "--handoff-tar", "/work/prepared.tar.zst", "--output-artifact-id", "openfold-stage-1",
            "--raw-input-sha256", "a" * 64,
            "--model-seeds", "101,202", "--offline",
        ]
        predict_argv = [
            "predict", "--query-json", "/work/input/query.json",
            "--provenance-marker", "/work/input/provenance.json", "--input-artifact-id", "openfold-stage-1",
            "--expected-raw-input-sha256", "a" * 64, "--output-dir", "/work/outputs",
            "--checkpoint", "/models/openfold3/of3-ob-2025-06-30-174k.pt",
            "--ccd-path", "/databases/openfold3/components.bcif",
            "--runner-yaml", "/work/runner.yaml", "--base-runner-yaml", "/opt/fs2/runtime/openfold3/runner-base.yaml",
            "--num-diffusion-samples", "1",
            "--num-model-seeds", "2", "--model-seeds", "101,202",
            "--msa-mode", "none", "--use-templates", "false",
            "--runtime-localization-marker", "/work/.fs2/runtime-localization.json",
        ]
        with mock.patch.object(module, "_prepare") as prepare_handler:
            module.main(prepare_argv)
            parsed = prepare_handler.call_args.args[0]
            self.assertEqual(parsed.model_seeds, "101,202")
            self.assertTrue(parsed.offline)
            self.assertEqual(parsed.output_artifact_id, "openfold-stage-1")
        with mock.patch.object(module, "_predict") as predict_handler:
            module.main(predict_argv)
            parsed = predict_handler.call_args.args[0]
            self.assertEqual(parsed.num_model_seeds, 2)
            self.assertEqual(parsed.num_diffusion_samples, 1)
            self.assertEqual(parsed.use_templates, "false")
        argv = module.build_command(
            query=Path("/work/query.json"), output=Path("/work/outputs"),
            checkpoint=Path("/models/openfold3/of3-ob-2025-06-30-174k.pt"),
            runner_yaml=Path("/work/runner.yaml"), num_diffusion_samples=1,
        )
        self.assertEqual(argv[:2], ["run_openfold", "predict"])
        self.assertIn("--inference-ckpt-path", argv)
        self.assertEqual(argv[argv.index("--use-msa-server") + 1], "false")
        self.assertEqual(argv[argv.index("--use-templates") + 1], "false")
        self.assertNotIn("--num-model-seeds", argv)
        source = wrapper.read_text(encoding="utf-8")
        self.assertIn("ccd.set_ccd_path(ccd_path)", source)
        self.assertNotIn("._CCD_FILE", source)
        self.assertIn("standalone_mode=False", source)

    def test_openfold_two_seed_one_sample_confidence_binds_structures_and_bounds(self) -> None:
        module = load_module("run_openfold3")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for seed in (101, 202):
                directory = output / "query" / f"seed_{seed}"
                directory.mkdir(parents=True)
                for sample in (1,):
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
                output, seeds=[101, 202], samples_per_seed=1
            )
            jsonschema.Draft202012Validator(CONFIDENCE_SCHEMA).validate(envelope)
            self.assertEqual(envelope["schema"], "fs2.nebius.ai/structure-confidence/v1")
            self.assertEqual(len(envelope["results"]), 2)
            self.assertEqual([result["sample_index"] for result in envelope["results"]], [0, 0])
            encoded = (output / "confidence.json").read_text(encoding="utf-8")
            self.assertNotIn("per_atom", encoded)
            self.assertNotIn(str(output), encoded)
            bad = output / "query" / "seed_101" / "query_seed_101_sample_1_confidences_aggregated.json"
            bad.write_text(
                json.dumps({"avg_plddt": 88.0, "gpde": float("inf")}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "finite scalar"):
                module._write_confidence(output, seeds=[101, 202], samples_per_seed=1)

    def test_openfold_multisample_cardinality_and_one_based_normalization(self) -> None:
        module = load_module("run_openfold3")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for seed in (7, 9):
                directory = output / "query" / f"seed_{seed}"
                directory.mkdir(parents=True)
                for one_based in (1, 2):
                    prefix = f"query_seed_{seed}_sample_{one_based}"
                    (directory / f"{prefix}_model.cif").write_text(
                        "ATOM 1 A 0 0 0\nATOM 2 A 1 0 0\nATOM 3 A 2 0 0\n",
                        encoding="utf-8",
                    )
                    (directory / f"{prefix}_confidences_aggregated.json").write_text(
                        '{"avg_plddt":88.0,"gpde":4.0}', encoding="utf-8"
                    )
            envelope = module._write_confidence(output, seeds=[7, 9], samples_per_seed=2)
            self.assertEqual(
                [(item["seed"], item["sample_index"]) for item in envelope["results"]],
                [(7, 0), (7, 1), (9, 0), (9, 1)],
            )
            duplicate = output / "query/seed_7/query_seed_7_sample_01_confidences_aggregated.json"
            duplicate.write_text('{"avg_plddt":88.0,"gpde":4.0}', encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "(canonical|exact seed/sample|one-to-one)"):
                module._write_confidence(output, seeds=[7, 9], samples_per_seed=2)

    def test_semantic_smoke_validates_exact_envelope_atoms_metrics_and_cardinality(self) -> None:
        contract = load_module("result_contract")
        smoke = load_module("image_smoke")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            structure = output / "result.cif"
            structure.write_text(
                "".join(f"ATOM {index} A {index} 0 0\n" for index in range(1, 11)),
                encoding="utf-8",
            )
            envelope = contract.write_confidence_envelope(
                output,
                runtime_id="esmfold2",
                model_revision="fixture",
                seeds=[5],
                samples_per_seed=1,
                results=[{
                    "seed": 5, "sample_index": 0, "structure": structure,
                    "summary": None, "metrics": {"plddt_mean": 1.0},
                }],
            )
            confidence_path, validated, structures = smoke._validate_semantic_output(
                output, runtime_id="esmfold2", seeds=[5], samples_per_seed=1
            )
            self.assertEqual(confidence_path, output / "confidence.json")
            self.assertEqual(validated, envelope)
            self.assertEqual(structures, [structure])

            bad = json.loads((output / "confidence.json").read_text())
            bad["results"][0]["metrics"]["plddt_mean"] = 1.0001
            (output / "confidence.json").write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "finite scalar"):
                smoke._validate_semantic_output(
                    output, runtime_id="esmfold2", seeds=[5], samples_per_seed=1
                )

            (output / "confidence.json").write_text(json.dumps(envelope), encoding="utf-8")
            structure.write_text("JUNK\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "bind its structure bytes"):
                smoke._validate_semantic_output(
                    output, runtime_id="esmfold2", seeds=[5], samples_per_seed=1
                )
            envelope["results"][0]["structure"]["bytes"] = structure.stat().st_size
            envelope["results"][0]["structure"]["sha256"] = hashlib.sha256(structure.read_bytes()).hexdigest()
            (output / "confidence.json").write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "fewer than 10 atom"):
                smoke._validate_semantic_output(
                    output, runtime_id="esmfold2", seeds=[5], samples_per_seed=1
                )

            with self.assertRaisesRegex(SystemExit, "exact seed/sample product"):
                contract.write_confidence_envelope(
                    output,
                    runtime_id="esmfold2",
                    model_revision="fixture",
                    seeds=[5, 6],
                    samples_per_seed=1,
                    results=[{
                        "seed": 5, "sample_index": 0, "structure": structure,
                        "summary": None, "metrics": {"plddt_mean": 0.5},
                    }],
                )

    def test_esm_production_defaults_and_non_hopper_flash_disable(self) -> None:
        source = (ROOT / "run_esmfold2.py").read_text(encoding="utf-8")
        self.assertIn('default=20', source)
        self.assertIn('default=200', source)
        self.assertIn('"--smoke"', source)
        self.assertIn("esmfold2_layers.FLASH_ATTN_AVAILABLE = False", source)
        self.assertIn('attention = "flash_attention_2" if args.hardware_mode == "h100" else "sdpa"', source)
        self.assertIn('confidence_path = output.parent / "confidence.json"', source)
        self.assertIn('"plddt_mean"', source)
        self.assertRegex(source, r'"plddt_mean",\s*float\(.+?\),\s*0\.0,\s*1\.0,',)
        self.assertIn("write_confidence_envelope", source)
        self.assertNotIn(".cpu().tolist()", source)
        self.assertIn("ccd_path.stat().st_size != CCD_BYTES", source)
        self.assertNotIn("_sha256(ccd_path)", source)
        self.assertNotIn("import hashlib", source)
        self.assertFalse(hasattr(load_module("run_esmfold2"), "_sha256"))
        metric_schema = CONFIDENCE_SCHEMA["properties"]["results"]["items"]
        metric_schema = metric_schema["properties"]["metrics"]["properties"]["plddt_mean"]
        self.assertEqual(metric_schema, {"type": "number", "minimum": 0, "maximum": 1})

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
                self.assertIn(
                    "runtime_localization.py /usr/local/bin/runtime_localization.py",
                    dockerfile,
                )
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
        self.assertIn("command -v gcc", runtime_stage)
        self.assertIn("ca-certificates gcc postgresql-client", runtime_stage)
        self.assertIn('unsupported-pinned-pytorch-cu126', protenix)
        self.assertEqual(
            protenix.count('importlib.metadata.version("protenix") == "2.0.0"'),
            1,
        )
        esm = (ROOT / "Dockerfile.esmfold2").read_text(encoding="utf-8")
        self.assertEqual(esm.count('importlib.metadata.version("esm") == "3.4.0"'), 1)
        smoke_source = (ROOT / "image_smoke.py").read_text(encoding="utf-8")
        self.assertIn('result["package_version"] != "3.4.0"', smoke_source)
        self.assertIn('result["package_version"] != "2.0.0"', smoke_source)
        of3 = (ROOT / "Dockerfile.openfold3").read_text(encoding="utf-8")
        self.assertNotIn("prepare_openfold3.py", of3)
        self.assertIn("run_openfold3.py /usr/local/bin/fs2-run-openfold3", of3)
        self.assertIn("openfold3-runner-base.yaml /opt/fs2/runtime/openfold3/runner-base.yaml", of3)
        self.assertIn("libaio-dev zstd", of3)

    def test_protenix_fast_layernorm_is_prebuilt_but_triton_jit_is_truthful(self) -> None:
        compiler = (ROOT / "protenix-torch-ext-compile.py").read_text(encoding="utf-8")
        self.assertIn('"arch=compute_90,code=sm_90"', compiler)
        self.assertIn('"arch=compute_90,code=compute_90"', compiler)
        self.assertNotRegex(compiler, r"compute_(70|80|86|89|100)")
        dockerfile = (ROOT / "Dockerfile.protenix-v2").read_text(encoding="utf-8")
        self.assertIn("verify_protenix_offline_prep.py", dockerfile)
        self.assertIn('rm -rf "${package_root}/model/layer_norm/kernel"', dockerfile)
        self.assertIn('"${package_root}/model/layer_norm/torch_ext_compile.py"', dockerfile)
        self.assertIn("env -C / /opt/protenix-venv/bin/python -c", dockerfile)
        self.assertIn("TRITON_CACHE_DIR=/cache/protenix/triton", dockerfile)
        self.assertIn("CUEQ_TRITON_CACHE_DIR=/cache/protenix/cueq-triton", dockerfile)
        self.assertIn("TORCH_EXTENSIONS_DIR=/cache/protenix/torch-extensions", dockerfile)
        self.assertIn("XDG_CACHE_HOME=/cache/protenix/xdg", dockerfile)
        self.assertIn("/cache/protenix/torch-extensions", dockerfile)
        self.assertIn("/cache/protenix/xdg", dockerfile)
        self.assertIn("chown -R 10001:10001 /models /outputs /cache/protenix", dockerfile)
        smoke = (ROOT / "image_smoke.py").read_text(encoding="utf-8")
        self.assertIn("active-triton-jit-first-shape-then-cache", smoke)
        self.assertIn('launcher_compiler = shutil.which("gcc") or shutil.which("clang")', smoke)
        self.assertIn("_probe_python_launcher_compiler", smoke)
        self.assertIn('"launcher_probe": "bounded-python-extension-compile-passed"', smoke)
        self.assertIn('"nvcc": "absent"', smoke)
        self.assertNotIn('result["runtime_jit"] = "disabled"', smoke)

    def test_protenix_source_patch_rejects_online_preprocessing(self) -> None:
        patch_source = (ROOT / "patch_protenix_source.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("FS2_MSA_MODE")', patch_source)
        self.assertIn("use_msa=False", patch_source)
        self.assertIn("use_template=False", patch_source)
        self.assertIn("use_rna_msa=False", patch_source)
        self.assertNotIn("FS2_PUBLIC_MSA_OPT_IN", patch_source)

    def test_openfold_preparation_binds_offline_msa_mode(self) -> None:
        module = load_module("run_openfold3")
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
        query = document["queries"]["fixture"]
        chain = query["chains"][0]
        self.assertIs(query["use_msas"], False)
        self.assertIs(query["use_main_msas"], False)
        self.assertIs(query["use_paired_msas"], False)
        self.assertNotIn("use_msas", chain)
        for field in module.EXTERNAL_CHAIN_FIELDS:
            with self.subTest(field=field):
                external = {
                    "queries": {
                        "fixture": {
                            "chains": [
                                {
                                    "molecule_type": "protein",
                                    "chain_ids": "A",
                                    "sequence": "ACDE",
                                    field: "/producer/absolute/path",
                                }
                            ]
                        }
                    }
                }
                with self.assertRaisesRegex(SystemExit, field):
                    module._bind_msa_mode(external, "none")
        with self.assertRaisesRegex(SystemExit, "only the fail-closed"):
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
        with mock.patch.object(module, "_prepare") as handler:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                module.main([
                    "prepare", "--input-manifest", "/i", "--query-json", "/q",
                    "--base-runner-yaml", "/b", "--runner-yaml", "/r",
                    "--provenance-marker", "/p", "--handoff-tar", "/t",
                    "--output-artifact-id", "id", "--raw-input-sha256", "a" * 64,
                    "--msa-mode", "precomputed", "--model-seeds", "1", "--offline",
                ])
            handler.assert_not_called()

    def test_smoke_defaults_to_semantic_and_build_mode_is_explicit(self) -> None:
        source = (ROOT / "image_smoke.py").read_text(encoding="utf-8")
        self.assertIn("--build-only", source)
        self.assertIn("exact-artifact-h100-semantic", source)
        self.assertIn("semantic smoke requires --semantic-request", source)
        self.assertIn("--runtime-localization-marker", source)
        self.assertIn("MIN_ATOM_RECORDS = 10", source)
        self.assertNotIn(
            'runtime_id in {"esmfold2", "esmfold2-fast", "protenix-v2"}:\n'
            "        expected_seeds",
            source,
        )
        publisher = (ROOT / "build-and-publish.sh").read_text(encoding="utf-8")
        self.assertIn('"${runtime_dir}/check.sh"', publisher)
        self.assertIn("fs2-image-smoke --build-only", publisher)
        self.assertIn("expected_cache_mounts", publisher)
        self.assertIn("expected_cache_environment", publisher)
        self.assertIn(".build_cache.effective_uid == 10001", publisher)
        self.assertIn(".build_cache.mount_roots == $expected_cache_mounts", publisher)
        self.assertIn(
            ".build_cache.environment == $expected_cache_environment", publisher
        )
        self.assertIn("bounded-create-read-remove-passed", publisher)
        self.assertLess(
            publisher.index("fs2-image-smoke --build-only"),
            publisher.index('docker push "$target"'),
        )

    def test_publisher_consumes_v2_repository_tag_and_configurable_registry(self) -> None:
        script = (ROOT / "build-and-publish.sh").read_text(encoding="utf-8")
        self.assertIn("--registry-root", script)
        self.assertIn("FS2_REGISTRY_ROOT", script)
        self.assertIn("--adapter-worktree", script)
        self.assertIn("FS2_RUNTIME_ADAPTER_WORKTREE", script)
        self.assertIn("verify_runtime_adapter_contract.py", script)
        self.assertIn("structure-secondary-image-build-receipt/v2", script)
        self.assertIn("image_source_revision:$image_source_revision", script)
        self.assertIn("origin_main_revision:$origin_main_revision", script)
        self.assertIn("merge_base_revision:$merge_base_revision", script)
        self.assertIn("runtime_adapter_revision:$runtime_adapter_revision", script)
        self.assertIn("runtime_adapter_branch:$runtime_adapter_branch", script)
        verifier = (ROOT / "tests/verify_runtime_adapter_contract.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"ls-remote"', verifier)
        self.assertIn("adapter commit is not the exact clean pushed branch head", verifier)
        self.assertIn(".repository", script)
        self.assertIn(".tag", script)
        self.assertNotIn("'.target'", script)
        self.assertGreaterEqual(script.count('target_state "$target"'), 2)
        self.assertIn("refusing to overwrite existing target", script)
        self.assertIn("refusing raced overwrite", script)
        self.assertNotIn(":latest", script)
        self.assertNotIn("docker login", script)
        self.assertIn(
            "refs/heads/main:refs/remotes/origin/main", script
        )
        self.assertIn("merge-base --is-ancestor", script)
        self.assertIn("refusing stale-base build", script)
        self.assertIn("refusing uncommitted-source build", script)
        self.assertLess(
            script.index("merge-base --is-ancestor"),
            script.index('"${runtime_dir}/check.sh"'),
        )
        self.assertLess(
            script.index("merge-base --is-ancestor"),
            script.index("docker buildx build"),
        )
        self.assertLess(
            script.index("merge-base --is-ancestor"),
            script.index('docker push "$target"'),
        )
        blocked = subprocess.run(
            [
                str(ROOT / "build-and-publish.sh"),
                "--adapter-worktree",
                str(ROOT / "does-not-exist"),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("concrete runtime adapter worktree is required", blocked.stdout)
        self.assertNotIn("SOURCE id=", blocked.stdout)

    def test_shell_entrypoints_are_syntactically_valid(self) -> None:
        for name in (
            "entrypoint.sh",
            "entrypoint-openfold3.sh",
            "build-and-publish.sh",
            "check.sh",
        ):
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
        self.assertEqual(len(superseded), 3)
        self.assertTrue(all(item["deployable"] is False for item in superseded))
        self.assertTrue(all(item["digest"].startswith("sha256:") for item in superseded))


if __name__ == "__main__":
    unittest.main()
