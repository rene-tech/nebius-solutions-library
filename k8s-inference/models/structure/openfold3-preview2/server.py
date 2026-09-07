"""Persistent, upstream OpenFold3 Preview2 adapter; this is not NVIDIA NIM.

The native 20-aa structural contract is retained. A fresh upstream trainer and
data module are built per request, while the exact loaded GPU module is reused.
No model kernels, checkpoint tensors, diffusion steps or scores are substituted.
"""
from __future__ import annotations

import datetime as dt
import importlib.metadata
import json
import logging
import math
import re
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ARTIFACTS = Path("/opt/fs2/openfold3-preview2-artifacts")
SOURCE_REVISION = "4a0eaeaeae8ca1d815d0a97d8eb45d639b91a47e"
SCORES = {"confidence_score": "sample_ranking_score", "complex_plddt_score": "avg_plddt",
          "complex_pde_score": "gpde", "ptm_score": "ptm", "iptm_score": "iptm"}


def event(phase, **fields):
    print(json.dumps({"phase": phase, "time": dt.datetime.now(dt.timezone.utc).isoformat(), **fields}), flush=True)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", value):
        raise ValueError("request, input and chain identifiers must be simple labels")
    return value


def prepare_request(payload, directory):
    request_id = identifier(payload["request_id"])
    if set(payload) != {"request_id", "inputs"} or len(payload["inputs"]) != 1:
        raise ValueError("Preview2 adapter currently supports exactly one input")
    item = payload["inputs"][0]
    if set(item) != {"input_id", "output_format", "molecules"} or item["output_format"] != "cif":
        raise ValueError("Expected native input_id, output_format=cif and molecules")
    input_id = identifier(item["input_id"])
    chains, has_msa = [], False
    for index, molecule in enumerate(item["molecules"]):
        if set(molecule) - {"type", "id", "sequence", "diffusion_samples", "msa"}:
            raise ValueError("Unsupported molecule fields; nothing is silently discarded")
        if molecule["type"] != "protein" or molecule.get("diffusion_samples", 1) != 1:
            raise ValueError("This qualified adapter supports protein chains and one diffusion sample")
        sequence = molecule["sequence"]
        if not isinstance(sequence, str) or not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", sequence):
            raise ValueError("Expected a nonempty canonical protein sequence")
        chain = {"molecule_type": "protein", "chain_ids": [identifier(molecule["id"])], "sequence": sequence}
        if "msa" in molecule:
            msa = molecule["msa"]
            if set(msa) != {"main"} or set(msa["main"]) != {"a3m"}:
                raise ValueError("Only inline main A3M alignments are supported")
            a3m = msa["main"]["a3m"]
            if set(a3m) != {"alignment", "format"} or a3m["format"] != "a3m" or not isinstance(a3m["alignment"], str):
                raise ValueError("Expected inline A3M alignment")
            chain_directory = directory / f"chain-{index}"
            chain_directory.mkdir()
            path = chain_directory / "inline_main.a3m"
            path.write_text(a3m["alignment"] + "\n")
            chain["main_msa_file_paths"] = [str(path)]
            has_msa = True
        chains.append(chain)
    if not chains or len({chain["chain_ids"][0] for chain in chains}) != len(chains):
        raise ValueError("Expected one or more uniquely identified protein chains")
    query = {"seeds": [42], "queries": {input_id: {"chains": chains, "use_msas": has_msa,
              "use_main_msas": has_msa, "use_paired_msas": False}}}
    return request_id, input_id, query


def scored_structure(request_id, cif_path, confidence):
    scores = {key: float(confidence[source]) for key, source in SCORES.items()}
    if not all(math.isfinite(value) for value in scores.values()):
        raise ValueError("Upstream confidence output contains a nonfinite score")
    return {"format": "cif", "name": request_id + "-preview2-seed42-sample1",
            "source": "aqlaboratory/openfold-3 Preview2 0.4.2; of3-p2-155k (not NVIDIA NIM)",
            "structure": cif_path.read_text(), **scores}


class Runtime:
    ready = False
    error = None

    def __init__(self):
        self.lock = threading.Lock()

    def runner(self, output):
        from openfold3.entry_points.experiment_runner import InferenceExperimentRunner
        from openfold3.entry_points.validator import InferenceExperimentConfig
        config = InferenceExperimentConfig(
            inference_ckpt_path=ARTIFACTS / "of3-p2-155k.pt",
            # Preview2's explicit ccd_file_path invokes the text CIF reader.
            # Its native BiotiteCCDWrapper reads our set_ccd_path() binary CCD.
            dataset_config_kwargs={"ccd_file_path": None, "msa": {
                # Preview2 keys its parser and concatenation order by basename.
                # Keep its native 16384-row main-MSA cap and the original bytes.
                "max_seq_counts": {"inline_main": 16384}, "aln_order": ["inline_main"],
            }},
            data_module_args={"num_workers": 0, "num_workers_validation": 0, "multiprocessing_context": None},
        )
        return InferenceExperimentRunner(config, num_diffusion_samples=1, use_msa_server=False,
                                         use_templates=False, output_dir=output)

    def load(self):
        try:
            event("IMPORT_BEGIN")
            if importlib.metadata.version("openfold3") != "0.4.2":
                raise RuntimeError("Preview2 requires the pinned 0.4.2 package, not OpenBind")
            import torch
            from biotite.structure.info.ccd import set_ccd_path
            from openfold3.entry_points.import_utils import _torch_gpu_setup
            _torch_gpu_setup()
            set_ccd_path(str(ARTIFACTS / "components.bcif"))
            logging.basicConfig(level=logging.INFO)
            self.startup_directory = tempfile.TemporaryDirectory(prefix="of3-preview2-startup-")
            runner = self.runner(Path(self.startup_directory.name))
            event("WEIGHT_LOAD_BEGIN", checkpoint="of3-p2-155k.pt")
            runner.setup()
            self.module = runner.lightning_module.eval().cuda()
            torch.cuda.synchronize()
            self.identity = {"model": "openfold3", "runtime_origin": "upstream-source-preview2",
                "package": "openfold3==0.4.2", "source_revision": SOURCE_REVISION,
                "artifacts": json.loads((ARTIFACTS / "manifest.json").read_text()),
                "torch": torch.__version__, "cuda": torch.version.cuda,
                "gpu": torch.cuda.get_device_name(), "precision": "upstream predict preset; trainer 32-true",
                "diffusion_samples": 1, "seed": 42, "external_msa_server": False, "templates": False}
            self.ready = True
            event("MODEL_READY", **self.identity)
        except Exception as exc:
            self.error = str(exc)
            event("LOAD_FAILED", error=self.error)
            traceback.print_exc()

    def predict(self, payload):
        from openfold3.projects.of3_all_atom.config.inference_query_format import InferenceQuerySet
        with self.lock, tempfile.TemporaryDirectory(prefix="of3-preview2-request-") as temporary:
            started = time.monotonic()
            directory = Path(temporary)
            request_id, input_id, query = prepare_request(payload, directory)
            runner = self.runner(directory / "output")
            # cached_property injection keeps the already-loaded exact GPU model;
            # fresh upstream data module, callbacks and trainer isolate each input.
            runner.__dict__["lightning_module"] = self.module
            event("INFERENCE_BEGIN", request_id=request_id)
            runner.run(InferenceQuerySet.model_validate(query))
            # Lightning's ordinary predict teardown moves the module to CPU.
            # Restore its GPU residency before publishing the completed response;
            # this is a device transfer of the same tensors, not a checkpoint load.
            import torch
            self.module.cuda()
            torch.cuda.synchronize()
            cifs = list((directory / "output").rglob("*_model.cif"))
            confidence_files = list((directory / "output").rglob("*_confidences_aggregated.json"))
            if len(cifs) != 1 or len(confidence_files) != 1:
                raise RuntimeError("Upstream output cardinality differs from one requested sample")
            result = scored_structure(request_id, cifs[0], json.loads(confidence_files[0].read_text()))
            elapsed = time.monotonic() - started
            event("INFERENCE_COMPLETE", request_id=request_id, seconds=elapsed)
            return {"request_id": request_id, "outputs": [{"input_id": input_id,
                    "runtime_metrics": {"inference_seconds": elapsed, "runtime_origin": "upstream-source-preview2"},
                    "structures_with_scores": [result]}]}


RUNTIME = Runtime()


class Handler(BaseHTTPRequestHandler):
    def send(self, code, value):
        data = json.dumps(value, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/health/live":
            self.send(200, {"status": "live"})
        elif self.path == "/v1/health/ready":
            self.send(200 if RUNTIME.ready else 503, {"status": "ready" if RUNTIME.ready else "loading", "error": RUNTIME.error})
        elif self.path == "/v1/runtime" and RUNTIME.ready:
            self.send(200, RUNTIME.identity)
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/biology/openfold/openfold3/predict":
            return self.send(404, {"error": "not found"})
        if not RUNTIME.ready:
            return self.send(503, {"error": "model not ready"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8 * 1024 * 1024:
                raise ValueError("Expected nonempty JSON body up to 8 MiB")
            self.send(200, RUNTIME.predict(json.loads(self.rfile.read(length))))
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self.send(500, {"error": str(exc)})


if __name__ == "__main__":
    event("PROCESS_START")
    threading.Thread(target=RUNTIME.load, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
