"""Create an owner-only short-lived browser session for live UI qualification."""

import argparse
import json
import os
from contextlib import closing
from pathlib import Path

from inventory import admin_client

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
with closing(admin_client("/home/tux/secure-handoff/cosmos-stockholm-sandbox2-20260917.kubeconfig",
                          "fs2-remediation-sandbox2", "https://89.169.99.188")) as client:
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                "expires": c.expires or -1, "httpOnly": True, "secure": True, "sameSite": "Strict"}
               for c in client.cookies.jar]
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
        json.dump({"cookies": cookies, "origins": []}, file)
print("Short-lived browser session stored privately; no token printed")
