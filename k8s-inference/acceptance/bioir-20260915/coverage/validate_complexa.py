#!/usr/bin/env python3
"""Independently validate saved coordinates and native full-workflow metrics."""
import collections,csv,io,json,math,tarfile
from control import ROOT,save
metrics=collections.defaultdict(list);structures=[];csvs=[];success_rows=0;metadata_steps=set()
with tarfile.open(ROOT/'artifacts/proteina-complexa-workspace.tgz') as archive:
    for member in archive.getmembers():
        if not member.isfile():continue
        if member.name.endswith('.pdb'):
            lines=archive.extractfile(member).read().decode().splitlines();atoms=[line for line in lines if line.startswith(('ATOM  ','HETATM'))]
            coords=[[float(line[pos:pos+8]) for pos in [30,38,46]] for line in atoms]
            assert atoms and all(math.isfinite(x) for row in coords for x in row),member.name
            ca={(line[21],line[22:27]) for line in atoms if line[12:16].strip()=='CA'}
            assert ca,member.name
            structures.append({'path':member.name,'atoms':len(atoms),'ca_residues':len(ca),'chains':sorted({c for c,_ in ca}),'all_coordinates_finite':True})
        if member.name.endswith('/binder_results_search_binder_local_pipeline_0.csv'):
            rows=list(csv.DictReader(io.StringIO(archive.extractfile(member).read().decode())));assert rows
            keys=['self_complex_pLDDT','self_complex_pTM','self_complex_i_pTM','self_complex_pAE','self_binder_scRMSD_ca','self_complex_scRMSD_ca']
            for row in rows:
                metadata_steps.add(row['generation_args_nsteps'])
                for key in keys:
                    value=float(row[key]);assert math.isfinite(value);metrics[key].append(value)
            csvs.append({'path':member.name,'rows':len(rows),'validated_metrics':keys})
        if member.name.endswith('/all_successes_protein_binder_self.csv'):
            success_rows+=len(list(csv.DictReader(io.StringIO(archive.extractfile(member).read().decode()))))
out={'all_valid':True,'coordinate_files':structures,'evaluation_csvs':csvs,'metrics':{key:{'n':len(values),'min':min(values),'max':max(values),'values':values} for key,values in metrics.items()},'native_success_csv_rows':success_rows,'scope':'All saved artifacts, including one transport-truncated completed run and its repeated timed recovery; do not use these artifact counts as timed request count.','metadata_generation_args_nsteps':sorted(metadata_steps),'actual_generation_nsteps':25,'metadata_caveat':'Evaluation stage reloads default generation metadata (400), whereas retained generate argv/logs show25 executed steps. Native metadata is preserved, not silently corrected.','quality_note':'Finite structures/full-stage completion is not scientific binder success. Native pass counts and low confidence are reported, with no efficacy claim. Protein-target variant only; ligand/AME variants not measured.'}
save('raw/proteina-complexa-independent-validation.json',out)
print(json.dumps({'pdbs':len(structures),'evaluation_csvs':len(csvs),'native_success_csv_rows':success_rows,'metrics':{k:{q:v[q] for q in ['n','min','max']} for k,v in out['metrics'].items()}},indent=2))
