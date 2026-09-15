#!/usr/bin/env python3
"""Use existing snapshot operations without modifying their shared source."""
from pathlib import Path
import json
import runpy
import sys
import time
import control
from control import NS, ROOT, NODE, k, save

assert len(sys.argv) > 2 and sys.argv[2].startswith('fs2-bioir-of3-')
if '--source' in sys.argv:
    assert sys.argv[sys.argv.index('--source') + 1].startswith('fs2-bioir-of3-')
if sys.argv[1] == 'ready':
    original_k = k
    last_check = [0]
    def guarded_k(*args, **kwargs):
        if 'get' in args and 'pod' in args and time.time() - last_check[0] > 15:
            node = json.loads(original_k('get', 'node', NODE, '-o', 'json'))
            save('inventory/' + sys.argv[2] + '-latest-node.json', {'unix': time.time(), 'node': node})
            last_check[0] = time.time()
            faults = {'MemoryPressure', 'DiskPressure', 'PIDPressure', 'NebiusGPUError', 'NebiusBootDiskIOError', 'NebiusContainerRuntimeError', 'KernelDeadlock', 'XfsShutdown', 'CperHardwareErrorFatal'}
            if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']) or any(c['type'] in faults and c['status'] == 'True' for c in node['status']['conditions']):
                save('inventory/' + sys.argv[2] + '-node-failure.json', {'unix': time.time(), 'node': node})
                raise RuntimeError('Assigned node is not Ready; stop and escalate without host changes')
        return original_k(*args, **kwargs)
    control.k = guarded_k
if sys.argv[1] in ('release', 'collect'):
    name = sys.argv[2]
    save('raw/' + name + '/events.json', json.loads(k('-n', NS, 'get', 'events', '--field-selector', 'involvedObject.name=' + name, '-o', 'json')))
    try:
        state = k('-n', NS, 'exec', name, '-c', 'boltz2', '--', 'nvidia-smi', '--query-gpu=name,uuid,driver_version,memory.used,utilization.gpu', '--format=csv,noheader')
        processes = k('-n', NS, 'exec', name, '-c', 'boltz2', '--', 'nvidia-smi', '--query-compute-apps=pid,process_name,used_gpu_memory', '--format=csv,noheader')
        save('raw/' + name + '/gpu-before-release.json', {'gpu': state, 'processes': processes})
        script = 'import json,pathlib; print(json.dumps({str(p):p.read_text(errors="replace") for p in pathlib.Path("/tmp").glob("of3-preview2-startup-*/logs/predict_err_rank*.log")}))'
        save('raw/' + name + '/native-prediction-errors.json', json.loads(k('-n', NS, 'exec', name, '-c', 'boltz2', '--', 'python', '-c', script)))
    except Exception as exc:
        save('raw/' + name + '/gpu-query-error.json', {'error': str(exc)})
runpy.run_path(str(Path(__file__).resolve().parent.parent / 'operate.py'), run_name='__main__')
