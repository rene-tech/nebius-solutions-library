"""Contract/transport qualification only: no live endpoint or capacity claims."""

import asyncio
import base64
import hashlib
import io
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from jsonschema import Draft202012Validator
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from scientific_ai.contracts import Owner
from scientific_ai.endpoint_catalog import catalog, contract_for
from scientific_ai.endpoint_contracts import (
    EndpointArtifactError,
    EndpointInputError,
    NativeResultError,
    OutputArtifacts,
    apply_tool_input_contract,
    dispatch_native,
    materialize_inputs,
    validate_payload,
)
from scientific_ai.gateway_store import GatewayStore


class MemoryS3:
    """Bucket-aware isolation double; never used by application code."""

    def __init__(self):
        self.data, self.reads = {}, []

    def get_object(self, *, Bucket, Key):
        self.reads.append((Bucket, Key))
        if (Bucket, Key) not in self.data:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.data[Bucket, Key])}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, **kwargs):
        if IfNoneMatch == "*" and (Bucket, Key) in self.data:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.data[Bucket, Key] = Body

    def upload_fileobj(self, source, bucket, key, **kwargs):
        self.data[bucket, key] = source.read()


@pytest.fixture
def store():
    return GatewayStore(
        MemoryS3(), bucket="synthetic-workspace", prefix="endpoints",
        owner=Owner(tenant_id="system", principal_id="qa"),
    )


def upload(store, content, media_type="application/json"):
    aid = str(uuid4())
    reference = {
        "artifact_id": aid,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "media_type": media_type,
        "compression": "none",
    }
    key = store.prefix + "uploads/" + aid
    store.client.put_object(Bucket=store.bucket, Key=key, Body=content)
    store.save("artifacts", aid, {**reference, "object_key": key}, create=True)
    return reference


def binary(content, media_type="application/zip"):
    return {
        "encoding": "base64", "media_type": media_type,
        "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
        "data": base64.b64encode(content).decode(),
    }


def test_catalog_is_immutable_and_does_not_claim_deployment():
    model = contract_for("admet-ai")
    with pytest.raises(FrozenInstanceError):
        model.model_id = "other"
    with pytest.raises(TypeError):
        catalog()["other"] = model
    schema = model.schema()
    schema["properties"].clear()
    assert "smiles" in model.schema()["properties"]
    description = model.describe()
    assert description["runtime_identity"] is None
    assert description["readiness"] == "unknown"
    assert description["execution"]["durable_async"] is False
    assert description["execution"]["idempotency"] is False
    assert "poll the returned operation ID" not in description["input_schema"]["description"]
    assert len(description["source_schema_sha256"]) == 64
    with pytest.raises(KeyError):
        contract_for("admet")


def test_named_tools_retain_verified_names_and_distinguish_historical_sources():
    expected = {
        "admet-ai": "infer_admet_ai_native", "ctoxpred2": "infer_ctoxpred2_native",
        "altumage": "infer_altumage_native", "phenoage": "infer_phenoage_native",
        "cellpose-cpsam-v2": "segment_cells_native", "sam2-1-hiera-large": "segment_track_media_native",
        "molmim": "molmim_run_native", "boltz2": "boltz2_predict_native",
        "msa-search-pdb70": "msa_search_native", "openfold2": "infer_openfold2_native",
        "openfold3": "infer_openfold3_native", "proteinmpnn": "infer_proteinmpnn_native",
        "diffdock": "infer_diffdock_native",
    }
    assert {key: contract.tool_name for key, contract in catalog().items()} == expected
    for model in catalog().values():
        assert model.tool_name_source_refs
        if model.model_id in {"admet-ai", "ctoxpred2", "cellpose-cpsam-v2", "sam2-1-hiera-large"}:
            assert model.tool_name_provenance.startswith("historical-")
        else:
            assert model.tool_name_provenance == "legacy-live-snapshot"


@pytest.mark.parametrize("model_id", [
    "admet-ai", "ctoxpred2", "phenoage", "altumage", "cellpose-cpsam-v2", "sam2-1-hiera-large",
    "molmim", "boltz2", "openfold2", "openfold3", "diffdock", "proteinmpnn", "msa-search-pdb70",
])
def test_catalog_has_meaningful_source_backed_schemas(model_id):
    model = contract_for(model_id)
    assert model.source_refs
    for surface in ("http", "mcp", "native"):
        schema = model.schema(surface)
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
        assert "payload" not in schema["properties"]
        assert "idempotency_key" not in schema["properties"]
        assert "wait_seconds" not in schema["properties"]
    for example in model.examples:
        validate_payload(model, example)
        validate_payload(model, example, surface="mcp")


@pytest.mark.parametrize("model_id", ["admet-ai", "ctoxpred2"])
def test_screening_keeps_exact_fields_and_rejects_ambiguous_or_unknown_inputs(model_id):
    model = contract_for(model_id)
    request = {"molecules": [{"id": "m1", "smiles": "CCO"}]}
    assert validate_payload(model, request) == request
    for bad in ({}, {"smiles": "CCO", **request}, {"smiles": "CCO", "endpoints": ["made-up"]},
                {"smiles": "CCO", "temperature": 1}, {"molecules": request["molecules"] * 2}):
        with pytest.raises(EndpointInputError):
            validate_payload(model, bad)
    if model_id == "ctoxpred2":
        assert validate_payload(model, {"smiles": "CCO", "method": "dl-sl", "seed": 7})["seed"] == 7


def test_phenoage_finite_units_and_sample_identity():
    model = contract_for("phenoage")
    request = model.examples[0]
    assert validate_payload(model, request) == request
    for field, value in (("albumin_g_l", 0), ("c_reactive_protein_mg_dl", float("nan")),
                         ("lymphocyte_percent", 101), ("age_years", float("inf"))):
        bad = deepcopy(request)
        bad["samples"][0][field] = value
        with pytest.raises(EndpointInputError):
            validate_payload(model, bad)
    with pytest.raises(EndpointInputError):
        validate_payload(model, {"samples": request["samples"] * 2})


def test_molmim_decode_budget_is_enforced_without_injecting_defaults():
    model = contract_for("molmim")
    assert validate_payload(model, {"smi": "CCO"}) == {"smi": "CCO"}
    with pytest.raises(EndpointInputError):
        validate_payload(model, {"smi": "CCO", "num_molecules": 3})
    validate_payload(model, {"smi": "CCO", "num_molecules": 6, "particles": 2, "iterations": 3})


def test_mcp_requires_artifacts_for_binary_media_and_full_methylation_arrays():
    image = {"image_base64": "cG5n", "media_type": "image/png", "research_only": True}
    validate_payload(contract_for("cellpose-cpsam-v2"), image, surface="native")
    with pytest.raises(EndpointInputError):
        validate_payload(contract_for("cellpose-cpsam-v2"), image, surface="mcp")
    for name in ("cpg_sites",):
        choices = contract_for("altumage").schema("mcp")["properties"][name]["anyOf"]
        assert len(choices) == 1 and "artifact_id" in choices[0]["properties"]


def test_materialization_reads_exact_owner_content_and_preserves_public_input(store):
    model = contract_for("admet-ai")
    data = b'[{"id":"m1","smiles":"CCO"}]'
    pointer = upload(store, data)
    request = {"molecules": pointer}
    native = materialize_inputs(model, request, store)
    assert native == {"molecules": [{"id": "m1", "smiles": "CCO"}]}
    assert request == {"molecules": pointer}
    with pytest.raises(EndpointArtifactError):
        materialize_inputs(model, request, store, max_materialized_bytes=1)


@pytest.mark.parametrize("other_owner", [
    Owner(tenant_id="system", principal_id="sibling"),
    Owner(tenant_id="other", principal_id="qa"),
])
def test_artifact_ids_do_not_cross_principal_or_tenant_boundaries(store, other_owner):
    pointer = upload(store, b"smiles\nCCO\n", "text/csv")
    other = GatewayStore(store.client, bucket=store.bucket, prefix="endpoints", owner=other_owner)
    before = len(store.client.reads)
    with pytest.raises(EndpointArtifactError):
        materialize_inputs(contract_for("admet-ai"), {"csv": pointer}, other)
    assert len(store.client.reads) == before + 1  # metadata lookup only


@pytest.mark.parametrize("mutation", [
    {"sha256": "f" * 64}, {"size_bytes": 1}, {"media_type": "text/plain"},
    {"compression": "gzip"}, {"url": "http://169.254.169.254/latest/meta-data"},
])
def test_input_reference_checks_all_metadata_and_rejects_urls(store, mutation):
    pointer = upload(store, b"smiles\nCCO\n", "text/csv")
    with pytest.raises((EndpointInputError, EndpointArtifactError)):
        materialize_inputs(contract_for("admet-ai"), {"csv": {**pointer, **mutation}}, store)


def test_tampered_metadata_cannot_point_at_sibling_object(store):
    pointer = upload(store, b"smiles\nCCO\n", "text/csv")
    record = store.get("artifacts", pointer["artifact_id"])
    store.save("artifacts", pointer["artifact_id"], {**record, "object_key": "endpoints/sibling/private"})
    before = len(store.client.reads)
    with pytest.raises(EndpointArtifactError):
        materialize_inputs(contract_for("admet-ai"), {"csv": pointer}, store)
    assert len(store.client.reads) == before + 1


def test_mutated_bytes_and_malicious_json_never_reach_native_runtime(store):
    pointer = upload(store, b'[{"id":"m1","smiles":"CCO"}]')
    record = store.get("artifacts", pointer["artifact_id"])
    store.client.data[store.bucket, record["object_key"]] += b"corrupt"
    with pytest.raises(EndpointArtifactError):
        materialize_inputs(contract_for("admet-ai"), {"molecules": pointer}, store)
    for data in (b"{}", b'[ {"id":"private-value", "smiles":"CCO", "url":"https://other"} ]', b"NaN"):
        with pytest.raises(EndpointInputError) as error:
            materialize_inputs(contract_for("admet-ai"), {"molecules": upload(store, data)}, store)
        assert "private-value" not in str(error.value.issues)


def test_missing_content_and_duplicate_json_fields_are_safe_artifact_errors(store):
    pointer = upload(store, b'[{"id":"m1","smiles":"CCO"}]')
    record = store.get("artifacts", pointer["artifact_id"])
    del store.client.data[store.bucket, record["object_key"]]
    with pytest.raises(EndpointArtifactError):
        materialize_inputs(contract_for("admet-ai"), {"molecules": pointer}, store)
    pointer = upload(store, b'[{"id":"private","id":"replacement","smiles":"CCO"}]')
    with pytest.raises(EndpointArtifactError) as error:
        materialize_inputs(contract_for("admet-ai"), {"molecules": pointer}, store)
    assert "private" not in str(error.value)


def test_inline_compatibility_cannot_bypass_transport_budgets(store):
    with pytest.raises(EndpointArtifactError):
        materialize_inputs(contract_for("admet-ai"), {"csv": "smiles\nCCO\n"}, store,
                           max_materialized_bytes=3)
    with pytest.raises(EndpointArtifactError):
        materialize_inputs(contract_for("cellpose-cpsam-v2"),
                           {"image_base64": "%%%%", "media_type": "image/png", "research_only": True}, store)


def test_altumage_nested_refs_materialize_and_validate_full_panel(store):
    model = contract_for("altumage")
    cpg = [f"cg{i:08d}" for i in range(20318)]
    beta = [0.5] * 20318
    request = {
        "cpg_sites": upload(store, json.dumps(cpg).encode()),
        "samples": [{"sample_id": "synthetic", "beta_values": upload(store, json.dumps(beta).encode())}],
    }
    value = materialize_inputs(model, request, store, surface="mcp")
    assert value["cpg_sites"] == cpg and value["samples"][0]["beta_values"] == beta
    bad = deepcopy(request)
    bad["samples"][0]["beta_values"] = upload(store, b"[0.5]")
    with pytest.raises(EndpointInputError):
        materialize_inputs(model, bad, store)
    beta[0] = None
    bad["samples"][0]["beta_values"] = upload(store, json.dumps(beta).encode())
    with pytest.raises(EndpointInputError):
        materialize_inputs(model, bad, store)
    assert materialize_inputs(model, {**bad, "missing_values": "reference_median"}, store)


def test_cellpose_input_bytes_and_media_type_agree(store):
    pointer = upload(store, b"synthetic-png", "image/png")
    payload = {"image_base64": pointer, "media_type": "image/png", "research_only": True}
    result = materialize_inputs(contract_for("cellpose-cpsam-v2"), payload, store, surface="mcp")
    assert base64.b64decode(result["image_base64"]) == b"synthetic-png"
    with pytest.raises(EndpointArtifactError):
        materialize_inputs(contract_for("cellpose-cpsam-v2"), {**payload, "media_type": "image/jpeg"}, store)


def test_sam2_modes_object_bound_and_box_order(store):
    model = contract_for("sam2-1-hiera-large")
    pointer = upload(store, b"synthetic-png", "image/png")
    request = {"media_base64": pointer, "media_type": "image/png", "mode": "prompted-image"}
    with pytest.raises(EndpointInputError):
        materialize_inputs(model, request, store)
    for bad in ({**request, "box": [2, 2, 1, 1]},
                {**request, "points": [{"x": 1, "y": 1, "object_id": i} for i in range(1, 10)]},
                {**request, "mode": "automatic-image", "box": [1, 1, 2, 2]}):
        with pytest.raises(EndpointInputError):
            materialize_inputs(model, bad, store)
    assert materialize_inputs(model, {**request, "box": [1, 1, 2, 2]}, store)


def test_structure_nested_a3m_and_pdb_artifacts(store):
    model = contract_for("boltz2")
    request = model.examples[0]
    text = request["polymers"][0]["msa"]["msa_search"]["a3m"]["alignment"]
    request["polymers"][0]["msa"]["msa_search"]["a3m"]["alignment"] = upload(
        store, text.encode(), "text/x-a3m"
    )
    result = materialize_inputs(model, request, store)
    assert result["polymers"][0]["msa"]["msa_search"]["a3m"]["alignment"] == text
    pdb = "ATOM      1  N   MET A   1       0.000   0.000   0.000  1.00  0.00           N\n"
    pointer = upload(store, pdb.encode(), "chemical/x-pdb")
    assert materialize_inputs(contract_for("proteinmpnn"), {"input_pdb": pointer}, store)["input_pdb"] == pdb
    assert materialize_inputs(contract_for("diffdock"), {"protein": pointer, "ligand": "CCO"}, store)


def test_s2_binary_outputs_are_verified_and_return_only_owned_artifact_references(store):
    model = contract_for("sam2-1-hiera-large")
    content = b"PK\x03\x04synthetic-test-result"
    output = OutputArtifacts(store).externalize(model, "native-op-1", binary(content))
    assert "data" not in output and "encoding" not in output
    pointer = output["artifact"]
    record = store.get("artifacts", pointer["artifact_id"])
    assert store.client.data[store.bucket, record["object_key"]] == content
    assert record["object_key"].startswith(store.prefix)
    assert OutputArtifacts(store).externalize(model, "native-op-1", binary(content)) == output
    for bad in ({**binary(content), "sha256": "f" * 64}, {**binary(content), "size_bytes": 0},
                {**binary(content), "data": "%%%"}):
        with pytest.raises(NativeResultError):
            OutputArtifacts(store).externalize(model, "native-op-1", bad)


def test_cellpose_binary_fields_and_large_arrays_stay_out_of_agent_results(store):
    result = {
        "object_count": 2,
        "mask_base64": base64.b64encode(b"mask").decode(), "mask_media_type": "image/png",
        "overlay_base64": base64.b64encode(b"overlay").decode(), "overlay_media_type": "image/png",
    }
    output = OutputArtifacts(store).externalize(contract_for("cellpose-cpsam-v2"), "op-1", result)
    assert output["object_count"] == 2
    assert "mask_artifact" in output and "overlay_artifact" in output
    assert "_base64" not in json.dumps(output)
    original = {"values": [1] * 50000}
    output = OutputArtifacts(store).externalize(contract_for("admet-ai"), "op-2", original)
    assert "values" not in output and output["artifact"]["media_type"] == "application/json"
    nested = {"results": [binary(b"binary-result")]}
    output = OutputArtifacts(store).externalize(contract_for("admet-ai"), "op-3", nested)
    assert "data" not in json.dumps(output) and output["artifact"]["media_type"] == "application/json"


def test_result_publication_remains_owner_isolated_with_shared_operation_id(store):
    other = GatewayStore(store.client, bucket=store.bucket, prefix="endpoints",
                         owner=Owner(tenant_id="system", principal_id="another-user"))
    mine = OutputArtifacts(store).publish("shared-s2-operation", b"result", "text/plain")
    theirs = OutputArtifacts(other).publish("shared-s2-operation", b"result", "text/plain")
    assert mine["artifact_id"] != theirs["artifact_id"]
    with pytest.raises(KeyError):
        other.get("artifacts", mine["artifact_id"])


def test_shared_dispatch_calls_native_once_and_preserves_original_operation_identity(store):
    calls = []

    def invoke(model_id, payload):
        calls.append((model_id, payload))
        return {"operation": {"id": "s2-op", "status": "SUCCEEDED", "input": "private"},
                "result": {"molecules": [{"id": "m1", "score": 0.1}]}}

    output = dispatch_native(contract_for("admet-ai"), {"smiles": "CCO"}, store=store, invoke=invoke)
    assert calls == [("admet-ai", {"smiles": "CCO"})]
    assert output["operation"] == {"id": "s2-op", "status": "SUCCEEDED"}
    assert "private" not in json.dumps(output)
    with pytest.raises(EndpointInputError):
        dispatch_native(contract_for("admet-ai"), {"smiles": "CCO", "wait_seconds": 1},
                        store=store, invoke=invoke)
    assert len(calls) == 1


def test_no_automatic_retry_or_fabricated_operation_on_ambiguous_failure(store):
    calls = []

    def unavailable(model_id, payload):
        calls.append(model_id)
        raise TimeoutError("synthetic transport timeout")

    with pytest.raises(TimeoutError):
        dispatch_native(contract_for("molmim"), {"smi": "CCO"}, store=store, invoke=unavailable)
    assert calls == ["molmim"]
    with pytest.raises(NativeResultError):
        dispatch_native(contract_for("molmim"), {"smi": "CCO"}, store=store,
                        invoke=lambda *_: {"result": {"success": True}})


def test_publication_failure_retains_s2_identity_without_repeating_execution(store, monkeypatch):
    calls = []

    def invoke(model_id, payload):
        calls.append(model_id)
        return {"operation": {"id": "native-completed-op", "status": "SUCCEEDED"},
                "result": binary(b"large-binary-output")}

    def denied(*args, **kwargs):
        raise OSError("private bucket detail")

    monkeypatch.setattr(store.client, "upload_fileobj", denied)
    with pytest.raises(NativeResultError) as caught:
        dispatch_native(contract_for("molmim"), {"smi": "CCO"}, store=store, invoke=invoke)
    assert calls == ["molmim"]
    error = caught.value.public()
    assert error["operation_id"] == "native-completed-op" and error["admission"] == "accepted"
    assert not error["retryable"] and "private bucket detail" not in json.dumps(error)


def test_real_sdk_lists_and_executes_the_same_flat_fields():
    model = replace(contract_for("molmim"), tool_name="test_exact_molmim")
    server = MCPServer("synthetic contract test")
    received = []

    @server.tool(name=model.tool_name)
    def execute(payload: dict) -> dict:
        received.append(payload)
        return {"accepted": payload}

    apply_tool_input_contract(server, model.tool_name, model)
    listed = asyncio.run(server.list_tools())
    assert listed[0].input_schema == model.schema("mcp")
    asyncio.run(server.call_tool(model.tool_name, {"smi": "CCO"}))
    assert received == [{"smi": "CCO"}]
    metadata = server._tool_manager.get_tool(model.tool_name).fn_metadata
    for bad in ({"payload": {"smi": "PRIVATE-SMILES"}}, {"smi": "PRIVATE-SMILES", "seed": 1},
                {"smi": "CCO", "idempotency_key": "not-implemented"}, {"smi": "CCO", "radius": float("nan")}):
        with pytest.raises(MCPError) as error:
            metadata.validate_arguments(bad)
        assert "PRIVATE-SMILES" not in str(error.value)
    with pytest.raises(ValueError):
        apply_tool_input_contract(server, model.tool_name, replace(model, tool_name="other"))
