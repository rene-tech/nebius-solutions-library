from verify_resume import retained_transfer_evidence


def test_transfer_evidence_records_retry_and_discards_sensitive_helper_fields():
    item = {
        "artifact": {"artifact_id": "artifact-one"},
        "size_bytes": 42,
        "sha256": "a" * 64,
    }
    result = retained_transfer_evidence(
        item,
        {
            "publication": "published",
            "transfer_attempts": 3,
            "path": "not-for-this-report",
            "authorization": "not-for-this-report",
        },
        "verification-one",
    )
    assert result == {
        "verification_run": "verification-one",
        "artifact_id": "artifact-one",
        "size_bytes": 42,
        "sha256": "a" * 64,
        "publication": "published",
        "transfer_attempts": 3,
    }


def test_unknown_retry_count_is_not_reported_as_zero():
    item = {
        "artifact": {"artifact_id": "artifact-one"},
        "size_bytes": 42,
        "sha256": "a" * 64,
    }
    result = retained_transfer_evidence(
        item, {"publication": "verified-existing"}, "verification-two"
    )
    assert result["transfer_attempts"] is None
    assert result["publication"] == "verified-existing"
