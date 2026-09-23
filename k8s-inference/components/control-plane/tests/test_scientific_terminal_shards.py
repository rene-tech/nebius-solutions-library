"""Multiple terminal jobs must publish one readable result, without GPU retries."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_scientific_artifacts import (
    TENANT,
    FakeObjectStore,
    execution_identity,
    open_attempt,
    scheduling_snapshot,
    upload,
)
from test_scientific_batch_production import profile_catalog

from fs2_serve.scientific_artifacts import (
    ArtifactAccess,
    ArtifactDirection,
    AttemptStatus,
    CloseStageAttempt,
    MemoryArtifactRepository,
    RunResultDraft,
    ScientificArtifactService,
)
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge


@pytest.mark.asyncio
async def test_terminal_shards_flatten_and_publish_with_real_artifact_service():
    repository = MemoryArtifactRepository(clock=lambda: datetime.now(UTC))
    objects = FakeObjectStore(clock=lambda: datetime.now(UTC))
    service = ScientificArtifactService(
        repository=repository, object_store=objects,
        allowed_media_types={'application/json', 'application/vnd.fs2.scientific-manifest+json'},
    )
    operation = uuid4()
    await repository.register_operation(operation, tenant_id=TENANT)
    manifests = {}
    commits = []
    original_artifacts = []
    input_record = None
    for shard in ('one', 'two'):
        attempt = await open_attempt(service, operation_id=operation, shard_id=shard)
        native = await upload(service, objects, operation_id=operation, attempt_id=attempt,
                              value=json.dumps({'job_id': shard}).encode(), media_type='application/json')
        original_artifacts.append(str(native.artifact_id))
        document = {'schema': 'fs2-serve.nebius.ai/scientific-artifact-manifest/v1',
                    'manifest_id': f'result-{shard}', 'entries': [
                        {'name': 'result', 'semantic_type': 'gromacs-workflow-result/v1',
                         'artifact': native.to_public_ref().model_dump(mode='json', exclude_none=True)},
                    ]}
        raw = json.dumps(document).encode()
        manifest = await upload(service, objects, operation_id=operation, attempt_id=attempt,
                                value=raw, media_type='application/vnd.fs2.scientific-manifest+json')
        if input_record is None:
            input_record = await upload(service, objects, operation_id=operation, attempt_id=attempt,
                                        value=b'{"input":true}', media_type='application/json',
                                        direction=ArtifactDirection.INPUT)
        manifests[manifest.artifact_id] = raw
        commits.append(SimpleNamespace(manifest_artifact_id=manifest.artifact_id))
        await service.close_attempt(CloseStageAttempt(
            operation_id=operation, attempt_id=attempt, tenant_id=TENANT,
            status=AttemptStatus.SUCCEEDED, completed_at=datetime.now(UTC),
        ))

    class Reader:
        async def read(self, identity, *, tenant_id, maximum_bytes):
            assert tenant_id == TENANT and len(manifests[identity]) < maximum_bytes
            return manifests[identity]

    bridge = ArtifactServiceBridge(artifacts=repository, batches=SimpleNamespace(),
                                   profiles=profile_catalog(), store=SimpleNamespace(),
                                   service=service, content_reader=Reader())
    state = SimpleNamespace(operation_id=operation, tenant_id=TENANT,
                            access_context=SimpleNamespace(profile='public', receipt_digest=None))
    identity = await bridge._terminal_manifest(state, tuple(commits))
    assert await bridge._terminal_manifest(state, tuple(commits)) == identity
    record = await repository.get_artifact(identity, tenant_id=TENANT)
    document = json.loads(objects.objects[record.storage_key][0])
    assert [item['name'] for item in document['entries']] == ['shard-0000.result', 'shard-0001.result']
    assert [item['artifact']['artifact_id'] for item in document['entries']] == original_artifacts
    publication = [item for item in await repository.list_attempts(operation, tenant_id=TENANT)
                   if item.stage_id == 'result-publication']
    assert len(publication) == 1 and publication[0].status is AttemptStatus.SUCCEEDED
    assert publication[0].admission.accelerator_count == 0
    assert publication[0].k8s_job_uid is None and publication[0].pod_uids == ()
    result = await service.commit_run_result(RunResultDraft(
        operation_id=operation, tenant_id=TENANT, terminal_status='succeeded',
        submitted_at=publication[0].started_at, completed_at=datetime.now(UTC),
        execution_identity=execution_identity(), access=ArtifactAccess(),
        scheduling_snapshot=scheduling_snapshot(), input_manifest_artifact_id=input_record.artifact_id,
        output_manifest_artifact_id=identity, validator_id='gromacs-workflow-v1',
        validation_status='passed', validation_receipt_digest='sha256:' + '1' * 64,
    ))
    assert result.result.output_manifest.artifact_id == str(identity)
    assert len(result.result.attempts) == 2


@pytest.mark.asyncio
async def test_single_terminal_shard_keeps_the_exact_existing_manifest():
    bridge = ArtifactServiceBridge.__new__(ArtifactServiceBridge)
    identity = uuid4()
    assert await bridge._terminal_manifest(None, (SimpleNamespace(manifest_artifact_id=identity),)) == identity
