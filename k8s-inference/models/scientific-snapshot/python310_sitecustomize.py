"""Compatibility for pinned Python 3.10 model images, mounted only on request.

The frozen lifecycle helper uses Python 3.11's hashlib.file_digest API. Supply
the same streaming operation on older interpreters without altering the model,
driver, library versions or frozen checkpoint helper. Register this file as
sitecustomize.py in that model's immutable source CM and include it in its
captured source identity. Python 3.11+ keeps its original implementation.
"""

import hashlib


if not hasattr(hashlib, "file_digest"):
    def file_digest(fileobj, digest, *, _bufsize=2**18):
        result = hashlib.new(digest) if isinstance(digest, str) else digest()
        buffer = bytearray(_bufsize)
        view = memoryview(buffer)
        while True:
            count = fileobj.readinto(buffer)
            if not count:
                return result
            result.update(view[:count])

    hashlib.file_digest = file_digest
