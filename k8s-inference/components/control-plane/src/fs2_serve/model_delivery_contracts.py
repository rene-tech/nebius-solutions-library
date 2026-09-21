"""Model-specific execution limits, separate from payloads and quality retries."""

from __future__ import annotations

from dataclasses import replace

from .registry import OperationalModel


def admission_model(model: OperationalModel, *, protocol: str, operation: str) -> OperationalModel:
    """Freeze delivery constraints into a new operation without changing a route.

    NVIDIA PAIDF bc571936's Transfer 2.5 cookbook has zero generation retries.
    Its NIM adapter cannot recover every lost response by a durable operation ID;
    retrying an HTTP failure, timeout or expired worker lease could generate twice.
    One gateway attempt therefore includes every outcome, not just quality failure.
    Existing durable operations retain their original admission snapshots.
    """
    source = model.dynamic_policy.publication.source_model_ref if model.dynamic_policy else model.id
    if (source, protocol, operation) != ("cosmos-transfer2-5-2b", "native", "transfer-video"):
        return model
    return replace(model, max_attempts=1)
