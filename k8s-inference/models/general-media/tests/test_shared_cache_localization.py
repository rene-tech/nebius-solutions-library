from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_ROOT = ROOT / "k8s"
EVIDENCE_ROOT = ROOT / "evidence"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/shared-cache-localization-receipt/v2"


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
        + b"\n"
    )


def documents(name: str) -> list[dict[str, object]]:
    return [
        item
        for item in yaml.safe_load_all((MANIFEST_ROOT / name).read_text(encoding="utf-8"))
        if item is not None
    ]


def localization_config(name: str) -> dict[str, object]:
    return next(
        item
        for item in documents(name)
        if item["kind"] == "ConfigMap" and "localize.py" in item["data"]
    )


class SharedCacheLocalizationTests(unittest.TestCase):
    def test_qwen_and_cosmos_bind_exact_content_addresses(self) -> None:
        expected = {
            "qwen3-8b.yaml": {
                "model_id": "qwen3-8b",
                "content_digest": "5b0f0f64ddb02ee2deeed4772968b9e2139a922acc9b9bb9c3488d23c678971d",
                "manifest_digest": "2cf721c69d9e1b66860274de129f0dd486172ef1dad289483ea891dab5b80806",
                "revision": "b968826d9c46dd6066d109eabc6255188de91218",
                "mount": "/models",
            },
            "cosmos3-nano.yaml": {
                "model_id": "cosmos3-nano",
                "content_digest": "dfa7b03382ba78d7f80703652706c3cfa777cefac48634df49345c4302af2c95",
                "manifest_digest": "49b4bf03d84857892b86366ef2f6c3b68ac575cb1a3f2de4122c41e638f4cc6e",
                "revision": "7a312c868bcce8e40b3eb40861300a9d0ba3fde1",
                "mount": "/model-cache",
            },
        }
        scripts = []
        for filename, identity in expected.items():
            resources = documents(filename)
            config = localization_config(filename)
            scripts.append(config["data"]["localize.py"])
            compile(config["data"]["localize.py"], f"{filename}:localize.py", "exec")
            lock = json.loads(config["data"]["model.lock.json"])
            self.assertEqual(2, lock["schema_version"])
            model = lock["model"]
            self.assertEqual(identity["model_id"], model["id"])
            self.assertEqual(identity["content_digest"], model["content_digest"])
            self.assertEqual(
                identity["manifest_digest"], model["artifact_manifest_digest"]
            )
            self.assertEqual(identity["revision"], model["revision"])
            normalized = [
                {
                    "path": item["path"],
                    "bytes": item["size"],
                    "sha256": item["sha256"],
                }
                for item in model["files"]
            ]
            self.assertEqual(
                identity["content_digest"],
                hashlib.sha256(canonical_bytes(normalized)).hexdigest(),
            )
            self.assertEqual(
                model["total_size_bytes"], sum(item["size"] for item in model["files"])
            )

            deployment = next(item for item in resources if item["kind"] == "Deployment")
            pod = deployment["spec"]["template"]["spec"]
            localizer = next(
                item for item in pod["initContainers"] if item["name"] == "localize-model"
            )
            runtime = pod["containers"][0]
            content_path = (
                f"{identity['mount']}/{identity['model_id']}/sha256/"
                f"{identity['content_digest']}/payload"
            )
            self.assertIn(content_path, runtime["args"])
            runtime_mount = next(
                item
                for item in runtime["volumeMounts"]
                if item["mountPath"] == identity["mount"]
            )
            self.assertTrue(runtime_mount["readOnly"])
            localizer_mount = next(
                item
                for item in localizer["volumeMounts"]
                if item["mountPath"] == identity["mount"]
            )
            self.assertFalse(localizer_mount.get("readOnly", False))
            self.assertEqual(
                RECEIPT_SCHEMA,
                deployment["spec"]["template"]["metadata"]["annotations"][
                    "fs2.nebius/localization-receipt-schema"
                ],
            )
        self.assertEqual(scripts[0], scripts[1])

    def test_cosmos_lock_is_the_reviewed_artifact_manifest(self) -> None:
        evidence = json.loads(
            (EVIDENCE_ROOT / "cosmos3-nano-artifact-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        lock = json.loads(
            localization_config("cosmos3-nano.yaml")["data"]["model.lock.json"]
        )["model"]
        self.assertEqual(
            "49b4bf03d84857892b86366ef2f6c3b68ac575cb1a3f2de4122c41e638f4cc6e",
            hashlib.sha256(canonical_bytes(evidence)).hexdigest(),
        )
        self.assertEqual(evidence["source"]["revision"], lock["revision"])
        self.assertEqual(evidence["content"]["digest"], lock["content_digest"])
        self.assertEqual(evidence["content"]["expanded_bytes"], lock["total_size_bytes"])
        self.assertEqual(
            evidence["content"]["files"],
            [
                {
                    "path": item["path"],
                    "bytes": item["size"],
                    "sha256": item["sha256"],
                }
                for item in lock["files"]
            ],
        )

    def test_one_writer_publishes_and_followers_take_receipt_fast_path(self) -> None:
        script = localization_config("qwen3-8b.yaml")["data"]["localize.py"]
        payloads = {"config.json": b"one", "weights.safetensors": b"two" * 1024}
        files = [
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in sorted(payloads.items())
        ]
        normalized = [
            {"path": item["path"], "bytes": item["size"], "sha256": item["sha256"]}
            for item in files
        ]
        content_digest = hashlib.sha256(canonical_bytes(normalized)).hexdigest()
        contract = {
            "schema_version": 2,
            "model": {
                "id": "fixture",
                "repo_id": "example/fixture",
                "revision": "a" * 40,
                "artifact_manifest_digest": "b" * 64,
                "content_digest": content_digest,
                "total_size_bytes": sum(len(value) for value in payloads.values()),
                "files": files,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "model.lock.json"
            lock_path.write_text(json.dumps(contract), encoding="utf-8")
            calls = 0
            calls_lock = threading.Lock()

            def snapshot_download(**kwargs: object) -> str:
                nonlocal calls
                self.assertEqual("example/fixture", kwargs["repo_id"])
                self.assertEqual("a" * 40, kwargs["revision"])
                self.assertFalse(kwargs["token"])
                self.assertTrue(kwargs["local_files_only"])
                with calls_lock:
                    calls += 1
                time.sleep(0.1)
                destination = (
                    Path(kwargs["cache_dir"])
                    / "models--example--fixture"
                    / "snapshots"
                    / ("a" * 40)
                )
                for name, content in payloads.items():
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                return str(destination)

            fake_huggingface = types.ModuleType("huggingface_hub")
            fake_huggingface.snapshot_download = snapshot_download
            environment = {
                "FS2_MODEL_LOCK_PATH": str(lock_path),
                "FS2_CACHE_ROOT": str(root / "cache"),
                "FS2_LOCALIZATION_WORKERS": "2",
                "FS2_CACHE_LOCK_TIMEOUT_SECONDS": "5",
            }
            namespace: dict[str, object] = {"__name__": "localizer_under_test"}
            with patch.dict(os.environ, environment, clear=False), patch.dict(
                sys.modules, {"huggingface_hub": fake_huggingface}
            ):
                exec(script, namespace)
                namespace["print"] = lambda *_args, **_kwargs: None
                errors: list[BaseException] = []

                def run() -> None:
                    try:
                        self.assertEqual(0, namespace["main"]())
                    except BaseException as error:  # surfaced in the owning test thread
                        errors.append(error)

                threads = [threading.Thread(target=run) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual([], errors)
                self.assertEqual(1, calls)

                content_root = (
                    root / "cache" / "fixture" / "sha256" / content_digest
                )
                self.assertFalse((content_root / "payload" / ".cache").exists())
                receipt = json.loads(
                    (content_root / "localization-receipt.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(RECEIPT_SCHEMA, receipt["schema"])
                self.assertEqual("a" * 40, receipt["revision"])
                self.assertEqual("b" * 64, receipt["artifact_manifest_digest"])
                self.assertEqual(content_digest, receipt["content_digest"])

                def forbidden(*_args: object, **_kwargs: object) -> object:
                    raise AssertionError("warm receipt path must not download or hash")

                namespace["snapshot_download"] = forbidden
                namespace["safe_sha256"] = forbidden
                self.assertEqual(0, namespace["main"]())


if __name__ == "__main__":
    unittest.main()
