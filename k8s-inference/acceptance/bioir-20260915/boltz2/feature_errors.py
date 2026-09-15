"""Probe strict native schema without expensive valid GPU requests."""
import copy
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

request = json.loads(Path("/models/fixtures/T1031.json").read_text())
cases = {}
for field, value in {"seed": 42, "model": "boltz2", "templates": [], "affinity": True}.items():
    cases[field] = {**request, field: value}
for kind in ("dna", "rna", "ligand"):
    candidate = copy.deepcopy(request)
    candidate["polymers"][0]["molecule_type"] = kind
    cases[kind] = candidate
cases["empty_polymers"] = {**request, "polymers": []}
cases["zero_samples"] = {**request, "diffusion_samples": 0}
results = []
for name, payload in cases.items():
    req = urllib.request.Request("http://127.0.0.1:8000/biology/mit/boltz2/predict",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            status, body = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read().decode()
    results.append({"case": name, "status": status, "expected": 422, "pass": status == 422,
                    "error_types": [e["type"] for e in json.loads(body).get("detail", [])]})
Path(sys.argv[1]).write_text(json.dumps(results, indent=2))
print(json.dumps(results))
