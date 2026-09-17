"""Shared bounds for exact operator session-exchange sliding-window admission."""

from __future__ import annotations

from datetime import datetime, timedelta

SESSION_EXCHANGE_ADMISSION_SLOTS = 10000
SESSION_EXCHANGE_EVIDENCE_SLOTS = 65536


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


def session_exchange_cutoff(
    source_fingerprint: str,
    *,
    attempted_at: datetime,
    window_seconds: int,
    maximum_source_attempts: int,
    maximum_aggregate_attempts: int,
) -> datetime:
    validate_session_exchange_settings(
        source_fingerprint,
        window_seconds=window_seconds,
        maximum_source_attempts=maximum_source_attempts,
        maximum_aggregate_attempts=maximum_aggregate_attempts,
    )
    if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
        raise ValueError("operator exchange timestamp must be timezone-aware")
    return attempted_at - timedelta(seconds=window_seconds)


def session_exchange_evidence_slot(source_fingerprint: str) -> int:
    """Select bounded forensic state; collisions never influence admission."""

    return int.from_bytes(bytes.fromhex(source_fingerprint)[0:4], "big") % SESSION_EXCHANGE_EVIDENCE_SLOTS
