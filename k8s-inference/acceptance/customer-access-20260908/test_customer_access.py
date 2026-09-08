"""Offline only: public harness credential persistence and acceptance boundaries."""

import asyncio
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import customer_access as customer
import httpx
import pytest
from mcp.shared.exceptions import MCPError

KEY_ID = "80bf6040-69b7-4eb6-bc04-8a7c8853506b"
FAKE_SECRET = "fs2_pat_" + UUID(KEY_ID).hex + "_" + "synthetic-test-only-not-a-real-key" * 2
ORIGIN = "https://example.invalid"
COMPARISON_TENANT = "tenant-e00f3wdfzwfjgbcyfv"


@pytest.mark.parametrize("value", [None, "", "kopra"])
def test_comparison_tenant_must_be_explicit_and_different(value):
    with pytest.raises(AssertionError, match="comparison_tenant"):
        customer.comparison_tenant(value)


def metadata(**updates):
    return {
        "id": KEY_ID,
        "name": customer.KEY_NAME,
        **customer.OWNER,
        "state": "active",
        "models": ["*"],
        "scopes": customer.SCOPES,
        **updates,
    }


def saved():
    return {
        "schema": "fs2-customer-key/v1",
        "name": customer.KEY_NAME,
        "owner": customer.OWNER,
        "origin": ORIGIN,
        "key_id": KEY_ID,
        "secret": FAKE_SECRET,
    }


def test_standard_key_has_no_admin_or_academic_scope():
    request = customer.key_request(customer.KEY_NAME, customer.OWNER, ["*"])
    assert request["scopes"] == customer.SCOPES
    assert not any(scope.startswith(("use.", "tenant.", "tokens.", "audit.")) for scope in request["scopes"])
    assert "academic_eligible" not in request and "app_ids" not in request
    assert request["max_concurrency"] == 1 and "expires_at" not in request


def test_create_then_resume_does_not_duplicate_or_disclose_secret(tmp_path, capsys):
    keys, posts = [], []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"items": keys}})
        posts.append(request)
        keys.append(metadata())
        return httpx.Response(201, json={"data": {"key": metadata(), "secret": FAKE_SECRET}})

    secret_path = tmp_path / "private" / "kopra.json"
    raw = tmp_path / "raw"
    raw.mkdir()
    trace = customer.shared.Trace(raw)
    with httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(handle)) as admin:
        first, created = customer.retained_key(admin, trace, secret_path, ORIGIN, create=True)
        second, created_again = customer.retained_key(admin, trace, secret_path, ORIGIN, create=True)
    assert created and not created_again and first == second
    assert len(posts) == 1
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert customer.read_key(secret_path)["secret"] == FAKE_SECRET
    assert all(FAKE_SECRET not in path.read_text() for path in raw.iterdir())
    assert FAKE_SECRET not in capsys.readouterr().out


@pytest.mark.parametrize(
    "rows,code",
    [
        ([metadata()], "secret_missing"),
        ([metadata(), metadata()], "multiple_named"),
        ([metadata(state="revoked")], "not_active"),
        ([metadata(models=["phenoage"])], "not_wildcard"),
        ([metadata(scopes=customer.SCOPES + ["use.noncommercial"])], "scopes_differ"),
    ],
)
def test_existing_key_without_recoverable_secret_never_creates_another(tmp_path, rows, code):
    methods = []

    def handle(request):
        methods.append(request.method)
        return httpx.Response(200, json={"data": {"items": rows}})

    with httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(handle)) as admin:
        with pytest.raises(AssertionError, match=code):
            customer.retained_key(admin, customer.shared.Trace(tmp_path), tmp_path / "key.json", ORIGIN, create=True)
    assert methods == ["GET"]


def test_key_file_is_exclusive_and_permission_checked(tmp_path):
    path = tmp_path / "key.json"
    customer.private_write(path, saved())
    with pytest.raises(FileExistsError):
        customer.private_write(path, saved())
    path.chmod(0o644)
    with pytest.raises(AssertionError, match="0600"):
        customer.read_key(path)


def test_dangling_secret_symlink_prevents_any_api_request(tmp_path):
    path = tmp_path / "key.json"
    path.symlink_to(tmp_path / "absent")
    with pytest.raises(OSError):
        customer.retained_key(None, None, path, ORIGIN, create=True)


def test_existing_owner_does_not_gain_a_second_academic_policy(monkeypatch):
    calls = []

    def call(admin, trace, method, path, **kwargs):
        calls.append((method, kwargs))
        return {
            "items": [
                {
                    "id": customer.USER_ID,
                    **customer.OWNER,
                    "enabled": True,
                    "display_name": "Kopra",
                    "academic_eligible": False,
                    "app_ids": [],
                }
            ]
        }

    monkeypatch.setattr(customer.shared, "admin_call", call)
    user = customer.ensure_user(None, None, create=True)
    assert user["academic_eligible"] is False and user["app_ids"] == []
    assert calls == [("GET", {})]


def test_original_cpu_fixture_retains_qualified_payload():
    request = customer.original_fixture()
    assert customer.shared.digest(request) == "b15ebdc61742dff7d3293e35a09b2a3207fd39d6e05ef7f881d900b4172b7842"


@pytest.mark.parametrize("failure", [False, True])
def test_restricted_key_cleanup_and_same_model_cross_owner_probe(tmp_path, monkeypatch, failure):
    calls, discoveries, denials = [], [], []

    def admin_call(admin, trace, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "POST":
            return {"key": {"id": KEY_ID}, "secret": FAKE_SECRET}
        if method == "DELETE":
            return {"state": "revoked"}
        return {}

    def handle(request):
        denials.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(403, json={"error": "outside token policy"})
        return httpx.Response(401 if request.url.path == "/v1/models" else 404, json={"error": "not found"})

    async def mcp_denied(*args):
        if failure:
            raise RuntimeError("transport failure is not an authorization pass")

    monkeypatch.setattr(customer.shared, "admin_call", admin_call)
    monkeypatch.setattr(customer, "discover", lambda *args, expected: discoveries.append(expected))
    monkeypatch.setattr(customer, "mcp_denied", mcp_denied)
    monkeypatch.setattr(
        customer, "public_client", lambda *_: httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(handle))
    )
    evidence = {}
    if failure:
        with pytest.raises(RuntimeError):
            customer.restricted_checks(
                None,
                customer.shared.Trace(tmp_path),
                ORIGIN,
                SimpleNamespace(
                    output=tmp_path,
                    comparison_tenant=COMPARISON_TENANT,
                    comparison_principal="terraform-bootstrap-client",
                ),
                "existing-operation",
                evidence,
            )
    else:
        customer.restricted_checks(
            None,
            customer.shared.Trace(tmp_path),
            ORIGIN,
            SimpleNamespace(
                output=tmp_path, comparison_tenant=COMPARISON_TENANT, comparison_principal="terraform-bootstrap-client"
            ),
            "existing-operation",
            evidence,
        )
        assert discoveries == [{"qwen3-8b"}, {"phenoage"}]
        assert next(item for item in calls if item[0] == "PATCH")[2]["payload"] == {"models": ["phenoage"]}
        assert ("GET", "/v1/operations/existing-operation/result") in denials
        assert evidence["different_customer_same_model_result_isolation"]
    assert evidence["disposable_key_revoked"]
    assert next(item for item in calls if item[0] == "POST")[2]["payload"]["tenant_id"] == COMPARISON_TENANT
    assert (
        next(item for item in calls if item[0] == "POST")[2]["payload"]["principal_id"] == "terraform-bootstrap-client"
    )
    assert sum(method == "DELETE" for method, *_ in calls) == 1


@pytest.mark.parametrize(
    "code,message,accepted",
    [
        (-32602, "model or protocol is outside token policy", True),
        (-32602, "operation not found", True),
        (-32603, "internal error", False),
    ],
)
def test_expected_mcp_rpc_denial_not_internal_failure(tmp_path, monkeypatch, code, message, accepted):
    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def call_tool(self, *args):
            raise MCPError(code, message)

    monkeypatch.setattr(customer.shared.httpx2, "AsyncClient", lambda **kwargs: Context())
    monkeypatch.setattr(customer.shared, "streamable_http_client", lambda *args, **kwargs: None)
    monkeypatch.setattr(customer.shared, "Client", lambda *args, **kwargs: Context())
    run = customer.mcp_denied(ORIGIN, FAKE_SECRET, customer.shared.Trace(tmp_path), "get_operation", {})
    if accepted:
        asyncio.run(run)
    else:
        with pytest.raises(AssertionError, match="unexpected_rpc"):
            asyncio.run(run)
    receipt = json.loads(next(Path(tmp_path).glob("*.json")).read_text())
    assert receipt["rpc_error"]["code"] == code


@pytest.mark.parametrize(
    "serving,science,expected,error",
    [
        (["phenoage"], ["bindcraft", "alphafold3", "app-science-clone"], None, None),
        (["phenoage"], [], {"phenoage"}, None),
        (["phenoage"], ["bindcraft"], {"phenoage"}, "restricted_catalog_grant"),
    ],
)
def test_real_protocol_shapes_union_and_restricted_grants(tmp_path, monkeypatch, serving, science, expected, error):
    paths, tools = [], []
    serving_body = {"object": "list", "data": [{"id": identity} for identity in serving]}
    science_body = {"object": "list", "data": [{"model_id": identity} for identity in science]}

    def handle(request):
        paths.append(request.url.path)
        assert request.url.path in {"/v1/models", "/v1/scientific-models"}
        return httpx.Response(200, json=serving_body if request.url.path == "/v1/models" else science_body)

    async def mcp_call(origin, token, trace, name, arguments):
        tools.append(name)
        return serving_body if name == "list_models" else science_body

    monkeypatch.setattr(customer.shared, "mcp_call", mcp_call)
    with httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(handle)) as client:
        if error:
            with pytest.raises(AssertionError, match=error):
                customer.discover(client, ORIGIN, FAKE_SECRET, customer.shared.Trace(tmp_path), expected=expected)
        else:
            found = customer.discover(client, ORIGIN, FAKE_SECRET, customer.shared.Trace(tmp_path), expected=expected)
            assert found["http"] == found["mcp"] == serving_body
            assert found["scientific_http"] == found["scientific_mcp"] == science_body
            assert set(found["model_ids"]) == set(serving) | set(science)
    assert paths == ["/v1/models", "/v1/scientific-models"]
    assert tools == ["list_models", "list_scientific_models"]


@pytest.mark.parametrize("mismatched", ["serving", "scientific"])
def test_each_protocol_pair_must_match_not_just_combined_count(tmp_path, monkeypatch, mismatched):
    def handle(request):
        field = "id" if request.url.path == "/v1/models" else "model_id"
        return httpx.Response(200, json={"data": [{field: "original"}]})

    async def mcp_call(origin, token, trace, name, arguments):
        protocol, field = ("serving", "id") if name == "list_models" else ("scientific", "model_id")
        return {"data": [{field: "different" if protocol == mismatched else "original"}]}

    monkeypatch.setattr(customer.shared, "mcp_call", mcp_call)
    with httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AssertionError, match=mismatched + "_http_mcp_inventory_differs"):
            customer.discover(client, ORIGIN, FAKE_SECRET, customer.shared.Trace(tmp_path))


def test_enabled_scientific_clone_requires_public_identity_paused_app_is_not_required():
    apps = [{"public_model_id": model, "enabled": True} for model in customer.REQUIRED_MODELS]
    apps.extend(
        [
            {"public_model_id": "app-science-clone", "model_ref": "protenix-v2", "enabled": True},
            {"public_model_id": "app-paused-serving", "enabled": False},
        ]
    )
    with pytest.raises(AssertionError, match="available_platform_models_missing"):
        customer.verify_platform_inventory(apps, customer.REQUIRED_MODELS | {"protenix-v2"})
    expected = customer.verify_platform_inventory(apps, customer.REQUIRED_MODELS | {"app-science-clone"})
    assert "app-science-clone" in expected and "app-paused-serving" not in expected
