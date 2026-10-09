import json
from importlib.resources import files
from string import Template

from . import UPSTREAM_REVISION

ASSETS = files("fs2_mindeval") / "assets"
TEMPLATES = json.loads((ASSETS / "templates.json").read_text())
PROVENANCE = json.loads((ASSETS / "provenance.json").read_text())
PROFILES = [json.loads(line) for line in (ASSETS / "profiles.jsonl").read_text().splitlines()]
CRITERIA = (
    "Clinical Accuracy & Competence",
    "Ethical & Professional Conduct",
    "Assessment & Response",
    "Therapeutic Relationship & Alliance",
    "AI-Specific Communication Quality",
)


def profile_detail(profile_id: str) -> dict:
    if not profile_id.startswith("profile-") or not profile_id[8:].isdigit():
        raise ValueError("unknown profile")
    index = int(profile_id[8:])
    if index >= len(PROFILES) or profile_id != f"profile-{index:03d}":
        raise ValueError("unknown profile")
    record = PROFILES[index]
    profile = {**record["member_attributes"], "member_narrative": record["member_narrative"]}
    return {
        "id": profile_id,
        "profile": profile,
        "upstream_revision": UPSTREAM_REVISION,
        "patient_system_prompt": Template(TEMPLATES["patient_v0_2"]).substitute(profile),
        "clinician_system_prompt": Template(TEMPLATES["clinician_v0_1"]).substitute(profile),
        "patient_template_version": "v0_2",
        "clinician_template_version": "v0_1",
    }


def profile_summaries() -> list[dict]:
    fields = ("name", "age", "depressive_symptoms", "anxious_symptoms")
    return [
        {"id": f"profile-{i:03d}", **{k: item["member_attributes"][k] for k in fields}}
        for i, item in enumerate(PROFILES)
    ]


def judge_messages(profile: dict, interaction: list[dict]) -> list[dict]:
    turns = []
    for message in interaction:
        role = "member" if message["role"] == "user" else "therapist"
        turns.append(f"<{role}>\n{message['content']}\n</{role}>")
    prompt = Template(TEMPLATES["judge_v0_1"]).substitute(conversation_str="\n".join(turns), **profile)
    return [{"role": "user", "content": prompt}]
