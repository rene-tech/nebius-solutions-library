#!/usr/bin/env bash
# Offline checks for the Complexa artifact ingestion.
#
# Promotion tests need the reviewed localization successor. Point
# FS2_LOCALIZATION_ADAPTERS at the directory holding its localization.py and
# primitives.py while that work is on its own branch; once it lands here the
# tests find it without the variable and the skip disappears.
set -euo pipefail

cd "$(dirname "$0")"

python3 -m py_compile fetch_artifacts.py promote_generations.py render_ingestion_jobs.py
python3 -c "
import json, pathlib, sys
sys.path.insert(0, '.')
import fetch_artifacts
document = fetch_artifacts.load_contract(pathlib.Path('ingestion-contract.json'))
print('contract valid:', len(document['artifacts']), 'artifacts,',
      sum(a['tree']['total_bytes'] for a in document['artifacts']), 'bytes')
"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
