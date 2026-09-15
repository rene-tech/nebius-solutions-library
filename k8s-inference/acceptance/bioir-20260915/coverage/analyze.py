#!/usr/bin/env python3
"""Summarize all retained attempts without imputing unsupported BIR speedups."""
import collections,datetime,json,math,pathlib,statistics,time
from control import ROOT,MODELS,save

def read(path,default=None):return json.loads(path.read_text()) if path.exists() else default
def utc(value):return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
def stats(values):return {'n':len(values),'seconds':values,'median_seconds':statistics.median(values),'min_seconds':min(values),'max_seconds':max(values),'population_stdev_seconds':statistics.pstdev(values)} if values else None

models={}
for model in MODELS:
    rows=[]
    for path in sorted((ROOT/'raw').glob(model+'*.jsonl')):
        if model=='evo2-40b' and 'previous' in path.name and path.stat().st_size==0:continue
        for line in path.read_text().splitlines():
            try:row=json.loads(line)
            except ValueError:continue
            row['source']=str(path.relative_to(ROOT));rows.append(row)
    attempts=[r for r in rows if r.get('type')=='attempt']
    valid=[r for r in attempts if r.get('valid')]
    runtime=[r for r in rows if r.get('type') in ['ready','torch','runtime_identity','environment']]
    cohorts=collections.defaultdict(list)
    for row in valid:cohorts[(row['cohort'],str(row['case']),row.get('batch',1))].append(row['validated_seconds'])
    product_successes=len(valid)
    decoded=None
    if model=='molmim':
        decoded=sum(bool(m['model_decoded']) for r in valid for m in r['output']['generated'])
        product_successes=sum(any(m['model_decoded'] for m in r['output']['generated']) for r in valid)
    pod=read(ROOT/'lifecycle'/f'{model}-released-pod.json') or read(ROOT/'lifecycle'/f'{model}-after.json') or read(ROOT/'lifecycle'/f'{model}-before.json')
    gpu_count=sum(int(c.get('resources',{}).get('requests',{}).get('nvidia.com/gpu',0)) for c in pod['spec']['containers']) if pod else None
    released=read(ROOT/'lifecycle'/f'{model}-released.json')
    allocation_seconds=None
    if pod and released:
        scheduled=next((c['lastTransitionTime'] for c in pod['status'].get('conditions',[]) if c['type']=='PodScheduled' and c['status']=='True'),pod['metadata']['creationTimestamp'])
        allocation_seconds=(released['unix']-utc(scheduled))*gpu_count
    feature=read(ROOT/'raw'/f'{model}-features.json')
    images=[c['image'] for c in pod['spec']['containers']] if pod else []
    model_row={'model':model,'baseline_status':'measured' if attempts and any(r.get('type')=='complete' for r in rows) else 'in_progress' if attempts or pod else 'pending','image_digests':images,'gpu_count':gpu_count,'node':pod['spec'].get('nodeName') if pod else None,'attempted_benchmark_requests':len(attempts),'schema_and_artifact_valid_requests':len(valid),'product_successful_requests':product_successes,'failed_benchmark_attempts':len(attempts)-len(valid),'newly_decoded_molecules':decoded,'cohorts':[{'cohort':key[0],'case':key[1],'native_batch':key[2],**stats(values)} for key,values in cohorts.items()],'allocated_gpu_seconds_including_pull_debug_idle_cleanup':allocation_seconds,'allocated_gpu_seconds_per_product_success':allocation_seconds/product_successes if allocation_seconds is not None and gpu_count and product_successes else None,'product_successes_per_allocated_gpu_hour':3600*product_successes/allocation_seconds if allocation_seconds and gpu_count else None,'bir_latency_seconds':None,'bir_speedup':None,'bir_status':'not_applicable_no_equivalent_implemented_path','feature_checks':feature,'runtime_evidence':runtime,'cleanup':'released' if released else 'pending','cohort_definition':'loopback request dispatch through validated HTTP artifact; batch runs include exact runtime child process and validator; image scheduling/loading separately retained','quality_note':'Artifact/schema validation is not a paired scientific quality benchmark against BIR.'}
    if model=='molmim':model_row['quality_note']='All measured responses returned the input incumbent with model_decoded=false; zero new decoded molecules. Fallback is explicitly implemented by current server._generate. GPU-seconds/new result undefined. Requests asking for4 got1fallback.'
    if model=='msa-search-pdb70':model_row['quality_note']='First-shape is cache miss; warm and mixed cohorts are A3M cache hits. Added nine genuine searches (three per fixture) after renaming only exact A3M files in a new task-owned emptyDir; OS/database caches remain warm. No GPU allocation. All30 outputs independently checked for query, aligned-column lengths and 128/34/51 record counts.'
    if model=='evo2-40b':model_row['quality_note']='Synthetic nonbiological DNA length fixtures, exact generated length/alphabet/per-token timings, deterministic repeats. Two H100s on a node with6existing customer allocations; shared-host contention possible. One additional2token feature request excluded from benchmark denominator but retained in allocation cost.'
    if model=='proteina-complexa':
        model_row['quality_note']='Nine timed, complete generate/filter/evaluate/analyze protein-target workflows: PDL1 batch1/batch2 and TNFalpha batch1, three seeds each,25 generation steps matching current positive fixture. Optional ESM/monomer/designability metrics disabled as current controller; AF2 self evaluation retained. Full artifacts independently validated, but finite output is not binder efficacy. Ligand/AME variants unmeasured. One final transport-truncated attempt is retained, excluded from timing aggregates, and repeated exactly.'
        model_row['independent_quality']=read(ROOT/'raw/proteina-complexa-independent-validation.json')
        model_row['transport_failure']=read(ROOT/'raw/proteina-complexa-transport-failure.json')
        model_row['unscored_transport_attempts']=1 if model_row['transport_failure'] else 0
        model_row['attempted_benchmark_requests']+=model_row['unscored_transport_attempts']
        model_row['native_scientific_success_rows']=(model_row['independent_quality'] or {}).get('native_success_csv_rows')
        model_row['product_success_definition']='A validated full-workflow artifact response, not a binder passing native scientific filters. Native scientific pass count is reported separately.'
        stage_cohorts=collections.defaultdict(list)
        for r in valid:
            for stage in r['stages']:stage_cohorts[(str(r['case']),r.get('batch',1),stage['argv'][1])].append(stage['seconds'])
        model_row['stage_timings']=[{'case':case,'native_batch':batch,'stage':stage,**stats(values)} for (case,batch,stage),values in stage_cohorts.items()]
    model_row['failed_harness_launches']=[str(p.relative_to(ROOT)) for p in (ROOT/'raw').glob(model+'*-client-stderr.log') if p.stat().st_size and (p.with_name(p.name.replace('-client-stderr.log','.jsonl')).exists() and p.with_name(p.name.replace('-client-stderr.log','.jsonl')).stat().st_size==0)]
    model_row['request_gpu_seconds_sum_not_occupancy']=sum(r['validated_seconds'] for r in attempts)*gpu_count if gpu_count else None
    model_row['current_runtime_identity_note']='Exact live image; image/runtime/cache changes made only for isolation are retained in manifests. This is not original NVIDIA NIM parity.'
    if pod:
        model_row['cold_lifecycle']={'pod_created_at':pod['metadata']['creationTimestamp'],'container_started_at':[c.get('state',{}).get('running',{}).get('startedAt') for c in pod['status'].get('containerStatuses',[])],'first_observed_health_unix':next((r.get('unix') for r in runtime if r.get('type')=='ready'),None),'note':'First observed health includes operator dispatch delay; not a pure startup latency. Exact scheduling/image events and runtime-reported loading are retained separately.'}
    if attempts and not valid:model_row['baseline_status']='measured_failed_no_valid_result'
    models[model]=model_row

out={'schema':'fs2-bioir-coverage-evaluation/v1','generated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_commit':'83bcb2d6c7f4dc112e414e00596e0d6b03e22712','bir_source_commit':'401c6fcc4a43925bcf1342b6c0979b060130b396','scope':MODELS,'models':models,'no_production_change':True,'limitations':['No BIR A/B measurements for unsupported full model paths. Null speedup is not1x.','Each short cohort has3repetitions; individual timings/dispersion are retained; no p99 claim.','Allocated GPU cost includes operator/debug idle time and cold image pulls in this experiment; it is not an optimized serving-cost forecast.','No snapshot capture/restore in this lane.','No disease/biological efficacy conclusion from validity checks.']}
out['detailed_models']=out.pop('models')
out['models']=[]
for model,row in models.items():
    conditional=model in ['rfdiffusion','proteina-complexa']
    out['models'].append({'model_id':model,'bir_fit':'conditional-operation-prototype' if conditional else 'no-direct-fit','status':row['baseline_status'],'baseline':row,'comparisons':{'bir_latency_seconds':None,'bir_speedup':None,'reason':'No implemented equivalent BIR execution path'},'quality':{'artifact_valid_requests':row['schema_and_artifact_valid_requests'],'product_successful_requests':row['product_successful_requests'],'note':row['quality_note'],'paired_bir_quality':None},'features':row['feature_checks'] or {'status':'partial','note':'Native repeated/batch outputs checked; broader feature/cancellation parity unverified.'},'snapshot':{'status':'not_tested_in_coverage','note':'Existing state residency is not BIR acceleration; new BIR snapshot tests are in the snapshot lane.'},'recommendation':'Investigate narrow operation seam only after shape/dtype and scientific parity gates; no deployment' if conditional else 'No BIR adoption for current model; retain current runtime' if model!='molmim' else 'Prioritize current generation/cardinality remediation; no BIR fit','limitations':['No BIR A/B speedup exists for this model.','No disease/biological efficacy conclusion.','Short cohorts: individual timings and dispersion, no tail-SLA claim.'],'evidence':['coverage/raw/'+model+'.jsonl','coverage/applicability.md','coverage/lifecycle','coverage/inventory']})
save('result.json',out)
report=['# Coverage baseline report — 2026-09-15','', 'Current serving images were cloned into isolated, explicitly scheduled workers. No BIR full-model path exists for these eight models; all BIR speedups are N/A. See [applicability](applicability.md) for source-level seams and related-product assessment.','', '| Model | Attempted / artifact-valid / product-successful | GPUs | Warm/request median ranges | Total allocated GPU-s per product-success | Status |','|---|---:|---:|---:|---:|---|']
for model,row in models.items():
    warm=[c['median_seconds'] for c in row['cohorts'] if c['cohort']=='warm-sequential' or c['cohort']=='new-process-local-image-shared-weights']
    timing=f'{min(warm):.4f}–{max(warm):.4f} s' if warm else 'no valid timing'
    if model=='msa-search-pdb70':
        miss=[c['median_seconds'] for c in row['cohorts'] if c['cohort']=='fresh-a3m-cache-miss-warm-os-cache']
        timing=f'miss {min(miss):.3f}–{max(miss):.3f} s; cached {min(warm)*1000:.2f}–{max(warm)*1000:.2f} ms'
    cost=f"{row['allocated_gpu_seconds_per_product_success']:.2f}" if row['allocated_gpu_seconds_per_product_success'] is not None else 'N/A'
    report.append(f"| {model} | {row['attempted_benchmark_requests']} / {row['schema_and_artifact_valid_requests']} / {row['product_successful_requests']} | {row['gpu_count']} | {timing} | {cost} | {row['baseline_status']}; {row['cleanup']} |")
report+=['','## Interpretation','', '- MolMIM: 21 valid response envelopes are not 21 newly generated molecules. All measured molecules were input fallbacks (`model_decoded=false`); the 4-molecule case returned only one incumbent. The current server documents this fallback. Address generation success/cardinality before offering performance claims; this evaluation makes no production change.','- MSA: roughly 2.2-second real searches and millisecond cached-A3M returns are separate cohorts. This service allocates 0 GPUs, so GPU-hour efficiency does not apply.','- Evo2: same 40B checkpoint, 2 H100s and native precision; first-shape cost differs from repeated requests. Measured replay rejection 409 and unknown-field rejection 400. Existing customer pods were not modified.','- DiffDock: first harness used the wrong route; all 21 HTTP 404 attempts are retained. The corrected native route produced 21 valid poses. Reported warm timings use corrected calls only; total resource cost includes the failed attempt period.','- RFdiffusion: actual 50-step generation, 80/160/256-residue backbones and native batch 2, with 3 repetitions. It is a new-process runtime; labeling it warm persistent would be wrong. The assigned L40S was used, so these are L40S results, not H100 extrapolations.','- Complexa: status and actual stage evidence are recorded in the structured result. A generation-only result cannot stand in for the complete generate/filter/evaluate/analyze workflow.','','## Evidence and reproduction','', '[result.json](result.json) contains every case median, individual repetition, dispersion, exact image digest, runtime metadata and cleanup state. [raw](raw) retains request/response JSONL, failures and server logs. [inventory](inventory) freezes live deployments and scientific execution mappings. [lifecycle](lifecycle) records image/start/stop events. Public input structures are 1UBQ/1LYZ/1MBN and are pinned in [fixtures](fixtures).','', '`prepare.py` prepares task-owned fixtures/ConfigMaps; `control.py clone` copies the frozen current HTTP runtime; `run.py` validates ConfigMap propagation then executes the in-pod client; `batch_control.py` and `batch_client.py` run scientific batch images; `cleanup.py` only deletes a named pod after verifying evaluation/lane labels; `analyze.py` regenerates this report. Explicit node and namespace choices follow the shared contract and manager exceptions.','', 'No default-network API benchmarks or production routing modifications occurred. Unsupported cancellation, broad feature parity, snapshot restore and paired BIR scientific quality remain unverified, not inferred from successful schema checks.']
(ROOT/'report.md').write_text('\n'.join(report)+'\n')
with (ROOT/'report.md').open('a') as stream:
    stream.write('\n## Complexa scientific scope and preserved failure\n\nNine timed complete workflows cover PDL1 native batches 1/2 and TNFalpha batch 1, with three seeds per case and 25 generation steps from the current positive fixture. Optional ESM/monomer/designability evaluation is disabled as in the current controller; AF2 self evaluation is retained. The evaluation CSV reloads default generation metadata and says 400 steps; retained generation argv/logs are authoritative for the actual 25 steps. This metadata discrepancy is preserved. Ligand and AME variants were not measured.\n\nThe final long-running exec stream disconnected while emitting its completed result. Its truncated row, stderr and artifacts remain; no timing was imputed. Only that case was repeated with a durable per-trial JSON record. Allocation cost includes both attempts. Independent validation checks every retained PDB coordinate and native AF2 confidence/scRMSD metrics; see `raw/proteina-complexa-independent-validation.json` for scientific pass counts. Artifact-valid requests do not imply successful binder designs.\n\n## Version and isolation evidence\n\n`inventory/*-build-inputs.json`, immutable image digests in every manifest, and frozen runtime identities pin source/checkpoint versions. `inventory/proteinmpnn-checkpoint-pin.json` records the current catalog checkpoint manifest; the exact benchmark image supplies that runtime. Complexa source/model pins are in `inventory/proteina-complexa-image-lock.json`; both 7.0 GB of protein checkpoints were SHA256-verified from the read-only evaluation mount after benchmark completion. `inventory/evo2-shared-host-after-health.json` records the original six customer GPU pods remaining ready with unchanged resources/images. No production state was changed.\n')
print(json.dumps({m:{k:r[k] for k in ['baseline_status','schema_and_artifact_valid_requests','product_successful_requests','cleanup']} for m,r in models.items()},indent=2))
