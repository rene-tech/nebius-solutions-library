#!/usr/bin/env python3
"""Sequential fresh-pod trials with no overlapping GPU allocations on a node."""
import argparse,json,subprocess,sys
from control import ROOT
p=argparse.ArgumentParser();p.add_argument('--model',choices=['boltz2','openfold2'],required=True);p.add_argument('--source',required=True);p.add_argument('--first-restored');a=p.parse_args()
ns='fs2-bioir-boltz2' if a.model=='boltz2' else 'fs2-bioir-snapshot';cases='T1031,T1038' if a.model=='boltz2' else '1crn,1lyz';warm=cases.split(',')[0]
def op(action,name,*args):subprocess.run([sys.executable,str(ROOT/'operate.py'),action,name,'--namespace',ns,*args],check=True)
if a.first_restored:
    op('release',a.first_restored);start=2
else:
    op('release',a.source);start=1
for index in range(start,4):
    name=f'fs2-bioir-snapshot-{a.model}-restore-{index}'
    op('restore',name,'--source',a.source);op('ready',name);op('request',name,'--cases',cases,'--label','measured');op('release',name)
for index in range(1,4):
    name=f'fs2-bioir-snapshot-{a.model}-normal-{index}';run=f'{a.model}-normal-{index}'
    if a.model=='boltz2':command=[sys.executable,str(ROOT/'control.py'),'donor','--name',name,'--run',run,'--loop','asyncio']
    else:command=[sys.executable,str(ROOT/'openfold_control.py'),'--name',name,'--run',run]
    subprocess.run(command,check=True);op('ready',name);op('request',name,'--cases',warm,'--label','warmup');op('request',name,'--cases',cases,'--label','measured');op('release',name)
name=f'fs2-bioir-snapshot-{a.model}-fallback'
op('restore',name,'--source',a.source,'--fallback');op('ready',name);op('request',name,'--cases',cases,'--label','fallback');op('release',name)
print('MATRIX COMPLETE',a.model,flush=True)
