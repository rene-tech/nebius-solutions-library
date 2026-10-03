"""Download the complete public MPINAT suite and build immutable API fixtures.

Only the finite benchmark length is derived with convert-tpr at execution time;
the original TPR is preserved byte-for-byte. No force-field/constraint edits.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import tarfile
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen
import zipfile

SOURCE = "https://www.mpinat.mpg.de/grubmueller/bench"
ATTRIBUTION = ("Dept. of Theoretical and Computational Biophysics, Max Planck Institute "
               "for Multidisciplinary Sciences, Göttingen, " + SOURCE)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        title = values.get("title", "")
        if tag == "a" and title.startswith(("bench", "CMET (", "HIF2A (", "SHP2 (")):
            url = urljoin(SOURCE, values["href"])
            name = Path(urlparse(url).path).name.removesuffix(".zip")
            if urlparse(url).hostname != "www.mpinat.mpg.de":
                raise ValueError("Unexpected benchmark download host")
            self.rows.append({"id": name.lower().replace("_", "-"), "name": name,
                              "title": title, "url": url})


def parameters(case, steps=10000, repetitions=3, mpi_nodes=None):
    """Repeat identical benchmark states, not independent scientific samples."""
    commands = [{"id": "finite-tpr", "command": "convert-tpr",
                 "args": ["-s", "original.tpr", "-o", "benchmark.tpr", "-nsteps", str(steps)],
                 "expected_outputs": ["benchmark.tpr"]}]
    for rep in range(1, repetitions + 1):
        # Leave native automatic dispatch for free-energy systems. For standard
        # all-bonds inputs explicitly retain CPU update; PEP-h may use GPU update.
        args = ["-s", "benchmark.tpr", "-deffnm", f"repeat{rep}", "-resethway", "-nb", "gpu"]
        if case["id"] in {"benchmem", "benchpep", "benchrib"}:
            args += ["-update", "cpu"]
        if case["id"] == "benchpep-h" and not mpi_nodes:
            args += ["-pme", "gpu", "-update", "gpu", "-bonded", "gpu"]
        commands += [{"id": f"repeat-{rep}", "command": "mdrun", "args": args},
                     {"id": f"join-energy-{rep}", "command": "eneconv",
                      "args": ["-f", {"files": f"repeat{rep}.part*.edr"}, "-o", f"energy{rep}.edr"],
                      "expected_outputs": [f"energy{rep}.edr"]},
                     {"id": f"energy-{rep}", "command": "energy",
                      "args": ["-f", f"energy{rep}.edr", "-o", f"energy{rep}.xvg"],
                      "stdin": "Potential\nTemperature\n0\n", "expected_outputs": [f"energy{rep}.xvg"]}]
    value = {"schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
             "jobs": [{"id": "benchmark", "steps": commands}], "threads": 8,
             "checkpoint_minutes": 5, "segment_minutes": 60, "max_wall_seconds": 21600,
             "max_output_bytes": 4 * 1024**3, "output_destination": "customer-bucket",
             "output_prefix": "runs/gromacs-mpinat-20261003"}
    if mpi_nodes:
        value["schema"] = "fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1"
        value["nodes"] = mpi_nodes
        value["jobs"][0]["id"] = "gang"
    return value


def prepare(row, root):
    out = root / row["id"]
    out.mkdir(parents=True, exist_ok=True)
    download = out / "upstream.zip"
    if not download.exists():
        with urlopen(row["url"], timeout=120) as response, download.with_suffix(".partial").open("wb") as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
        download.with_suffix(".partial").replace(download)
    content = download.read_bytes()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        tprs = [item for item in entries if item.filename.endswith(".tpr")]
        if len(tprs) != 1 or tprs[0].file_size > 1024**3:
            raise ValueError(f"{row['id']}: inspect archive structure before deriving a fixture")
        tpr = archive.read(tprs[0])
        row.update(zip_members=[item.filename for item in entries], original_member=tprs[0].filename,
                   archive_sha256=sha(content), tpr_sha256=sha(tpr), tpr_bytes=len(tpr),
                   license="CC-BY-4.0", attribution=ATTRIBUTION)
    (out / "original.tpr").write_bytes(tpr)
    memory = io.BytesIO()
    provenance = json.dumps(row, indent=2).encode() + b"\n"
    with tarfile.open(fileobj=memory, mode="w") as archive:
        for name, data in (("original.tpr", tpr), ("provenance.json", provenance)):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, 0
            archive.addfile(info, io.BytesIO(data))
    bundle = gzip.compress(memory.getvalue(), mtime=0)
    (out / "input.tar.gz").write_bytes(bundle)
    row.update(bundle_sha256=sha(bundle), bundle_bytes=len(bundle))
    save(out / "provenance.json", row)
    save(out / "parameters.json", parameters(row))
    print(json.dumps({"prepared": row["id"], "tpr_bytes": len(tpr), "sha256": row["tpr_sha256"]}), flush=True)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    parser = Links()
    with urlopen(SOURCE, timeout=60) as response:
        html = response.read()
    parser.feed(html.decode())
    if len(parser.rows) != 24 or len({r["id"] for r in parser.rows}) != 24:
        raise ValueError("Upstream benchmark roster changed; inspect before running")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "source.html").write_bytes(html)
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(lambda row: prepare(row, args.output), parser.rows))
    save(args.output / "suite.json", {"source": SOURCE, "cases": rows, "steps": 10000,
                                     "warmup": "reset halfway", "repetitions": 3,
                                     "scientific_convergence_claimed": False})


if __name__ == "__main__":
    main()
