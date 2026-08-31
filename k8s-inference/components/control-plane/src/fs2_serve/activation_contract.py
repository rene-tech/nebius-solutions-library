"""Controller projection of the canonical catalog's typed activation contract.

This module deliberately owns no JSON schema, signature parser, trust root, or
contract file. ``load_gateway_catalog`` performs those checks and publishes a
typed ``GatewayModel.scale_contract`` plus its signed ``ActivationBinding``.
The controller only projects that already-validated pair into exact Kubernetes
GET/PATCH inputs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from fs2_serve_catalog.loader import ScaleContract as CatalogScaleContract

from .models import ActivationTargetState
from .registry import OperationalModel


class ActivationContractError(ValueError):
    """The current typed route cannot authorize an activation mutation."""


@dataclass(frozen=True)
class ActivationTarget:
    api_version: str
    kind: str
    resource: str
    namespace: str
    name: str
    uid: str
    template_digest: str


@dataclass(frozen=True)
class ObservedTarget:
    model_id: str
    target_uid: str
    resource_version: str
    observed_generation: int
    template_digest: str
    active: bool
    ready: bool
    observed_at: datetime
    transition_digest: str | None = None
    transition_before_resource_version: str | None = None
    transition_before_generation: int | None = None
    observed_replicas: int = 0
    ready_replicas: int = 0
    available_replicas: int = 0
    zero_gpu_clients: bool = True
    cleanup_complete: bool = True

    @property
    def zero_resources(self) -> bool:
        return bool(
            not self.active
            and self.observed_replicas == 0
            and self.ready_replicas == 0
            and self.available_replicas == 0
            and self.zero_gpu_clients
            and self.cleanup_complete
        )


@dataclass(frozen=True)
class ScaleTransition:
    """One signed-contract-authorized mutation from an exact durable state."""

    contract_digest: str
    binding_digest: str
    model_id: str
    model_revision: str
    intent_id: str
    operation_attempt: int
    target_uid: str
    template_digest: str
    before_resource_version: str
    before_generation: int
    before_active: bool
    desired_active: bool
    digest: str

    @classmethod
    def authorize(
        cls,
        contract: ScaleContract,
        before: ObservedTarget,
        *,
        intent_id: str,
        operation_attempt: int,
        desired_active: bool,
    ) -> ScaleTransition:
        try:
            parsed_intent_id = UUID(intent_id)
        except ValueError:
            raise ActivationContractError("transition intent ID is not canonical") from None
        if str(parsed_intent_id) != intent_id or not 0 <= operation_attempt <= 10:
            raise ActivationContractError("transition intent fence is outside the closed contract")
        if before.active is desired_active:
            raise ActivationContractError("transition does not change the signed replica state")
        payload = {
            "schema": "fs2-serve.nebius.ai/replica-scale-transition/v1",
            "scale_contract_digest": contract.digest,
            "binding_digest": contract.binding_digest,
            "model_id": contract.model_id,
            "model_revision": contract.model_revision,
            "intent_id": intent_id,
            "operation_attempt": operation_attempt,
            "target_uid": before.target_uid,
            "template_digest": before.template_digest,
            "before_resource_version": before.resource_version,
            "before_generation": before.observed_generation,
            "before_active": before.active,
            "desired_active": desired_active,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return cls(
            contract_digest=contract.digest,
            binding_digest=contract.binding_digest,
            model_id=contract.model_id,
            model_revision=contract.model_revision,
            intent_id=intent_id,
            operation_attempt=operation_attempt,
            target_uid=before.target_uid,
            template_digest=before.template_digest,
            before_resource_version=before.resource_version,
            before_generation=before.observed_generation,
            before_active=before.active,
            desired_active=desired_active,
            digest=hashlib.sha256(encoded).hexdigest(),
        )

    def validate_issued(self, after: ObservedTarget) -> None:
        """Validate the exact result of an already-issued PATCH.

        Readiness and scale-to-zero cleanup can lag the Kubernetes API write.
        Keeping this check separate lets a newly fenced controller adopt the
        same intent-bound PATCH and continue waiting without issuing a second
        mutation.
        """

        if (
            after.model_id != self.model_id
            or after.target_uid != self.target_uid
            or after.template_digest != self.template_digest
            or after.resource_version == self.before_resource_version
            or after.observed_generation != self.before_generation + 1
            or after.active is not self.desired_active
            or after.transition_digest != self.digest
        ):
            raise ActivationContractError("issued PATCH is outside the signed one-step transition")

    def validate(self, after: ObservedTarget) -> None:
        self.validate_issued(after)
        if not after.ready or (not self.desired_active and not after.zero_resources):
            raise ActivationContractError("post-PATCH target is outside the signed readiness boundary")


@dataclass(frozen=True)
class ScaleContract:
    """Exact controller view derived only from canonical typed objects."""

    source: CatalogScaleContract
    model_id: str
    model_revision: str
    binding_digest: str
    digest: str
    target: ActivationTarget
    scale_field: str
    active_value: int | bool
    idle_value: int | bool
    allow_scale_to_zero: bool
    idle_seconds: int

    @classmethod
    def from_model(cls, model: OperationalModel) -> ScaleContract:
        binding = model.binding
        activation = binding.activation
        source = model.gateway.scale_contract
        raw: dict[str, Any] = source.to_dict()
        target = raw["target"]
        policy = raw["policy"]
        boundary = raw["controller_boundary"]["activation_controller"]
        if (
            not model.enabled
            or binding.backend_class != "local-kubernetes"
            or not activation.enabled
            or source.activation_mode != "replica-scale"
            or activation.scale_contract_digest != source.digest
            or target is None
        ):
            raise ActivationContractError("model lacks one enabled canonical activation contract")
        if (
            activation.controller_namespace != boundary["namespace"]
            or activation.controller_deployment_name != boundary["deployment_name"]
            or activation.controller_service_account_name != boundary["service_account_name"]
            or activation.controller_leader_lease_name != boundary["leader_lease_name"]
            or activation.controller_leader_role_namespace != boundary["leader_role_namespace"]
            or activation.controller_leader_role_name != boundary["leader_role_name"]
            or activation.controller_target_role_namespace != boundary["target_role_namespace"]
            or activation.controller_target_role_name != boundary["target_role_name"]
        ):
            raise ActivationContractError("activation controller differs from the typed boundary")
        expected_target = (
            activation.target_api_version,
            activation.target_kind,
            activation.target_namespace,
            activation.target_name,
            activation.target_template_identity_sha256,
        )
        typed_target = (
            target["api_version"],
            target["kind"],
            target["namespace"],
            target["name"],
            target["template_identity_sha256"],
        )
        if expected_target != typed_target:
            raise ActivationContractError("signed activation target differs from the typed contract")
        exact_values = (activation.target_uid, activation.target_template_identity_sha256)
        if any(value is None for value in exact_values):
            raise ActivationContractError("signed activation target lacks immutable UID or template identity")
        kind = target["kind"]
        try:
            resource, scale_field = {
                "Deployment": ("deployments", "spec.replicas"),
                "NIMService": ("nimservices", "spec.replicas"),
            }[kind]
        except KeyError as exc:
            raise ActivationContractError("the activation controller cannot own this target kind") from exc
        if (
            policy["replica_scaler_owner"] != "fs2-model-activation-controller"
            or policy["desired_floor"] != 0
            or policy["desired_max"] != 1
        ):
            raise ActivationContractError("typed activation policy is outside the controller boundary")
        assert activation.target_uid is not None
        assert activation.target_template_identity_sha256 is not None
        assert activation.target_name is not None
        return cls(
            source=source,
            model_id=model.id,
            model_revision=model.model_revision,
            binding_digest=binding.binding_digest,
            digest=source.digest,
            target=ActivationTarget(
                api_version=target["api_version"],
                kind=kind,
                resource=resource,
                namespace=target["namespace"],
                name=activation.target_name,
                uid=activation.target_uid,
                template_digest=activation.target_template_identity_sha256,
            ),
            scale_field=scale_field,
            active_value=policy["desired_max"],
            idle_value=policy["desired_floor"],
            allow_scale_to_zero=policy["scale_to_zero"],
            idle_seconds=policy["cooldown_seconds"],
        )

    def validate_observation(
        self,
        observation: ObservedTarget,
        prior: ActivationTargetState | None,
    ) -> None:
        if (
            observation.model_id != self.model_id
            or observation.target_uid != self.target.uid
            or observation.template_digest != self.target.template_digest
        ):
            raise ActivationContractError("live UID/template differs from signed ownership")
        if prior is None:
            if not observation.resource_version or observation.observed_generation <= 0:
                raise ActivationContractError("live target has no usable resourceVersion or generation")
            return
        if (
            prior.model_id != self.model_id
            or prior.target_uid != self.target.uid
            or prior.template_digest != self.target.template_digest
            or observation.resource_version != prior.resource_version
            or observation.observed_generation != prior.observed_generation
        ):
            raise ActivationContractError("live target differs from the durable fenced transition state")

    def transition(
        self,
        before: ObservedTarget,
        *,
        intent_id: str,
        operation_attempt: int,
        desired_active: bool,
    ) -> ScaleTransition:
        if (
            before.model_id != self.model_id
            or before.target_uid != self.target.uid
            or before.template_digest != self.target.template_digest
        ):
            raise ActivationContractError("transition origin differs from signed ownership")
        return ScaleTransition.authorize(
            self,
            before,
            intent_id=intent_id,
            operation_attempt=operation_attempt,
            desired_active=desired_active,
        )

    def recover_transition(
        self,
        after: ObservedTarget,
        *,
        intent_id: str,
        operation_attempt: int,
        desired_active: bool,
    ) -> ScaleTransition:
        """Reopen an atomically annotated post-PATCH state after controller loss."""

        if (
            after.transition_digest is None
            or after.transition_before_resource_version is None
            or after.transition_before_generation is None
        ):
            raise ActivationContractError("post-PATCH target lacks its transition origin")
        before = ObservedTarget(
            model_id=after.model_id,
            target_uid=after.target_uid,
            resource_version=after.transition_before_resource_version,
            observed_generation=after.transition_before_generation,
            template_digest=after.template_digest,
            active=not desired_active,
            ready=True,
            observed_at=after.observed_at,
        )
        transition = self.transition(
            before,
            intent_id=intent_id,
            operation_attempt=operation_attempt,
            desired_active=desired_active,
        )
        transition.validate_issued(after)
        return transition

    def validate_durable_target(self, target: ActivationTargetState) -> None:
        """Reopen one DB-fenced state in the signed transition chain."""

        self.validate_observation(
            ObservedTarget(
                model_id=target.model_id,
                target_uid=target.target_uid,
                resource_version=target.resource_version,
                observed_generation=target.observed_generation,
                template_digest=target.template_digest,
                active=target.active,
                ready=True,
                observed_at=target.observed_at,
            ),
            target,
        )
