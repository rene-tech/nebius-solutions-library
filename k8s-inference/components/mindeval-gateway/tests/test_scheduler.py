import asyncio
from collections import Counter

from fs2_mindeval.scheduler import FairScheduler


async def test_ten_teams_no_starvation_and_five_workers():
    scheduler = FairScheduler(global_concurrency=10)
    active, peaks, order = Counter(), Counter(), []

    async def call(team):
        active[team] += 1
        peaks[team] = max(peaks[team], active[team])
        order.append(team)
        await asyncio.sleep(0.003)
        active[team] -= 1
        return {"usage": {"total_tokens": 1}}

    tasks = [
        asyncio.create_task(
            scheduler.submit(team=str(team), model="judge", tokens=10, limit=5, call=lambda team=team: call(str(team)))
        )
        for team in range(10)
        for _ in range(20)
    ]
    results = await asyncio.gather(*tasks)
    await scheduler.close()
    assert Counter(order) == {str(team): 20 for team in range(10)}
    assert len(set(order[:10])) == 10
    assert max(peaks.values()) <= 5
    assert max(queued for _, queued in results) > 0


async def test_team_limit_applies_across_models():
    scheduler = FairScheduler(global_concurrency=50)
    active, peak = 0, 0

    async def call():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return {}

    await asyncio.gather(
        *(scheduler.submit(team="a", model=str(i % 3), tokens=1, limit=5, call=call) for i in range(30))
    )
    assert peak == 5
    await scheduler.close()


async def test_global_model_rpm_and_tpm_shared_between_teams():
    scheduler = FairScheduler(window=0.035, default_rpm=2, default_tpm=20)

    async def call():
        return {"usage": {"total_tokens": 10}}

    results = await asyncio.gather(
        *(scheduler.submit(team=str(i), model="m", tokens=10, limit=5, call=call) for i in range(6))
    )
    assert results[2][1] >= 25
    assert results[4][1] >= 60
    await scheduler.close()


async def test_cancelled_queued_work_never_consumes_provider_call():
    scheduler = FairScheduler(global_concurrency=1)
    gate, started, calls = asyncio.Event(), asyncio.Event(), []

    async def call():
        calls.append(1)
        started.set()
        await gate.wait()

    first = asyncio.create_task(scheduler.submit(team="a", model="m", tokens=1, limit=1, call=call))
    await started.wait()
    second = asyncio.create_task(scheduler.submit(team="b", model="m", tokens=1, limit=1, call=call))
    await asyncio.sleep(0)
    second.cancel()
    gate.set()
    await asyncio.gather(first, second, return_exceptions=True)
    await scheduler.close()
    assert len(calls) == 1
