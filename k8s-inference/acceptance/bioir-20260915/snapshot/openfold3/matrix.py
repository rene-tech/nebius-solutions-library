#!/usr/bin/env python3
"""Three sequential fresh restores, three identical-harness normals and fallback."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
p = argparse.ArgumentParser()
p.add_argument('--source', default='fs2-bioir-of3-donor')
a = p.parse_args()
def op(action, name, *extra):
    subprocess.run([sys.executable, str(ROOT / 'operate.py'), action, name, *extra], check=True)
op('release', a.source)
for index in range(1, 4):
    name = f'fs2-bioir-of3-restore-{index}'
    op('restore', name, '--source', a.source)
    op('ready', name)
    op('request', name, '--cases', '1crn,1lyz,T1031_msa', '--label', 'measured')
    op('release', name)
for index in range(1, 4):
    name = f'fs2-bioir-of3-normal-{index}'
    subprocess.run([sys.executable, str(ROOT / 'control.py'), '--name', name, '--run', f'of3-normal-{index}'], check=True)
    op('ready', name)
    op('request', name, '--cases', '1crn', '--label', 'warmup')
    op('request', name, '--cases', '1crn,1lyz,T1031_msa', '--label', 'measured')
    op('release', name)
name = 'fs2-bioir-of3-fallback'
op('restore', name, '--source', a.source, '--fallback')
op('ready', name)
op('request', name, '--cases', '1crn,1lyz,T1031_msa', '--label', 'fallback')
op('release', name)
print('OpenFold3 snapshot matrix complete', flush=True)
