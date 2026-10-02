from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from fs2_serve.workbench_executor import WorkbenchExecutor
from fs2_serve.workbench_models import WorkbenchBinding
from fs2_serve.workbench_repository import MemoryWorkbenchRepository


def source(endpoint="aiendpoint-source"):
    return SimpleNamespace(
        metadata=SimpleNamespace(id=endpoint, parent_id="project-test", name="customer"),
        status=SimpleNamespace(state="RUNNING", public_endpoints=["https://customer.example.test"]),
        spec=SimpleNamespace(
            image="registry/lc@sha256:old",
            platform="cpu-d3",
            preset="4vcpu-16gb",
            volumes=[
                SimpleNamespace(source="computefilesystem-test", container_path="/data"),
                SimpleNamespace(source="s3://customer", container_path="/workspace"),
            ],
            environment_variables=[SimpleNamespace(name="SCIENTIFIC_REQUIRE_PERSISTENT_STATE", value="true")],
        ),
    )


class Connection:
    def __init__(self):
        self.writes = []

    async def execute(self, query, *values):
        self.writes.append((query, values))

    @asynccontextmanager
    async def transaction(self):
        yield


class Cloud:
    def __init__(self, fail_ready=False):
        self.calls = []
        self.fail_ready = fail_ready

    async def get(self, endpoint):
        self.calls.append(("get", endpoint))
        return source(endpoint)

    async def stop(self, endpoint):
        self.calls.append(("stop", endpoint))

    async def successor(self, original, name, image, operation_id, rollback=False):
        self.calls.append(("successor", name, rollback))
        return "aiendpoint-recovery" if rollback else "aiendpoint-new"

    async def ready(self, endpoint):
        self.calls.append(("ready", endpoint))
        if self.fail_ready and endpoint == "aiendpoint-new":
            raise TimeoutError("private-provider-detail-must-not-leak")
        return SimpleNamespace(url="https://new.example.test")


async def setup(protected=False):
    repo = MemoryWorkbenchRepository()
    value = WorkbenchBinding(
        id=uuid4(),
        tenant_id="customer",
        principal_ids=["customer"],
        name="Customer",
        endpoint_id="aiendpoint-source",
        project_id="project-test",
        state_filesystem_id="computefilesystem-test",
        protected=protected,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    await repo.register(value)
    return repo, {
        "id": uuid4(),
        "workbench_id": value.id,
        "kind": "upgrade",
        "progress": {},
        "specification": {"target_release": "candidate"},
    }


@pytest.mark.asyncio
async def test_upgrade_stops_single_writer_before_successor_and_retains_resources():
    repo, row = await setup()
    cloud, connection = Cloud(), Connection()
    await WorkbenchExecutor(repo, cloud, {"candidate": "image"}, set()).execute(connection, row)
    actions = [value[0] for value in cloud.calls]
    assert actions == ["get", "stop", "successor", "ready"]
    assert connection.writes[-1][1][1] == "succeeded"
    assert '"predecessor_retained": true' in connection.writes[-1][1][2]


@pytest.mark.asyncio
async def test_failed_successor_is_stopped_before_rollback_and_error_is_payload_free():
    repo, row = await setup()
    cloud, connection = Cloud(fail_ready=True), Connection()
    await WorkbenchExecutor(repo, cloud, {"candidate": "image"}, set()).execute(connection, row)
    assert ("stop", "aiendpoint-new") in cloud.calls
    assert cloud.calls[-2][0] == "successor" and cloud.calls[-2][2] is True
    assert connection.writes[-1][1][1] == "failed"
    assert '"stage": "rolled_back"' in connection.writes[-1][1][2]
    assert "private-provider-detail" not in str(connection.writes)


@pytest.mark.asyncio
@pytest.mark.parametrize("protect_binding", [False, True])
async def test_owner_hold_is_rechecked_by_executor_not_only_the_ui(protect_binding):
    repo, row = await setup(protected=protect_binding)
    cloud, connection = Cloud(), Connection()
    protected = set() if protect_binding else {"aiendpoint-source"}
    await WorkbenchExecutor(repo, cloud, {"candidate": "image"}, protected).execute(connection, row)
    assert cloud.calls == []
    assert connection.writes[-1][1][1] == "failed"


@pytest.mark.asyncio
async def test_legacy_local_state_never_stops_endpoint():
    repo, row = await setup()
    repo.binding_values[row["workbench_id"]].state_filesystem_id = None
    cloud, connection = Cloud(), Connection()
    await WorkbenchExecutor(repo, cloud, {"candidate": "image"}, set()).execute(connection, row)
    assert cloud.calls == []


@pytest.mark.asyncio
async def test_restart_uses_original_source_and_same_successor_name():
    repo, row = await setup()
    row["progress"] = {
        "source_endpoint": "aiendpoint-source",
        "source_stopped": True,
        "successor_endpoint": "aiendpoint-new",
    }
    cloud, connection = Cloud(), Connection()
    await WorkbenchExecutor(repo, cloud, {"candidate": "image"}, set()).execute(connection, row)
    assert cloud.calls[0] == ("get", "aiendpoint-source")
    assert cloud.calls[2] == ("successor", f"fs2-wb-{row['id']}", False)
