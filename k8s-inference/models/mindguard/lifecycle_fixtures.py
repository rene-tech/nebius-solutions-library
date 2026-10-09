"""Synthetic observable transcript fixtures; no clinical-accuracy claim."""

import json
from pathlib import Path


def requests(model_id):
    return [
        {"model": model_id, "language": "en", "messages": [
            {"role": "user", "content": "My workload is stressful."},
            {"role": "assistant", "content": "How has it felt this week?"},
            {"role": "user", "content": "I want help organizing my week and feel safe."},
        ]},
        {"model": model_id, "language": "en", "messages": [
            {"role": "user", "content": "I have been struggling badly."},
            {"role": "assistant", "content": "Can you tell me how things feel right now?"},
            {"role": "user", "content": "I am afraid I might hurt myself and need immediate support."},
        ]},
    ]


def validate(result, model_id):
    lock = json.loads(Path(__file__).with_name("public-models.lock.json").read_text())
    revision = lock["models"][model_id]["revision"]
    assert result["model_id"] == model_id
    assert result["model_revision"] == revision
    assert result["role"] == "safety_classifier"
    assert result["status"] == "completed"
    assert result["enforcement"] == "observe"
    assert result["evaluated_user_turns"] == result["input_user_turns"] == 2
    assert len(result["assessments"]) == 2
    for index, row in enumerate(result["assessments"]):
        assert row["model_id"] == model_id and row["model_revision"] == revision
        assert row["enforcement"] == "observe" and row["role"] == "safety_classifier"
        assert row["status"] == "completed" and row["error"] is None
        assert row["safety"] in {"safe", "unsafe"}
        assert row["coverage"]["target_user_message_index"] == 2 * index
        assert row["coverage"]["context_message_count"] == 2 * index + 1
        assert row["coverage"]["truncated"] is False
        assert (row["safety"] == "unsafe") == bool(row["categories"])
    return {"checked_user_turns": 2, "model_revision": revision, "complete_context_coverage": True,
            "observational_only": True, "clinical_accuracy_claim": False}
