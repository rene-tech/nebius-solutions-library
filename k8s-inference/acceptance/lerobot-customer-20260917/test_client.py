import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path
from builtins import ExceptionGroup
from types import SimpleNamespace

import httpx2
import pytest
import zstandard
from fs2_serve.models import OperationView

import client
import dataset_io

OP = "00000000-0000-4000-8000-000000000021"
UPLOAD = "00000000-0000-4000-8000-000000000022"
TOKEN = "private-test-key-must-not-appear"


@pytest.mark.parametrize("status", ["running", "succeeded", "cancelled"])
def test_actual_native_operation_dto_and_scientific_envelope(status):
    # Native GET returns this actual server DTO directly; its `operation` is
    # the action name, not the scientific status envelope's nested object.
    native = OperationView(
        id=OP,
        parent_operation_id=UPLOAD,
        tenant_id="robotics",
        principal_id="test-canary",
        token_id=UPLOAD,
        model_id="cosmos3-nano",
        model_revision="pinned-test-revision",
        protocol="native",
        operation="generate-media",
        idempotency_key="test-native-operation-dto",
        status=status,
        accepted_at="2026-09-18T02:00:00Z",
        available_at="2026-09-18T02:00:00Z",
    ).model_dump(mode="json")
    assert native["operation"] == "generate-media"
    assert client.operation(native) is native
    assert client.operation(native)["parent_operation_id"] == UPLOAD
    assert client.operation({"operation": native, "batch": {}}) is native
    assert client.operation(native)["status"] == status


def test_key_requires_0600_and_no_symlink(tmp_path):
    key = tmp_path / "key"
    key.write_text(json.dumps({"secret": TOKEN}))
    key.chmod(0o600)
    assert client.read_key(key) == TOKEN
    key.chmod(0o644)
    with pytest.raises(client.harness.AcceptanceError, match="0600"):
        client.read_key(key)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(OSError):
        client.read_key(link)


def test_atomic_journal_refuses_secret(tmp_path):
    path = tmp_path / "run.json"
    client.save(path, {"operation_id": OP}, TOKEN)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(client.harness.AcceptanceError, match="receipt_contains_secret"):
        client.save(path, {"oops": TOKEN}, TOKEN)
    assert json.loads(path.read_text()) == {"operation_id": OP}


def test_mcp_exception_group_preserves_terminal_code_without_secret():
    error = ExceptionGroup(
        "may contain private transport details",
        [
            ExceptionGroup(
                "nested", [client.harness.AcceptanceError("operation_terminal_failure")]
            )
        ],
    )
    assert client.safe_error_code(error) == "operation_terminal_failure"
    assert (
        client.safe_error_code(RuntimeError("https://secret.example?token=private"))
        == "RuntimeError"
    )


def test_directory_bundle_is_deterministic_and_input_unchanged(tmp_path):
    source = tmp_path / "dataset"
    source.mkdir()
    (source / "data").mkdir()
    (source / "data/rows").write_bytes(b"source-data")
    (source / "fs2-bundle-manifest.json").write_text("old inventory remains on input")
    before = {
        p.relative_to(source).as_posix(): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    }
    one, two = tmp_path / "one.zst", tmp_path / "two.zst"
    assert dataset_io.pack_directory(
        source, one, max_bytes=10000
    ) == dataset_io.pack_directory(source, two, max_bytes=10000)
    localized = tmp_path / "localized"
    dataset_io.extract_bounded(
        one, localized, sha256=dataset_io.sha256_file(one), max_bytes=10000
    )
    assert (localized / "data/rows").read_bytes() == b"source-data"
    assert {
        p.relative_to(source).as_posix(): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    } == before


def test_pack_refuses_symlinks_and_output_in_source(tmp_path):
    source = tmp_path / "dataset"
    source.mkdir()
    (source / "file").write_bytes(b"one")
    with pytest.raises(ValueError, match="output_inside_dataset"):
        dataset_io.pack_directory(source, source / "out", max_bytes=10000)
    (source / "link").symlink_to(source / "file")
    with pytest.raises(ValueError, match="symlink"):
        dataset_io.pack_directory(source, tmp_path / "out", max_bytes=10000)


def test_compressed_and_expanded_limits_are_independent(tmp_path):
    source = tmp_path / "dataset"
    source.mkdir()
    (source / "compressible").write_bytes(b"x" * 4096)
    result = dataset_io.pack_directory(
        source, tmp_path / "bundle", max_bytes=1000, max_expanded_bytes=8192
    )
    assert result["size_bytes"] < 1000
    with pytest.raises(ValueError, match="dataset_size_limit_exceeded"):
        dataset_io.pack_directory(
            source, tmp_path / "too-large", max_bytes=1000, max_expanded_bytes=2048
        )


@pytest.mark.parametrize(
    "name,kind,size,limit",
    [
        ("../escape", "file", 1, 100),
        ("/escape", "file", 1, 100),
        ("link", "symlink", 0, 100),
        ("large", "file", 200, 100),
    ],
)
def test_unpack_preflight_refuses_unsafe_or_oversized(
    tmp_path, name, kind, size, limit
):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = size
        if kind == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, "outside"
        tar.addfile(info, io.BytesIO(b"x" * size) if kind == "file" else None)
    archive = tmp_path / "unsafe.zst"
    archive.write_bytes(zstandard.ZstdCompressor().compress(raw.getvalue()))
    with pytest.raises(ValueError, match="bundle"):
        dataset_io.extract_bounded(
            archive,
            tmp_path / "output",
            sha256=dataset_io.sha256_file(archive),
            max_bytes=limit,
        )
    assert not (tmp_path / "output").exists()


def test_selected_video_must_change():
    with pytest.raises(ValueError, match="selected_video_unchanged"):
        dataset_io.require_visual_changes(
            {"visual_comparison": [{"selected": True, "changed_frames": 0}]}
        )
    dataset_io.require_visual_changes(
        {
            "visual_comparison": [
                {"selected": True, "changed_frames": 5},
                {"selected": False, "changed_frames": 0},
            ]
        }
    )


def test_worker_local_artifact_label_joins_exact_public_manifest():
    content = {
        "sha256": "a" * 64,
        "size_bytes": 1000,
        "media_type": "application/x-tar",
        "compression": "zstd",
    }
    variant = {
        "variant_index": 0,
        "seed": 7,
        "artifact": {"artifact_id": OP + ".variant-00", **content},
    }
    entry = {
        "semantic_type": "lerobot-v3-augmented-bundle/v1",
        "artifact": {"artifact_id": UPLOAD, **content},
    }
    assert client.published_variant(variant, entry, OP, 0, 7) == entry["artifact"]
    assert variant["artifact"]["artifact_id"] == OP + ".variant-00"
    variant["artifact"]["sha256"] = "b" * 64
    with pytest.raises(
        client.harness.AcceptanceError, match="variant_manifest_identity_mismatch"
    ):
        client.published_variant(variant, entry, OP, 0, 7)


@pytest.mark.parametrize("policy", ["lighting.json", "environment.json", "blur.json"])
def test_policies_parse_after_local_source_mapping(policy):
    value = json.loads((Path(__file__).parent / policy).read_text())
    result = dataset_io.parameters(
        value,
        {
            "artifact_id": OP,
            "sha256": "a" * 64,
            "size_bytes": 1000,
            "media_type": "application/x-tar",
            "compression": "zstd",
        },
    )
    assert result["source"]["kind"] == "uploaded-bundle"
    assert "source" not in value


def test_upload_failure_keeps_ids_and_never_retries(tmp_path):
    data = tmp_path / "data"
    data.write_bytes(b"data")
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx2.Response(
                201,
                json={
                    "operation_id": OP,
                    "upload_id": UPLOAD,
                    "content_path": f"/v1/scientific-artifacts/uploads/{UPLOAD}/content?operation_id={OP}",
                    "max_content_bytes": 100,
                },
            )
        raise httpx2.ConnectError("simulated transport failure", request=request)

    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handle), base_url="https://gateway.example"
        ) as http:
            public = client.DatasetClient(
                None, http, {"run_id": "test"}, tmp_path, TOKEN
            )
            with pytest.raises(httpx2.ConnectError):
                await public.upload_file("dataset", data, "application/x-tar", "zstd")

    asyncio.run(run())
    assert len(calls) == 2 and [r[0] for r in calls] == ["POST", "PUT"]
    journal = json.loads((tmp_path / "run.json").read_text())
    assert journal["uploads"]["dataset"]["operation_id"] == OP
    assert journal["uploads"]["dataset"]["phase"] == "content_pending"
    assert TOKEN not in (tmp_path / "run.json").read_text()


def test_submission_transports_keep_same_key(tmp_path):
    calls = []

    def handle(request):
        calls.append(
            (
                request.method,
                request.headers.get("Idempotency-Key"),
                json.loads(request.content),
            )
        )
        return httpx2.Response(202, json={"operation": {"id": OP}})

    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handle), base_url="https://gateway.example"
        ) as http:
            public = client.DatasetClient(None, http, {}, tmp_path, TOKEN)
            for _ in range(2):
                assert (
                    client.harness.operation_id(
                        await public.submit({"payload": "constant"}, "same", "http")
                    )
                    == OP
                )

    asyncio.run(run())
    assert calls == [("POST", "same", {"payload": "constant"})] * 2


def test_cancellation_waits_for_resource_release(tmp_path, monkeypatch):
    reads = []
    values = [False, True]

    async def no_wait(_):
        pass

    monkeypatch.setattr(client.asyncio, "sleep", no_wait)

    def handle(request):
        reads.append(request.method)
        return httpx2.Response(
            200,
            json={
                "operation": {"id": OP, "status": "cancelled"},
                "batch": {
                    "status": "cancelled",
                    "stages": [
                        {
                            "stage_id": "augment",
                            "status": "cancelled",
                            "attempts": [{"resource_released": values.pop(0)}],
                        }
                    ],
                },
            },
        )

    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handle), base_url="https://gateway.example"
        ) as http:
            public = client.DatasetClient(
                None, http, {"operation_id": OP}, tmp_path, TOKEN
            )
            await public.wait_existing(10, "cancelled")

    asyncio.run(run())
    assert reads == ["GET", "GET"]


def test_resume_lock_failure_does_not_overwrite_journal(tmp_path, monkeypatch):
    token_file = tmp_path / "key"
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    state = {
        "operation_id": OP,
        "key_fingerprint": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "endpoint": "https://gateway.example",
    }
    client.save(tmp_path / "run.json", state, TOKEN)
    monkeypatch.setattr(
        client.fcntl, "flock", lambda *args: (_ for _ in ()).throw(BlockingIOError())
    )
    monkeypatch.setattr(
        client.sys,
        "argv",
        [
            "client.py",
            "resume",
            "--key-file",
            str(token_file),
            "--reader-python",
            "/unused",
            "--output",
            str(tmp_path),
        ],
    )
    assert client.main() == 1
    assert json.loads((tmp_path / "run.json").read_text()) == state


@pytest.mark.parametrize("corrupt_second", [False, True])
def test_every_returned_variant_is_downloaded_and_validated(
    tmp_path, monkeypatch, corrupt_second
):
    def reference(number, media_type="application/x-tar", compression="zstd"):
        return {
            "artifact_id": f"00000000-0000-4000-8000-{number:012d}",
            "sha256": "a" * 64,
            "size_bytes": 100,
            "media_type": media_type,
            "compression": compression,
        }

    manifest = reference(101, "application/vnd.fs2.scientific-manifest+json", "none")
    result_ref = reference(102, "application/json", "none")
    variants = [reference(103), reference(104)]
    entries = [
        {
            "name": "result",
            "semantic_type": "lerobot-augmentation-result/v1",
            "artifact": result_ref,
        }
    ]
    result = {
        "schema": "fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-result/v1",
        "operation_id": OP,
        "status": "succeeded",
        "failures": [],
        "variants": [],
    }
    for index, artifact in enumerate(variants):
        entries.append(
            {
                "name": f"variant-{index:02d}",
                "semantic_type": "lerobot-v3-augmented-bundle/v1",
                "artifact": artifact,
            }
        )
        result["variants"].append(
            {
                "variant_index": index,
                "seed": 7 + index,
                "artifact": {**artifact, "artifact_id": f"{OP}.variant-{index:02d}"},
                "provenance_sha256": "b" * 64,
            }
        )
    if corrupt_second:
        result["variants"][1]["artifact"]["sha256"] = "c" * 64
    validated, downloaded = [], []

    def validate_reader(python, *arguments):
        variant = Path(arguments[arguments.index("--variant") + 1])
        receipt = Path(arguments[arguments.index("--receipt") + 1])
        validated.append(variant.name)
        client.harness.write_private(receipt, {"validation": {"status": "passed"}}, ())

    monkeypatch.setattr(client, "reader", validate_reader)

    class FakePublic:
        output, token = tmp_path, TOKEN
        state = {
            "operation_id": OP,
            "request": {"parameters": {"variants": {"count": 2, "seeds": [7, 8]}}},
        }

        async def response(self, method, path):
            return {"terminal_status": "succeeded", "output_manifest": manifest}

        async def call(self, name, arguments):
            assert name == "inspect_scientific_artifact_manifest"
            return {"artifact": manifest, "truncated": False, "entries": entries}

        async def download_file(self, artifact, destination, maximum):
            downloaded.append(destination.name)
            if destination.name == "result.json":
                client.harness.write_private(destination, result, ())

        def persist(self):
            pass

    public = FakePublic()
    args = SimpleNamespace(
        reader_python=Path("/unused"),
        max_bytes=5 * 1024**3,
        max_expanded_bytes=8 * 1024**3,
    )
    if corrupt_second:
        with pytest.raises(
            client.harness.AcceptanceError, match="variant_manifest_identity_mismatch"
        ):
            asyncio.run(client.collect_outputs(public, args))
        assert "outcome" not in public.state
        assert validated == ["variant-00.json"]
    else:
        asyncio.run(client.collect_outputs(public, args))
        assert validated == ["variant-00.json", "variant-01.json"]
        assert downloaded == [
            "output-manifest.json",
            "result.json",
            "variant-00.tar.zst",
            "variant-01.tar.zst",
        ]
        assert len(public.state["validations"]) == 2
        assert public.state["outcome"] == "dataset_integrity_passed"
