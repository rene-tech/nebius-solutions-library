#!/usr/bin/env bash
# Offline checks for the Proteina-Complexa runtime image slice.
# No GPU, cluster, registry or checkpoint is required.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "== python compile =="
for module in runtime_entrypoint.py build_proteina_complexa.py \
              qualification/render_plan.py qualification/submit_plan.py \
              qualification/validate_result.py qualification/assemble_evidence.py \
              tests/test_proteina_complexa_runtime.py; do
  python3 -m py_compile "$module"
  echo "   ok $module"
done

echo "== json parses =="
for document in image-lock.json qualification/generated-plan.json; do
  [ -f "$document" ] || { echo "   skip $document (not generated yet)"; continue; }
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$document"
  echo "   ok $document"
done
for document in evidence/*.json; do
  [ -e "$document" ] || continue
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$document"
  echo "   ok $document"
done

echo "== dependency lock digest matches the lock file =="
python3 - <<'PY'
import hashlib, json, pathlib
lock = json.loads(pathlib.Path("image-lock.json").read_text())
expected = lock["image"]["dependency_lock"]["sha256"]
actual = hashlib.sha256(pathlib.Path("requirements.lock").read_bytes()).hexdigest()
assert actual == expected, f"requirements.lock is {actual}, lock pins {expected}"
print(f"   ok sha256:{actual}")
PY

echo "== contract tests =="
# Discovery, not a single named module: the independent gate in
# qualification/validate_result.py used only to be byte-compiled here, never
# executed, which is how a 162-byte two-atom PDB came to pass the whole ligand
# gate.  tests/test_validate_result_gate.py now runs it for real.
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t . -p 'test_*.py' 2>&1 | tail -5

echo "== plan renders =="
python3 qualification/render_plan.py \
  --image sha256:0000000000000000000000000000000000000000000000000000000000000000 \
  --run-prefix fs2-cxq-render-check \
  --output /tmp/fs2-cxq-render-check.json >/dev/null
python3 - <<'PY'
import json
plan = json.load(open("/tmp/fs2-cxq-render-check.json"))
jobs = [item for item in plan["manifests"] if item["kind"] == "Job"]
assert len(jobs) == 3, f"expected three variant Jobs, got {len(jobs)}"
for job in jobs:
    command = job["spec"]["template"]["spec"]["containers"][0]["command"]
    assert command[0] == "python", command
    joined = " ".join(command)
    for shell_token in ("&&", "||", ";", "|", "bash", "sh -c"):
        assert shell_token not in joined, f"shell token {shell_token!r} in {joined}"
print(f"   ok {len(jobs)} shell-free variant jobs")
PY
rm -f /tmp/fs2-cxq-render-check.json

echo "== the semantic gate rejects a degenerate output =="
# Defence in depth: prove from the shell gate, not only from unittest, that
# the published two-atom counterexample is refused.
python3 - <<'GATECHECK'
import json, pathlib, subprocess, sys, tempfile

ATOM = (
    "ATOM      1  CA  ALA B   1       0.000   0.000   0.000  1.00  0.00           C\n"
    "HETATM 9000  C1  OQO A   1       0.000   0.000   0.000  1.00  0.00           C\n"
    "END\n"
)
ARGV = [
    "python", "-m", "proteinfoundation.generate",
    "++ckpt_path=/opt/fs2/artifacts/complexa-ligand",
    "++ckpt_name=complexa_ligand.ckpt",
    "++autoencoder_ckpt_path=/opt/fs2/artifacts/complexa-ligand/complexa_ligand_ae.ckpt",
]

with tempfile.TemporaryDirectory() as raw:
    directory = pathlib.Path(raw) / "ligand"
    directory.mkdir(parents=True)
    (directory / "designed.pdb").write_text(ATOM, encoding="utf-8")
    (directory / "upstream.log").write_text(
        "INFO GPU available: True (cuda), used: True\n", encoding="utf-8")
    (directory / "result.json").write_text(json.dumps({
        "terminal_state": "PASS", "variant": "ligand", "upstream_exit_code": 0,
        "argv": ARGV,
        "artifact_verification": {
            "content_digests_verified": False,
            "markers": [{"label": "a", "digest_verified": False},
                        {"label": "b", "digest_verified": False}],
            "rosettafold3": {"bound": True, "exercised": False}},
        "phases": {"model_load_seconds": 1.0, "sampling_seconds": 1.0,
                   "compute_seconds": 1.0, "lora_reapplied": True},
        "target": {"binder_length": [100], "ligand_residues": ["OQO"]},
    }), encoding="utf-8")
    finished = subprocess.run(
        [sys.executable, "qualification/validate_result.py",
         "--root", raw, "--variant", "ligand"],
        capture_output=True, text=True, check=False)
    if finished.returncode == 0:
        sys.exit("REGRESSION: the two-atom counterexample passed the semantic gate")
    print(f"   ok the two-atom counterexample is refused (exit {finished.returncode})")
GATECHECK

echo "ALL CHECKS PASSED"
