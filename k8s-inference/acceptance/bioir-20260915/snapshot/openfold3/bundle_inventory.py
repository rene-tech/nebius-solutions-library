"""Read-only fingerprint of only the task-owned snapshot PVC."""
import hashlib
import json
from pathlib import Path
import time

root = Path('/bundle')
started = time.time()
rows = []
for path in sorted(root.rglob('*')):
    if not path.is_file() or path.is_symlink():
        continue
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    rows.append({'path': str(path.relative_to(root)), 'bytes': path.stat().st_size, 'sha256': digest.hexdigest()})
print(json.dumps({'started_unix': started, 'completed_unix': time.time(), 'total_file_bytes': sum(v['bytes'] for v in rows), 'files': rows}))
