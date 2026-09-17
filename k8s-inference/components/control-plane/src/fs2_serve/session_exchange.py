"""Shared fixed-window coordinates for operator session-exchange admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import floor

SESSION_EXCHANGE_AGGREGATE_SHARDS = 16
SESSION_EXCHANGE_SOURCE_SLOTS = 65536


@dataclass(frozen=True)
class SessionExchangeCoordinates:
    source_slot_a: int
    source_slot_b: int
    aggregate_shard_count: int
    aggregate_shard: int
    aggregate_shard_quota: int


def validate_session_exchange_settings(
    source_fingerprint: str,
    *,
    window_seconds: int,
    maximum_source_attempts: int,
    maximum_aggregate_attempts: int,
) -> None:
    if len(source_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in source_fingerprint
    ):
        raise ValueError("operator exchange source fingerprint is invalid")
    if not 1 <= window_seconds <= 3600 or not 1 <= maximum_source_attempts <= 100:
        raise ValueError("operator exchange limiter settings are invalid")
    if (
        not 10 <= maximum_aggregate_attempts <= 10000
        or maximum_aggregate_attempts <= maximum_source_attempts
    ):
        raise ValueError("operator exchange aggregate limiter setting is invalid")


def session_exchange_coordinates(
    source_fingerprint: str,
    *,
    window_seconds: int,
    maximum_source_attempts: int,
    maximum_aggregate_attempts: int,
) -> SessionExchangeCoordinates:
    validate_session_exchange_settings(
        source_fingerprint,
        window_seconds=window_seconds,
        maximum_source_attempts=maximum_source_attempts,
        maximum_aggregate_attempts=maximum_aggregate_attempts,
    )
    digest = bytes.fromhex(source_fingerprint)
    slot_a = int.from_bytes(digest[0:4], "big") % SESSION_EXCHANGE_SOURCE_SLOTS
    slot_b = int.from_bytes(digest[4:8], "big") % SESSION_EXCHANGE_SOURCE_SLOTS
    if slot_b == slot_a:
        slot_b = (slot_b + 1) % SESSION_EXCHANGE_SOURCE_SLOTS
    shard_count = min(
        SESSION_EXCHANGE_AGGREGATE_SHARDS,
        max(1, maximum_aggregate_attempts // maximum_source_attempts),
    )
    shard = int.from_bytes(digest[8:10], "big") % shard_count
    shard_quota = maximum_aggregate_attempts // shard_count
    if shard < maximum_aggregate_attempts % shard_count:
        shard_quota += 1
    return SessionExchangeCoordinates(
        source_slot_a=slot_a,
        source_slot_b=slot_b,
        aggregate_shard_count=shard_count,
        aggregate_shard=shard,
        aggregate_shard_quota=shard_quota,
    )


def session_exchange_window_start(attempted_at: datetime, window_seconds: int) -> datetime:
    if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
        raise ValueError("operator exchange timestamp must be timezone-aware")
    epoch = floor(attempted_at.timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, tz=UTC)
