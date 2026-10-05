from verify_historical_metrics import judge


def row(sampled_at=1, age=2, available=1, history=3):
    return {
        "pod": "reader",
        "status": "observed",
        "freshness": {
            "available": available,
            "age_seconds": age,
            "sampled_timestamp_seconds": sampled_at,
        },
        "series_counts": {"queues": 5, "queue_ages": 1, "history": history},
    }


def test_requires_recurrent_history_and_fresh_queues_not_just_successful_scrape():
    samples = [{"readers": [row()]}, {"readers": [row(sampled_at=31)]}]
    assert judge(samples, ["reader"])["status"] == "passed"
    assert judge(samples[:1], ["reader"])["status"] == "failed"
    samples[1]["readers"][0]["series_counts"]["queues"] = 0
    assert judge(samples, ["reader"])["status"] == "failed"


def test_unavailable_is_explicit_and_never_fabricates_zero_or_stale_success():
    samples = [
        {"readers": [row()]},
        {"readers": [row(sampled_at=31)]},
        {"readers": [row(age=35, available=0, history=0)]},
    ]
    assert judge(samples, ["reader"])["status"] == "passed"
    samples[-1]["readers"][0]["freshness"]["available"] = 1
    assert judge(samples, ["reader"])["status"] == "failed"
    samples[-1]["readers"] = [{"pod": "reader", "status": "unavailable"}]
    assert judge(samples, ["reader"])["status"] == "failed"
