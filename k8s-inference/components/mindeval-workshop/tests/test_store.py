import asyncio

import pytest

from fs2_workshop.models import CreateRuns, Intervention

IDENTITY = {"tenant_id": "event", "principal_id": "team1", "token_id": "test", "max_concurrency": 5}


def request(**kwargs):
    return CreateRuns(profile_ids=["profile-000"], patient_model="patient", clinician_models=["clinician"], **kwargs)


async def create(store, identity=None, key="test", **kwargs):
    return (await store.create(identity or IDENTITY, "not-a-real-api-key", request(**kwargs), key))[0]


async def test_idempotency_is_atomic_and_owner_scoped(store):
    rows = await asyncio.gather(*(create(store) for _ in range(12)))
    assert len({r["id"] for r in rows}) == 1
    assert "credential_ciphertext" not in rows[0]
    other = await create(store, {**IDENTITY, "principal_id": "team2"})
    assert other["id"] != rows[0]["id"]
    with pytest.raises(ValueError, match="different request"):
        await create(store, max_turns=3)
    with pytest.raises(KeyError):
        await store.get(other["id"], IDENTITY)


async def test_fifty_calls_ten_teams_and_five_cap(store):
    for team in range(10):
        req = CreateRuns(profile_ids=[f"profile-{n:03d}" for n in range(6)], patient_model="p", clinician_models=["c"])
        await store.create({**IDENTITY, "principal_id": f"team{team}"}, "test", req, "batch")
    rows = await asyncio.gather(*(store.claim(str(n), 90, 5) for n in range(60)))
    active = [r for r in rows if r]
    assert len(active) == 50
    assert {sum(r["principal_id"] == f"team{n}" for r in active) for n in range(10)} == {5}
    assert await store.pool.fetchval("SELECT count(*) FROM fs2_workshop.runs WHERE status='queued'") == 10


async def test_token_lower_cap_is_honored(store):
    identity = {**IDENTITY, "max_concurrency": 1}
    await create(store, identity, "first")
    await create(store, identity, "second")
    assert await store.claim("one", 90, 5)
    assert await store.claim("two", 90, 5) is None


async def test_intervention_fences_inflight_result_and_audio(store):
    created = await create(store)
    running = await store.claim("worker", 90, 5)
    await store.intervene(created["id"], IDENTITY, Intervention(action="pause"))
    success = await store.finish_step(
        running, running["state"], "queued", "turn.completed", {"old": True}, audio=(1, b"wave", {})
    )
    assert not success
    assert (await store.get(created["id"], IDENTITY))["status"] == "paused"
    assert await store.pool.fetchval("SELECT count(*) FROM fs2_workshop.audio") == 0
    assert await store.pool.fetchval("SELECT count(*) FROM fs2_workshop.events WHERE kind='step.superseded'") == 1


async def test_stale_worker_requires_explicit_resume(store):
    row = await create(store)
    await store.claim("old", 90, 5)
    await store.pool.execute(
        "UPDATE fs2_workshop.runs SET lease_until=now()-interval '1 second' WHERE id=$1", row["id"]
    )
    assert await store.claim("new", 90, 5) is None
    assert (await store.get(row["id"], IDENTITY))["status"] == "interrupted"
    await store.intervene(row["id"], IDENTITY, Intervention(action="resume"))
    assert (await store.claim("new", 90, 5))["id"] == row["id"]


async def test_human_turn_and_recording_are_atomic(store):
    row = await create(store)
    taken = await store.intervene(row["id"], IDENTITY, Intervention(action="takeover", role="clinician"))
    said = await store.intervene(
        row["id"],
        IDENTITY,
        Intervention(action="say", role="clinician", text="Hello there"),
        version=taken["version"],
        audio=(b"wav", {"test": True}),
    )
    assert said["status"] == "queued"
    assert said["state"]["next_role"] == "patient"
    assert said["state"]["transcript"][-1]["human"]
    assert said["state"]["transcript"][-1]["audio_url"].endswith("/audio/1")
    with pytest.raises(ValueError, match="changed"):
        await store.intervene(row["id"], IDENTITY, Intervention(action="say", text="stale"), version=taken["version"])


async def test_takeover_of_other_speaker_does_not_deadlock(store):
    row = await create(store)
    taken = await store.intervene(row["id"], IDENTITY, Intervention(action="takeover", role="patient"))
    assert taken["status"] == "queued"
    assert taken["state"]["takeover_role"] == "patient"


async def test_terminal_releases_credential_and_cannot_resume(store):
    row = await create(store)
    await store.intervene(row["id"], IDENTITY, Intervention(action="abort"))
    assert (await store.get(row["id"], IDENTITY))["credential_ciphertext"] is None
    with pytest.raises(ValueError, match="terminal"):
        await store.intervene(row["id"], IDENTITY, Intervention(action="resume"))
