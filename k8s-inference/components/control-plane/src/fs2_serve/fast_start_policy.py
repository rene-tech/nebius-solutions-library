"""Pure automatic fast-start assignment policy.

This module deliberately owns no database or Kubernetes writes.  The caller
supplies bounded history aggregates, current qualification, mechanism health,
and cost inputs; the result can then be persisted with the ModelDeployment
revision/status owner.

Do not feed ``OperationView.cold_start_seconds`` into qualification.  That
legacy value is accepted-to-ready and therefore includes queue/capacity time.
Fast-start qualification needs the separately instrumented
GPU-capacity-available-to-endpoint-ready interval.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from .fast_start import LEVEL_ORDER, LEVEL_TARGET_SECONDS, FastStartFallbackPolicy, FastStartLevel


class AutomaticDecisionReason(StrEnum):
    INITIAL_MINIMUM = "InitialMinimum"
    STABLE = "Stable"
    MISSING_DATA_MINIMUM = "MissingDataMinimum"
    NO_ELIGIBLE_PATH = "NoEligiblePath"
    POLICY_BOUNDS_CHANGED = "PolicyBoundsChanged"
    PROMOTION_PENDING = "PromotionPending"
    PROMOTION_COOLDOWN = "PromotionCooldown"
    PROMOTED = "Promoted"
    TARGET_MISS_PROMOTION = "TargetMissPromotion"
    DEMOTION_PENDING = "DemotionPending"
    DEMOTION_BLOCKED_BY_MISS = "DemotionBlockedByMiss"
    DEMOTED = "Demoted"


FAST_START_LEVELS: Final[tuple[FastStartLevel, ...]] = LEVEL_ORDER
FAST_START_TARGET_SECONDS: Final[dict[FastStartLevel, float | None]] = {
    level: None if target is None else float(target) for level, target in LEVEL_TARGET_SECONDS.items()
}
MINIMUM_QUALIFICATION_SUCCESSES: Final[int] = 20


def _rank(level: FastStartLevel) -> int:
    return FAST_START_LEVELS.index(level)


def _aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _finite_nonnegative(value: float, field: str) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class FastStartPath:
    """One exact-tuple startup path and its retained evidence."""

    mechanism_id: str
    qualified_level: FastStartLevel
    ready: bool
    qualification_current: bool
    qualified_p95_model_start_seconds: float | None
    successful_attempts: int
    failed_attempts: int
    hourly_cost: float | None

    def __post_init__(self) -> None:
        if not self.mechanism_id or len(self.mechanism_id) > 128:
            raise ValueError("mechanism_id is outside the bound")
        if self.successful_attempts < 0 or self.failed_attempts < 0:
            raise ValueError("attempt counts must be non-negative")
        if self.qualified_p95_model_start_seconds is not None:
            _finite_nonnegative(self.qualified_p95_model_start_seconds, "qualified p95")
        if self.hourly_cost is not None:
            _finite_nonnegative(self.hourly_cost, "hourly_cost")

    def supports(self, level: FastStartLevel) -> bool:
        """Return whether the path truthfully supports ``level`` right now."""

        if not self.ready:
            return False
        if level is FastStartLevel.OFF:
            return True
        if (
            not self.qualification_current
            or self.successful_attempts < MINIMUM_QUALIFICATION_SUCCESSES
            or self.failed_attempts != 0
            or self.qualified_p95_model_start_seconds is None
            or _rank(self.qualified_level) < _rank(level)
        ):
            return False
        ceiling = FAST_START_TARGET_SECONDS[self.qualified_level]
        assert ceiling is not None
        return self.qualified_p95_model_start_seconds <= ceiling


@dataclass(frozen=True, slots=True)
class FastStartHistoryWindow:
    """Payload-free request/activation aggregate for one rolling window."""

    started_at: datetime
    ended_at: datetime
    request_count: int
    cold_activation_count: int
    idle_gap_episode_count: int
    target_miss_count: int
    complete: bool = True

    def __post_init__(self) -> None:
        _aware(self.started_at, "started_at")
        _aware(self.ended_at, "ended_at")
        if self.ended_at <= self.started_at:
            raise ValueError("history window must have positive duration")
        counts = (
            self.request_count,
            self.cold_activation_count,
            self.idle_gap_episode_count,
            self.target_miss_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("history counts must be non-negative")
        if self.cold_activation_count > self.request_count:
            raise ValueError("cold activations cannot exceed requests")
        if self.idle_gap_episode_count > max(0, self.request_count - 1):
            raise ValueError("idle-gap episodes cannot exceed observed request gaps")
        if self.target_miss_count > self.cold_activation_count:
            raise ValueError("target misses cannot exceed cold activations")

    @property
    def duration_hours(self) -> float:
        return (self.ended_at - self.started_at).total_seconds() / 3600.0

    @property
    def expected_cold_activations_per_hour(self) -> float:
        episodes = max(self.cold_activation_count, self.idle_gap_episode_count)
        return episodes / self.duration_hours

    @classmethod
    def from_observations(
        cls,
        *,
        started_at: datetime,
        ended_at: datetime,
        request_times: Sequence[datetime],
        cold_activation_times: Sequence[datetime],
        target_miss_times: Sequence[datetime],
        idle_episode_seconds: float,
        complete: bool = True,
    ) -> FastStartHistoryWindow:
        """Build a window after a bounded store query.

        The first request is not inferred to be cold because the request before
        ``started_at`` is unknown.  A durable activation transition can still
        count it through ``cold_activation_times``.
        """

        _finite_nonnegative(idle_episode_seconds, "idle_episode_seconds")
        _aware(started_at, "started_at")
        _aware(ended_at, "ended_at")
        if ended_at <= started_at:
            raise ValueError("history window must have positive duration")

        def checked(values: Sequence[datetime], field: str) -> list[datetime]:
            result = sorted(values)
            for value in result:
                _aware(value, field)
                if value < started_at or value >= ended_at:
                    raise ValueError(f"{field} lies outside the history window")
            return result

        requests = checked(request_times, "request time")
        activations = checked(cold_activation_times, "cold activation time")
        misses = checked(target_miss_times, "target miss time")
        idle_episodes = sum(
            1
            for previous, current in zip(requests, requests[1:], strict=False)
            if (current - previous).total_seconds() >= idle_episode_seconds
        )
        return cls(
            started_at=started_at,
            ended_at=ended_at,
            request_count=len(requests),
            cold_activation_count=len(activations),
            idle_gap_episode_count=idle_episodes,
            target_miss_count=len(misses),
            complete=complete,
        )


@dataclass(frozen=True, slots=True)
class AutomaticFastStartPolicy:
    minimum_level: FastStartLevel
    maximum_level: FastStartLevel
    wait_second_value: float | None
    fallback_policy: FastStartFallbackPolicy = FastStartFallbackPolicy.ALLOW_LOWER_LEVEL
    promotion_consecutive_wins: int = 3
    promotion_target_misses: int = 2
    promotion_cooldown: timedelta = timedelta(minutes=30)
    demotion_stable_for: timedelta = timedelta(hours=24)
    demotion_cooldown: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        if _rank(self.minimum_level) > _rank(self.maximum_level):
            raise ValueError("minimum_level cannot exceed maximum_level")
        if self.wait_second_value is not None:
            _finite_nonnegative(self.wait_second_value, "wait_second_value")
        if self.promotion_consecutive_wins < 1 or self.promotion_target_misses < 1:
            raise ValueError("promotion thresholds must be positive")
        for field, value in (
            ("promotion_cooldown", self.promotion_cooldown),
            ("demotion_stable_for", self.demotion_stable_for),
            ("demotion_cooldown", self.demotion_cooldown),
        ):
            if value.total_seconds() < 0:
                raise ValueError(f"{field} must be non-negative")


@dataclass(frozen=True, slots=True)
class AutomaticFastStartState:
    assigned_level: FastStartLevel
    pending_level: FastStartLevel | None = None
    pending_since: datetime | None = None
    consecutive_wins: int = 0
    last_transition_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.pending_level is None) != (self.pending_since is None):
            raise ValueError("pending level and timestamp must be present together")
        if self.consecutive_wins < 0:
            raise ValueError("consecutive_wins must be non-negative")
        if self.pending_level is None and self.consecutive_wins != 0:
            raise ValueError("wins require a pending level")
        if self.pending_since is not None:
            _aware(self.pending_since, "pending_since")
        if self.last_transition_at is not None:
            _aware(self.last_transition_at, "last_transition_at")


@dataclass(frozen=True, slots=True)
class AutomaticFastStartDecision:
    assigned_level: FastStartLevel
    mechanism_id: str | None
    satisfied: bool
    fallback_level: FastStartLevel | None
    fallback_mechanism_id: str | None
    reason: AutomaticDecisionReason
    score: float | None
    eligible_levels: tuple[FastStartLevel, ...]
    state: AutomaticFastStartState


@dataclass(frozen=True, slots=True)
class _Option:
    level: FastStartLevel
    path: FastStartPath
    score: float


def _supported_levels(paths: Sequence[FastStartPath]) -> tuple[FastStartLevel, ...]:
    return tuple(level for level in FAST_START_LEVELS if any(path.supports(level) for path in paths))


def _path_score(path: FastStartPath, activation_rate: float, wait_second_value: float) -> float | None:
    if path.hourly_cost is None or path.qualified_p95_model_start_seconds is None:
        return None
    return path.hourly_cost + activation_rate * path.qualified_p95_model_start_seconds * wait_second_value


def _options(
    policy: AutomaticFastStartPolicy,
    paths: Sequence[FastStartPath],
    activation_rate: float,
) -> list[_Option]:
    assert policy.wait_second_value is not None
    result: list[_Option] = []
    minimum_rank = _rank(policy.minimum_level)
    maximum_rank = _rank(policy.maximum_level)
    for path in paths:
        supported_rank = min(_rank(path.qualified_level), maximum_rank)
        if path.qualified_level is FastStartLevel.OFF and policy.minimum_level is FastStartLevel.OFF:
            supported_rank = 0
        if supported_rank < minimum_rank:
            continue
        level = FAST_START_LEVELS[supported_rank]
        if not path.supports(level):
            continue
        score = _path_score(path, activation_rate, policy.wait_second_value)
        if score is not None:
            result.append(_Option(level=level, path=path, score=score))
    return sorted(result, key=lambda option: (option.score, -_rank(option.level), option.path.mechanism_id))


def _best_path(
    paths: Sequence[FastStartPath],
    level: FastStartLevel,
    activation_rate: float | None,
    wait_second_value: float | None,
) -> tuple[FastStartPath | None, float | None]:
    supported = [path for path in paths if path.supports(level)]
    if not supported:
        return None, None
    scored: list[tuple[float, str, FastStartPath]] = []
    if activation_rate is not None and wait_second_value is not None:
        for path in supported:
            score = _path_score(path, activation_rate, wait_second_value)
            if score is not None:
                scored.append((score, path.mechanism_id, path))
    if scored:
        score, _, path = min(scored)
        return path, score
    return min(supported, key=lambda path: (-_rank(path.qualified_level), path.mechanism_id)), None


def _fallback(
    policy: AutomaticFastStartPolicy,
    paths: Sequence[FastStartPath],
    assigned_level: FastStartLevel,
    activation_rate: float | None,
) -> tuple[FastStartLevel | None, str | None]:
    if policy.fallback_policy is FastStartFallbackPolicy.REQUIRE_TARGET:
        return None, None
    for level in reversed(FAST_START_LEVELS[: _rank(assigned_level)]):
        path, _ = _best_path(paths, level, activation_rate, policy.wait_second_value)
        if path is not None:
            return level, path.mechanism_id
    return None, None


def _result(
    *,
    policy: AutomaticFastStartPolicy,
    paths: Sequence[FastStartPath],
    state: AutomaticFastStartState,
    reason: AutomaticDecisionReason,
    activation_rate: float | None,
) -> AutomaticFastStartDecision:
    path, score = _best_path(paths, state.assigned_level, activation_rate, policy.wait_second_value)
    satisfied = path is not None
    fallback_level: FastStartLevel | None = None
    fallback_mechanism: str | None = None
    if not satisfied:
        fallback_level, fallback_mechanism = _fallback(policy, paths, state.assigned_level, activation_rate)
    return AutomaticFastStartDecision(
        assigned_level=state.assigned_level,
        mechanism_id=None if path is None else path.mechanism_id,
        satisfied=satisfied,
        fallback_level=fallback_level,
        fallback_mechanism_id=fallback_mechanism,
        reason=reason,
        score=score,
        eligible_levels=_supported_levels(paths),
        state=state,
    )


def _minimum_state(
    level: FastStartLevel, *, prior: AutomaticFastStartState | None, now: datetime
) -> AutomaticFastStartState:
    changed = prior is not None and prior.assigned_level is not level
    return AutomaticFastStartState(
        assigned_level=level,
        last_transition_at=now if changed else (None if prior is None else prior.last_transition_at),
    )


def _cooldown_elapsed(last_transition_at: datetime | None, cooldown: timedelta, now: datetime) -> bool:
    return last_transition_at is None or now - last_transition_at >= cooldown


def evaluate_automatic_fast_start(
    *,
    policy: AutomaticFastStartPolicy,
    paths: Iterable[FastStartPath],
    short_history: FastStartHistoryWindow | None,
    long_history: FastStartHistoryWindow | None,
    prior_state: AutomaticFastStartState | None,
    now: datetime,
) -> AutomaticFastStartDecision:
    """Evaluate one idempotent automatic-policy interval.

    The caller persists ``decision.state`` and supplies it to the next run.
    Policy-bound changes and missing history/cost inputs select the configured
    minimum immediately.  Normal mechanism loss keeps the assigned level while
    demotion hysteresis runs; ``satisfied`` becomes false immediately.
    """

    _aware(now, "now")
    path_list = tuple(paths)
    mechanism_ids = [path.mechanism_id for path in path_list]
    if len(mechanism_ids) != len(set(mechanism_ids)):
        raise ValueError("mechanism IDs must be unique")
    for history in (short_history, long_history):
        if history is not None and history.ended_at > now:
            raise ValueError("history cannot end in the future")
    if prior_state is not None and any(
        value is not None and value > now for value in (prior_state.pending_since, prior_state.last_transition_at)
    ):
        raise ValueError("automatic policy state cannot be from the future")

    complete = bool(
        short_history is not None
        and long_history is not None
        and short_history.complete
        and long_history.complete
        and policy.wait_second_value is not None
    )
    if not complete:
        state = _minimum_state(policy.minimum_level, prior=prior_state, now=now)
        return _result(
            policy=policy,
            paths=path_list,
            state=state,
            reason=AutomaticDecisionReason.MISSING_DATA_MINIMUM,
            activation_rate=None,
        )

    assert short_history is not None
    assert long_history is not None
    activation_rate = long_history.expected_cold_activations_per_hour
    options = _options(policy, path_list, activation_rate)
    supported_in_bounds = any(
        path.supports(level)
        for path in path_list
        for level in FAST_START_LEVELS[_rank(policy.minimum_level) : _rank(policy.maximum_level) + 1]
    )
    if supported_in_bounds and not options:
        # At least one path is usable but lacks cost or p95 input, so the score
        # is incomplete rather than evidence that the minimum is optimal.
        state = _minimum_state(policy.minimum_level, prior=prior_state, now=now)
        return _result(
            policy=policy,
            paths=path_list,
            state=state,
            reason=AutomaticDecisionReason.MISSING_DATA_MINIMUM,
            activation_rate=activation_rate,
        )

    desired = policy.minimum_level if not options else options[0].level
    no_eligible_path = not options

    if prior_state is None:
        pending = None
        pending_since = None
        wins = 0
        if _rank(desired) > _rank(policy.minimum_level):
            pending = FAST_START_LEVELS[_rank(policy.minimum_level) + 1]
            pending_since = now
            wins = 1
        state = AutomaticFastStartState(
            assigned_level=policy.minimum_level,
            pending_level=pending,
            pending_since=pending_since,
            consecutive_wins=wins,
        )
        return _result(
            policy=policy,
            paths=path_list,
            state=state,
            reason=(
                AutomaticDecisionReason.NO_ELIGIBLE_PATH
                if no_eligible_path
                else AutomaticDecisionReason.INITIAL_MINIMUM
            ),
            activation_rate=activation_rate,
        )

    current_rank = _rank(prior_state.assigned_level)
    minimum_rank = _rank(policy.minimum_level)
    maximum_rank = _rank(policy.maximum_level)
    if current_rank < minimum_rank or current_rank > maximum_rank:
        bounded = policy.minimum_level if current_rank < minimum_rank else policy.maximum_level
        state = AutomaticFastStartState(assigned_level=bounded, last_transition_at=now)
        return _result(
            policy=policy,
            paths=path_list,
            state=state,
            reason=AutomaticDecisionReason.POLICY_BOUNDS_CHANGED,
            activation_rate=activation_rate,
        )

    desired_rank = _rank(desired)
    if desired_rank == current_rank:
        state = AutomaticFastStartState(
            assigned_level=prior_state.assigned_level,
            last_transition_at=prior_state.last_transition_at,
        )
        return _result(
            policy=policy,
            paths=path_list,
            state=state,
            reason=(AutomaticDecisionReason.NO_ELIGIBLE_PATH if no_eligible_path else AutomaticDecisionReason.STABLE),
            activation_rate=activation_rate,
        )

    if desired_rank > current_rank:
        step = FAST_START_LEVELS[current_rank + 1]
        same_pending = prior_state.pending_level is step
        wins = prior_state.consecutive_wins + 1 if same_pending else 1
        pending_since = prior_state.pending_since if same_pending else now
        assert pending_since is not None
        promotion_ready = wins >= policy.promotion_consecutive_wins
        target_miss = short_history.target_miss_count >= policy.promotion_target_misses
        cooldown_ready = _cooldown_elapsed(prior_state.last_transition_at, policy.promotion_cooldown, now)
        if (promotion_ready or target_miss) and cooldown_ready:
            state = AutomaticFastStartState(assigned_level=step, last_transition_at=now)
            return _result(
                policy=policy,
                paths=path_list,
                state=state,
                reason=(
                    AutomaticDecisionReason.TARGET_MISS_PROMOTION if target_miss else AutomaticDecisionReason.PROMOTED
                ),
                activation_rate=activation_rate,
            )
        state = AutomaticFastStartState(
            assigned_level=prior_state.assigned_level,
            pending_level=step,
            pending_since=pending_since,
            consecutive_wins=wins,
            last_transition_at=prior_state.last_transition_at,
        )
        return _result(
            policy=policy,
            paths=path_list,
            state=state,
            reason=(
                AutomaticDecisionReason.PROMOTION_COOLDOWN
                if (promotion_ready or target_miss) and not cooldown_ready
                else AutomaticDecisionReason.PROMOTION_PENDING
            ),
            activation_rate=activation_rate,
        )

    if short_history.target_miss_count > 0:
        state = AutomaticFastStartState(
            assigned_level=prior_state.assigned_level,
            last_transition_at=prior_state.last_transition_at,
        )
        return _result(
            policy=policy,
            paths=path_list,
            state=state,
            reason=AutomaticDecisionReason.DEMOTION_BLOCKED_BY_MISS,
            activation_rate=activation_rate,
        )

    step = FAST_START_LEVELS[current_rank - 1]
    if _rank(step) < desired_rank:
        step = desired
    same_pending = prior_state.pending_level is step
    pending_since = prior_state.pending_since if same_pending else now
    assert pending_since is not None
    stable = now - pending_since >= policy.demotion_stable_for
    cooldown_ready = _cooldown_elapsed(prior_state.last_transition_at, policy.demotion_cooldown, now)
    if stable and cooldown_ready:
        state = AutomaticFastStartState(assigned_level=step, last_transition_at=now)
        return _result(
            policy=policy,
            paths=path_list,
            state=state,
            reason=AutomaticDecisionReason.DEMOTED,
            activation_rate=activation_rate,
        )
    state = AutomaticFastStartState(
        assigned_level=prior_state.assigned_level,
        pending_level=step,
        pending_since=pending_since,
        last_transition_at=prior_state.last_transition_at,
    )
    return _result(
        policy=policy,
        paths=path_list,
        state=state,
        reason=(
            AutomaticDecisionReason.NO_ELIGIBLE_PATH if no_eligible_path else AutomaticDecisionReason.DEMOTION_PENDING
        ),
        activation_rate=activation_rate,
    )
