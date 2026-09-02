from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fs2_serve.fast_start_policy import (
    AutomaticDecisionReason,
    AutomaticFastStartPolicy,
    AutomaticFastStartState,
    FastStartFallbackPolicy,
    FastStartHistoryWindow,
    FastStartLevel,
    FastStartPath,
    evaluate_automatic_fast_start,
)

NOW = datetime(2026, 9, 2, 16, tzinfo=UTC)


def history(
    *,
    hours: int,
    requests: int = 10,
    activations: int = 1,
    idle_episodes: int = 1,
    misses: int = 0,
    complete: bool = True,
) -> FastStartHistoryWindow:
    return FastStartHistoryWindow(
        started_at=NOW - timedelta(hours=hours),
        ended_at=NOW,
        request_count=requests,
        cold_activation_count=activations,
        idle_gap_episode_count=idle_episodes,
        target_miss_count=misses,
        complete=complete,
    )


def path(
    level: FastStartLevel,
    *,
    mechanism: str | None = None,
    p95: float | None = None,
    cost: float | None = 0.0,
    successes: int = 20,
    failures: int = 0,
    ready: bool = True,
    current: bool = True,
) -> FastStartPath:
    default_p95 = {
        FastStartLevel.OFF: 400.0,
        FastStartLevel.L1: 250.0,
        FastStartLevel.L2: 100.0,
        FastStartLevel.L3: 50.0,
        FastStartLevel.L4: 25.0,
    }[level]
    return FastStartPath(
        mechanism_id=mechanism or level.value.lower(),
        qualified_level=level,
        ready=ready,
        qualification_current=current,
        qualified_p95_model_start_seconds=default_p95 if p95 is None else p95,
        successful_attempts=successes,
        failed_attempts=failures,
        hourly_cost=cost,
    )


def policy(
    minimum: FastStartLevel = FastStartLevel.L1,
    maximum: FastStartLevel = FastStartLevel.L4,
    *,
    wait_value: float | None = 0.01,
    fallback: FastStartFallbackPolicy = FastStartFallbackPolicy.ALLOW_LOWER_LEVEL,
) -> AutomaticFastStartPolicy:
    return AutomaticFastStartPolicy(
        minimum_level=minimum,
        maximum_level=maximum,
        wait_second_value=wait_value,
        fallback_policy=fallback,
    )


def evaluate(
    *,
    selected_policy: AutomaticFastStartPolicy,
    paths: list[FastStartPath],
    prior: AutomaticFastStartState | None = None,
    short: FastStartHistoryWindow | None = None,
    long: FastStartHistoryWindow | None = None,
    now: datetime = NOW,
):
    return evaluate_automatic_fast_start(
        policy=selected_policy,
        paths=paths,
        short_history=short if short is not None else history(hours=1),
        long_history=long if long is not None else history(hours=24),
        prior_state=prior,
        now=now,
    )


def test_path_qualification_is_fail_closed() -> None:
    assert path(FastStartLevel.L2).supports(FastStartLevel.L2)
    assert path(FastStartLevel.L2).supports(FastStartLevel.L1)
    assert not path(FastStartLevel.L2, successes=19).supports(FastStartLevel.L1)
    assert not path(FastStartLevel.L2, failures=1).supports(FastStartLevel.L1)
    assert not path(FastStartLevel.L2, p95=121).supports(FastStartLevel.L2)
    assert not path(FastStartLevel.L2, current=False).supports(FastStartLevel.L1)
    assert not path(FastStartLevel.L2, ready=False).supports(FastStartLevel.L1)
    assert path(FastStartLevel.OFF, successes=0, current=False).supports(FastStartLevel.OFF)


def test_observation_builder_counts_idle_episodes_without_guessing_first_request() -> None:
    window = FastStartHistoryWindow.from_observations(
        started_at=NOW - timedelta(hours=1),
        ended_at=NOW,
        request_times=[
            NOW - timedelta(minutes=55),
            NOW - timedelta(minutes=54),
            NOW - timedelta(minutes=20),
        ],
        cold_activation_times=[NOW - timedelta(minutes=19)],
        target_miss_times=[],
        idle_episode_seconds=600,
    )
    assert window.request_count == 3
    assert window.idle_gap_episode_count == 1
    assert window.expected_cold_activations_per_hour == pytest.approx(1.0)


def test_missing_history_or_cost_selects_minimum_without_fabricating_success() -> None:
    selected = policy(minimum=FastStartLevel.L2, wait_value=None)
    decision = evaluate_automatic_fast_start(
        policy=selected,
        paths=[path(FastStartLevel.L1), path(FastStartLevel.L4, ready=False)],
        short_history=None,
        long_history=None,
        prior_state=AutomaticFastStartState(assigned_level=FastStartLevel.L4),
        now=NOW,
    )
    assert decision.assigned_level is FastStartLevel.L2
    assert decision.reason is AutomaticDecisionReason.MISSING_DATA_MINIMUM
    assert not decision.satisfied
    assert decision.fallback_level is FastStartLevel.L1


def test_require_target_does_not_offer_a_lower_fallback() -> None:
    selected = policy(
        minimum=FastStartLevel.L2,
        wait_value=None,
        fallback=FastStartFallbackPolicy.REQUIRE_TARGET,
    )
    decision = evaluate_automatic_fast_start(
        policy=selected,
        paths=[path(FastStartLevel.L1)],
        short_history=None,
        long_history=None,
        prior_state=None,
        now=NOW,
    )
    assert not decision.satisfied
    assert decision.fallback_level is None
    assert decision.fallback_mechanism_id is None


def test_cost_and_idle_episode_rate_drive_candidate_then_three_wins_promote_one_level() -> None:
    selected = policy(minimum=FastStartLevel.L1, maximum=FastStartLevel.L4, wait_value=0.01)
    paths = [
        path(FastStartLevel.L1, mechanism="regional", p95=250, cost=0),
        path(FastStartLevel.L4, mechanism="ram", p95=25, cost=0.5),
    ]
    short = history(hours=1, requests=10, activations=0, idle_episodes=3)
    long = history(hours=24, requests=50, activations=0, idle_episodes=10)
    first = evaluate(selected_policy=selected, paths=paths, short=short, long=long)
    assert first.assigned_level is FastStartLevel.L1
    assert first.state.pending_level is FastStartLevel.L2
    assert first.state.consecutive_wins == 1

    second = evaluate(
        selected_policy=selected,
        paths=paths,
        short=short,
        long=long,
        prior=first.state,
        now=NOW + timedelta(minutes=5),
    )
    third = evaluate(
        selected_policy=selected,
        paths=paths,
        short=short,
        long=long,
        prior=second.state,
        now=NOW + timedelta(minutes=10),
    )
    assert third.reason is AutomaticDecisionReason.PROMOTED
    assert third.assigned_level is FastStartLevel.L2
    assert third.mechanism_id == "ram"
    assert third.satisfied


def test_two_target_misses_promote_after_cooldown() -> None:
    selected = policy()
    previous = AutomaticFastStartState(
        assigned_level=FastStartLevel.L1,
        last_transition_at=NOW - timedelta(hours=1),
    )
    short = history(hours=1, requests=5, activations=2, idle_episodes=1, misses=2)
    decision = evaluate(
        selected_policy=selected,
        paths=[path(FastStartLevel.L1, cost=10), path(FastStartLevel.L4, cost=0)],
        prior=previous,
        short=short,
    )
    assert decision.reason is AutomaticDecisionReason.TARGET_MISS_PROMOTION
    assert decision.assigned_level is FastStartLevel.L2


def test_demotion_is_one_level_after_24_hours_and_is_blocked_by_a_miss() -> None:
    selected = policy()
    pending_since = NOW - timedelta(hours=24)
    previous = AutomaticFastStartState(
        assigned_level=FastStartLevel.L4,
        pending_level=FastStartLevel.L3,
        pending_since=pending_since,
        last_transition_at=NOW - timedelta(hours=25),
    )
    paths = [path(FastStartLevel.L1, p95=200, cost=0), path(FastStartLevel.L4, p95=25, cost=100)]
    blocked = evaluate(
        selected_policy=selected,
        paths=paths,
        prior=previous,
        short=history(hours=1, requests=3, activations=1, idle_episodes=0, misses=1),
    )
    assert blocked.reason is AutomaticDecisionReason.DEMOTION_BLOCKED_BY_MISS
    assert blocked.assigned_level is FastStartLevel.L4

    demoted = evaluate(selected_policy=selected, paths=paths, prior=previous)
    assert demoted.reason is AutomaticDecisionReason.DEMOTED
    assert demoted.assigned_level is FastStartLevel.L3


def test_mechanism_loss_marks_unsatisfied_before_hysteretic_demotion() -> None:
    selected = policy()
    previous = AutomaticFastStartState(
        assigned_level=FastStartLevel.L4,
        last_transition_at=NOW - timedelta(hours=2),
    )
    decision = evaluate(
        selected_policy=selected,
        paths=[path(FastStartLevel.L1), path(FastStartLevel.L4, ready=False)],
        prior=previous,
    )
    assert decision.assigned_level is FastStartLevel.L4
    assert decision.reason is AutomaticDecisionReason.DEMOTION_PENDING
    assert not decision.satisfied
    assert decision.fallback_level is FastStartLevel.L1


def test_operator_bounds_change_is_immediate() -> None:
    selected = policy(minimum=FastStartLevel.L2, maximum=FastStartLevel.L3)
    decision = evaluate(
        selected_policy=selected,
        paths=[path(FastStartLevel.L3)],
        prior=AutomaticFastStartState(assigned_level=FastStartLevel.OFF),
    )
    assert decision.reason is AutomaticDecisionReason.POLICY_BOUNDS_CHANGED
    assert decision.assigned_level is FastStartLevel.L2
    assert decision.satisfied


@pytest.mark.parametrize(
    "selected_policy",
    [
        AutomaticFastStartPolicy(
            minimum_level=FastStartLevel.OFF,
            maximum_level=FastStartLevel.OFF,
            wait_second_value=0,
        ),
        AutomaticFastStartPolicy(
            minimum_level=FastStartLevel.L4,
            maximum_level=FastStartLevel.L4,
            wait_second_value=0,
        ),
    ],
)
def test_equal_bounds_are_valid(selected_policy: AutomaticFastStartPolicy) -> None:
    assert selected_policy.minimum_level is selected_policy.maximum_level


def test_invalid_inputs_fail_before_a_decision() -> None:
    with pytest.raises(ValueError, match="minimum_level"):
        AutomaticFastStartPolicy(
            minimum_level=FastStartLevel.L4,
            maximum_level=FastStartLevel.L1,
            wait_second_value=0.1,
        )
    duplicate = path(FastStartLevel.L1, mechanism="same")
    with pytest.raises(ValueError, match="unique"):
        evaluate(
            selected_policy=policy(),
            paths=[duplicate, duplicate],
        )
    with pytest.raises(ValueError, match="future"):
        evaluate(
            selected_policy=policy(),
            paths=[path(FastStartLevel.L1)],
            prior=AutomaticFastStartState(
                assigned_level=FastStartLevel.L1,
                last_transition_at=NOW + timedelta(seconds=1),
            ),
        )
