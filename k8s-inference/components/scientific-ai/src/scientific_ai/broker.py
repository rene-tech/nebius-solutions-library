"""S3 streaming with per-object URLs; bucket credentials never enter GPU pods.

The application's authenticated broker authorizes the *current* S2 attempt and
one operation prefix before signing a 60-second URL. Data goes directly to the
customer's S3 endpoint. Multipart handles large trajectories without buffering
them in API memory. No URL or bearer is logged.
"""

from __future__ import annotations

import io
from urllib.parse import urlsplit

import httpx

PART_SIZE = 64 * 1024**2


def https_origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("an authenticated HTTPS endpoint is required")
    if parsed.query or parsed.fragment:
        raise ValueError("endpoint must not contain credentials or query arguments")
    return url.rstrip("/")


class _Stream(io.RawIOBase):
    def __init__(self, response: httpx.Response):
        self.response = response
        self.blocks = response.iter_bytes(chunk_size=1024**2)
        self.buffer = b""

    def readable(self):
        return True

    def readinto(self, destination):
        if not self.buffer:
            self.buffer = next(self.blocks, b"")
        size = min(len(destination), len(self.buffer))
        destination[:size], self.buffer = self.buffer[:size], self.buffer[size:]
        return size

    def close(self):
        self.response.close()
        super().close()


class BrokerObjects:
    def __init__(self, *, http: httpx.Client, url: str, token: str, attempt_id: str):
        self.http, self.url = http, https_origin(url)
        self.token, self.attempt_id = token, attempt_id

    def _request(self, action: str, key: str, **values) -> dict:
        response = self.http.post(
            self.url + "/internal/storage",
            headers={"Authorization": "Bearer " + self.token},
            json={"action": action, "key": key, "attempt_id": self.attempt_id, **values},
            follow_redirects=False,
        )
        if response.status_code in (408, 429, 500, 502, 503, 504):
            raise ConnectionError("job storage authorization is temporarily unavailable")
        if response.status_code != 200:
            raise PermissionError(f"job storage grant refused (HTTP {response.status_code})")
        return response.json()

    def get(self, key: str):
        target = self._request("get", key)
        request = self.http.build_request("GET", target["url"])
        response = self.http.send(request, stream=True, follow_redirects=False)
        if response.status_code != 200:
            response.close()
            if response.status_code in (408, 429, 500, 502, 503, 504):
                raise ConnectionError("customer object is temporarily unavailable")
            raise OSError("customer object could not be read")
        return io.BufferedReader(_Stream(response), buffer_size=1024**2)

    def put(self, key: str, source) -> None:
        first = source.read(PART_SIZE)
        second = source.read(PART_SIZE)
        if not second:
            target = self._request("put", key)
            response = self.http.put(target["url"], content=first, follow_redirects=False)
            if response.status_code in (408, 429, 500, 502, 503, 504):
                raise ConnectionError("customer object write is temporarily unavailable")
            if response.status_code not in (200, 201, 204):
                raise OSError("customer object could not be written")
            return
        upload = self._request("multipart-create", key)["upload_id"]
        parts = []
        try:
            data, pending = first, second
            while data:
                number = len(parts) + 1
                target = self._request("multipart-part", key, upload_id=upload, part_number=number)
                response = self.http.put(target["url"], content=data, follow_redirects=False)
                if response.status_code != 200 or "etag" not in response.headers:
                    raise OSError("customer object part could not be written")
                parts.append({"PartNumber": number, "ETag": response.headers["etag"]})
                data, pending = pending, source.read(PART_SIZE)
            self._request("multipart-complete", key, upload_id=upload, parts=parts)
        except Exception:
            try:
                self._request("multipart-abort", key, upload_id=upload)
            except Exception:
                pass  # Original failure matters; provider lifecycle cleans abandoned parts.
            raise
