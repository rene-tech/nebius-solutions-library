"""Exact image, retained H100 child bytes: positive geometry and negative old resize."""
import hashlib
import json
from pathlib import Path
import sys
import av
import numpy as np
from fs2_lerobot_augmentation.cosmos import CosmosClient,CosmosError,_validate_transfer_video_alignment,conditioning_scope
from fs2_lerobot_augmentation.contracts import AugmentationRequest,Selection
from fs2_lerobot_augmentation.dataset import open_and_validate,rewrite_variant,package_dataset
sys.path.insert(0,'/input/fixtures')
from validate_variant import compare_variant

base=Path('/input'); out=Path('/output')
manifest=json.loads((base/'manifest.json').read_bytes())
request=AugmentationRequest.parse(json.loads((base/'request.json').read_bytes()))
source=open_and_validate(base/'source',repo_id='fs2qualification/recorded-aloha',selection=Selection(episodes='all',cameras='all'))
checked=[]; replacements={}
for row in manifest['positive']:
    path=base/row['name']
    assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
    _validate_transfer_video_alignment(path,width=640,height=480,frames=64,fps=25)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']
    replacements[(row['episode_index'],'observation.images.cam_high')]=path
    checked.append({'name':row['name'],'sha256':row['sha256'],'geometry':'640x480/64frames/25FPS','unchanged':True})
rejected=[]
for row in manifest['negative']:
    path=base/row['name']; before=hashlib.sha256(path.read_bytes()).hexdigest()
    assert before==row['sha256']
    try:
        _validate_transfer_video_alignment(path,width=640,height=480,frames=64,fps=25)
    except CosmosError as error:
        assert error.code=='COSMOS_MEDIA_ALIGNMENT_INVALID' and not error.retryable
        rejected.append({'name':row['name'],'sha256':before,'code':error.code})
    else: raise AssertionError('Old448x256 child incorrectly accepted as640x480')
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
scope=conditioning_scope(request.augmentation)
payload=CosmosClient._video_payload(reference={'artifact_id':'fixture'},prompt='cool lighting',negative_prompt='',
 width=640,height=480,frames=64,fps=25,seed=1,augmentation=request.augmentation)
assert payload['size']=='640x480' and payload['fps']==25 and payload['mode']=='transfer-video'
provenance={'source':{'tree_sha256':source.tree_sha256},'scientific_scope':scope,
 'operations':[{'episode_index':row['episode_index'],'camera':'observation.images.cam_high',
                'operation_id':row['operation_id']} for row in manifest['positive']],
 'scope':'CPU writer regression using retained H100 V2V outputs; not a new transfer GPU or public acceptance'}
variant=rewrite_variant(source,output_root=out/'variant-00',output_repo_id='fs2qualification/exact-alignment',
 video_replacements=replacements,action_replacements={},provenance=provenance)
validation=compare_variant(source,variant,replacements=set(replacements),provenance=provenance)
artifact=package_dataset(variant.root,out/'variant-00.tar.zst')
receipt={'status':'passed','runtime_image':manifest['runtime_image'],'source_commit':manifest['source_commit'],
 'source_tree_sha256':source.tree_sha256,'positive_unchanged':checked,'rejected_mismatched_geometry':rejected,
 'validation':validation,'scientific_scope':scope,'payload':payload,
 'output_artifact':{'sha256':artifact.sha256,'size_bytes':artifact.size_bytes},
 'qualification_scope':'Exact CPU coordinator image plus retained real H100 child bytes; no new GPU call, no public transfer proof.'}
(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'status':'passed','positive':len(checked),'negative':len(rejected),'frames':validation['frames'],
                  'nonvideo_values':validation['nonvideo_values_compared']}))
