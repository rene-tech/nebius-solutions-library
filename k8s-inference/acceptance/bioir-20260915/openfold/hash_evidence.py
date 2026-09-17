#!/usr/bin/env python3
"""Write reproducibility hashes for scripts, manifests and public inputs."""
import hashlib
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parent
files = sorted([*ROOT.glob('*.py'), *ROOT.glob('manifests/*.json'), *ROOT.glob('fixtures/*')])
rows = [{'path': str(p.relative_to(ROOT)), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size} for p in files if p.is_file()]
(ROOT / 'source-input-hashes.json').write_text(json.dumps({'algorithm': 'sha256', 'files': rows}, indent=2) + '\n')
print(json.dumps({'hashed_files': len(rows)}))
