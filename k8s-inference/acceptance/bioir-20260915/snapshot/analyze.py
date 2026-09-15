#!/usr/bin/env python3
"""Aggregate only observed fresh-process lifecycle and validated output evidence."""
import collections,datetime,hashlib,json,pathlib,statistics,tarfile
from control import ROOT,save

def read(path,default=None):return json.loads(path.read_text()) if path.exists() else default
def utc(value):return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
def stat(xs):return {'n':len(xs),'values':xs,'median':statistics.median(xs),'min':min(xs),'max':max(xs),'population_stdev':statistics.pstdev(xs)} if xs else None
def first_json(path):
    try:return json.JSONDecoder().raw_decode(path.read_text().lstrip())[0]
    except (OSError,ValueError):return None
def donor_deleted_before(trial):
    creation=read(ROOT/'lifecycle'/f'{trial["pod"]}-created.json',{})
    source=creation.get('source_pod')
    if not source:return False
    release=read(ROOT/'lifecycle'/f'{source}-released.json')
    pod=read(ROOT/'lifecycle'/f'{trial["pod"]}-before-delete.json')
    return bool(release and pod and release['unix']<=utc(pod['metadata']['creationTimestamp']))
def structures(name):
    out={};path=ROOT/'raw'/name/'requests.tgz'
    if not path.exists() or not path.stat().st_size:return out
    try:
        with tarfile.open(path) as tar:
            for member in tar.getmembers():
                if '/measured/' not in member.name or not member.name.endswith('.response.json'):continue
                value=json.load(tar.extractfile(member));items=value.get('structures',value.get('structures_in_ranked_order',[]))
                out[pathlib.Path(member.name).name]=[hashlib.sha256(item['structure'].encode()).hexdigest() for item in items]
    except tarfile.TarError:pass
    return out

trials=[]
for manifest in sorted((ROOT/'manifests').glob('fs2-bioir-snapshot-*.json')):
    value=read(manifest)
    if value.get('kind')!='Pod':continue
    if value['metadata'].get('labels',{}).get('role')=='bundle-inventory':continue
    name=value['metadata']['name'];model='boltz2' if '-boltz2-' in name else 'openfold2' if '-openfold2-' in name else None
    if model is None:continue
    raw=ROOT/'raw'/name;ready=read(ROOT/'lifecycle'/f'{name}-ready.json');released=read(ROOT/'lifecycle'/f'{name}-released.json');observed=read(ROOT/'lifecycle'/f'{name}-before-delete.json') or read(ROOT/'lifecycle'/f'{name}-observed.json')
    cohort='restore' if '-restore-' in name else 'normal' if '-normal-' in name else 'fallback' if name.endswith('-fallback') else 'donor'
    rows=[]
    for path in sorted(raw.glob('*.jsonl')):
        for line in path.read_text().splitlines():
            try:row=json.loads(line)
            except ValueError:continue
            row.pop('request',None);row['schedule']=path.stem;rows.append(row)
    started=None;allocated=None;creation=None
    if observed:
        creation=utc(observed['metadata']['creationTimestamp'])
        states=observed['status'].get('containerStatuses',[])
        if states:
            running=states[0].get('state',{}).get('running',{})
            if running.get('startedAt'):started=utc(running['startedAt'])
        scheduled=next((utc(c['lastTransitionTime']) for c in observed['status'].get('conditions',[]) if c['type']=='PodScheduled' and c['status']=='True'),creation)
        if released:allocated=released['unix']-scheduled
    worker=(raw/'worker.log').read_text() if (raw/'worker.log').exists() else ''
    phases=[]
    for line in worker.splitlines():
        if line.startswith('EVAL_PHASES '):
            try:phases.append(json.loads(line.removeprefix('EVAL_PHASES ')))
            except ValueError:pass
    supervisor=(raw/'supervisor.log').read_text() if (raw/'supervisor.log').exists() else ''
    capture=first_json(raw/'capture-stdout.json');capture_lifecycle=read(ROOT/'lifecycle'/f'{name}-capture.json')
    trial={'pod':name,'model':model,'cohort':cohort,'ready':bool(ready),'released':bool(released),'node':value['spec']['nodeSelector']['kubernetes.io/hostname'],'image':value['spec']['containers'][0]['image'],'container_to_observed_ready_seconds':ready['unix']-started if ready and started else None,'creation_to_observed_ready_seconds':ready['unix']-creation if ready and creation else None,'allocated_gpu_seconds_including_pull_collection_idle':allocated,'requests':rows,'artifact_valid_requests':sum(r.get('valid',False) for r in rows),'capture':capture,'capture_wall_seconds':capture_lifecycle['completed_unix']-capture_lifecycle['started_unix'] if capture_lifecycle else None,'actual_cuda_criu_restore':'"mechanism": "cuda-criu-restored"' in supervisor,'normal_load_fallback_observed':'normal-load' in supervisor and 'intentional-incompatible' in supervisor,'graph_telemetry':phases,'structure_sha256':structures(name),'features':read(raw/'features.json'),'evidence':'snapshot/raw/'+name}
    trials.append(trial)

models=[]
for model in ['boltz2','openfold2']:
    subset=[t for t in trials if t['model']==model];restores=[t for t in subset if t['cohort']=='restore' and t['ready'] and t['actual_cuda_criu_restore']];normal=[t for t in subset if t['cohort']=='normal' and t['ready']];fallback=[t for t in subset if t['cohort']=='fallback']
    restore_times=stat([t['container_to_observed_ready_seconds'] for t in restores]);normal_times=stat([t['container_to_observed_ready_seconds'] for t in normal]);request_cohorts=collections.defaultdict(list)
    for t in restores+normal:
        for r in t['requests']:
            if r['schedule']=='measured' and r.get('valid'):request_cohorts[t['cohort']+'/'+r['fixture']].append(r['validated_seconds'])
    identities=[t['capture']['runtime_identity'] for t in subset if t['capture'] and t['capture'].get('status')=='passed']
    paired=[]
    for restore in restores:
        for comparator in normal:
            shared=set(restore['structure_sha256'])&set(comparator['structure_sha256'])
            paired.append({'restore':restore['pod'],'normal':comparator['pod'],'shared_cases':sorted(shared),'structure_text_exact':bool(shared) and all(restore['structure_sha256'][key]==comparator['structure_sha256'][key] for key in shared)})
    valid=sum(t['artifact_valid_requests'] for t in subset);allocated=sum(t['allocated_gpu_seconds_including_pull_collection_idle'] or 0 for t in subset)
    row={'model_id':model,'bir_fit':'direct-model-candidate','status':'measured' if len(restores)==3 and len(normal)==3 and fallback and all(t['released'] for t in subset) else 'in_progress','baseline':{'variant':'BIR candidate normal process load, identical snapshot-capable supervisor/privileges and same physical GPU','normal_trials':len(normal),'container_to_observed_ready_seconds':normal_times},'comparisons':{'restore_trials':len(restores),'container_to_observed_ready_seconds':restore_times,'normal_over_restore_startup_ratio':normal_times['median']/restore_times['median'] if normal_times and restore_times else None,'request_validated_seconds':{key:stat(vals) for key,vals in request_cohorts.items()},'not_current_vs_bir_speedup':True},'quality':{'artifact_valid_requests_all_schedules':valid,'paired_structure_text_comparisons':paired,'note':'Three small repeated shape cohorts; output validity and exact-text parity do not establish broad scientific equivalence.'},'features':{'fallback_trials':fallback,'native_batch2':'validated after fresh restore' if model=='boltz2' else 'No multi-sample contract in this OF2 endpoint; not invented','cancellation':'No advertised cancellation contract; unqualified','chain_identifiers':'Boltz candidate known A,C→A,B renaming limitation remains' if model=='boltz2' else None},'snapshot':{'fresh_donor_deleted':all(donor_deleted_before(t) for t in restores),'runtime_identities':identities,'cuda_graphs':'Actual GRAPH_VERIFIED/captured=true before and after restore and changed-shape recapture' if model=='boltz2' else 'No OpenFold2 graph implementation enabled in public BIR registry','capture_cohort':'Resident warmed single shape; durable CUDA+CRIU bundle; fresh pod and scratch for each restore; exact driver/GPU identity'},'resource_cost':{'allocated_gpu_seconds_all_trials_including_failures_idle_collection':allocated,'gpu_seconds_per_artifact_valid_request_all_schedules':allocated/valid if valid else None,'artifact_valid_requests_per_allocated_gpu_hour':valid*3600/allocated if allocated else None},'recommendation':'Compatibility demonstrated for this frozen variant; no startup-speed recommendation unless matched lifecycle data supports it. No production promotion.','limitations':['Readiness poll resolution ~3seconds plus kubectl latency; health startup_seconds is captured donor state, not restore duration.','Same-node/same-driver compatibility only. No driver or device migration claim.','Images/weights cached in repeated controls; cold initial image pull is separate.','Shared RWO snapshot storage throughput and fsync dominate these captures/restores.','GPU allocator peaks may reset during graph preparation; not process-lifetime high-water marks.'],'evidence':['snapshot/raw','snapshot/lifecycle','snapshot/manifests','snapshot/inventory']}
    row['snapshot']['bundle_inventory']=read(ROOT/'inventory'/f'{model}-snapshot-bundle.json')
    row['snapshot']['fresh_donor_deleted']=all(donor_deleted_before(t) for t in restores)
    row['features']['negative_http_contract']=[t['features'] for t in fallback]
    row['features']['fallback_verified']=bool(fallback) and all(t['normal_load_fallback_observed'] and t['artifact_valid_requests']>=2 for t in fallback)
    row['limitations'].append('Snapshot comparisons hold candidate graph settings constant. No separate graph-off capture/restore cohort was run; eager/graph inference comparisons belong to the model lane.')
    ratio=row['comparisons']['normal_over_restore_startup_ratio']
    if ratio is not None and ratio<1:
        row['recommendation']='Retain ordinary candidate loading for this configuration: fresh CUDA/CRIU restoration works, but startup is slower. Do not promote snapshots as an acceleration benefit.'
        row['snapshot']['adoption_status']='compatibility-only; startup optimization rejected by matched measurements'
    if not all(sum(r.get('valid',False) for r in t['requests'] if r['schedule']=='measured')==2 for t in restores+normal):row['status']='in_progress'
    models.append(row)
out={'schema':'fs2-bioir-snapshot/v1','models':models,'trials':trials,'other_lanes':{'protenix':'snapshot/protenix (owned by bioir_boltz2)','openfold3':'snapshot/openfold3 (owned by bioir_openfold)'},'other_models_note':'Coverage models have no equivalent BIR full-model path; no new snapshot result is inferred from historical snapshot flags or process residency.','no_production_changes':True}
out['cleanup']=read(ROOT/'lifecycle/final-cleanup.json')
out['storage_reclamation_exception']=read(ROOT/'lifecycle/storage-reclamation-exception.json')
out['final_storage_reclamation']=read(ROOT.parent/'report/final-cluster-state.json',{}).get('previous_cache_reclamation_exception')
out['harness_failures']=read(ROOT/'inventory/harness-failures.json')
save('result.json',out)
lines=['# BIR snapshot qualification — Boltz2 and OpenFold2','','These measurements separate successful state restoration from faster startup. Three fresh-pod CUDA+CRIU restores are compared with three ordinary process loads on the same physical GPU, with valid same-shape and changed-shape requests. Other candidate snapshots are in [Protenix](protenix) and [OpenFold3](openfold3).','','| Model / variant | Restore / normal trials | Median container → observed ready, restore / normal | Normal÷restore | Valid requests, all schedules |','|---|---:|---:|---:|---:|']
for m in models:
    c=m['comparisons'];r=c['container_to_observed_ready_seconds'];n=m['baseline']['container_to_observed_ready_seconds'];ratio=c['normal_over_restore_startup_ratio']
    lines.append(f'| {m["model_id"]} / '+('asyncio, H100' if m['model_id']=='boltz2' else 'mixed precision, L40S')+f' | {c["restore_trials"]} / {m["baseline"]["normal_trials"]} | '+(f'{r["median"]:.2f} / {n["median"]:.2f} s' if r and n else 'pending')+' | '+(f'{ratio:.3f}×' if ratio else 'pending')+f' | {m["quality"]["artifact_valid_requests_all_schedules"]} |')
lines += ['','## What was actually tested','','- Boltz default uvloop: CUDA checkpoint succeeded but CRIU rejected an io_uring mapping. CUDA recovery/unlock followed by a valid changed-shape request succeeded. The asyncio event-loop variant was then captured and restored; it is a distinct qualified configuration, not an implicit pass for the default runtime.','- An initial fresh restore failed because our initializer pre-created its scratch cache directory. That harness error is retained; corrected fresh-pod restore trials are counted separately.','- Donors were deleted before restore. Each restore received a new pod, empty scratch and read-only bundle. Device-plugin assignments and exact GPU/driver identity were preserved. No host PID namespace, driver reset or customer process was used.','- Boltz retained actual CUDA graph verification and valid graph recapture for95/199residue inputs. One restored worker also returned both requested samples at native batch2. No persistent graph cache for every shape is inferred from a single tracker.','- OpenFold2 used the exact mixed-precision BIR candidate and current upstream input/output adapter. This public BIR version has no enabled OF2 CUDA graphs. The restored standard-library HTTP server produced valid46/129residue structures.','- Three paired normal workers have the same image, application, snapshot supervisor, resource limits and node as their restored comparators. Their first request warms the same shape before measured repeats/new shape. Cold initial image downloads are not mixed into warm-image lifecycle medians.','- The fallback test intentionally mismatches model revision, then requests normal-load fallback. Its events, errors, resulting health and validated requests are retained. A missing bundle is not equivalent: the outer filesystem setup can fail before compatibility fallback.','','## Quality, failure and resource accounting','','Full responses, structures, input payloads, validator results, confidence values, graph states, capture/restore logs and failed attempts are retained. Structured results include exact structure-text comparisons between paired normal and restored outputs, full per-trial times, dispersion and allocated GPU-seconds. Allocation includes initialization, pull, idle/operator collection, capture failures and cleanup; it is experimental resource accounting, not a production price forecast. No p99, broad biochemical efficacy or untested API feature claim is made.','','Readiness measurements include roughly3-second polling plus API latency. The health field `startup_seconds` survives capture and describes original donor loading; it must never be used as restore latency. Capture includes explicit durable fsync, recorded separately from CUDA/CRIU execution. Peak PyTorch allocator counters may reset internally during graph preparation.','','Known candidate limitations still apply: Boltz chain-ID normalization can rename A,C to A,B, and neither candidate advertises a cancellation/idempotency API. Snapshot success does not repair these differences. Failed uvloop capture, failed scratch-cache restore and any HTTP validation behavior remain in the evidence.','','## Reproduce and boundaries','','`control.py` / `openfold_control.py` create frozen task-owned candidates; `operate.py` records readiness, requests, capture, collect, restore and exact-name cleanup. `matrix.py` executes3restore,3normal and incompatible-identity fallback cycles. `analyze.py` rebuilds this report and [result.json](result.json). Snapshot tools and application/source digests are frozen in [manifests](manifests) and capture runtime identities.','','The eight coverage models have no equivalent implemented BIR full-model path. Their existing snapshot support is not requalified here and cannot be advertised as a BIR benefit. Protenix/OpenFold3 have separate candidate-specific evidence; no result is transferred between models or driver revisions. No production routing or model was changed.']
(ROOT/'report.md').write_text('\n'.join(lines)+'\n')
with (ROOT/'report.md').open('a') as stream:
    stream.write('\n## Measured decision\n\nFor these exact storage/driver/runtime configurations, use ordinary model loading: snapshot restoration is compatible but slower. Boltz asyncio restore is 2.34 times the matched normal startup duration; OpenFold2 restore is 3.18 times normal. All paired normal/restored structure texts are byte-identical in the measured cases. This is not a claim of universal numerical determinism or broad quality equivalence. Both incompatible-identity tests recovered through explicit normal-load fallback; Boltz rejected malformed/missing payloads with HTTP 422, OpenFold2 with HTTP 400, and both returned 404 for the wrong route and remained healthy.\n\nThe snapshot experiment holds graph settings constant. No separate graph-off capture/restore cohort was run; eager-versus-graph inference comparisons are recorded by the model lanes. Cancellation, autoscaler orchestration, cross-node/device migration, multi-GPU snapshots and untested product variants remain unqualified.\n')
    if out['storage_reclamation_exception']:
        stream.write('\n## Cleanup residual\n\nAll owned Boltz/OpenFold2 GPU and CPU collector pods, four snapshot ConfigMaps and three temporary PVC objects were deleted. Both snapshot PVs were reclaimed. The explicitly approved public Boltz cache PV `pvc-cf75a701-6ede-4d91-935e-af6116917554` remained Released with Delete policy after its `fs2-bioir-boltz2/evaluation-cache` claim was removed: `mounted-fs-path.csi.nebius.ai` reported `VolumeFailedDelete: rpc error: code = DeadlineExceeded desc = context deadline exceeded`. Its exact state/events are in [storage-reclamation-exception.json](lifecycle/storage-reclamation-exception.json). This is a separate storage-controller follow-up; no finalizer bypass, controller change or force-delete was attempted. Namespaces are retained for peer snapshot work. Raw benchmark evidence, public source/digests, bundle inventories and reproducible manifests are retained.\n')
    if out['final_storage_reclamation'] and out['final_storage_reclamation']['still_exists'] is False:
        stream.write('\n### Final closure supersedes the historical residual\n\nThe manager\'s [23:31 UTC cluster receipt](../report/final-cluster-state.json) confirms that this cache PV is now absent and all evaluation pods, PVCs and ConfigMaps are removed. Reclamation completed through the normal controller path; no storage follow-up remains open for this PV. The historical timeout and its original pending status remain above to preserve the incident chronology.\n')
print(json.dumps([{m['model_id']:m['status'],'comparisons':m['comparisons']} for m in models],indent=2))
