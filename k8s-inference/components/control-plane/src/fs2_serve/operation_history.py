"""Customer operation history pagination, independent of operator endpoints."""

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, Field, ValidationError

from .models import OperationView, StrictModel


class OperationPage(StrictModel):
    data: tuple[OperationView, ...] = Field(max_length=200)
    next_cursor: str | None = None


class _Cursor(StrictModel):
    accepted_at: AwareDatetime
    id: UUID


def decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if cursor is None:
        return None
    try:
        payload = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = _Cursor.model_validate_json(payload)
    except (ValueError, binascii.Error, ValidationError) as error:
        raise ValueError("invalid operation history cursor") from error
    return value.accepted_at, value.id


def encode_cursor(operation: OperationView) -> str:
    payload = json.dumps({"accepted_at": operation.accepted_at.isoformat(), "id": str(operation.id)})
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
