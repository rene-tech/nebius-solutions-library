"""Observational public MindGuard classifiers, separate from the private v2 clinician.

The checkpoint template targets the last user message. Transcript coverage therefore
requires a separate prefix evaluation for every user message, never one final call.
Endpoints are operator configuration, not user-supplied request fields. Callers must
persist returned assessments/errors under the enclosing authenticated run.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

MindGuardModelId = Literal["mindguard-4b", "mindguard-8b"]
CategoryCode = Literal["S1", "S2"]
REVISIONS: dict[str, str] = {
    "mindguard-4b": "f66279d31561e5e96736cb9cc5c2ec6e7d49d1c2",
    "mindguard-8b": "a24baa872d902224b3de80e912ad1f4201b514ed",
}
CATEGORY_LABELS = {"S1": "Self-harm risk", "S2": "Threats to others, abuse or neglect"}


class MindGuardModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MindGuardMessage(MindGuardModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=200_000)


class MindGuardError(MindGuardModel):
    code: str
    message: str
    retryable: bool = False
    http_status: int | None = None


class MindGuardCoverage(MindGuardModel):
    input_message_count: int = Field(ge=0)
    context_message_count: int = Field(ge=0)
    target_user_message_index: int | None = Field(default=None, ge=0)
    evaluated_user_turns: int = Field(default=0, ge=0, le=1)
    truncated: Literal[False] = False


class MindGuardAssessment(MindGuardModel):
    model_id: MindGuardModelId
    model_revision: str
    role: Literal["safety_classifier"] = "safety_classifier"
    enforcement: Literal["observe"] = "observe"
    status: Literal["completed", "error", "unavailable"]
    safety: Literal["safe", "unsafe"] | None = None
    categories: list[CategoryCode] = Field(default_factory=list)
    coverage: MindGuardCoverage
    latency_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    raw_output: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: MindGuardError | None = None

    @model_validator(mode="after")
    def consistent_result(self) -> MindGuardAssessment:
        if self.model_revision != REVISIONS[self.model_id]:
            raise ValueError("classifier revision differs from the pinned checkpoint")
        if self.status == "completed":
            if self.safety is None or self.error is not None or self.coverage.evaluated_user_turns != 1:
                raise ValueError("completed assessment requires a classification and one evaluated user turn")
            if (self.safety == "unsafe") != bool(self.categories):
                raise ValueError("unsafe requires categories; safe must not have categories")
        elif self.safety is not None or self.categories or self.error is None:
            raise ValueError("failed assessment requires an error, never a safety classification")
        return self


class MindGuardTranscriptAssessment(MindGuardModel):
    model_id: MindGuardModelId
    model_revision: str
    role: Literal["safety_classifier"] = "safety_classifier"
    enforcement: Literal["observe"] = "observe"
    status: Literal["completed", "partial", "unavailable"]
    input_user_turns: int
    evaluated_user_turns: int
    flagged_user_message_indices: list[int]
    assessments: list[MindGuardAssessment]


def parse_mindguard_output(output: str) -> tuple[Literal["safe", "unsafe"], list[CategoryCode]]:
    """Parse the pinned two-line format; ambiguity/errors never become a safe result."""
    match = re.fullmatch(
        r"\s*Safety:\s*(Safe|Unsafe)\s*\nCategories:\s*(None|S[12](?:\s*,\s*S[12])*)\s*",
        output,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("invalid MindGuard classification format")
    safety = cast(Literal["safe", "unsafe"], match.group(1).lower())
    codes = [] if match.group(2).lower() == "none" else sorted(set(re.split(r"\s*,\s*", match.group(2).upper())))
    if (safety == "unsafe") != bool(codes):
        raise ValueError("MindGuard safety label and categories disagree")
    return safety, cast(list[CategoryCode], codes)


async def assess_mindguard(
    messages: Sequence[MindGuardMessage],
    *,
    model_id: MindGuardModelId,
    endpoint: str | None,
    client: httpx.AsyncClient,
    language: str = "en",
    api_key: str | None = None,
) -> MindGuardAssessment:
    """Assess only the last user message, with preceding context and no truncation.

    `endpoint` is the configured preview/qualified runtime base URL, ending in /v1.
    Use an HTTP client with redirects disabled; it must not retry arbitrary POSTs.
    Trailing assistant messages are excluded because they follow the target user.
    """
    user_indices = [index for index, message in enumerate(messages) if message.role == "user"]
    target = user_indices[-1] if user_indices else None
    context = list(messages[: target + 1]) if target is not None else []
    coverage = MindGuardCoverage(
        input_message_count=len(messages), context_message_count=len(context), target_user_message_index=target
    )
    common: dict[str, Any] = {"model_id": model_id, "model_revision": REVISIONS[model_id], "coverage": coverage}
    warnings = ["short_context_outside_preferred_multiturn_use"] if len(user_indices) < 2 else []
    if target is not None and target + 1 < len(messages):
        warnings.append("trailing_assistant_messages_are_not_classification_targets")
    common["warnings"] = warnings

    def failure(code: str, message: str, *, unavailable: bool = False, **details: Any) -> MindGuardAssessment:
        return MindGuardAssessment(
            **common,
            status="unavailable" if unavailable else "error",
            error=MindGuardError(code=code, message=message, **details),
        )

    if not endpoint:
        return failure(
            "classifier_unavailable", "The safety classifier has no qualified runtime endpoint.", unavailable=True
        )
    if target is None:
        return failure("missing_user_turn", "A user turn is required for classification.")
    if language != "en":
        return failure("unsupported_language", "These checkpoints are qualified only for English transcripts.")

    started = time.perf_counter()
    try:
        response = await client.post(
            endpoint.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            json={
                "model": model_id,
                "messages": [message.model_dump() for message in context],
                "temperature": 0,
                "max_tokens": 15,
                "stream": False,
                "seed": 0,
            },
            timeout=120.0,
            follow_redirects=False,
        )
        common["latency_ms"] = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            return failure(
                "classifier_http_error",
                "The safety classifier request failed.",
                http_status=response.status_code,
                retryable=response.status_code in {429, 502, 503, 504},
            )
        body = response.json()
        if body.get("model") != model_id:
            return failure("model_identity_mismatch", "The runtime returned a different model identity.")
        choice = body["choices"][0]
        if choice.get("finish_reason") != "stop":
            return failure("incomplete_classification", "The classifier did not complete its output.")
        output = choice["message"]["content"]
        if not isinstance(output, str) or len(output) > 2048:
            return failure("invalid_classification", "The classifier returned an invalid response.")
        safety, categories = parse_mindguard_output(output)
        usage = body.get("usage") or {}
        common["coverage"] = coverage.model_copy(update={"evaluated_user_turns": 1})
        return MindGuardAssessment(
            **common,
            status="completed",
            safety=safety,
            categories=categories,
            raw_output=output,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
    except httpx.TimeoutException:
        common["latency_ms"] = (time.perf_counter() - started) * 1000
        return failure("classifier_timeout", "The safety classifier timed out.", retryable=True)
    except httpx.HTTPError:
        common["latency_ms"] = (time.perf_counter() - started) * 1000
        return failure("classifier_transport_error", "The safety classifier could not be reached.", retryable=True)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        common["coverage"] = coverage
        return failure("invalid_classification", "The classifier returned an invalid response.")


async def assess_mindguard_transcript(
    messages: Sequence[MindGuardMessage],
    *,
    model_id: MindGuardModelId,
    endpoint: str | None,
    client: httpx.AsyncClient,
    language: str = "en",
    api_key: str | None = None,
) -> MindGuardTranscriptAssessment:
    """Evaluate every user-turn prefix; errors remain visible and never stop the conversation."""
    indices = [index for index, message in enumerate(messages) if message.role == "user"]
    assessments = [
        await assess_mindguard(
            messages[: index + 1],
            model_id=model_id,
            endpoint=endpoint,
            client=client,
            language=language,
            api_key=api_key,
        )
        for index in indices
    ]
    completed = sum(result.status == "completed" for result in assessments)
    return MindGuardTranscriptAssessment(
        model_id=model_id,
        model_revision=REVISIONS[model_id],
        input_user_turns=len(indices),
        evaluated_user_turns=completed,
        status="completed" if indices and completed == len(indices) else "partial" if completed else "unavailable",
        flagged_user_message_indices=[
            index for index, result in zip(indices, assessments, strict=True) if result.safety == "unsafe"
        ],
        assessments=assessments,
    )
