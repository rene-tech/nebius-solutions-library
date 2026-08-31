from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import CATALOG_ROOT, CONTROL_ROOT, REPO_ROOT

from fs2_serve import live_acceptance
from fs2_serve.live_acceptance import (
    ACCEPTANCE_SCHEMA,
    TLS_MODE_DISPOSABLE_STAGING,
    TLS_MODE_VERIFIED,
    AcceptanceCase,
    AcceptanceError,
    AcceptanceRunner,
    _load_cases,
    _materialize_payload_assets,
    discover_model_tools,
    read_token_file,
    response_summary,
    tls_verify_for_mode,
    write_evidence,
)
from fs2_serve.live_release import LiveRelease, render_live_release


@pytest.mark.parametrize("script_name", ("accept_all_models_live.py", "render_all_models_live.py"))
def test_source_operator_script_ignores_stale_installed_packages(tmp_path: Path, script_name: str) -> None:
    script = CONTROL_ROOT / f"scripts/{script_name}"
    probe = (
        "import json,runpy;"
        f"runpy.run_path({str(script)!r},run_name='source_acceptance_probe');"
        "import fs2_serve,fs2_serve_catalog;"
        "print(json.dumps([fs2_serve.__file__,fs2_serve_catalog.__file__]))"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(  # noqa: S603 - fixed local interpreter and source probe
        [sys.executable, "-I", "-c", probe],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    control_source, catalog_source = (Path(value).resolve() for value in json.loads(result.stdout))
    assert control_source.is_relative_to(CONTROL_ROOT / "src")
    assert catalog_source.is_relative_to(CATALOG_ROOT)


def test_token_reader_requires_owner_mode_0600_and_never_reflects_value(tmp_path: Path) -> None:
    secret = "fs2_pat_" + "a" * 64
    path = tmp_path / "pat"
    path.write_text(secret + "\n", encoding="ascii")
    path.chmod(0o600)
    assert read_token_file(path) == secret

    path.chmod(0o640)
    with pytest.raises(AcceptanceError, match="token_file_mode_invalid") as captured:
        read_token_file(path)
    assert secret not in str(captured.value)


def test_disposable_staging_tls_is_explicit_and_restricted_to_public_ipv4() -> None:
    assert tls_verify_for_mode("https://204.12.177.31", TLS_MODE_VERIFIED) is True
    assert tls_verify_for_mode("https://204.12.177.31", TLS_MODE_DISPOSABLE_STAGING) is False

    for endpoint in ("https://example.com", "https://127.0.0.1", "https://10.0.0.1"):
        with pytest.raises(AcceptanceError, match="disposable_staging_tls_endpoint_invalid"):
            tls_verify_for_mode(endpoint, TLS_MODE_DISPOSABLE_STAGING)
    with pytest.raises(AcceptanceError, match="tls_mode_invalid"):
        tls_verify_for_mode("https://204.12.177.31", "insecure")


@pytest.mark.parametrize(
    ("tls_mode", "expected_verify"),
    ((TLS_MODE_VERIFIED, True), (TLS_MODE_DISPOSABLE_STAGING, False)),
)
def test_runner_applies_one_tls_mode_to_http_and_mcp_clients(
    monkeypatch: pytest.MonkeyPatch,
    tls_mode: str,
    expected_verify: bool,
) -> None:
    http_clients: list[dict[str, object]] = []
    mcp_clients: list[dict[str, object]] = []

    class FakeHttpClient:
        def __init__(self, **kwargs: object) -> None:
            http_clients.append(kwargs)

    class FakeMcpHttpClient:
        def __init__(self, **kwargs: object) -> None:
            mcp_clients.append(kwargs)

    monkeypatch.setattr(live_acceptance.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(live_acceptance.httpx2, "AsyncClient", FakeMcpHttpClient)

    runner = AcceptanceRunner(
        origin="https://204.12.177.31",
        token="fs2_pat_" + "a" * 64,
        release=release(),
        cases=(),
        timeout_seconds=300,
        concurrency=1,
        tls_mode=tls_mode,
    )

    assert runner.tls_mode == tls_mode
    assert len(http_clients) == 2
    assert all(client["verify"] is expected_verify for client in http_clients)
    assert len(mcp_clients) == 1
    assert mcp_clients[0]["verify"] is expected_verify
    assert all(client["trust_env"] is False for client in (*http_clients, *mcp_clients))


def test_sdxl_gateway_json_envelope_is_required_and_summarized_without_pixels() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"pixels"
    value = {"data": [{"b64_json": base64.b64encode(png).decode("ascii")}], "model": "sdxl"}
    raw = json.dumps(value, separators=(",", ":")).encode()

    summary = response_summary(value, raw, "png-b64-json")

    assert summary["artifact_bytes"] == len(png)
    assert summary["artifact_sha256"]
    assert "pixels" not in json.dumps(summary)
    with pytest.raises(AcceptanceError, match="semantic_response_schema_invalid"):
        response_summary({"data": [{"url": "https://private.invalid"}]}, b"{}", "png-b64-json")


def test_cosmos_mp4_envelope_is_bounded_and_summarized_without_media() -> None:
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24
    value = {
        "model": "nvidia/Cosmos3-Nano",
        "revision": "7a312c868bcce8e40b3eb40861300a9d0ba3fde1",
        "mode": "text-to-video",
        "mime_type": "video/mp4",
        "data_base64": base64.b64encode(mp4).decode("ascii"),
        "bytes": len(mp4),
        "sha256": live_acceptance.sha256_bytes(mp4),
        "width": 448,
        "height": 256,
        "frames": 25,
        "fps": 24,
        "timings_ms": {"queue": 1.0, "upstream": 100.0, "total": 101.0},
    }
    raw = json.dumps(value, separators=(",", ":")).encode()

    summary = response_summary(value, raw, "mp4-b64-json")

    assert summary["artifact_bytes"] == len(mp4)
    assert summary["artifact_sha256"] == value["sha256"]
    assert summary["frames"] == 25
    assert "data_base64" not in summary
    assert base64.b64encode(mp4).decode("ascii") not in json.dumps(summary)

    for field, invalid in (
        ("mime_type", "application/octet-stream"),
        ("bytes", len(mp4) + 1),
        ("sha256", "0" * 64),
        ("frames", 401),
    ):
        corrupted = {**value, field: invalid}
        with pytest.raises(AcceptanceError, match="semantic_response_schema_invalid"):
            response_summary(corrupted, json.dumps(corrupted).encode(), "mp4-b64-json")


def test_native_and_openai_summaries_persist_only_digests() -> None:
    openai = {"choices": [{"message": {"content": "private model answer"}}]}
    summary = response_summary(openai, json.dumps(openai).encode(), "openai-chat")
    assert "private model answer" not in json.dumps(summary)
    assert summary["content_sha256"]

    native = {"structure": "private coordinates"}
    summary = response_summary(native, json.dumps(native).encode(), "json-object")
    assert "private coordinates" not in json.dumps(summary)


class Tool:
    def __init__(self, name: str, metadata: dict[str, object]) -> None:
        self.name = name
        self.metadata = metadata

    def model_dump(self, *, mode: str, by_alias: bool) -> dict[str, object]:
        assert mode == "json" and by_alias is True
        return {"name": self.name, "_meta": self.metadata}


def test_mcp_tools_are_discovered_by_model_revision_and_protocol_metadata() -> None:
    case = AcceptanceCase(
        model_id="sdxl",
        revision="a" * 40,
        protocol="native",
        operation="generate-image",
        payload={"response_format": "b64_json"},
        payload_sha256="b" * 64,
        response_kind="png-b64-json",
    )
    tool = Tool(
        "generate_image_native",
        {"fs2_model_id": "sdxl", "fs2_model_revision": "a" * 40, "fs2_protocol": "native"},
    )
    assert discover_model_tools([tool], (case,)) == {"sdxl": "generate_image_native"}

    wrong = Tool(
        "generate_image_native",
        {"fs2_model_id": "sdxl", "fs2_model_revision": "c" * 40, "fs2_protocol": "native"},
    )
    with pytest.raises(AcceptanceError, match="mcp_model_tool_set_invalid"):
        discover_model_tools([wrong], (case,))


@dataclass
class Semantic:
    state: str
    request_sha256: tuple[str, ...]
    invocation: dict[str, str]
    serialization: str = "sha256-canonical-json-no-newline/v1"

    def to_dict(self) -> dict[str, object]:
        return {
            "serialization": self.serialization,
            "requests": [{"id": "fixture", "payload_sha256": self.request_sha256[0]}],
            "assets": [],
        }


class Catalog:
    def __init__(self, payload_digest: str) -> None:
        self.semantic = Semantic(
            state="qualified",
            request_sha256=(payload_digest,),
            invocation={"protocol": "native", "operation": "generate-image"},
        )

    def semantic_request_contract(self, model_id: str) -> Semantic:
        assert model_id == "sdxl"
        return self.semantic


def release() -> LiveRelease:
    return LiveRelease(
        release_id="release",
        catalog_digest="c" * 64,
        inventory_digest="d" * 64,
        bindings_config_map_name="bindings",
        routes_config_map_name="routes",
        config_maps=(),
        helm_values={},
        routes=(
            {
                "model_id": "sdxl",
                "model_revision": "e" * 40,
                "protocols": {"native": "/generate"},
                "operations": ["generate-image"],
            },
        ),
        qualification_projection={},
    )


def test_case_loader_binds_payload_to_canonical_semantic_contract(tmp_path: Path) -> None:
    payload = {"prompt": "fixture", "response_format": "b64_json"}
    from fs2_serve.live_acceptance import sha256_bytes
    from fs2_serve.live_release import canonical_json

    digest = sha256_bytes(canonical_json(payload))
    catalog = Catalog(digest)
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "schema": ACCEPTANCE_SCHEMA,
                "cases": {
                    "sdxl": {
                        "protocol": "native",
                        "operation": "generate-image",
                        "payload": payload,
                        "payload_sha256": digest,
                        "response_kind": "png-b64-json",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cases = _load_cases(path, catalog, release())  # type: ignore[arg-type]
    assert cases[0].payload == payload


def test_published_acceptance_cases_exactly_cover_the_live_release() -> None:
    catalog = live_acceptance.load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)
    published_release = render_live_release(
        catalog,
        CONTROL_ROOT / "contracts/all-models-live-services.json",
    )

    cases = _load_cases(
        CONTROL_ROOT / "contracts/all-models-live-acceptance.json",
        catalog,
        published_release,
    )

    assert {case.model_id for case in cases} == set(catalog.tested_model_ids)
    cosmos = next(case for case in cases if case.model_id == "cosmos3-nano")
    assert cosmos.operation == "generate-media"
    assert cosmos.payload_sha256 == "e4f2897190d5efc4f551af9c1f3cee9880b7a001c17589065d0338bd277a53e2"
    assert cosmos.response_kind == "mp4-b64-json"


def test_packaged_licensed_image_is_digest_bound_and_materialized_as_data_uri(
    tmp_path: Path,
) -> None:
    raw = b"\xff\xd8\xff" + b"licensed fixture pixels"
    digest = live_acceptance.sha256_bytes(raw)
    assets = tmp_path / "assets"
    assets.mkdir()
    fixture = assets / f"{digest}.jpg"
    fixture.write_bytes(raw)
    canonical_uri = "https://fixtures.invalid/cxr.jpg"
    canonical_payload = {"messages": [{"content": [{"type": "image_url", "image_url": {"url": canonical_uri}}]}]}
    semantic_document = {
        "requests": [{"id": "cxr", "payload_sha256": "a" * 64}],
        "assets": [
            {
                "request_id": "cxr",
                "kind": "licensed-image",
                "uri": canonical_uri,
                "content_sha256": digest,
                "bytes": len(raw),
            }
        ],
    }

    wire_payload = _materialize_payload_assets(
        canonical_payload,
        "a" * 64,
        semantic_document,
        tmp_path / "cases.json",
    )

    wire_uri = wire_payload["messages"][0]["content"][0]["image_url"]["url"]
    assert wire_uri.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(wire_uri.partition(",")[2], validate=True) == raw
    assert canonical_payload["messages"][0]["content"][0]["image_url"]["url"] == canonical_uri

    fixture.write_bytes(raw + b"tampered")
    with pytest.raises(AcceptanceError, match="acceptance_asset_file_invalid"):
        _materialize_payload_assets(
            canonical_payload,
            "a" * 64,
            semantic_document,
            tmp_path / "cases.json",
        )


def test_evidence_is_atomic_mode_0600_and_rejects_token(tmp_path: Path) -> None:
    output = (tmp_path / "evidence.json").resolve()
    write_evidence(output, {"result": "PASS", "digest": "f" * 64}, forbidden=("fs2_secret",))
    assert os.stat(output).st_mode & 0o777 == 0o600
    with pytest.raises(AcceptanceError, match="evidence_redaction_failed"):
        write_evidence(output, {"result": "fs2_secret"}, forbidden=("fs2_secret",))
