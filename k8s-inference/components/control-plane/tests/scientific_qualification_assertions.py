"""Assert provenance for active profiles, including withdrawn stale acceptance."""

from __future__ import annotations

import hashlib
import json
import re

from conftest import SOLUTION_ROOT


def assert_active_qualification_history(profile: dict, semantic_qualified_at: str) -> None:
    qualification = profile["qualification"]
    assert profile["state"] == "active"
    assert qualification["public_completion_receipt_sha256"] is None
    assert qualification["scheduler_eligibility_receipt_sha256"] is None
    notes = [
        note
        for note in profile["policy"]["limitations"]
        if note.startswith("Historical acceptance retained unchanged:")
    ]
    if not notes:
        assert qualification["qualified_at"] == semantic_qualified_at
        return
    assert len(notes) == 1
    match = re.search(r"completion=([a-f0-9]{64}), scheduler=([a-f0-9]{64})\.", notes[0])
    assert match is not None
    completion_digest, scheduler_digest = match.groups()
    paths = list(
        SOLUTION_ROOT.glob(f"models/**/activation/qualification/scheduler-eligibility-{scheduler_digest}.json")
    )
    assert len(paths) == 1
    raw = paths[0].read_bytes()
    assert hashlib.sha256(raw).hexdigest() == scheduler_digest
    historical = json.loads(raw)
    assert historical["model_id"] == profile["model_id"]
    assert historical["public_completion_receipt_sha256"] == completion_digest
    # This date describes the previous successful run, not a new acceptance.
    assert qualification["qualified_at"] == historical["qualified_at"]
    assert (
        historical["execution_identity_sha256"] != profile["execution_identity"]["execution_identity_sha256"]
        or historical["execution_map_sha256"] != qualification["execution_map_sha256"]
    )
