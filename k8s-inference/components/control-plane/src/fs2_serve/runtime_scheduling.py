"""Authenticated, opaque scheduling groups for owned speech workers only.

This does not authorize customers or replace canonical admission. Identity comes
from the durable claimed operation, never from forwarded customer headers.
"""

import hashlib
import hmac
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RuntimeScheduling:
    group_key: bytes = field(repr=False)
    gateway_token: str = field(repr=False)

    @classmethod
    def load(cls, group_key_file: Path | None, gateway_token_file: Path | None):
        if group_key_file is None and gateway_token_file is None:
            return None
        if (group_key_file is None or gateway_token_file is None or group_key_file == gateway_token_file
                or not group_key_file.is_absolute() or not gateway_token_file.is_absolute()):
            raise ValueError("speech_scheduling_requires_distinct_mounted_secrets")
        try:
            with group_key_file.open("rb") as handle:
                key = handle.read(4097)
            with gateway_token_file.open("rb") as handle:
                token_bytes = handle.read(4097)
            token = token_bytes.decode("ascii").strip()
        except (OSError, UnicodeError):
            raise ValueError("speech_scheduling_secret_unavailable") from None
        if (not 32 <= len(key) <= 4096 or len(token_bytes) > 4096 or not 32 <= len(token) <= 1024
                or any(c.isspace() or ord(c) < 33 for c in token) or key.strip() == token.encode()):
            raise ValueError("speech_scheduling_secret_invalid")
        return cls(key, token)

    def headers(self, operation) -> dict[str, str]:
        tenant, model = operation.tenant_id, operation.model_id
        if (not isinstance(tenant, str) or not 1 <= len(tenant) <= 120
                or not isinstance(model, str) or not 1 <= len(model) <= 128):
            raise ValueError("speech_scheduling_identity_invalid")
        subject = b"fs2-stt-scheduling-v1\0" + model.encode() + b"\0" + tenant.encode()
        return {"authorization": "Bearer " + self.gateway_token,
                "x-fs2-scheduling-group": hmac.new(self.group_key, subject, hashlib.sha256).hexdigest()}
