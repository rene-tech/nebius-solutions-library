"""Task pod owns the scheduled GPU; reject any pre-existing GPU process."""
import json
import subprocess
import time

def query(fields, kind="gpu"):
    result = subprocess.run(["nvidia-smi", f"--query-{kind}={fields}", "--format=csv,noheader"],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()

gpu = query("name,uuid,driver_version,memory.used,utilization.gpu")
processes = query("pid,process_name,used_memory", "compute-apps")
print("EVAL_GPU_PREFLIGHT " + json.dumps({"unix": time.time(), "gpu": gpu, "processes": processes}), flush=True)
if processes:
    raise RuntimeError("Assigned GPU already has compute processes; evaluation did not start")
