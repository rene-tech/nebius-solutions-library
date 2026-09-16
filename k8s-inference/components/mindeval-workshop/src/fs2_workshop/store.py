"""Durable, fair workshop queue shared by API/worker replicas."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from cryptography.fernet import Fernet

from .models import TERMINAL, CreateRuns, Intervention


def decoded(row):
    result = dict(row)
    result["state"] = json.loads(result["state"]) if isinstance(result["state"], str) else result["state"]
    return result


def public_run(row):
    return {k: v for k, v in row.items() if k not in {"credential_ciphertext", "lease_owner", "lease_until"}}


class Store:
    def __init__(self, pool, key: bytes):
        self.pool = pool
        self.cipher = Fernet(key)

    async def event(self, connection, run_id, kind, data):
        await connection.execute(
            "INSERT INTO fs2_workshop.events(run_id,kind,data) VALUES($1,$2,$3::jsonb)", run_id, kind, json.dumps(data)
        )

    async def create(self, identity: dict, token: str, request: CreateRuns, idempotency_key: str):
        body = request.model_dump()
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        tenant, principal = identity["tenant_id"], identity["principal_id"]
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"workshop:{tenant}:{principal}")
            prior = await c.fetchrow(
                "SELECT * FROM fs2_workshop.batches WHERE tenant_id=$1 AND principal_id=$2 AND idempotency_key=$3",
                tenant,
                principal,
                idempotency_key,
            )
            if prior:
                if prior["request_sha256"] != digest:
                    raise ValueError("idempotency key was used for a different request")
                rows = await c.fetch(
                    "SELECT * FROM fs2_workshop.runs WHERE batch_id=$1 ORDER BY created_at,id", prior["id"]
                )
                return [public_run(decoded(r)) for r in rows]
            count = await c.fetchval(
                "SELECT count(*) FROM fs2_workshop.runs WHERE tenant_id=$1 AND principal_id=$2 "
                "AND status NOT IN ('completed','failed','aborted')",
                tenant,
                principal,
            )
            if count + len(request.profile_ids) * len(request.clinician_models) > 160:
                raise OverflowError("team has 160 active or queued runs; wait or abort an existing batch")
            batch_id, rows = uuid4(), []
            await c.execute(
                "INSERT INTO fs2_workshop.batches(id,tenant_id,principal_id,idempotency_key,request_sha256) "
                "VALUES($1,$2,$3,$4,$5)",
                batch_id,
                tenant,
                principal,
                idempotency_key,
                digest,
            )
            for profile in request.profile_ids:
                for clinician in request.clinician_models:
                    run_id = uuid4()
                    state = {
                        "worker_limit": min(5, identity.get("max_concurrency", 1)),
                        "config": {**body, "profile_id": profile, "clinician_model": clinician},
                        "transcript": [
                            {"role": "patient", "content": "Hello", "seed": True, "source": "upstream-mindeval-v1"}
                        ],
                        "interventions": [],
                        "pending_nudges": [],
                        "intervened": False,
                        "next_role": "clinician",
                        "profile": None,
                        "registration": None,
                        "judgment": None,
                        "error": None,
                        "classification": None,
                        "audio": [],
                    }
                    row = await c.fetchrow(
                        "INSERT INTO fs2_workshop.runs "
                        "(id,batch_id,tenant_id,principal_id,token_id,credential_ciphertext,status,state) "
                        "VALUES($1,$2,$3,$4,$5,$6,'queued',$7::jsonb) RETURNING *",
                        run_id,
                        batch_id,
                        tenant,
                        principal,
                        identity["token_id"],
                        self.cipher.encrypt(token.encode()),
                        json.dumps(state),
                    )
                    await self.event(c, run_id, "run.created", {"config": state["config"]})
                    rows.append(public_run(decoded(row)))
            return rows

    async def get(self, run_id: UUID, identity: dict):
        row = await self.pool.fetchrow(
            "SELECT * FROM fs2_workshop.runs WHERE id=$1 AND tenant_id=$2 AND principal_id=$3",
            run_id,
            identity["tenant_id"],
            identity["principal_id"],
        )
        if not row:
            raise KeyError("run not found")
        return decoded(row)

    async def list(self, identity):
        rows = await self.pool.fetch(
            "SELECT * FROM fs2_workshop.runs WHERE tenant_id=$1 AND principal_id=$2 ORDER BY created_at DESC LIMIT 200",
            identity["tenant_id"],
            identity["principal_id"],
        )
        return [public_run(decoded(r)) for r in rows]

    async def claim(self, owner: str, lease: int, max_team_workers: int):
        async with self.pool.acquire() as c, c.transaction():
            # Serialize only short admission transactions, never inference.
            await c.execute("SELECT pg_advisory_xact_lock(731684932)")
            stale = await c.fetch(
                "UPDATE fs2_workshop.runs SET status='interrupted',lease_owner=NULL,lease_until=NULL,"
                "version=version+1,updated_at=now() WHERE status='running' AND lease_until<now() RETURNING id"
            )
            for row in stale:
                await self.event(
                    c, row["id"], "run.interrupted", {"reason": "worker lost; explicit resume prevents hidden replay"}
                )
            row = await c.fetchrow(
                "SELECT r.* FROM fs2_workshop.runs r WHERE r.status='queued' AND "
                "(SELECT count(*) FROM fs2_workshop.runs a WHERE a.tenant_id=r.tenant_id "
                "AND a.principal_id=r.principal_id AND a.status='running')"
                "<LEAST($1,COALESCE((r.state->>'worker_limit')::int,1)) "
                "ORDER BY (SELECT max(a.updated_at) FROM fs2_workshop.runs a "
                "WHERE a.tenant_id=r.tenant_id AND a.principal_id=r.principal_id AND a.status='running') "
                "NULLS FIRST,r.updated_at FOR UPDATE SKIP LOCKED LIMIT 1",
                max_team_workers,
            )
            if not row:
                return None
            row = await c.fetchrow(
                "UPDATE fs2_workshop.runs SET status='running',lease_owner=$2,"
                "lease_until=now()+$3*interval '1 second',updated_at=now() WHERE id=$1 RETURNING *",
                row["id"],
                owner,
                lease,
            )
            await self.event(c, row["id"], "run.dispatched", {"worker": owner})
            return decoded(row)

    async def heartbeat(self, run_id, owner, lease):
        await self.pool.execute(
            "UPDATE fs2_workshop.runs SET lease_until=now()+$3*interval '1 second' "
            "WHERE id=$1 AND lease_owner=$2 AND status='running'",
            run_id,
            owner,
            lease,
        )

    async def finish_step(self, row, state: dict, status: str, kind: str, event: dict, *, audio=None):
        async with self.pool.acquire() as c, c.transaction():
            result = await c.fetchrow(
                "UPDATE fs2_workshop.runs SET state=$4::jsonb,status=$5,version=version+1,"
                "lease_owner=NULL,lease_until=NULL,updated_at=now(),"
                "credential_ciphertext=CASE WHEN $5 IN ('completed','failed','aborted') THEN NULL "
                "ELSE credential_ciphertext END WHERE id=$1 AND version=$2 AND lease_owner=$3 "
                "AND status='running' RETURNING id",
                row["id"],
                row["version"],
                row["lease_owner"],
                json.dumps(state),
                status,
            )
            await self.event(c, row["id"], kind if result else "step.superseded", event)
            if result and audio:
                await c.execute(
                    "INSERT INTO fs2_workshop.audio(run_id,turn_index,wav,metadata) VALUES($1,$2,$3,$4::jsonb)",
                    row["id"],
                    audio[0],
                    audio[1],
                    json.dumps(audio[2]),
                )
            return result is not None

    async def intervene(self, run_id, identity, command: Intervention, *, version=None, audio=None):
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow(
                "SELECT * FROM fs2_workshop.runs WHERE id=$1 AND tenant_id=$2 AND principal_id=$3 FOR UPDATE",
                run_id,
                identity["tenant_id"],
                identity["principal_id"],
            )
            if not row:
                raise KeyError("run not found")
            if row["status"] in TERMINAL:
                raise ValueError("run is terminal; create a new run to change the conversation")
            if version is not None and row["version"] != version:
                raise ValueError("run changed during recording; your message was not submitted")
            state = decoded(row)["state"]
            status = row["status"]
            data = {**command.model_dump(), "at": (await c.fetchval("SELECT now()")).isoformat()}
            if command.action == "pause":
                status = "paused"
            elif command.action == "abort":
                status = "aborted"
            elif command.action == "takeover":
                state["takeover_role"] = command.role
                status = "takeover" if state["next_role"] == command.role else "queued"
            elif command.action == "resume":
                if status == "running":
                    raise ValueError("run is already running")
                status = "queued"
                state.pop("takeover_role", None)
            elif command.action == "say":
                if status != "takeover" or state.get("takeover_role") != command.role:
                    raise ValueError("take over the selected role before sending a message")
                if state["next_role"] != command.role:
                    raise ValueError("wait for the other role to reply")
                state["transcript"].append(
                    {
                        "role": command.role,
                        "content": command.text,
                        "source": command.source,
                        "at": data["at"],
                        "human": True,
                    }
                )
                if audio:
                    index = len(state["transcript"]) - 1
                    state["transcript"][-1]["audio_url"] = f"/v1/workshop/runs/{run_id}/audio/{index}"
                    await c.execute(
                        "INSERT INTO fs2_workshop.audio(run_id,turn_index,wav,metadata) VALUES($1,$2,$3,$4::jsonb)",
                        run_id,
                        index,
                        audio[0],
                        json.dumps(audio[1]),
                    )
                state["next_role"] = "patient" if command.role == "clinician" else "clinician"
                status = "queued"
            elif command.action == "nudge":
                state["pending_nudges"].append(data)
                status = "queued" if status == "running" else status
            state["intervened"] = True
            state["interventions"].append(data)
            updated = await c.fetchrow(
                "UPDATE fs2_workshop.runs SET state=$2::jsonb,status=$3,version=version+1,"
                "lease_owner=NULL,lease_until=NULL,updated_at=now(),"
                "credential_ciphertext=CASE WHEN $3='aborted' THEN NULL ELSE credential_ciphertext END "
                "WHERE id=$1 RETURNING *",
                run_id,
                json.dumps(state),
                status,
            )
            await self.event(c, run_id, "intervention." + command.action, data)
            return public_run(decoded(updated))


async def migrate():
    connection = await asyncpg.connect(os.environ["WORKSHOP_DATABASE_URL"])
    try:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(731684933)")
            await connection.execute(Path(__file__).with_name("schema.sql").read_text())
    finally:
        await connection.close()


def migrate_main():
    asyncio.run(migrate())
