"""Promote only the prepared Cosmos template through the existing admin API.

Default is read-only preparation. --execute drains this App, waits for its
observed cold revision, and applies only the new template while restoring the
original lifecycle and all other settings. A failed attempt is retained privately;
there is no blind retry, rollback, direct CRD patch, or customer-key change.
"""

import argparse
import base64
import copy
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from prepare_contract import MODEL, NEW, OLD, TEMPLATE_NAME


def replacement(original):
    if original["modelRef"] != MODEL or original["runtime"]["templateRef"]["digest"] != OLD:
        raise ValueError("unexpected_current_cosmos_template")
    proposed = copy.deepcopy(original)
    proposed["runtime"]["templateRef"] = {"name": TEMPLATE_NAME, "digest": NEW}
    return proposed


def cold_observed(current, view):
    observation = view.get("observation") or {}
    status = observation.get("status") or {}
    replicas = status.get("replicas") or {}
    return (observation.get("revision") == current["revision"]
            and status.get("spec_digest") == current["etag"]
            and status.get("phase") == "Cold"
            and all(replicas.get(field) == 0 for field in ("desired", "ready", "available")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "origin"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipt = {"started_at": datetime.now(UTC).isoformat(), "execute": args.execute,
               "model": MODEL, "outcome": "incomplete", "steps": []}

    def save():
        (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")

    command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
               "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]
    # Explicit operator-supplied kubeconfig/context, fixed read-only command, no shell.
    secret = json.loads(subprocess.check_output(command))  # noqa: S603
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    path = "/admin/api/v1/model-deployments/" + MODEL
    try:
        with httpx.Client(base_url=args.origin, headers={"origin": args.origin},
                          timeout=60, trust_env=False) as client:
            response = client.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token})
            response.raise_for_status()

            def read(suffix=""):
                response = client.get(path + suffix)
                response.raise_for_status()
                return response.json()["data"]

            original = read()
            proposed = replacement(original["spec"])
            receipt.update(before=original, proposed_spec=proposed)
            save()
            if not args.execute:
                receipt["outcome"] = "prepared_not_applied"
                return
            if original["spec"]["lifecycle"]["desiredState"] == "Enabled":
                response = client.post(path + ":drain", json={
                    "base_etag": original["etag"], "idempotency_key": "cosmos-drain-" + uuid4().hex})
                receipt["steps"].append({"action": "drain", "http_status": response.status_code,
                                          "response": response.json()})
                save()
                response.raise_for_status()
            deadline = time.monotonic() + 300
            while True:
                current, view = read(), read("/status")
                expected = copy.deepcopy(original["spec"])
                if original["spec"]["lifecycle"]["desiredState"] == "Enabled":
                    expected["lifecycle"]["desiredState"] = "Draining"
                if current["spec"] != expected:
                    raise ValueError("settings_changed_during_drain")
                if cold_observed(current, view):
                    break
                if time.monotonic() >= deadline:
                    raise ValueError("cold_revision_observation_timeout")
                time.sleep(3)
            receipt["cold_observation"] = view
            proposal = {"name": MODEL, "namespace": original["namespace"],
                        "base_etag": current["etag"], "spec": proposed}
            response = client.post("/admin/api/v1/model-deployments:plan-preview", json=proposal)
            response.raise_for_status()
            preview = response.json()["data"]
            receipt["preview"] = preview
            save()
            if preview["decision"]["disposition"] != "accepted":
                raise ValueError("template_preview_rejected")
            response = client.post("/admin/api/v1/model-deployments:apply", json={
                "preview_id": preview["preview_id"], "proposed_etag": preview["proposed_etag"],
                "proposal": proposal, "idempotency_key": "cosmos-template-" + uuid4().hex})
            receipt["steps"].append({"action": "apply", "http_status": response.status_code,
                                      "response": response.json()})
            save()
            response.raise_for_status()
            receipt["after"] = read()
            if receipt["after"]["spec"] != proposed:
                raise ValueError("template_readback_mismatch")
            receipt["outcome"] = "desired_template_applied_controller_readback_still_required"
    except (ValueError, KeyError, httpx.HTTPError) as exc:
        receipt["outcome"] = "failed_inspect_private_receipt_before_retry"
        receipt["error_type"] = type(exc).__name__
        if isinstance(exc, ValueError):
            receipt["error_code"] = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            receipt["http_status"] = exc.response.status_code
        raise SystemExit("Promotion stopped; inspect the private receipt before recovery.") from None
    finally:
        save()
        print(json.dumps({"model": MODEL, "outcome": receipt["outcome"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
