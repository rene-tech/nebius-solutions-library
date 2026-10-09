"""Retain exact package/help, library architecture and target GPU provenance."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from cluster import owned
from mirror_runtime import KUBE, SOURCE, TARGET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pod = owned(args.pod)
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    gpu = subprocess.check_output(KUBE + ["exec", args.pod, "--", "nvidia-smi", "--query-gpu=name,uuid,driver_version,compute_cap,memory.total,pci.bus_id", "--format=csv"], text=True)
    (output / "gpu.txt").write_text(gpu)
    inventories = {}
    for build in ("sm86", "sm90"):
        help_text = subprocess.check_output(KUBE + ["exec", args.pod, "--", "env", "LD_LIBRARY_PATH=/usr/local/lammps/" + build + "/lib:/usr/local/cuda/lib:/usr/local/cuda/lib64:/usr/local/fftw/lib", "/usr/local/lammps/" + build + "/bin/lmp", "-h"], text=True)
        (output / (build + "-help.txt")).write_text(help_text)
        library = output / ("liblammps-" + build + ".so")
        if not library.is_file():
            subprocess.run(KUBE + ["cp", args.pod + ":/usr/local/lammps/" + build + "/lib/liblammps.so.0", str(library)], check=True)
        cubins = subprocess.check_output(["/usr/local/cuda-12.8/bin/cuobjdump", "-lelf", str(library)], text=True)
        ptx = subprocess.check_output(["/usr/local/cuda-12.8/bin/cuobjdump", "-lptx", str(library)], text=True)
        (output / (build + "-cubins.txt")).write_text(cubins)
        (output / (build + "-ptx.txt")).write_text(ptx)
        packages = help_text.split("Installed packages:", 1)[1].split("List of individual", 1)[0].split()
        inventories[build] = {"packages": packages, "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(), "native_cubin_architectures": sorted(set(re.findall(r"\.sm_([0-9]+)\.cubin", cubins))), "native_cubin_count": len(cubins.splitlines()), "ptx_count": len(ptx.splitlines()), "required_styles": {name: name in help_text.split() for name in ("lj/cut/kk", "eam/kk", "reaxff/kk", "qeq/reaxff/kk", "lj/charmm/coul/long/kk", "snap/kk", "tersoff/kk", "pppm/kk", "shake/kk")}}
    receipt = {"source": SOURCE, "regional_mirror": TARGET, "verified_mirror_digest": subprocess.check_output(["crane", "digest", TARGET], text=True).strip(), "pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"], "runtime_image": pod["spec"]["containers"][0]["image"], "image_id": pod["status"]["containerStatuses"][0]["imageID"], "gpu": gpu, "builds": inventories, "upstream_source_revision": "c7ae612a9497437412cb787b78769570f48653dd", "upstream_tag": "stable_22Jul2025", "cuda_package_version": "12.8", "gpu_package_built": False, "sm89_native_build_present": False, "same_process_suspend_is_persistent_snapshot": False}
    (output / "inventory.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
