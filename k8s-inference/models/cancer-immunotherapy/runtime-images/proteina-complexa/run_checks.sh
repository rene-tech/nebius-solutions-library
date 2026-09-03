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
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_proteina_complexa_runtime -v 2>&1 | tail -5

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

echo "ALL CHECKS PASSED"
