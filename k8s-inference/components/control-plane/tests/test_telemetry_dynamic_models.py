from __future__ import annotations

from prometheus_client.parser import text_string_to_metric_families

from fs2_serve.telemetry import Metrics


def _queue_samples(metrics: Metrics) -> dict[tuple[str, str], float]:
    rendered = metrics.render().decode()
    return {
        (sample.labels["model"], sample.labels["state"]): sample.value
        for family in text_string_to_metric_families(rendered)
        if family.name == "fs2_serve_operations"
        for sample in family.samples
    }


def test_queue_projection_activates_and_clears_a_live_added_model() -> None:
    metrics = Metrics([])

    metrics.set_queue({("live-added-model", "queued"): 3})
    assert _queue_samples(metrics)[("live-added-model", "queued")] == 3

    metrics.set_queue({})
    samples = _queue_samples(metrics)
    assert samples[("live-added-model", "queued")] == 0
    assert samples[("live-added-model", "activating")] == 0
    assert samples[("live-added-model", "running")] == 0
