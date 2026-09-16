import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import JSONResponse

from .adapter import TokenFactoryAdapter, parse_judgment
from .contracts import CompletionRequest, GatewayError, Identity, JudgmentRequest, RunRegistration
from .prompts import ASSETS, PROVENANCE, judge_messages, profile_detail, profile_summaries
from .scheduler import FairScheduler
from .store import Store

PATIENT_MODELS = {"Qwen/Qwen3-30B-A3B-Instruct-2507"}
CLINICIAN_MODELS = {
    "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "openai/gpt-oss-120b",
    "zai-org/GLM-5.1",
    "deepseek-ai/DeepSeek-V4-Pro",
}
LOG = logging.getLogger("mindeval")


def family(model: str) -> str:
    value = model.lower()
    # Distillations inherit their base family for conflict detection.
    if "qwen" in value:
        return "qwen"
    if "hermes" in value or "llama" in value:
        return "llama"
    if "gemma" in value:
        return "gemma"
    return value.split("/", 1)[0]


def credential() -> str:
    key = os.getenv("NEBIUS_TOKEN_FACTORY_API_KEY") or os.getenv("NEBIUS_API_KEY")
    if not key:
        path = Path(os.getenv("MINDEVAL_KEY_FILE", "/run/secrets/token-factory/api-key"))
        if path.is_file():
            key = path.read_text().strip()
    if not key:
        raise RuntimeError("Token Factory credential is not configured")
    return key


def create_app(*, adapter=None, store=None, identity_provider=None, judge_model=None):
    scheduler = adapter.scheduler if adapter else FairScheduler()
    state = {"adapter": adapter, "store": store, "judge": judge_model}
    auth_client = httpx.AsyncClient(timeout=10)
    auth_url = os.getenv("MINDEVAL_AUTHZ_URL", "http://fs2-serve-control-plane.fs2-system.svc:8080/internal/ext-authz")

    @asynccontextmanager
    async def lifespan(app):
        state["adapter"] = state["adapter"] or TokenFactoryAdapter(credential(), scheduler)
        state["store"] = state["store"] or Store(os.getenv("MINDEVAL_DB_PATH", "/data/mindeval/gateway.sqlite"))
        if not state["judge"]:
            selection = json.loads((ASSETS / "judge_selection.json").read_text())
            state["judge"] = selection["selected_model"]
        await state["adapter"].discover()
        yield
        await scheduler.close()
        await state["adapter"].client.aclose()
        await auth_client.aclose()

    app = FastAPI(title="Scientific AI MindEval gateway", version="1", lifespan=lifespan)

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        # Visible answer/reasoning are never logged or reflected through error metadata.
        metadata = {k: v for k, v in exc.telemetry.items() if k not in {"content", "reasoning"}}
        return JSONResponse(
            status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message, "telemetry": metadata}}
        )

    async def verified(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise GatewayError("unauthorized", "platform bearer token is required", status=401)
        if identity_provider:
            identity = await identity_provider(authorization)
        else:
            try:
                response = await auth_client.get(auth_url, headers={"authorization": authorization})
            except httpx.HTTPError:
                raise GatewayError(
                    "authorization_unavailable", "platform authorization is unavailable", status=503
                ) from None
            if response.status_code != 200:
                status = 401 if response.status_code in {401, 403} else 503
                raise GatewayError("unauthorized", "platform rejected bearer token", status=status)
            try:
                identity = Identity(
                    tenant_id=response.headers["x-fs2-tenant"],
                    principal_id=response.headers["x-fs2-principal"],
                    token_id=response.headers["x-fs2-token-id"],
                    scopes=json.loads(response.headers["x-fs2-scopes"]),
                    models=json.loads(response.headers["x-fs2-models"]),
                    max_concurrency=int(response.headers["x-fs2-max-concurrency"]),
                )
            except (KeyError, ValueError):
                raise GatewayError(
                    "authorization_contract_missing", "platform authorization policy headers are missing", status=503
                ) from None
        identity.require()
        return identity

    def team(identity):
        return json.dumps([identity.tenant_id, identity.principal_id], separators=(",", ":"))

    def check_public_model(model):
        if not any(item["id"] == model for item in state["adapter"].catalog["data"]):
            raise GatewayError("model_unavailable", "model is absent from the discovered public catalog", status=422)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "catalog_ready": state["adapter"] is not None and state["adapter"].catalog is not None}

    @app.get("/v1/mindeval/catalog")
    async def catalog(identity: Identity = Depends(verified)):
        current = state["adapter"].catalog
        models = []
        for entry in current["data"]:
            if identity.models.intersection({"*", "mindeval", entry["id"]}):
                models.append(
                    {
                        **entry,
                        "family": family(entry["id"]),
                        "patient_eligible": entry["id"] in PATIENT_MODELS,
                        "clinician_eligible": entry["id"] in CLINICIAN_MODELS
                        and family(entry["id"]) != family(state["judge"]),
                    }
                )
        return {
            **current,
            "data": models,
            "judge_model": state["judge"],
            "judge_family": family(state["judge"]),
            "provenance": PROVENANCE,
            "limits": {"max_profiles": 20, "workers_per_team": 5},
        }

    @app.get("/v1/mindeval/profiles")
    async def profiles(identity: Identity = Depends(verified)):
        return {**PROVENANCE, "upstream_revision": PROVENANCE["revision"], "data": profile_summaries()}

    @app.get("/v1/mindeval/profiles/{profile_id}")
    async def profile(profile_id: str, identity: Identity = Depends(verified)):
        try:
            return profile_detail(profile_id)
        except ValueError:
            raise GatewayError("profile_not_found", "unknown upstream profile", status=404) from None

    @app.post("/v1/mindeval/runs/{run_id}/register")
    async def register(run_id: str, request: RunRegistration, identity: Identity = Depends(verified)):
        if not 1 <= len(run_id) <= 120:
            raise GatewayError("invalid_run_id", "run ID length outside bound", status=422)
        for profile_id in request.profile_ids:
            try:
                profile_detail(profile_id)
            except ValueError:
                raise GatewayError("profile_not_found", "unknown upstream profile", status=422) from None
        if request.patient_model not in PATIENT_MODELS:
            raise GatewayError("patient_model_denied", "choose a validated patient model", status=422)
        if any(model not in CLINICIAN_MODELS for model in request.clinician_models):
            raise GatewayError("clinician_model_denied", "choose a contract-tested clinician model", status=422)
        for model in [request.patient_model, *request.clinician_models, state["judge"]]:
            identity.require(model)
            check_public_model(model)
        if any(family(model) == family(state["judge"]) for model in request.clinician_models):
            raise GatewayError(
                "judge_family_conflict", "fixed judge family conflicts with the clinician suite", status=422
            )
        # Refresh at each NEW run. A replay returns the original immutable snapshot.
        try:
            existing, snapshot = state["store"].get(team(identity), run_id)
            return state["store"].register(team(identity), run_id, request.model_dump(), snapshot)
        except GatewayError as exc:
            if exc.code != "run_not_registered":
                raise
        snapshot = {
            "run_id": run_id,
            "catalog_snapshot": await state["adapter"].discover(),
            "provenance": PROVENANCE,
            "judge_model": state["judge"],
            "judge_family": family(state["judge"]),
            "profile_ids": request.profile_ids,
            "patient_model": request.patient_model,
            "clinician_models": request.clinician_models,
        }
        return state["store"].register(team(identity), run_id, request.model_dump(), snapshot)

    def registered(identity, run_id, profile_id, model, role):
        config, snapshot = state["store"].get(team(identity), run_id)
        if profile_id not in config["profile_ids"]:
            raise GatewayError("profile_not_registered", "profile is outside this run", status=403)
        allowed = [config["patient_model"]] if role == "patient" else config["clinician_models"]
        if model not in allowed:
            raise GatewayError("model_not_registered", "model is outside this run role", status=403)
        identity.require(model)
        return snapshot

    async def invoke(identity, request, model, messages, temperature, role):
        request_id = str(uuid4())
        event = {"request_id": request_id, "profile_id": request.profile_id, "role": role, "model": model}
        try:
            result = await state["adapter"].complete(
                team=team(identity),
                model=model,
                messages=messages,
                max_completion_tokens=request.max_completion_tokens,
                temperature=temperature,
                limit=identity.max_concurrency,
            )
            if role == "judge":
                try:
                    result["judgment"] = parse_judgment(result["content"])
                except GatewayError as exc:
                    exc.telemetry = {
                        **result["telemetry"],
                        "usage": result["usage"],
                        "finish_reason": result["finish_reason"],
                    }
                    raise
                result["overall_score"] = sum(result["judgment"].values()) / 5
            result["telemetry"]["request_id"] = request_id
            event.update(
                status="succeeded", usage=result["usage"], finish_reason=result["finish_reason"], **result["telemetry"]
            )
            return result
        except GatewayError as exc:
            event.update(
                status="failed",
                error_code=exc.code,
                **{k: v for k, v in exc.telemetry.items() if k not in {"content", "reasoning"}},
            )
            exc.telemetry["request_id"] = request_id
            raise
        finally:
            state["store"].event(team(identity), request.run_id, event)
            LOG.info(
                json.dumps(
                    {
                        "run_id": request.run_id,
                        "tenant_id": identity.tenant_id,
                        "principal_id": identity.principal_id,
                        **event,
                    }
                )
            )

    @app.post("/v1/mindeval/completions")
    async def completion(request: CompletionRequest, identity: Identity = Depends(verified)):
        registered(identity, request.run_id, request.profile_id, request.model, request.role)
        return await invoke(
            identity,
            request,
            request.model,
            [m.model_dump() for m in request.messages],
            request.temperature,
            request.role,
        )

    @app.post("/v1/mindeval/judgments")
    async def judgment(request: JudgmentRequest, identity: Identity = Depends(verified)):
        snapshot = registered(identity, request.run_id, request.profile_id, request.clinician_model, "clinician")
        model = snapshot["judge_model"]
        identity.require(model)
        messages = judge_messages(
            profile_detail(request.profile_id)["profile"], [m.model_dump() for m in request.interaction]
        )
        return await invoke(identity, request, model, messages, 0, "judge")

    @app.get("/v1/mindeval/runs/{run_id}/events")
    async def events(run_id: str, after: int = Query(default=0, ge=0), identity: Identity = Depends(verified)):
        return {"data": state["store"].events(team(identity), run_id, after)}

    @app.get("/v1/mindeval/queue")
    async def queue(identity: Identity = Depends(verified)):
        return scheduler.status()

    return app


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")), workers=1)
