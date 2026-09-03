"""Scientific model-readiness projection from immutable catalog evidence."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .registry import OperationalModel, Registry, RegistryError
from .scientific_admin import (
    ScientificAdminSourceUnavailableError,
    ScientificModelAdminAdapter,
    ScientificModelSnapshot,
)
from .scientific_admin_models import (
    ScientificAccessAuthorization,
    ScientificAccessGate,
    ScientificBackendIdentity,
    ScientificCachingReadiness,
    ScientificExplicitAlternative,
    ScientificModelProjectionIssue,
    ScientificModelReadiness,
    ScientificModelReadinessList,
    ScientificQualificationJoin,
    ScientificServiceClass,
)
from .scientific_batch.service import ScientificBatchService, ScientificProfileDiscovery

SCIENTIFIC_SOURCE_RECEIPTS = "scientific-source-candidate-receipts.json"
SCIENTIFIC_WORKLOAD_PROFILES = "scientific-workload-profiles.json"
ACADEMIC_ASSET_READINESS = "academic-asset-readiness.json"
MAX_PROJECTION_ISSUES = 256
_REFERENCE_DATA_MODELS = frozenset({"alphafold3", "protenix-v2"})
_ALTERNATIVES = {
    "alphafold3": ScientificExplicitAlternative(
        model_id="openfold3",
        display_name="OpenFold3",
        reason="Open alternative; it is not represented as native AlphaFold3.",
    ),
    "bindcraft": ScientificExplicitAlternative(
        model_id="freebindcraft",
        display_name="FreeBindCraft",
        reason="Open alternative; it is not represented as native BindCraft/PyRosetta.",
    ),
}


# Same rule as scientific_batch.adapters.common.profile_state_is_consistent: a candidate is
# never routed, a dispatchable or qualified profile always is, and a mismatched pair is
# rejected rather than silently accepted.
_ROUTED_PROFILE_STATES = frozenset({"active", "qualified"})


def _profile_state_is_consistent(state: object, route_exposed: object) -> bool:
    if state == "candidate-unqualified":
        return route_exposed is False
    if state in _ROUTED_PROFILE_STATES:
        return route_exposed is True
    return False


class ScientificProfileDiscoveryAdapter:
    """Project only profiles that the controller says this tenant can submit."""

    def __init__(
        self,
        *,
        scientific_batches: ScientificBatchService | None,
        global_catalog: ScientificModelAdminAdapter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.scientific_batches = scientific_batches
        self.global_catalog = global_catalog
        self.clock = clock or (lambda: datetime.now(UTC))

    async def list_models(self, *, tenant_id: str | None = None) -> ScientificModelSnapshot:
        if tenant_id is None and self.global_catalog is not None:
            return await self.global_catalog.list_models()
        profiles = (
            ()
            if self.scientific_batches is None
            else self.scientific_batches.discovery_profiles(
                tenant_id=tenant_id,
                allowed_models=frozenset({"*"}),
                surface="admin",
            )
        )
        return ScientificModelSnapshot(
            data=ScientificModelReadinessList(items=[self._project(profile) for profile in profiles]),
            observed_at=self.clock().astimezone(UTC),
        )


    @staticmethod
    def _project(profile: ScientificProfileDiscovery) -> ScientificModelReadiness:
        service_classes = [ScientificServiceClass(value) for value in profile.service_classes]
        access_profile: Literal["standard", "academic"] = (
            "academic" if profile.access_profile == "academic" else "standard"
        )
        access_state: Literal["not-required", "unverified", "verified", "blocked"] = (
            "verified" if access_profile == "academic" else "not-required"
        )
        execution_mode: Literal["scientific-batch", "hybrid"] = (
            "hybrid" if profile.execution_mode == "hybrid" else "scientific-batch"
        )
        return ScientificModelReadiness(
            model_id=profile.model_id,
            candidate_id=profile.model_id,
            display_name=profile.display_name,
            readiness="qualified",
            readiness_reason=(
                "The exact profile, execution map, access binding, and scheduler eligibility are active."
            ),
            workload_profile="published",
            missing_evidence=[],
            qualification=ScientificQualificationJoin(
                state="qualified",
                reason="The controller exposed this exact qualified profile as submittable for the selected tenant.",
                compared=[
                    "source_revision",
                    "runtime_image_digest",
                    "execution_identity_digest",
                    "tenant_access",
                    "scheduler_eligibility",
                ],
            ),
            execution_mode=execution_mode,
            batch_supported=True,
            interactive_supported=execution_mode == "hybrid",
            service_classes=service_classes,
            backend=ScientificBackendIdentity(
                backend_id=profile.variant_id,
                kind="qualified-scientific-profile",
                source_repository=profile.source_repository,
                source_revision=profile.source_revision,
                model_revision=profile.source_revision,
                runtime_image_digest=profile.runtime_image_digest,
                execution_identity_digest=profile.execution_identity_sha256,
            ),
            access=ScientificAccessGate(
                profile=access_profile,
                state=access_state,
                gate=(
                    "Deployment-bound tenant academic authorization is verified."
                    if access_profile == "academic"
                    else "No restricted academic asset is required by this backend."
                ),
                receipt_digest=profile.access_receipt_digest,
                authorization=None,
                formal_license_status=(
                    "FormalAcceptancePending" if access_profile == "academic" else "not-applicable"
                ),
                alternative=None,
            ),
            caching=ScientificCachingReadiness(
                exact_tier="not-observed",
                image="verified",
                artifacts="verified",
                reference_data="unsupported",
                runtime_checkpoint="unavailable",
                gpu_snapshot="unavailable",
                reason="Discovery does not infer a fast-start tier without an exact observation.",
            ),
        )


@dataclass(frozen=True, slots=True)
class CandidateRouteScope:
    """The exact serving lane and variants that belong to one candidate."""

    lane_id: str
    variant_ids: frozenset[str]
    mapped: bool = False


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: object, *, maximum: int = 256) -> str | None:
    return value if isinstance(value, str) and 1 <= len(value) <= maximum else None


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScientificAdminSourceUnavailableError(f"scientific candidate {label} is invalid")
    return value


def _text(value: object, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ScientificAdminSourceUnavailableError(f"scientific candidate {label} is invalid")
    return value


def _operational(registry: Registry, model_id: str) -> OperationalModel | None:
    try:
        return registry.get(model_id, require_enabled=False)
    except KeyError:
        return None


def _execution_identity_join(
    *,
    candidate_id: str,
    route_scope: CandidateRouteScope,
    backend_identity: str,
    operational: OperationalModel | None,
    execution_identity: Mapping[str, Any],
) -> ScientificQualificationJoin:
    """Join a scientific candidate to a live model only on its whole identity.

    Serving lanes are shared: `rfdiffusion-upstream` resolves to the
    `rfdiffusion` lane, which the delivered catalog also serves from a vendor
    NIM container. Accepting the lane name as proof would publish that
    container's digest and qualification as the candidate's own evidence, so
    every immutable field the candidate declares has to match.
    """

    lane_id = route_scope.lane_id
    lane = lane_id if lane_id != candidate_id else None
    if operational is None:
        return ScientificQualificationJoin(
            state="not-registered",
            reason=f"no live model is registered on serving lane {lane_id}",
            serving_lane_id=lane,
        )

    expected = {
        "model_revision": _optional_text(execution_identity.get("model_revision")),
        "runtime_image_digest": _optional_text(execution_identity.get("runtime_image_digest")),
    }
    if any(value is None for value in expected.values()):
        return ScientificQualificationJoin(
            state="evidence-absent",
            reason="the candidate declares no immutable model revision and runtime image to compare",
            serving_lane_id=lane,
            compared=sorted(key for key, value in expected.items() if value is not None),
        )

    # A promoted variant intentionally replaces the disabled canonical base
    # runtime. Registry currently exposes its exact variant and image but not a
    # typed source-revision projection, so never compare the candidate source
    # revision to the base model revision or claim a complete join from it.
    variant_source_unavailable = operational.variant_id is not None
    live = {"runtime_image_digest": operational.gateway.runtime_image_digest}
    if not variant_source_unavailable:
        live["model_revision"] = operational.gateway.model_revision
    mismatched = sorted(key for key, value in expected.items() if live.get(key) != value)
    if variant_source_unavailable:
        mismatched = [key for key in mismatched if key != "model_revision"]
        compared = ["runtime_image_digest"]
    else:
        compared = sorted(expected)
    # A mapped candidate may share a public model ID with a vendor NIM. It can
    # inherit qualification only from one of the exact source variants named by
    # the variant map, never from the canonical lane merely because the ID is
    # the same. Unmapped native candidates likewise cannot inherit a NIM route.
    if route_scope.mapped:
        compared.append("variant_id")
        if operational.variant_id not in route_scope.variant_ids:
            mismatched.append("variant_id")
    elif backend_identity == "native-upstream":
        compared.append("runtime_kind")
        if operational.gateway.runtime_kind.casefold() == "nim":
            mismatched.append("runtime_kind")
    compared = sorted(set(compared))
    mismatched = sorted(set(mismatched))
    if mismatched:
        return ScientificQualificationJoin(
            state="identity-mismatch",
            reason=(
                f"the live model on lane {lane_id} declares a different execution identity, "
                "so its evidence is not this candidate's"
            ),
            serving_lane_id=lane,
            compared=compared,
            mismatched=mismatched,
        )
    # Both accepted v1 documents are candidate-only contracts by schema. A
    # live route can expose an identity mismatch, but it cannot upgrade those
    # bytes. A future qualified projection needs a new typed schema and an
    # authoritative evidence join.
    return ScientificQualificationJoin(
        state="evidence-absent",
        reason="the v1 candidate receipt and workload profile explicitly remain unqualified",
        serving_lane_id=lane,
        compared=compared,
    )


class ScientificCatalogFileAdapter:
    """Read candidate identities while upgrading only exact live-qualified routes."""

    def __init__(
        self,
        *,
        registry: Registry,
        receipts_file: Path,
        variants_file: Path | None = None,
        profiles_file: Path | None = None,
        academic_readiness_file: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.registry = registry
        self.receipts_file = receipts_file
        self.variants_file = variants_file or receipts_file.with_name("model-variants.json")
        self.profiles_file = profiles_file or receipts_file.with_name(SCIENTIFIC_WORKLOAD_PROFILES)
        self.academic_readiness_file = academic_readiness_file or receipts_file.with_name(ACADEMIC_ASSET_READINESS)
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _bounded_document(path: Path, *, label: str) -> Mapping[str, Any]:
        try:
            raw = path.read_text(encoding="utf-8")
            if len(raw.encode("utf-8")) > 4 * 1024 * 1024:
                raise ScientificAdminSourceUnavailableError(f"{label} is too large")
            return _mapping(json.loads(raw), label)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScientificAdminSourceUnavailableError(f"{label} is unavailable") from exc

    def _workload_profiles(
        self,
    ) -> tuple[
        dict[str, Mapping[str, Any]],
        dict[str, str],
        list[ScientificModelProjectionIssue],
    ]:
        document = self._bounded_document(self.profiles_file, label="scientific workload profile set")
        if document.get("schema") != "fs2-serve.nebius.ai/scientific-workload-profiles/v1":
            raise ScientificAdminSourceUnavailableError("scientific workload profile schema is unsupported")
        values = document.get("profiles")
        if not isinstance(values, list) or len(values) > 256:
            raise ScientificAdminSourceUnavailableError("scientific workload profile list is invalid")
        profiles: dict[str, Mapping[str, Any]] = {}
        errors: dict[str, str] = {}
        issues: list[ScientificModelProjectionIssue] = []
        for value in values:
            profile = _mapping_or_empty(value)
            model_id = _optional_text(profile.get("model_id"), maximum=128)
            reason: str | None = None
            if not profile:
                reason = "workload profile entry is not an object"
            elif model_id is None:
                reason = "workload profile entry has no bounded model identity"
            elif model_id in profiles or model_id in errors:
                reason = "workload profile identity is duplicated"
                profiles.pop(model_id, None)
            elif profile.get("schema") != "fs2-serve.nebius.ai/scientific-workload-profile/v1":
                reason = "workload profile entry schema is unsupported"
            elif not _profile_state_is_consistent(
                profile.get("state"), profile.get("route_exposed")
            ):
                reason = "v1 workload profile state and route exposure disagree"
            if reason is not None:
                if model_id is not None:
                    errors[model_id] = reason
                issues.append(
                    ScientificModelProjectionIssue(
                        candidate_id=model_id,
                        source="workload-profile",
                        reason=reason,
                    )
                )
                continue
            assert model_id is not None
            profiles[model_id] = profile
        return profiles, errors, issues

    def _academic_readiness(
        self,
    ) -> tuple[
        dict[str, Mapping[str, Any]],
        bool,
        dict[str, str],
        list[ScientificModelProjectionIssue],
    ]:
        """Return per-model academic evidence and whether a request-time receipt is demanded.

        A document that demands a request-time receipt contradicts the
        deployment-bound admission model. That is reported against the academic
        models it covers, not raised for the whole catalog, so unrelated
        standard candidates still project.
        """

        document = self._bounded_document(self.academic_readiness_file, label="academic asset readiness projection")
        if document.get("schema") != "fs2-serve.nebius.ai/academic-asset-readiness/v1":
            raise ScientificAdminSourceUnavailableError("academic asset readiness schema is unsupported")
        receipt_required = document.get("request_time_license_receipt_required") is not False
        asset_namespace = _optional_text(
            _mapping_or_empty(document.get("execution")).get("namespace"),
            maximum=63,
        )
        values = document.get("models")
        if not isinstance(values, list) or len(values) > 64:
            raise ScientificAdminSourceUnavailableError("academic asset readiness list is invalid")
        readiness: dict[str, Mapping[str, Any]] = {}
        errors: dict[str, str] = {}
        issues: list[ScientificModelProjectionIssue] = []
        for value in values:
            item = _mapping_or_empty(value)
            model_id = _optional_text(item.get("model_id"), maximum=128)
            reason: str | None = None
            if not item:
                reason = "academic readiness entry is not an object"
            elif model_id is None:
                reason = "academic readiness entry has no bounded model identity"
            elif model_id in readiness or model_id in errors:
                reason = "academic readiness identity is duplicated"
                readiness.pop(model_id, None)
            elif _optional_text(item.get("asset_id"), maximum=128) is None:
                reason = "academic readiness entry has no bounded asset identity"
            if reason is not None:
                if model_id is not None:
                    errors[model_id] = reason
                issues.append(
                    ScientificModelProjectionIssue(
                        candidate_id=model_id,
                        source="academic-readiness",
                        reason=reason,
                    )
                )
                continue
            assert model_id is not None
            readiness[model_id] = {**item, "_asset_namespace": asset_namespace}
        return readiness, receipt_required, errors, issues

    def _route_scopes(self) -> tuple[dict[str, CandidateRouteScope], list[ScientificModelProjectionIssue]]:
        if not self.variants_file.is_file():
            return {}, []
        try:
            raw = self.variants_file.read_text(encoding="utf-8")
            if len(raw.encode("utf-8")) > 4 * 1024 * 1024:
                raise ScientificAdminSourceUnavailableError("model variant map is too large")
            document = _mapping(json.loads(raw), "model variant map")
            candidates = _mapping(document.get("fallback_candidates"), "model variant candidates")
            if len(candidates) > 256:
                raise ScientificAdminSourceUnavailableError("model variant candidate list is invalid")
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ScientificAdminSourceUnavailableError,
        ):
            return {}, [
                ScientificModelProjectionIssue(
                    candidate_id=None,
                    source="variant-map",
                    reason="model variant map is unavailable",
                )
            ]
        scopes: dict[str, CandidateRouteScope] = {}
        issues: list[ScientificModelProjectionIssue] = []
        for candidate_id, value in candidates.items():
            bounded_candidate = _optional_text(candidate_id, maximum=128)
            candidate = _mapping_or_empty(value)
            lane_id = _optional_text(candidate.get("lane_id"), maximum=128)
            if bounded_candidate is None or not candidate or lane_id is None:
                issues.append(
                    ScientificModelProjectionIssue(
                        candidate_id=bounded_candidate,
                        source="variant-map",
                        reason="model variant candidate is invalid",
                    )
                )
                continue
            profile_variants = _mapping_or_empty(candidate.get("profile_variants"))
            variant_ids = frozenset(
                value
                for value in profile_variants.values()
                if isinstance(value, str) and 1 <= len(value) <= 128
            )
            scopes[bounded_candidate] = CandidateRouteScope(
                lane_id=lane_id,
                variant_ids=variant_ids,
                mapped=True,
            )
        return scopes, issues

    async def list_models(self, *, tenant_id: str | None = None) -> ScientificModelSnapshot:
        del tenant_id
        observed_at = self.clock().astimezone(UTC)
        try:
            raw = self.receipts_file.read_text(encoding="utf-8")
            if len(raw.encode("utf-8")) > 1024 * 1024:
                raise ScientificAdminSourceUnavailableError("scientific candidate receipt set is too large")
            document = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScientificAdminSourceUnavailableError("scientific candidate receipts are unavailable") from exc
        root = _mapping(document, "receipt set")
        if root.get("schema") != "fs2-serve.nebius.ai/scientific-source-candidate-receipts/v1":
            raise ScientificAdminSourceUnavailableError("scientific candidate receipt schema is unsupported")
        receipts = root.get("receipts")
        if not isinstance(receipts, list) or len(receipts) > 256:
            raise ScientificAdminSourceUnavailableError("scientific candidate receipt list is invalid")

        route_scopes, variant_issues = self._route_scopes()
        try:
            profiles, profile_errors, profile_issues = self._workload_profiles()
        except ScientificAdminSourceUnavailableError as exc:
            profiles, profile_errors = {}, {}
            profile_issues = [
                ScientificModelProjectionIssue(
                    candidate_id=None,
                    source="workload-profile",
                    reason=str(exc)[:300] or "scientific workload profiles are unavailable",
                )
            ]
        try:
            academic_readiness, receipt_required, academic_errors, academic_issues = self._academic_readiness()
        except ScientificAdminSourceUnavailableError as exc:
            academic_readiness, receipt_required, academic_errors = {}, False, {}
            academic_issues = [
                ScientificModelProjectionIssue(
                    candidate_id=None,
                    source="academic-readiness",
                    reason=str(exc)[:300] or "academic asset readiness is unavailable",
                )
            ]
        # Every candidate projects on its own evidence. One model missing a
        # workload profile withholds that model's execution shape; it must not
        # take down the readiness of the models that do publish one.
        items: list[ScientificModelReadiness] = []
        issues = [*variant_issues, *profile_issues, *academic_issues]
        candidate_counts: dict[str, int] = {}
        for value in receipts:
            candidate_id = _optional_text(_mapping_or_empty(value).get("model_id"), maximum=128)
            if candidate_id is not None:
                candidate_counts[candidate_id] = candidate_counts.get(candidate_id, 0) + 1
        seen_candidates: set[str] = set()
        for value in receipts:
            receipt = _mapping_or_empty(value)
            candidate_id = _optional_text(receipt.get("model_id"), maximum=128)
            if candidate_id is None:
                issues.append(
                    ScientificModelProjectionIssue(
                        candidate_id=None,
                        source="candidate-receipt",
                        reason="candidate receipt has no bounded model identity",
                    )
                )
                continue
            if candidate_counts[candidate_id] > 1:
                if candidate_id in seen_candidates:
                    continue
                seen_candidates.add(candidate_id)
                issues.append(
                    ScientificModelProjectionIssue(
                        candidate_id=candidate_id,
                        source="candidate-receipt",
                        reason="candidate receipt identity is duplicated",
                    )
                )
                items.append(
                    self._unprojectable(
                        receipt,
                        candidate_id=candidate_id,
                        route_scope=route_scopes.get(
                            candidate_id,
                            CandidateRouteScope(lane_id=candidate_id, variant_ids=frozenset()),
                        ),
                    )
                )
                continue
            seen_candidates.add(candidate_id)
            try:
                item = self._project(
                    receipt,
                    route_scopes=route_scopes,
                    profiles=profiles,
                    profile_errors=profile_errors,
                    academic_readiness=academic_readiness,
                    academic_errors=academic_errors,
                    request_time_receipt_demanded=receipt_required,
                )
                if "source-identity-agreement" in item.missing_evidence:
                    issues.append(
                        ScientificModelProjectionIssue(
                            candidate_id=candidate_id,
                            source="workload-profile",
                            reason=(
                                "candidate receipt, workload profile source, and execution model identities disagree"
                            ),
                        )
                    )
            except (ScientificAdminSourceUnavailableError, ValueError) as exc:
                reason = str(exc)[:300] or "candidate projection is invalid"
                issues.append(
                    ScientificModelProjectionIssue(
                        candidate_id=candidate_id,
                        source="candidate-receipt",
                        reason=reason,
                    )
                )
                item = self._unprojectable(
                    receipt,
                    candidate_id=candidate_id,
                    route_scope=route_scopes.get(
                        candidate_id,
                        CandidateRouteScope(lane_id=candidate_id, variant_ids=frozenset()),
                    ),
                )
            items.append(item)
        return ScientificModelSnapshot(
            data=ScientificModelReadinessList(
                items=sorted(items, key=lambda item: (item.model_id, item.candidate_id)),
                projection_issues=sorted(
                    issues,
                    key=lambda issue: (issue.source, issue.candidate_id or "", issue.reason),
                )[:MAX_PROJECTION_ISSUES],
            ),
            observed_at=observed_at,
        )

    def _project(
        self,
        receipt: Mapping[str, Any],
        *,
        route_scopes: Mapping[str, CandidateRouteScope],
        profiles: Mapping[str, Mapping[str, Any]],
        profile_errors: Mapping[str, str],
        academic_readiness: Mapping[str, Mapping[str, Any]],
        academic_errors: Mapping[str, str],
        request_time_receipt_demanded: bool,
    ) -> ScientificModelReadiness:
        candidate_id = _text(receipt.get("model_id"), "model_id", 128)
        route_scope = route_scopes.get(
            candidate_id,
            CandidateRouteScope(lane_id=candidate_id, variant_ids=frozenset()),
        )
        lane_id = route_scope.lane_id
        raw_access_profile = _text(receipt.get("access_profile"), "access profile", 32)
        if receipt.get("status") != "candidate" or receipt.get("qualification_state") != "unqualified":
            raise ScientificAdminSourceUnavailableError("v1 candidate receipt qualification state is invalid")
        if raw_access_profile not in {"standard", "academic"}:
            raise ScientificAdminSourceUnavailableError("scientific candidate access profile is unsupported")
        access_profile: Literal["standard", "academic"] = "academic" if raw_access_profile == "academic" else "standard"
        academic = access_profile == "academic"
        receipt_source = _mapping_or_empty(receipt.get("source"))
        missing: list[str] = []

        # Candidate profiles are identity-bearing evidence. A mapped candidate
        # may share a serving lane with another backend, but it may never
        # inherit that lane's workload profile.
        profile = profiles.get(candidate_id)
        profile_error = profile_errors.get(candidate_id)
        if profile is None:
            missing.append("workload-profile")
            if profile_error is not None:
                missing.append("workload-profile-invalid")
            execution_identity: Mapping[str, Any] = {}
            display_name = _optional_text(receipt.get("upstream_name"), maximum=200) or candidate_id
            repository = _optional_text(receipt_source.get("repository")) or "unknown"
            source_revision = _optional_text(receipt_source.get("revision"))
            execution_mode: Literal["scientific-batch", "hybrid"] | None = None
            service_classes: list[ScientificServiceClass] = []
            interactive_supported: bool | None = None
        else:
            execution_identity = _mapping_or_empty(profile.get("execution_identity"))
            display_name = _text(profile.get("display_name"), "workload profile display_name", 200)
            profile_source = _mapping_or_empty(profile.get("source"))
            repository = _optional_text(profile_source.get("repository")) or "unknown"
            source_revision = _optional_text(profile_source.get("revision")) or _optional_text(
                receipt_source.get("revision")
            )
            raw_execution_mode = profile.get("execution_mode")
            if raw_execution_mode not in {"scientific-batch", "hybrid"}:
                raise ScientificAdminSourceUnavailableError("scientific workload execution mode is unsupported")
            execution_mode = raw_execution_mode
            service_classes, interface_missing = self._service_classes(profile)
            missing.extend(interface_missing)
            interactive_supported = ScientificServiceClass.INTERACTIVE in service_classes

        model_revision = _optional_text(execution_identity.get("model_revision"))
        runtime_image = _optional_text(execution_identity.get("runtime_image_digest"))
        execution_identity_digest = _optional_text(execution_identity.get("execution_identity_sha256"))
        document_identity_consistent = True
        if profile is not None:
            profile_source = _mapping_or_empty(profile.get("source"))
            profile_source_kind = _optional_text(profile_source.get("kind"), maximum=32)
            profile_source_repository = _optional_text(profile_source.get("repository"), maximum=512)
            profile_source_revision = _optional_text(profile_source.get("revision"))
            receipt_source_kind = _optional_text(receipt_source.get("kind"), maximum=32)
            receipt_source_repository = _optional_text(receipt_source.get("repository"), maximum=512)
            receipt_source_revision = _optional_text(receipt_source.get("revision"))
            document_identity_consistent = (
                receipt_source_kind is not None
                and receipt_source_repository is not None
                and receipt_source_revision is not None
                and profile_source_kind is not None
                and profile_source_repository is not None
                and profile_source_revision is not None
                and model_revision is not None
                and receipt_source_kind == profile_source_kind
                and receipt_source_repository == profile_source_repository
                and receipt_source_revision == profile_source_revision == model_revision
            )
            if not document_identity_consistent:
                missing.append("source-identity-agreement")
            missing.append("qualified-evidence")

        if not document_identity_consistent:
            qualification = ScientificQualificationJoin(
                state="identity-mismatch",
                reason="candidate receipt and workload profile identify different source trees",
                serving_lane_id=lane_id if lane_id != candidate_id else None,
                compared=["receipt_source", "profile_source", "model_revision"],
                mismatched=["source_identity"],
            )
            # Do not choose one conflicting source as the backend identity.
            repository = "conflicting-catalog-evidence"
            source_revision = None
            model_revision = None
            runtime_image = None
            execution_identity_digest = None
        else:
            qualification = _execution_identity_join(
                candidate_id=candidate_id,
                route_scope=route_scope,
                backend_identity=_text(receipt.get("backend_identity"), "backend identity", 64),
                operational=_operational(self.registry, lane_id),
                execution_identity=execution_identity,
            )
        qualified = qualification.state == "qualified"

        access, access_missing = self._access(
            candidate_id=candidate_id,
            access_profile=access_profile,
            receipt=receipt,
            profile=profile,
            academic_state=None if candidate_id in academic_errors else academic_readiness.get(candidate_id),
            request_time_receipt_demanded=request_time_receipt_demanded,
        )
        missing.extend(access_missing)
        if candidate_id in academic_errors:
            missing.append("academic-asset-readiness-invalid")

        if profile is not None:
            if model_revision is None:
                missing.append("model-revision")
            if runtime_image is None:
                missing.append("runtime-image-digest")
            if execution_identity_digest is None:
                missing.append("execution-identity-digest")

        readiness: Literal["qualified", "candidate", "blocked", "unknown"]
        if access.state == "blocked":
            readiness = "blocked"
            readiness_reason = access.gate
        elif academic and access.state != "verified":
            readiness = "unknown"
            readiness_reason = (
                "Deployment-bound academic authorization is not verified for this candidate, so runtime "
                "readiness is withheld."
            )
        elif not document_identity_consistent:
            readiness = "unknown"
            readiness_reason = (
                "Candidate receipt and workload profile revisions disagree, so no backend identity is selected."
            )
        elif qualified:
            readiness = "qualified"
            readiness_reason = "The exact runtime, semantic validator, and elastic execution evidence are qualified."
        elif profile is None:
            readiness = "unknown"
            readiness_reason = (
                "No scientific workload profile is published for this candidate, so its execution shape, "
                "runtime identity, and readiness are not yet observable."
            )
        else:
            readiness = "candidate"
            readiness_reason = (
                "Deployment-bound academic access is authorized; runtime or semantic qualification is pending."
                if academic
                else _optional_text(receipt.get("notes"), maximum=300)
                or "The candidate is identified but not yet runtime or semantically qualified."
            )

        return ScientificModelReadiness(
            model_id=lane_id,
            candidate_id=candidate_id,
            display_name=display_name,
            readiness=readiness,
            readiness_reason=readiness_reason,
            workload_profile="published" if profile is not None else "absent",
            missing_evidence=sorted(dict.fromkeys(missing))[:12],
            qualification=qualification,
            execution_mode=execution_mode,
            batch_supported=profile is not None,
            interactive_supported=interactive_supported,
            service_classes=service_classes,
            backend=ScientificBackendIdentity(
                backend_id=f"{candidate_id}:{_optional_text(receipt.get('backend_identity'), maximum=64) or 'unknown'}",
                kind=_optional_text(receipt.get("backend_identity"), maximum=64) or "unknown",
                source_repository=repository,
                source_revision=source_revision,
                model_revision=model_revision,
                runtime_image_digest=runtime_image,
                execution_identity_digest=execution_identity_digest,
            ),
            access=access,
            caching=ScientificCachingReadiness(
                exact_tier="not-observed",
                image="verified" if qualified and runtime_image is not None else "candidate",
                artifacts="verified" if qualified and model_revision is not None else "candidate",
                reference_data="candidate" if lane_id in _REFERENCE_DATA_MODELS else "unsupported",
                runtime_checkpoint="unavailable",
                gpu_snapshot="unavailable",
                reason="No exact fast-start observation is joined to this scientific backend identity.",
            ),
        )

    @staticmethod
    def _unprojectable(
        receipt: Mapping[str, Any],
        *,
        candidate_id: str,
        route_scope: CandidateRouteScope,
    ) -> ScientificModelReadiness:
        """Publish a bounded unknown row for one invalid candidate only."""

        receipt_source = _mapping_or_empty(receipt.get("source"))
        display_name = _optional_text(receipt.get("upstream_name"), maximum=200) or candidate_id
        academic = receipt.get("access_profile") == "academic"
        return ScientificModelReadiness(
            model_id=route_scope.lane_id,
            candidate_id=candidate_id,
            display_name=display_name,
            readiness="unknown",
            readiness_reason=(
                "This candidate's catalog evidence is invalid; other scientific model rows remain available."
            ),
            workload_profile="absent",
            missing_evidence=["candidate-receipt", "workload-profile"],
            qualification=ScientificQualificationJoin(
                state="evidence-absent",
                reason="the candidate evidence could not be projected safely",
                serving_lane_id=route_scope.lane_id if route_scope.lane_id != candidate_id else None,
            ),
            execution_mode=None,
            batch_supported=False,
            interactive_supported=None,
            service_classes=[],
            backend=ScientificBackendIdentity(
                backend_id=f"{candidate_id}:unknown",
                kind="unknown",
                source_repository=_optional_text(receipt_source.get("repository")) or "unknown",
                source_revision=_optional_text(receipt_source.get("revision")),
                model_revision=None,
                runtime_image_digest=None,
                execution_identity_digest=None,
            ),
            access=ScientificAccessGate(
                profile="academic" if academic else "standard",
                state="unverified",
                gate=(
                    "The invalid candidate receipt does not establish deployment-bound academic authorization."
                    if academic
                    else "The invalid candidate receipt does not establish a standard access state."
                ),
                receipt_digest=None,
                authorization=None,
                formal_license_status="FormalAcceptancePending" if academic else "not-applicable",
                alternative=_ALTERNATIVES.get(candidate_id) if academic else None,
            ),
            caching=ScientificCachingReadiness(
                exact_tier="not-observed",
                image="unavailable",
                artifacts="unavailable",
                reference_data="unavailable",
                runtime_checkpoint="unavailable",
                gpu_snapshot="unavailable",
                reason="No fast-start evidence can be joined to an invalid candidate projection.",
            ),
        )

    @staticmethod
    def _service_classes(profile: Mapping[str, Any]) -> tuple[list[ScientificServiceClass], list[str]]:
        interface = _mapping_or_empty(profile.get("interface"))
        raw = interface.get("service_classes")
        if not isinstance(raw, list) or not raw:
            return [], ["service-classes"]
        try:
            return [ScientificServiceClass(_text(value, "service class", 32)) for value in raw], []
        except ValueError as exc:
            raise ScientificAdminSourceUnavailableError("scientific workload service class is unsupported") from exc

    def _access(
        self,
        *,
        candidate_id: str,
        access_profile: Literal["standard", "academic"],
        receipt: Mapping[str, Any],
        profile: Mapping[str, Any] | None,
        academic_state: Mapping[str, Any] | None,
        request_time_receipt_demanded: bool,
    ) -> tuple[ScientificAccessGate, list[str]]:
        """Project access from the accepted contracts without upgrading it.

        The receipt is authoritative for the licence profile. Academic
        authorization comes from the generated readiness projection, which is
        the only document that publishes it; the workload profile carries no
        authorization block.
        """

        missing: list[str] = []
        profile_access = _mapping_or_empty((profile or {}).get("access"))
        if profile is not None:
            declared = profile_access.get("profile")
            if declared is not None and declared != access_profile:
                missing.append("access-profile-agreement")
            if profile_access.get("credentials_embedded") is not False:
                missing.append("credential-free-attestation")

        if access_profile != "academic":
            state = _access_state(receipt.get("access_state"), profile_access.get("state"))
            return (
                ScientificAccessGate(
                    profile="standard",
                    state=state,
                    gate="No restricted academic asset is required by this backend.",
                    receipt_digest=None,
                    authorization=None,
                    formal_license_status="not-applicable",
                    alternative=None,
                ),
                missing,
            )

        alternative = _ALTERNATIVES.get(candidate_id)
        if academic_state is None:
            missing.append("academic-asset-readiness")
            return (
                ScientificAccessGate(
                    profile="academic",
                    state="unverified",
                    gate=(
                        "No academic asset readiness is published for this candidate, so deployment-bound "
                        "authorization cannot be confirmed. It is not represented as granted."
                    ),
                    receipt_digest=None,
                    authorization=None,
                    formal_license_status="FormalAcceptancePending",
                    alternative=alternative,
                ),
                missing,
            )

        granted = (
            academic_state.get("use_authorization_status") == "Granted"
            and academic_state.get("execution_authorization_status") == "Authorized"
        )
        admission = academic_state.get("serving_admission")
        admitted = admission in {"PendingRuntimeReadiness", "AdmittedNoPerRequestLicenseReceipt"}
        serving_admission: Literal["PendingRuntimeReadiness", "AdmittedNoPerRequestLicenseReceipt"] = (
            "AdmittedNoPerRequestLicenseReceipt"
            if admission == "AdmittedNoPerRequestLicenseReceipt"
            else "PendingRuntimeReadiness"
        )
        if request_time_receipt_demanded:
            return (
                ScientificAccessGate(
                    profile="academic",
                    state="blocked",
                    gate=(
                        "The academic readiness projection demands a request-time licence receipt, which "
                        "contradicts deployment-bound admission. Access is withheld until it is corrected."
                    ),
                    receipt_digest=None,
                    authorization=None,
                    formal_license_status="FormalAcceptancePending",
                    alternative=alternative,
                ),
                [*missing, "receipt-free-admission"],
            )
        if not granted or not admitted:
            return (
                ScientificAccessGate(
                    profile="academic",
                    state="blocked",
                    gate=(
                        "Deployment-bound academic authorization is not granted for this candidate, so it "
                        "cannot be admitted. Use the explicit alternative instead."
                    ),
                    receipt_digest=None,
                    authorization=None,
                    formal_license_status="FormalAcceptancePending",
                    alternative=alternative,
                ),
                missing,
            )

        raw_formal = academic_state.get("formal_license_status")
        formal_license_status: Literal["FormalAcceptancePending", "FormalAcceptanceRecorded", "not-applicable"] = (
            raw_formal if raw_formal in {"FormalAcceptancePending", "FormalAcceptanceRecorded"} else
            "FormalAcceptancePending"
        )
        return (
            ScientificAccessGate(
                profile="academic",
                state="verified",
                gate=(
                    "Deployment-bound academic use is Granted and execution is Authorized; no request-time "
                    "licence receipt is required. Formal acceptance is advisory and is not an admission gate."
                ),
                receipt_digest=None,
                authorization=ScientificAccessAuthorization(
                    # _academic_readiness rejects an entry without this exact
                    # identity, so verified access never synthesises one.
                    asset_id=_text(academic_state.get("asset_id"), "academic asset identity", 128),
                    backend_id=_optional_text(academic_state.get("backend_id"), maximum=128),
                    license_id=_optional_text(academic_state.get("license_id"), maximum=200),
                    serving_admission=serving_admission,
                    asset_namespace=_optional_text(academic_state.get("_asset_namespace"), maximum=63),
                ),
                formal_license_status=formal_license_status,
                alternative=_academic_alternative(academic_state) or alternative,
            ),
            missing,
        )


def _access_state(
    receipt_state: object,
    profile_state: object,
) -> Literal["not-required", "unverified", "verified", "blocked"]:
    """Carry the declared access state through without upgrading it."""

    # Independent source declarations can narrow one another but never upgrade
    # a blocked or unverified state. This ordering is deliberately conservative
    # when a receipt and a workload profile disagree.
    states = [
        value
        for value in (receipt_state, profile_state)
        if value in {"not-required", "unverified", "verified", "blocked"}
    ]
    for value in ("blocked", "unverified", "verified", "not-required"):
        if value in states:
            return value
    return "unverified"


def _academic_alternative(academic_state: Mapping[str, Any]) -> ScientificExplicitAlternative | None:
    value = _mapping_or_empty(academic_state.get("alternative"))
    model_id = _optional_text(value.get("model_id"), maximum=128)
    if model_id is None:
        return None
    return ScientificExplicitAlternative(
        model_id=model_id,
        display_name=model_id,
        reason=_optional_text(value.get("reason"), maximum=200)
        or "This is a separate model with its own identity and results.",
    )


def scientific_receipts_file(catalog_dir: Path) -> Path:
    """Resolve receipts only from the exact catalog root Registry loaded.

    Image delivery sets ``catalog_dir`` to ``/opt/fs2/catalog`` and PVC
    delivery sets it to ``/etc/fs2-serve/catalog``. Falling back between those
    roots could join receipts from one revision to live routes from another.
    """

    direct = catalog_dir / "contracts" / SCIENTIFIC_SOURCE_RECEIPTS
    if direct.is_file():
        return direct
    raise RegistryError("scientific source candidate receipts are not installed")
