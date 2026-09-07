#!/usr/bin/env python3
"""Execute an untouched accepted scientific command through a restored loader.

Private operator inputs contain the original argv. The wrapper still validates
all provenance, localization and output contracts; this probe additionally
requires nonempty finite-coordinate structures and records their hashes.
"""

import argparse
import json
from pathlib import Path
import subprocess


REMOTE = r"""
import hashlib,json,math,os,shlex,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
request=json.loads(sys.stdin.readline())
os.environ.update(request["environment"])
os.environ[request["worker_variable"]]="http://127.0.0.1:8000"
os.setgid(10001)
os.setuid(10001)
started=datetime.now(timezone.utc).isoformat()
before=time.monotonic()
result=subprocess.run(request["command"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
print(result.stdout,flush=True)
if result.returncode:
    raise SystemExit(result.returncode)
root=Path(request["output"])
structures=sorted(root.rglob("*.cif"))+sorted(root.rglob("*.pdb"))
assert structures,"normal scientific wrapper emitted no structure"
verified=[]
for path in structures:
    try:
        import gemmi
        atoms=[a for m in gemmi.read_structure(str(path)) for c in m for r in c for a in r]
        assert atoms and all(math.isfinite(v) for a in atoms for v in (a.pos.x,a.pos.y,a.pos.z))
        count=len(atoms)
    except ImportError:
        headers=[];count=0
        for raw in path.read_text().splitlines():
            line=raw.strip()
            if line.startswith("_atom_site."):
                headers.append(line.split()[0]);continue
            if not headers or not line:continue
            if line=="#" or line=="loop_" or line.startswith("_"):
                if count:break
                continue
            fields=shlex.split(line)
            assert len(fields)==len(headers)
            assert all(math.isfinite(float(fields[headers.index("_atom_site.Cartn_"+axis)])) for axis in "xyz")
            count+=1
        assert count
    verified.append({"path":str(path.relative_to(root)),"bytes":path.stat().st_size,"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"finite_atoms":count})
print("FS2_SNAPSHOT_SEMANTIC "+json.dumps({"status":"passed","started_at":started,"finished_at":datetime.now(timezone.utc).isoformat(),"execution_seconds":time.monotonic()-before,"argv_sha256":hashlib.sha256(json.dumps(request["command"],separators=(",",":")).encode()).hexdigest(),"structures":verified}),flush=True)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", default="k8s-inference-h100")
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--pod", required=True)
    parser.add_argument("--container", default="scientific-stage")
    parser.add_argument("--python", required=True)
    parser.add_argument("--command", type=Path, required=True)
    parser.add_argument("--worker-variable", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    environment_file = args.command.with_name("request-environment.json")
    request = {
        "command": json.loads(args.command.read_bytes()),
        "worker_variable": args.worker_variable,
        "output": args.output,
        "environment": json.loads(environment_file.read_bytes())
        if environment_file.exists()
        else {},
    }
    command = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "-n",
        args.namespace,
        "exec",
        "-i",
        args.pod,
        "-c",
        args.container,
        "--",
        args.python,
        "-c",
        REMOTE,
    ]
    raise SystemExit(
        subprocess.run(command, input=json.dumps(request) + "\n", text=True).returncode
    )


if __name__ == "__main__":
    main()
