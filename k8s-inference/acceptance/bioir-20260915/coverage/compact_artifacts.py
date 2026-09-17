#!/usr/bin/env python3
"""Keep logs and scientific outputs in git; retain redundant schedule caches locally."""
import hashlib,pathlib,shutil,tarfile
from control import ROOT,save
source=ROOT/'artifacts/rfdiffusion-workspace.tgz'
retained=pathlib.Path('/home/tux/.local/state/fs2-bioir-evaluation-20260915/coverage/rfdiffusion-full-workspace.tgz')
retained.parent.mkdir(parents=True,exist_ok=True)
assert not retained.exists()
shutil.move(source,retained)
omitted=[]
with tarfile.open(retained,'r:gz') as original,tarfile.open(source,'w:gz') as compact:
    for member in original:
        if '/scratch/schedules/' in member.name:
            omitted.append({'path':member.name,'bytes':member.size});continue
        compact.addfile(member,original.extractfile(member) if member.isfile() else None)
save('artifacts/rfdiffusion-receipt.json',{'path':str(source.relative_to(ROOT)),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'bytes':source.stat().st_size,'full_archive_retained_at':str(retained),'full_archive_sha256':hashlib.sha256(retained.read_bytes()).hexdigest(),'omitted_from_compact_only':omitted,'note':'All generated structures, trajectories, .trb metadata, request/result envelopes and logs preserved. Only redundant diffusion schedule caches omitted from committed compact archive.'})
print('compact bytes',source.stat().st_size,'full retained bytes',retained.stat().st_size)
