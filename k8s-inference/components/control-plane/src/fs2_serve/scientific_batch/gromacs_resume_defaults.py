"""Apply recorded, input/runtime-qualified defaults to native continuation.

Profiles are content-addressed, not customer-specific. Unknown inputs, runtimes,
MPI, biasing and explicit performance choices keep their original settings.
The immutable TPR/checkpoint and all scientific output files remain untouched.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..models import StrictModel

PerformanceMode = Literal["auto", "preserve"]
PERFORMANCE_FLAGS = frozenset({"-nb", "-bonded", "-pme", "-pmefft", "-update", "-pin", "-nstlist", "-reprod"})


class ResumeProfile(StrictModel):
    id: str
    runtime_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tpr_sha256: list[str]
    threads: int = Field(ge=1, le=256)
    mdrun_defaults: dict[str, str]
    evidence: str


class ResumeProfiles(StrictModel):
    profiles: list[ResumeProfile]


def load_profiles() -> tuple[ResumeProfile, ...]:
    document = ResumeProfiles.model_validate_json(Path(__file__).with_name("gromacs_resume_profiles.json").read_bytes())
    for profile in document.profiles:
        if set(profile.mdrun_defaults) != {"-nb", "-bonded", "-pme", "-update", "-pin", "-nstlist"}:
            raise ValueError("resume defaults may contain only the six qualified execution flags")
    return tuple(document.profiles)


def apply_resume_defaults(
    parameters: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    model_id: str,
    runtime_image_digest: str,
    performance_mode: PerformanceMode = "auto",
    profiles: tuple[ResumeProfile, ...] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return new parameters and an immutable explanation for the lineage.

    No inference about arbitrary scientific inputs: selection requires an exact
    qualified TPR, worker and CPU shape. A single explicit execution flag opts the
    whole step out, avoiding untested mixtures of customer and platform settings.
    """
    value = copy.deepcopy(parameters)
    receipt: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/gromacs-resume-adjustments/v1",
        "performance_mode": performance_mode,
        "profile_id": None,
        "performance_reason": "no_matching_qualified_profile",
        "runtime_image_digest": runtime_image_digest,
        "mdrun_defaults_added": {},
        "analysis_selectors": [],
    }
    active = checkpoint["state"].get("active_step")
    step = value["jobs"][0]["steps"][0]
    if performance_mode == "preserve":
        receipt["performance_reason"] = "caller_requested_preservation"
    elif model_id != "gromacs" or not active or step["command"] != "mdrun":
        receipt["performance_reason"] = "not_single_gpu_active_simulation"
    elif "plumed_input" in step or any(
        isinstance(token, str) and token.split("=", 1)[0] in PERFORMANCE_FLAGS for token in step["args"]
    ):
        receipt["performance_reason"] = "explicit_customer_execution_settings"
    else:
        matches = [
            profile
            for profile in (load_profiles() if profiles is None else profiles)
            if active["tpr_sha256"] in profile.tpr_sha256
            and profile.runtime_image_digest == runtime_image_digest
            and profile.threads == value["threads"]
        ]
        if len(matches) > 1:
            raise ValueError("ambiguous qualified GROMACS resume defaults")
        if matches:
            profile = matches[0]
            for flag, setting in profile.mdrun_defaults.items():
                step["args"].extend([flag, setting])
            receipt.update(
                profile_id=profile.id,
                performance_reason="qualified_defaults_applied",
                mdrun_defaults_added=dict(profile.mdrun_defaults),
            )

    # Empty closed segments are valid with sparse trajectory output. Native
    # trjcat/eneconv cannot read them. Omitted selector choices become nonempty;
    # explicit true/false and literal filenames remain under customer control.
    # Pattern expansion still occurs after MD, including newly produced parts.
    for job in value["jobs"]:
        for command in job["steps"]:
            if command["command"] not in {"trjcat", "eneconv"}:
                continue
            file_input = False
            for index, argument in enumerate(command["args"]):
                if isinstance(argument, str) and argument.startswith("-"):
                    file_input = argument == "-f"
                elif file_input and isinstance(argument, dict) and "nonempty" not in argument:
                    argument["nonempty"] = True
                    receipt["analysis_selectors"].append(
                        {
                            "job_id": job["id"],
                            "step_id": command["id"],
                            "argument_index": index,
                            "nonempty": True,
                        }
                    )
    return value, receipt
