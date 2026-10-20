"""dispatcher.released_pod: which terminal run pods the sweep deletes (api 0.11.153)."""

import dispatcher


def _pod(phase="Succeeded", claims=("op-1-work",), owner="Job"):
    return {
        "metadata": {
            "name": "op-1-abc",
            "namespace": "tenant-demo",
            "ownerReferences": [{"kind": owner, "name": "op-1"}] if owner else [],
        },
        "spec": {"volumes": [{"name": "work", "persistentVolumeClaim": {"claimName": c}} for c in claims]},
        "status": {"phase": phase},
    }


def test_a_terminal_pod_whose_work_claim_is_gone_is_released():
    assert dispatcher.released_pod(_pod(), set()) is True
    assert dispatcher.released_pod(_pod(phase="Failed"), {"other-work"}) is True


def test_a_pod_is_kept_while_its_claim_exists_or_it_still_runs_or_has_no_claim():
    assert dispatcher.released_pod(_pod(), {"op-1-work"}) is False  # the kept volume: resume/export retry territory
    assert dispatcher.released_pod(_pod(phase="Running"), set()) is False
    assert dispatcher.released_pod(_pod(phase="Pending"), set()) is False
    assert dispatcher.released_pod(_pod(claims=()), set()) is False  # nothing attachable to pin
    assert dispatcher.released_pod(_pod(owner=None), set()) is False  # not a Job's pod
    assert dispatcher.released_pod(_pod(claims=("op-1-work", "shared")), {"shared"}) is False  # one claim still exists
