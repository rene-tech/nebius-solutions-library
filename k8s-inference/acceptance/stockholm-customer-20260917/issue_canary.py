#!/usr/bin/env python3
"""Issue exactly one disposable same-policy Stockholm key after an explicit GO.

Never changes an existing token. Output key material is captured directly into
an exclusive owner-only file and is never printed. This is not run by collectors.
"""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from collect_live import POD_READ, collect, kube, require_release, write_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--expected-cp-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        raise ValueError("existing_canary_file_refused")
    before = collect(args.kubeconfig, args.context)
    require_release(before, args.expected_cp_image)
    principal = "stockholm-canary-" + str(uuid4())
    payload = dict(before["team_policy"], principal_id=principal, name=principal,
                   expires_at=(datetime.now(UTC) + timedelta(hours=6)).isoformat())
    # Reuse the in-pod read/auth setup but suppress its metadata print. No secret
    # passes through argv, the shell, logging, or the parent process's stdout.
    setup = POD_READ[:POD_READ.index("print(json.dumps(")]
    code = setup + "\npayload = " + repr(payload) + '''
request = urllib.request.Request('http://127.0.0.1:8080/admin/v1/tokens',
    data=json.dumps(payload).encode(), method='POST',
    headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json',
             'Host':urlsplit(origin).netloc})
with urllib.request.urlopen(request, timeout=15) as response:
    print(response.read().decode())
'''
    issued = kube(args.kubeconfig, args.context, "-n", "fs2-system", "exec", "-i",
                  "deploy/fs2-serve-control-plane", "-c", "control-plane", "--", "python", "-", script=code)
    # Persist raw issuance immediately, including if a response shape has drifted,
    # so an operator can recover/revoke this one created key without resubmitting.
    write_private(args.output, {"schema": "fs2-customer-key/v1", "disposable": True,
                               "principal_id": principal, "issued": issued,
                               "token_id": issued.get("id"),
                               "secret": issued.get("token"), "policy": before["team_policy"]})
    print(json.dumps({"principal_id": principal, "output": str(args.output),
                      "expires_at": payload["expires_at"], "existing_customer_keys_changed": False}))


if __name__ == "__main__":
    main()
