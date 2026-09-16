"""Mechanically extract exact upstream assets; do not execute upstream modules."""

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

revision = "1c17f9e66c092d9480c4bda5a2bebc80b7f84961"
source = Path(sys.argv[1]).resolve()
assert subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() == revision
destination = Path(__file__).resolve().parents[1] / "src/fs2_mindeval/assets"
destination.mkdir(parents=True, exist_ok=True)
selected = {
    "prompts.py": {"MINDEVAL_CLINICIAN_TEMPLATE": "clinician_v0_1", "INTERACTION_MEMBER_TEMPLATE": "patient_v0_2"},
    "judge_prompts.py": {"JUDGE_PROMPT_TEMPLATE": "judge_v0_1"},
}
templates = {}
hashes = {}
for filename, names in selected.items():
    content = (source / "mindeval" / filename).read_bytes()
    hashes[f"mindeval/{filename}"] = hashlib.sha256(content).hexdigest()
    for node in ast.parse(content).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names:
            templates[names[node.targets[0].id]] = ast.literal_eval(node.value.args[0])
assert len(templates) == 3
(destination / "templates.json").write_text(json.dumps(templates, ensure_ascii=False, indent=2) + "\n")
for filename in ("profiles.jsonl", "human_annotations.jsonl"):
    content = (source / "data" / filename).read_bytes()
    hashes[f"data/{filename}"] = hashlib.sha256(content).hexdigest()
    (destination / filename).write_bytes(content)
(destination / "provenance.json").write_text(
    json.dumps(
        {
            "repository": "https://github.com/SWORDHealth/mind-eval",
            "revision": revision,
            "sha256": hashes,
            "template_sha256": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in templates.items()},
            "paper": "https://arxiv.org/abs/2511.18491",
            "patient_template_version": "v0_2",
            "clinician_template_version": "v0_1",
            "judge_template_version": "v0_1",
        },
        indent=2,
    )
    + "\n"
)
print(json.dumps({"revision": revision, "templates": list(templates), "hashes": hashes}))
