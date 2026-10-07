"""Qualification of the observation helper, never a call using a customer key."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "customer_recovery",
    Path(__file__).resolve().parent.parent
    / "gromacs-managed-resume-20261006/continue_customer.py",
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def test_second_resume_preserves_qualified_flags_instead_of_reapplying_them():
    adjustment = {
        "profile_id": None,
        "performance_reason": "explicit_customer_execution_settings",
    }
    command = [
        "gmx",
        "mdrun",
        "-nb",
        "gpu",
        "-bonded",
        "gpu",
        "-pme",
        "auto",
        "-update",
        "auto",
        "-pin",
        "auto",
        "-nstlist",
        "200",
    ]
    checkpoint = {"state": {"commands": [{"command": command}]}}
    recovery.verify_tuning(adjustment, checkpoint)
    command[-1] = "100"
    with pytest.raises(ValueError, match="not the qualified"):
        recovery.verify_tuning(adjustment, checkpoint)


def test_unknown_profile_is_not_mislabeled_as_optimized():
    with pytest.raises(ValueError, match="neither applied nor preserved"):
        recovery.verify_tuning(
            {"profile_id": None, "performance_reason": "no_matching_qualified_profile"},
            {},
        )
    recovery.verify_tuning({"profile_id": "single-gpu-list200-v1"}, {})
