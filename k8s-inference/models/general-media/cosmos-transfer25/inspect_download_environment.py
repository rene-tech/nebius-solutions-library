"""Print non-sensitive credential-shape and static NIM startup diagnostics.

Run only inside the pinned NIM canary image. Never print environment values,
credential length, credential hashes, or exception messages.
"""

import json
import os
from pathlib import Path


def main() -> None:
    key = os.environ.get("NGC_API_KEY", "")
    print(
        json.dumps(
            {
                "credential_present": bool(key),
                "credential_trim_required": key != key.strip(),
                "credential_contains_cr": "\r" in key,
                "credential_contains_lf": "\n" in key,
                "credential_ascii_only": key.isascii(),
                "credential_has_internal_whitespace": any(c.isspace() for c in key.strip()),
                "credential_has_control_character": any(ord(c) < 32 or ord(c) == 127 for c in key),
                "proxy_variable_presence": {
                    name: bool(os.environ.get(name))
                    for name in (
                        "HTTP_PROXY",
                        "HTTPS_PROXY",
                        "ALL_PROXY",
                        "NO_PROXY",
                        "http_proxy",
                        "https_proxy",
                        "all_proxy",
                        "no_proxy",
                    )
                },
            },
            sort_keys=True,
        ),
        flush=True,
    )
    # These files are immutable source in the pinned image, not runtime config.
    for name, first, last in (
        ("/opt/nim/.venv/bin/start_server", 1, 60),
        ("/opt/nim/.venv/lib/python3.12/site-packages/nimlib/nim_sdk.py", 1, 150),
        ("/opt/nim/.venv/lib/python3.12/site-packages/nimlib/nim_sdk.py", 270, 420),
    ):
        path = Path(name)
        print(json.dumps({"static_source": name, "first": first, "last": last}))
        if path.is_file():
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if first <= number <= last:
                    print(f"{number}: {line}")


if __name__ == "__main__":
    main()
