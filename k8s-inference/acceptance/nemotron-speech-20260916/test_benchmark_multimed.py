from benchmark_multimed import aggregate
from score_medical import alignment, words


def test_german_normalization_preserves_medical_negation_and_compounds():
    assert words("Keine Überempfindlichkeit, Fuß-Schmerzen!") == ["keine", "überempfindlichkeit", "fuß", "schmerzen"]
    assert alignment("keine Allergie gegen Penicillin", "Allergie gegen Penicillin")["wer"] == 0.25
    assert alignment("Blutdruck", "Blut Druck")["wer"] > 0


def test_failed_requests_are_scored_as_deletions_not_dropped():
    rows = [
        {"http_status": 200, "wall_seconds": 2, "quality": alignment("keine Allergie", "keine Allergie"),
         "result": {"text": "keine Allergie", "audio_seconds": 10, "processing_seconds": 1}},
        {"http_status": 503, "wall_seconds": 1, "quality": alignment("kein Fieber", "")},
    ]
    summary = aggregate(rows)
    assert summary["requests"] == 2 and summary["failed_or_empty"] == 1
    assert summary["reference_words"] == 4 and summary["wer"] == 0.5
    assert summary["summed_http_seconds"] == 3
    assert summary["processing_real_time_factor"] == 0.1


def test_empty_summary_is_not_perfect_accuracy():
    assert aggregate([])["wer"] is None


def test_punctuation_only_reference_is_retained_but_not_ground_truth():
    summary = aggregate([{"http_status": 200, "wall_seconds": 1, "quality": None,
                          "result": {"text": "ein Wort", "audio_seconds": 6, "processing_seconds": 0.5}}])
    assert summary["requests"] == 1 and summary["unscorable_references"] == 1
    assert summary["wer"] is None
