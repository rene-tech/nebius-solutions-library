import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .contracts import GatewayError


class Store:
    """Durable registration and telemetry for the explicitly single-replica gateway."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS runs (team TEXT, id TEXT, config TEXT, snapshot TEXT, PRIMARY KEY(team,id))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "team TEXT, run_id TEXT, event TEXT)"
        )
        self.db.commit()

    def register(self, team: str, run_id: str, config: dict, snapshot: dict):
        serialized = json.dumps(config, sort_keys=True)
        row = self.db.execute("SELECT config,snapshot FROM runs WHERE team=? AND id=?", (team, run_id)).fetchone()
        if row:
            if row[0] != serialized:
                raise GatewayError("registration_conflict", "run model/profile selection is immutable", status=409)
            return json.loads(row[1])
        self.db.execute("INSERT INTO runs VALUES (?,?,?,?)", (team, run_id, serialized, json.dumps(snapshot)))
        self.db.commit()
        return snapshot

    def get(self, team: str, run_id: str):
        row = self.db.execute("SELECT config,snapshot FROM runs WHERE team=? AND id=?", (team, run_id)).fetchone()
        if not row:
            raise GatewayError("run_not_registered", "register this run before inference", status=404)
        return json.loads(row[0]), json.loads(row[1])

    def event(self, team: str, run_id: str, event: dict):
        event = {"at": datetime.now(timezone.utc).isoformat(), **event}
        self.db.execute("INSERT INTO events(team,run_id,event) VALUES (?,?,?)", (team, run_id, json.dumps(event)))
        self.db.commit()

    def events(self, team: str, run_id: str, after: int = 0):
        self.get(team, run_id)
        return [
            {"id": row[0], **json.loads(row[1])}
            for row in self.db.execute(
                "SELECT id,event FROM events WHERE team=? AND run_id=? AND id>? ORDER BY id LIMIT 1000",
                (team, run_id, after),
            )
        ]
