import copy

import pytest
from test_gromacs_resume import fixture, original

from fs2_serve.scientific_batch.gromacs_resume import continuation_parameters
from fs2_serve.scientific_batch.gromacs_resume_defaults import apply_resume_defaults, load_profiles


def prepared():
    _, checkpoint = fixture()
    profile = load_profiles()[0]
    checkpoint["state"]["active_step"]["tpr_sha256"] = profile.tpr_sha256[0]
    checkpoint["files"][0]["sha256"] = profile.tpr_sha256[0]
    parameters = original()
    parameters["threads"] = 8
    parameters = continuation_parameters(parameters, checkpoint, model_id="gromacs", max_wall_seconds=1209600)
    return parameters, checkpoint, profile


def test_qualified_defaults_preserve_science_and_history_and_record_changes():
    parameters, checkpoint, profile = prepared()
    before = copy.deepcopy((parameters, checkpoint))
    result, receipt = apply_resume_defaults(
        parameters, checkpoint, model_id="gromacs", runtime_image_digest=profile.runtime_image_digest
    )
    assert (parameters, checkpoint) == before
    assert result["threads"] == 8
    active = result["jobs"][0]["steps"][0]
    assert active["args"][:4] == ["-s", "run.tpr", "-deffnm", "md"]
    assert dict(zip(active["args"][4::2], active["args"][5::2], strict=True)) == profile.mdrun_defaults
    assert active["restart_checkpoint"] == "fs2-production.cpt"
    assert receipt["profile_id"] == profile.id
    assert receipt["performance_reason"] == "qualified_defaults_applied"
    assert len(receipt["analysis_selectors"]) == 1
    assert result["jobs"][0]["steps"][1]["args"][1] == {"files": "md.part*.xtc", "nonempty": True}


@pytest.mark.parametrize(
    "flag,value",
    [
        ("-nb", "cpu"),
        ("-nb", "auto"),
        ("-bonded", "gpu"),
        ("-bonded", "cpu"),
        ("-pme", "cpu"),
        ("-pmefft", "cpu"),
        ("-pin", "on"),
        ("-nstlist", "100"),
        ("-update", "cpu"),
    ],
)
def test_any_explicit_execution_choice_is_preserved_without_mixing_profiles(flag, value):
    parameters, checkpoint, profile = prepared()
    parameters["jobs"][0]["steps"][0]["args"].extend([flag, value])
    result, receipt = apply_resume_defaults(
        parameters, checkpoint, model_id="gromacs", runtime_image_digest=profile.runtime_image_digest
    )
    assert result["jobs"][0]["steps"][0] == parameters["jobs"][0]["steps"][0]
    assert receipt["profile_id"] is None
    assert receipt["performance_reason"] == "explicit_customer_execution_settings"


@pytest.mark.parametrize("mismatch", ["input", "runtime", "threads", "mpi", "preserve", "plumed", "complete"])
def test_unqualified_contexts_never_receive_tuning(mismatch):
    parameters, checkpoint, profile = prepared()
    arguments = dict(model_id="gromacs", runtime_image_digest=profile.runtime_image_digest)
    if mismatch == "input":
        checkpoint["state"]["active_step"]["tpr_sha256"] = "f" * 64
    elif mismatch == "runtime":
        arguments["runtime_image_digest"] = "sha256:" + "0" * 64
    elif mismatch == "threads":
        parameters["threads"] = 4
    elif mismatch == "mpi":
        arguments["model_id"] = "gromacs-mpi"
    elif mismatch == "preserve":
        arguments["performance_mode"] = "preserve"
    elif mismatch == "plumed":
        parameters["jobs"][0]["steps"][0]["plumed_input"] = "bias.dat"
    else:
        checkpoint["state"]["active_step"] = None
    result, receipt = apply_resume_defaults(parameters, checkpoint, **arguments)
    assert result["jobs"][0]["steps"][0] == parameters["jobs"][0]["steps"][0]
    assert receipt["profile_id"] is None


@pytest.mark.parametrize("explicit", [False, True])
def test_explicit_empty_file_selection_and_other_commands_are_preserved(explicit):
    parameters, checkpoint, profile = prepared()
    selector = parameters["jobs"][0]["steps"][1]["args"][1]
    selector["nonempty"] = explicit
    parameters["jobs"][0]["steps"].append(
        {
            "id": "check",
            "command": "check",
            "args": ["-f", {"files": "md.part*.xtc"}],
        }
    )
    result, receipt = apply_resume_defaults(
        parameters, checkpoint, model_id="gromacs", runtime_image_digest=profile.runtime_image_digest
    )
    assert result["jobs"][0]["steps"][1:] == parameters["jobs"][0]["steps"][1:]
    assert not receipt["analysis_selectors"]


def test_repeated_adaptation_does_not_duplicate_flags_or_analysis_changes():
    parameters, checkpoint, profile = prepared()
    arguments = dict(model_id="gromacs", runtime_image_digest=profile.runtime_image_digest)
    first, _ = apply_resume_defaults(parameters, checkpoint, **arguments)
    second, receipt = apply_resume_defaults(first, checkpoint, **arguments)
    assert first == second
    assert not receipt["mdrun_defaults_added"]
    assert not receipt["analysis_selectors"]
