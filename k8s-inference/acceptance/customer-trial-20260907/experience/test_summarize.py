import json

import pytest

from summarize import build, statistics_for


def sample(ordinal, started, completed, *, status='passed', phase='during'):
    return {'ordinal': ordinal, 'started_at': started, 'completed_at': completed,
            'status': status, 'phase': phase, 'surface': 'http' if ordinal % 2 else 'mcp',
            'client_seconds': 2, 'submission_attempts': 1}


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def test_failures_remain_in_latency_counts_and_small_samples_have_no_p95():
    rows = [{'client_seconds': 1, 'status': 'passed'}, {'client_seconds': 30, 'status': 'failed'}]
    summary = statistics_for(rows)
    assert summary['samples'] == 2
    assert summary['passed'] == summary['failed'] == 1
    assert summary['median_seconds'] == 15.5
    assert summary['max_seconds'] == 30
    assert summary['p95_seconds'] is None


def test_empirical_percentile_requires_twenty_samples():
    summary = statistics_for([{'client_seconds': number, 'status': 'passed'} for number in range(1, 21)])
    assert summary['p95_seconds'] == pytest.approx(19.05)


def test_handoff_keeps_exact_cadence_and_both_episodes(tmp_path):
    write_rows(tmp_path / 'interactive.jsonl', [sample(1, '2026-09-07T10:00:00Z', '2026-09-07T10:00:02Z', phase='baseline')])
    write_rows(tmp_path / 'r02/interactive.jsonl', [sample(2, '2026-09-07T10:00:25Z', '2026-09-07T10:00:27Z')])
    result = build(tmp_path)
    assert result['total']['samples'] == 2
    assert result['handoff']['start_to_start_seconds'] == 25
    assert result['handoff']['idle_between_completed_and_next_start_seconds'] == 23
    assert result['by_phase']['baseline']['samples'] == 1
    assert result['by_phase']['during']['samples'] == 1
    assert result['by_surface']['http']['samples'] == 1
    assert result['by_surface']['mcp']['samples'] == 1


def test_success_only_latency_is_separate_without_dropping_failures(tmp_path):
    first = sample(1, '2026-09-07T10:00:00Z', '2026-09-07T10:00:02Z')
    failed = sample(2, '2026-09-07T10:00:25Z', '2026-09-07T10:00:55Z', status='failed')
    failed['client_seconds'] = 30
    write_rows(tmp_path / 'interactive.jsonl', [first, failed])
    result = build(tmp_path)
    assert result['total']['failed'] == 1
    assert result['total']['max_seconds'] == 30
    assert result['by_surface']['mcp']['failed'] == 1
    assert result['successful_response_latency']['samples'] == 1
    assert result['successful_response_latency']['max_seconds'] == 2
    assert len(result['requests']) == 2


@pytest.mark.parametrize('ordinals', [[1, 1], [1, 3]])
def test_missing_or_repeated_sample_is_not_silently_ignored(tmp_path, ordinals):
    write_rows(tmp_path / 'interactive.jsonl', [sample(index, '2026-09-07T10:00:00Z', '2026-09-07T10:00:02Z') for index in ordinals])
    with pytest.raises(ValueError, match='sequence'):
        build(tmp_path)


def test_active_sampler_cannot_be_reported_complete(tmp_path):
    write_rows(tmp_path / 'interactive.jsonl', [sample(1, '2026-09-07T10:00:00Z', '2026-09-07T10:00:02Z')])
    with pytest.raises(ValueError, match='Sampler still active'):
        build(tmp_path, complete=True)


def test_harness_404_is_preserved_separately_from_inference_success(tmp_path):
    write_rows(tmp_path / 'interactive.jsonl', [sample(1, '2026-09-07T10:00:00Z', '2026-09-07T10:00:02Z')])
    write_rows(tmp_path / 'discovery.jsonl', [{'checks': [{'path': '/healthz', 'http_status': 404, 'passed': False}]}])
    result = build(tmp_path)
    assert result['total']['failed'] == 0
    assert result['discovery_checks']['/healthz']['http_status_counts']['404'] == 1
    assert any('healthz' in note for note in result['harness_caveats'])
