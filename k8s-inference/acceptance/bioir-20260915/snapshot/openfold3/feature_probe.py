import subprocess
import sys
result = subprocess.run([sys.executable, '/snapshot-app/probe_contract.py', '--model', 'openfold3', '--output', '/tmp/of3-feature-probes.json', '--cases', '/snapshot-app/cases.json'], check=True, capture_output=True, text=True)
print(result.stdout)
