"""Read-only, bounded readiness body sampling on the exact three API readers.

Only HTTP status, timing and the already public error code/message are retained;
no successful readiness document, request header or customer data is collected.
"""

import argparse
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

REMOTE = r"""
import json,sys,time,urllib.error,urllib.request
from datetime import UTC,datetime
deadline=time.monotonic()+int(sys.argv[1])
while time.monotonic()<deadline:
    start=time.monotonic()
    event={'at':datetime.now(UTC).isoformat()}
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/readyz',timeout=3) as response:
            event['status']=response.status
    except urllib.error.HTTPError as error:
        event['status']=error.code
        try:
            value=json.loads(error.read(8192))
            failure=value.get('error',{})
            if isinstance(failure,dict):
                event['code']=failure.get('type',failure.get('code'))
                event['message']=failure.get('message')
        except (ValueError,AttributeError):
            event['error_type']='unreadable_readiness_error'
    except Exception as error:
        event['error_type']=type(error).__name__
    event['elapsed_seconds']=round(time.monotonic()-start,6)
    print(json.dumps(event),flush=True)
    time.sleep(5)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=900)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 1800:
        parser.error("seconds must be between 30 and 1800")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    base = ["kubectl", "--context", args.context, "-n", "fs2-system"]
    selected = subprocess.run(
        [
            *base,
            "--request-timeout=15s",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/component=gateway,app.kubernetes.io/instance=fs2-serve-control-plane",
            "-o",
            "json",
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=25,
    )
    pods = json.loads(selected.stdout)["items"]
    if len(pods) != 3 or any(pod["metadata"].get("deletionTimestamp") for pod in pods):
        raise ValueError("Expected exactly three stable existing API readers")

    def sample(pod):
        name = pod["metadata"]["name"]
        with (args.output / (name + ".jsonl")).open("w") as records:
            result = subprocess.run(
                [
                    *base,
                    "exec",
                    name,
                    "-c",
                    "control-plane",
                    "--",
                    "python",
                    "-c",
                    REMOTE,
                    str(args.seconds),
                ],
                text=True,
                stdout=records,
                stderr=subprocess.PIPE,
                timeout=args.seconds + 40,
                check=False,
            )
        return {
            "name": name,
            "uid": pod["metadata"]["uid"],
            "images": [
                item.get("imageID")
                for item in pod["status"].get("containerStatuses", [])
            ],
            "exit_code": result.returncode,
        }

    with ThreadPoolExecutor(max_workers=3) as executor:
        result = list(executor.map(sample, pods))
    (args.output / "summary.json").write_text(
        json.dumps(
            {"observed_at": datetime.now(UTC).isoformat(), "pods": result}, indent=2
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "readers": len(result),
                "failed_collectors": sum(item["exit_code"] != 0 for item in result),
            }
        )
    )


if __name__ == "__main__":
    main()
