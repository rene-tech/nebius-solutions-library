"""Canonical readiness identity for the exact enabled activation route set."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from .activation_contract import ScaleContract
from .registry import OperationalModel

ACTIVATION_SET_SCHEMA = "fs2-serve.nebius.ai/activation-controller-set/v1"


@dataclass(frozen=True)
class ActivationSet:
    """Value-suppressed identity shared by gateway and activation controller."""

    digest: str
    model_ids: tuple[str, ...]

    @property
    def required(self) -> bool:
        return bool(self.model_ids)


def activation_set(models: Sequence[OperationalModel]) -> ActivationSet:
    """Hash the sorted, typed local replica-scale routes and nothing else."""

    projection: list[dict[str, str]] = []
    for model in sorted(models, key=lambda item: item.id):
        if not model.enabled:
            continue
        binding = model.binding
        if binding.backend_class != "local-kubernetes" or not binding.activation.enabled:
            continue
        contract = ScaleContract.from_model(model)
        projection.append(
            {
                "model_id": model.id,
                "model_revision": model.model_revision,
                "binding_digest": binding.binding_digest,
                "scale_contract_digest": contract.digest,
                "intent_interface_sha256": binding.activation.intent_interface_sha256,
            }
        )
    payload = {"schema": ACTIVATION_SET_SCHEMA, "models": projection}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return ActivationSet(
        digest=hashlib.sha256(encoded).hexdigest(),
        model_ids=tuple(item["model_id"] for item in projection),
    )
