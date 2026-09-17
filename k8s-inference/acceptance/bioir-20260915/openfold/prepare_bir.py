#!/usr/bin/env python3
"""Render BIR model-seam comparator; initializer copies exact baseline assets."""
import argparse
import json
from pathlib import Path
from prepare_baseline import NODES, NAMESPACE, ROOT, save, kubectl

parser = argparse.ArgumentParser()
parser.add_argument('--image', required=True)
parser.add_argument('--model', choices=['openfold2', 'openfold3'], default='openfold2')
parser.add_argument('--attempt', type=int, default=1)
parser.add_argument('--base-has-of2-deps', action='store_true')
parser.add_argument('--upstream-env-control', action='store_true')
parser.add_argument('--cuda-graph', action='store_true')
parser.add_argument('--gpu-resident', action='store_true')
parser.add_argument('--gpu', choices=NODES, default='h100')
parser.add_argument('--precision', choices=['float32', 'mixed'], default='float32')
args = parser.parse_args()
suffix = f'-r{args.attempt}' if args.attempt > 1 else ''
optimization = '-graph-resident' if args.cuda_graph else '-resident' if args.gpu_resident else ''
assert not args.cuda_graph or args.gpu_resident
name = f'{args.model}-bir-{args.precision}{optimization}{suffix}-{args.gpu}'
if args.upstream_env_control:
    assert args.model == 'openfold2'
    name = f'openfold2-upstream-env{suffix}-{args.gpu}'
pods = json.loads(kubectl('get', 'pods', '-A', '-o', 'json'))['items']
allocated = [p for p in pods if p['spec'].get('nodeName') == NODES[args.gpu] and p['status']['phase'] not in ('Succeeded', 'Failed') and any(int(c.get('resources', {}).get('requests', {}).get('nvidia.com/gpu', 0)) for c in p['spec']['containers'])]
if allocated:
    raise SystemExit('Assigned GPU already allocated: ' + ', '.join(p['metadata']['name'] for p in allocated))
node = json.loads(kubectl('get', 'node', NODES[args.gpu], '-o', 'json'))
assert any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions'])
assert not any(c['type'] in ('DiskPressure', 'MemoryPressure') and c['status'] == 'True' for c in node['status']['conditions'])
plugin = next(p for p in pods if p['spec'].get('nodeName') == NODES[args.gpu] and 'nvidia-device-plugin' in p['metadata']['name'] and p['status']['phase'] == 'Running')
gpu_state = kubectl('-n', plugin['metadata']['namespace'], 'exec', plugin['metadata']['name'], '--', 'nvidia-smi', '--query-gpu=name,driver_version,memory.used,utilization.gpu', '--format=csv,noheader')
processes = kubectl('-n', plugin['metadata']['namespace'], 'exec', plugin['metadata']['name'], '--', 'nvidia-smi', '--query-compute-apps=pid,process_name,used_gpu_memory', '--format=csv,noheader')
assert not processes.strip(), 'GPU compute processes exist; do not allocate.'
save(f'raw/{name}-preflight.json', {'active_gpu_pods': [], 'node': NODES[args.gpu], 'gpu_state': gpu_state, 'gpu_compute_processes': processes, 'node_conditions': node['status']['conditions'], 'pending_gpu_pods': [p['metadata']['namespace'] + '/' + p['metadata']['name'] for p in pods if p['status']['phase'] == 'Pending' and any(c.get('resources', {}).get('requests', {}).get('nvidia.com/gpu') for c in p['spec']['containers'])]})
labels = {'evaluation': 'fs2-bioir-20260915', 'lane': 'openfold', 'model': args.model, 'variant': 'bir-' + args.precision}
current_image = 'cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/h100-fleet/openfold2@sha256:9fc70e781b18f4f547da237e2eb81387df3c38d092bb44c46145db6347e7e164'
configmap = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': args.model + '-bir-source', 'namespace': NAMESPACE, 'labels': labels}, 'data': {n: (ROOT / n).read_text() for n in [f'bir_{args.model}_server.py', 'resident_gpu.py', 'benchmark_http.py', 'probe_contract.py']}}
mounts = [{'name': 'payload', 'mountPath': '/payload'}, {'name': 'source', 'mountPath': '/eval'}, {'name': 'deps', 'mountPath': '/deps'}, {'name': 'shm', 'mountPath': '/dev/shm'}]
copy_script = 'import shutil; from pathlib import Path; p=Path("/payload"); shutil.copytree("/opt/openfold", p/"openfold"); shutil.copytree("/opt/fs2/openfold2", p/"server"); shutil.copytree("/opt/fs2/openfold2-artifacts", p/"weights")'
pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': NAMESPACE, 'labels': labels}, 'spec': {
    'automountServiceAccountToken': False, 'restartPolicy': 'Never', 'nodeSelector': {'kubernetes.io/hostname': NODES[args.gpu]}, 'tolerations': [{'key': 'dedicated', 'operator': 'Equal', 'value': 'fs2-inference', 'effect': 'NoSchedule'}],
    'securityContext': {'runAsUser': 10001, 'runAsGroup': 10001, 'fsGroup': 10001},
    'initContainers': [
        {'name': 'exact-current-artifacts', 'image': current_image, 'command': ['python', '-c', copy_script], 'volumeMounts': mounts[:1], 'resources': {'requests': {'cpu': '1', 'memory': '2Gi'}, 'limits': {'cpu': '2', 'memory': '4Gi'}}},
        {'name': 'upstream-preprocessor-dependencies', 'image': args.image, 'command': ['python', '-m', 'pip', 'install', '--no-cache-dir', '--target', '/deps', 'ml-collections==1.1.0', 'dm-tree==0.1.9', 'numpy==2.2.6'], 'volumeMounts': [{'name': 'deps', 'mountPath': '/deps'}], 'resources': {'requests': {'cpu': '1', 'memory': '2Gi'}, 'limits': {'cpu': '2', 'memory': '4Gi'}}}
    ],
    'containers': [{'name': 'model', 'image': args.image, 'command': ['python', '/eval/bir_openfold2_server.py'], 'env': [{'name': 'PYTHONPATH', 'value': '/deps:/payload/server:/payload/openfold'}, {'name': 'FS2_OPENFOLD2_CHECKPOINT', 'value': '/payload/weights/finetuning_no_templ_ptm_1.pt'}, {'name': 'BIOIR_EVAL_PRECISION', 'value': args.precision}, {'name': 'PYTHONUNBUFFERED', 'value': '1'}], 'volumeMounts': mounts, 'resources': {'requests': {'cpu': '8', 'memory': '32Gi' if args.gpu == 'l40s' else '64Gi', 'nvidia.com/gpu': '1', 'ephemeral-storage': '20Gi'}, 'limits': {'cpu': '32', 'memory': '48Gi' if args.gpu == 'l40s' else '192Gi', 'nvidia.com/gpu': '1', 'ephemeral-storage': '60Gi'}}, 'readinessProbe': {'httpGet': {'path': '/v1/health/ready', 'port': 8000}, 'periodSeconds': 5}}],
    'volumes': [{'name': 'payload', 'emptyDir': {'sizeLimit': '5Gi'}}, {'name': 'source', 'configMap': {'name': 'openfold2-bir-source'}}, {'name': 'deps', 'emptyDir': {'sizeLimit': '2Gi'}}, {'name': 'shm', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '8Gi'}}]
}}
if args.model == 'openfold3':
    pod['spec']['initContainers'][0]['image'] = 'cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/openfold3-preview2@sha256:1e35247f0de8be59119ddf875c2631173a8c533c90f7b21e1c01ef70a069c00c'
    pod['spec']['initContainers'][0]['command'] = ['/bin/bash', '-ec', 'source /opt/fs2/activate.sh; python -c \'import shutil; shutil.copytree("/opt/fs2/openfold3-preview2", "/payload/server"); shutil.copytree("/opt/fs2/openfold3-preview2-artifacts", "/payload/weights")\'']
    # Resolve application dependencies once, then pin and isolate them. The
    # public BIR scipy==1.13.1 pin conflicts with pdbeccdutils>=scipy1.14.1;
    # this prototype uses the current upstream-compatible scipy1.18.1 and
    # records that required joint-environment qualification in the report.
    of3_dependencies = ['openfold3==0.4.2', 'pytorch-lightning==2.6.6', 'pdbeccdutils==1.0.4', 'awscli==1.46.1', 'awscrt==0.36.3', 'boto3==1.43.95', 'ijson==3.5.1', 'memory_profiler==0.61.0', 'wandb==0.30.0', 'lmdb==2.3.0', 'torchmetrics==1.9.0', 'lightning-utilities==0.15.3', 'docutils==0.19', 'colorama==0.4.6', 'rsa==4.7.2', 'jmespath==1.1.0', 'botocore==1.43.95', 's3transfer==0.19.2', 'psutil==7.2.2', 'gemmi==0.7.5', 'scipy==1.18.1', 'opentelemetry-exporter-otlp-proto-http==1.44.0', 'opentelemetry-exporter-otlp-proto-common==1.44.0', 'func_timeout==4.3.5']
    pod['spec']['initContainers'][1]['command'] = ['python', '-m', 'pip', 'install', '--no-cache-dir', '--no-deps', '--target', '/deps/lib/python3.12/site-packages', *of3_dependencies]
    pod['spec']['containers'][0]['command'] = ['python', '/eval/bir_openfold3_server.py']
    pod['spec']['containers'][0]['env'][0]['value'] = '/deps/lib/python3.12/site-packages:/payload/server'
    pod['spec']['volumes'][1]['configMap']['name'] = 'openfold3-bir-source'
    pod['spec']['volumes'][2]['emptyDir']['sizeLimit'] = '8Gi'
for env_name, env_value in [('BIOIR_CACHE', '/tmp/bioir-cache'), ('OPENFOLD_CACHE', '/tmp/openfold-cache'), ('XDG_CACHE_HOME', '/tmp/xdg-cache'), ('TRITON_CACHE_DIR', '/tmp/triton-cache'), ('TORCH_EXTENSIONS_DIR', '/tmp/torch-extensions'), ('TORCHINDUCTOR_CACHE_DIR', '/tmp/torchinductor-cache'), ('USER', 'bioir-evaluation')]:
    pod['spec']['containers'][0]['env'].append({'name': env_name, 'value': env_value})
pod['spec']['containers'][0]['env'] += [{'name': 'BIOIR_CUDA_GRAPH', 'value': str(int(args.cuda_graph))}, {'name': 'BIOIR_GPU_RESIDENT', 'value': str(int(args.gpu_resident))}]
if args.model == 'openfold2' and args.base_has_of2_deps:
    pod['spec']['initContainers'].pop(1)
if args.upstream_env_control:
    pod['spec']['containers'][0]['command'] = ['python', '/payload/server/server.py']
    labels['variant'] = 'upstream-env-control'
save(f'manifests/{name}.json', {'apiVersion': 'v1', 'kind': 'List', 'items': [configmap, pod]})
print(ROOT / f'manifests/{name}.json')
