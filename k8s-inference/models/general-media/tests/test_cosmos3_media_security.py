"""SAI-23 source regressions for the embedded Cosmos media adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

import yaml
from pydantic import TypeAdapter, ValidationError


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "cosmos3-nano.yaml"


def adapter_source() -> tuple[str, list[dict[str, object]]]:
    documents = [item for item in yaml.safe_load_all(MANIFEST.read_text()) if item]
    config = next(
        item
        for item in documents
        if item["kind"] == "ConfigMap" and "adapter.py" in item["data"]
    )
    return config["data"]["adapter.py"], documents


def load_adapter() -> dict[str, object]:
    source, _ = adapter_source()
    namespace: dict[str, object] = {"__name__": "cosmos3_media_security_test"}
    exec(compile(source, "cosmos3-nano.yaml#adapter.py", "exec"), namespace)  # noqa: S102
    return namespace


def data_url(media_type: str, content: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}"


PNG = b"\x89PNG\r\n\x1a\nsynthetic"
MP4 = b"\x00\x00\x00\x18ftypmp42synthetic"


class _Response:
    status_code = 200
    headers = {"content-type": "video/mp4", "content-length": str(len(MP4))}

    async def aiter_bytes(self):
        yield MP4


class _Stream:
    async def __aenter__(self):
        return _Response()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def stream(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _Stream()


class CosmosMediaSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.adapter = load_adapter()
        cls.request = TypeAdapter(cls.adapter["GenerateRequest"])

    def test_all_five_qualified_modes_parse_with_materialized_media(self) -> None:
        payloads = (
            {"mode": "text-to-image", "prompt": "Synthetic image"},
            {"mode": "text-to-video", "prompt": "Synthetic video"},
            {
                "mode": "image-to-video",
                "prompt": "Synthetic animation",
                "input_reference": data_url("image/png", PNG),
            },
            {
                "mode": "video-to-video",
                "prompt": "Synthetic restyle",
                "vision_path": data_url("video/mp4", MP4),
            },
            {
                "mode": "transfer-video",
                "prompt": "Synthetic transfer",
                "controls": [
                    {
                        "control_type": "depth",
                        "reference": data_url("image/png", PNG),
                    }
                ],
            },
        )

        self.assertEqual(
            [item["mode"] for item in payloads],
            [self.request.validate_python(item).mode for item in payloads],
        )

    def test_runtime_dto_rejects_every_external_locator_path(self) -> None:
        private_target = "https://10.5.0.1/"
        payloads = (
            {
                "mode": "image-to-video",
                "prompt": "Synthetic",
                "input_reference": private_target,
            },
            {
                "mode": "video-to-video",
                "prompt": "Synthetic",
                "vision_path": private_target,
            },
            {
                "mode": "transfer-video",
                "prompt": "Synthetic",
                "controls": [
                    {"control_type": "depth", "reference": private_target}
                ],
            },
        )

        for payload in payloads:
            with self.subTest(mode=payload["mode"]), self.assertRaises(ValidationError):
                self.request.validate_python(payload)

    def test_runtime_content_validation_covers_images_videos_and_controls(self) -> None:
        decode = self.adapter["decode_materialized_reference"]

        self.assertEqual(
            (PNG, "image/png"),
            decode(
                data_url("image/png", PNG),
                expected="image",
                limit=24 * 1024 * 1024,
            ),
        )
        self.assertEqual(
            (MP4, "video/mp4"),
            decode(
                data_url("video/mp4", MP4),
                expected="video",
                limit=24 * 1024 * 1024,
            ),
        )
        self.assertEqual(
            (PNG, "image/png"),
            decode(
                data_url("image/png", PNG),
                expected="image-or-video",
                limit=4 * 1024 * 1024,
            ),
        )
        with self.assertRaises(self.adapter["AdapterError"]):
            decode(
                data_url("image/png", PNG),
                expected="video",
                limit=24 * 1024 * 1024,
            )

    def test_transfer_dispatch_uses_only_materialized_bytes_and_shared_local_path(self) -> None:
        body = self.request.validate_python(
            {
                "mode": "transfer-video",
                "prompt": "Synthetic transfer",
                "input_reference": data_url("video/mp4", MP4),
                "controls": [
                    {
                        "control_type": "depth",
                        "reference": data_url("image/png", PNG),
                    }
                ],
            }
        )
        client = _Client()
        with tempfile.TemporaryDirectory() as temporary:
            self.adapter["UPSTREAM_TMP"] = Path(temporary)
            raw, media_type = asyncio.run(
                self.adapter["generate_media_video"](client, body)
            )
            self.assertEqual([], list(Path(temporary).iterdir()))

        self.assertEqual(MP4, raw)
        self.assertEqual("video/mp4", media_type)
        self.assertEqual(1, len(client.calls))
        files = client.calls[0]["files"]
        self.assertEqual(MP4, files["input_reference"][1])
        extra_params = json.loads(files["extra_params"][1])
        self.assertTrue(
            extra_params["depth"]["control_path"].startswith(
                "/cosmos-control-tmp/fs2-cosmos-control-"
            )
        )

    def test_text_image_and_video_conditioning_modes_reach_the_bounded_dispatch(self) -> None:
        cases = (
            (
                {"mode": "text-to-video", "prompt": "Synthetic text video"},
                None,
            ),
            (
                {
                    "mode": "image-to-video",
                    "prompt": "Synthetic image animation",
                    "input_reference": data_url("image/png", PNG),
                },
                PNG,
            ),
            (
                {
                    "mode": "video-to-video",
                    "prompt": "Synthetic video restyle",
                    "vision_path": data_url("video/mp4", MP4),
                },
                MP4,
            ),
        )

        with tempfile.TemporaryDirectory() as temporary:
            self.adapter["UPSTREAM_TMP"] = Path(temporary)
            for payload, expected_reference in cases:
                with self.subTest(mode=payload["mode"]):
                    body = self.request.validate_python(payload)
                    client = _Client()
                    raw, media_type = asyncio.run(
                        self.adapter["generate_media_video"](client, body)
                    )
                    self.assertEqual(MP4, raw)
                    self.assertEqual("video/mp4", media_type)
                    files = client.calls[0]["files"]
                    if expected_reference is None:
                        self.assertNotIn("input_reference", files)
                    else:
                        self.assertEqual(expected_reference, files["input_reference"][1])

    def test_manifest_shares_only_the_bounded_control_spool(self) -> None:
        _, documents = adapter_source()
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        containers = {item["name"]: item for item in pod["containers"]}
        volumes = {item["name"]: item for item in pod["volumes"]}

        for name in ("vllm-omni", "bounded-json-adapter"):
            mount = next(
                item
                for item in containers[name]["volumeMounts"]
                if item["name"] == "cosmos-control-tmp"
            )
            self.assertEqual("/cosmos-control-tmp", mount["mountPath"])
        self.assertEqual(
            "32Mi",
            volumes["cosmos-control-tmp"]["emptyDir"]["sizeLimit"],
        )


if __name__ == "__main__":
    unittest.main()
