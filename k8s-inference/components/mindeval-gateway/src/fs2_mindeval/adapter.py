import asyncio
import json
import math
import re
import time
from datetime import datetime, timezone
from hashlib import sha256

import httpx

from . import BASE_URL
from .contracts import GatewayError
from .prompts import CRITERIA
from .scheduler import FairScheduler


def parse_judgment(content: str) -> dict[str, float]:
    """Accept five exact numeric rubric lines or a strict five-key JSON object.

    Missing, duplicated, malformed or out-of-range scores always fail. Upstream
    averages are fractional on a 1–6 scale, so integer-only parsing is incorrect.
    """
    text = content.strip()
    # The pinned rubric itself demonstrates this exact enclosing XML tag.
    if text.startswith("<output>") and text.endswith("</output>"):
        text = text[len("<output>") : -len("</output>")].strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    if text.startswith("{"):

        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate criterion")
                result[key] = value
            return result

        try:
            scores = json.loads(text, object_pairs_hook=unique_pairs)
        except (ValueError, TypeError):
            raise GatewayError("invalid_judgment", "judge returned invalid JSON") from None
        if not isinstance(scores, dict) or set(scores) != set(CRITERIA):
            raise GatewayError("invalid_judgment", "judge must return all five rubric criteria exactly once")
    else:
        scores = {}
        for criterion in CRITERIA:
            pattern = rf"^\s*{re.escape(criterion)}:\s*([0-9]+(?:\.[0-9]+)?)\s*$"
            values = re.findall(pattern, text, re.MULTILINE)
            if len(values) != 1:
                raise GatewayError("invalid_judgment", f"missing or duplicate criterion: {criterion}")
            scores[criterion] = float(values[0])
        # Prevent partial parsing of malformed extra criterion lines.
        if len([line for line in text.splitlines() if line.strip()]) != 5:
            raise GatewayError("invalid_judgment", "judge returned unexpected content outside its five scores")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 1 <= value <= 6
        for value in scores.values()
    ):
        raise GatewayError("invalid_judgment", "judge scores must be finite numbers between 1 and 6")
    return {key: float(scores[key]) for key in CRITERIA}


def parse_response(data: dict) -> dict:
    try:
        choice = data["choices"][0]
        message = choice["message"]
        finish = choice["finish_reason"]
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str):
            raise TypeError()
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if not isinstance(reasoning, str):
            reasoning = json.dumps(reasoning, ensure_ascii=False)
        if "</think>" in content:
            before, content = content.rsplit("</think>", 1)
            reasoning = "\n".join(filter(None, (reasoning, before.replace("<think>", "").strip())))
        elif "<think>" in content:
            reasoning = "\n".join(filter(None, (reasoning, content.split("<think>", 1)[1])))
            content = ""
        usage = data.get("usage") or {}
        result = {
            "content": content.strip(),
            "reasoning": reasoning.strip(),
            "finish_reason": finish,
            "usage": usage,
            "provider_model": data.get("model"),
            "provider_request_id": data.get("id"),
        }
    except (KeyError, TypeError, IndexError, AttributeError):
        raise GatewayError("invalid_response", "provider response does not contain a chat completion") from None
    if finish == "length":
        raise GatewayError("length_finished", "provider exhausted the completion budget", telemetry=result)
    if finish != "stop":
        raise GatewayError("unexpected_finish", f"provider returned finish_reason={finish}", telemetry=result)
    if not result["content"]:
        code = "reasoning_only" if result["reasoning"] else "empty_content"
        raise GatewayError(code, "provider returned no visible answer", telemetry=result)
    return result


class ProviderStatus(Exception):
    def __init__(self, response: httpx.Response):
        self.response = response


class TokenFactoryAdapter:
    def __init__(
        self,
        key: str,
        scheduler: FairScheduler,
        *,
        client: httpx.AsyncClient | None = None,
        attempts: int = 4,
        sleep=asyncio.sleep,
    ):
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15))
        self.key, self.scheduler, self.attempts, self.sleep = key, scheduler, attempts, sleep
        self.compatibility = set()
        self.catalog = None

    async def discover(self) -> dict:
        response = await self.client.get(
            f"{BASE_URL}/models", params={"verbose": "true"}, headers={"authorization": f"Bearer {self.key}"}
        )
        if response.status_code != 200:
            raise GatewayError(
                "catalog_unavailable", f"catalog discovery returned HTTP {response.status_code}", status=503
            )
        raw = response.json()
        public = [
            model
            for model in raw["data"]
            if not model["id"].startswith("dedicated/")
            and model.get("architecture", {}).get("modality", "text->text").endswith("->text")
        ]
        snapshot = {"base_url": BASE_URL, "discovered_at": datetime.now(timezone.utc).isoformat(), "data": public}
        snapshot["sha256"] = sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()
        snapshot["source_sha256"] = sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        self.catalog = snapshot
        self.scheduler.configure(public)
        return snapshot

    async def complete(
        self,
        *,
        team: str,
        model: str,
        messages: list[dict],
        max_completion_tokens: int,
        temperature: float,
        limit: int = 5,
    ) -> dict:
        started, queue_ms, retry_codes = time.monotonic(), 0.0, []
        payload = {"model": model, "messages": messages, "temperature": temperature, "stream": False}
        if model in self.compatibility:
            payload["max_tokens"] = max_completion_tokens
        else:
            payload["max_completion_tokens"] = max_completion_tokens
        # UTF-8 bytes plus message overhead are a conservative tokenizer upper bound.
        reserve = sum(len(message["content"].encode()) + 16 for message in messages) + max_completion_tokens
        for attempt in range(self.attempts):

            async def send():
                response = await self.client.post(
                    f"{BASE_URL}/chat/completions", json=payload, headers={"authorization": f"Bearer {self.key}"}
                )
                if response.status_code != 200:
                    raise ProviderStatus(response)
                try:
                    return response.json()
                except ValueError:
                    raise GatewayError("invalid_json", "provider returned invalid JSON") from None

            try:
                raw, queued = await self.scheduler.submit(
                    team=team, model=model, tokens=reserve, limit=limit, call=send
                )
                queue_ms += queued
                result = parse_response(raw)
                result["model"] = model
                result["telemetry"] = {
                    "queue_ms": round(queue_ms, 3),
                    "latency_ms": round((time.monotonic() - started) * 1000, 3),
                    "retries": attempt,
                    "retry_codes": retry_codes,
                    "token_parameter": "max_tokens" if "max_tokens" in payload else "max_completion_tokens",
                }
                return result
            except ProviderStatus as exc:
                status = exc.response.status_code
                # Compatibility fallback is permitted only for an explicit unsupported parameter error.
                body = exc.response.text.lower()
                fallback = (
                    status == 400
                    and "max_completion_tokens" in body
                    and any(term in body for term in ("unsupported", "not supported", "unknown", "unrecognized"))
                    and "max_completion_tokens" in payload
                )
                if fallback:
                    payload["max_tokens"] = payload.pop("max_completion_tokens")
                    self.compatibility.add(model)
                    retry_codes.append("max_tokens_compatibility")
                    continue
                if status not in {429, 500, 502, 503, 504}:
                    raise GatewayError(
                        f"provider_http_{status}",
                        f"provider rejected request (HTTP {status})",
                        telemetry={"retries": attempt},
                    ) from None
                retry_codes.append(f"http_{status}")
                try:
                    retry_after = min(30.0, max(0.0, float(exc.response.headers.get("retry-after", 0))))
                except ValueError:
                    retry_after = 0
                delay = max(retry_after, min(8.0, 2**attempt))
            except (httpx.TimeoutException, httpx.TransportError):
                retry_codes.append("transport_error")
                delay = min(8.0, 2**attempt)
            except GatewayError as exc:
                exc.telemetry.update(
                    {
                        "queue_ms": round(queue_ms, 3),
                        "latency_ms": round((time.monotonic() - started) * 1000, 3),
                        "retries": attempt,
                        "retry_codes": retry_codes,
                    }
                )
                raise
            if attempt + 1 < self.attempts:
                await self.sleep(delay)
        raise GatewayError(
            "provider_retries_exhausted",
            "provider retry budget exhausted",
            telemetry={
                "retries": self.attempts - 1,
                "retry_codes": retry_codes,
                "queue_ms": queue_ms,
                "latency_ms": (time.monotonic() - started) * 1000,
            },
        )
