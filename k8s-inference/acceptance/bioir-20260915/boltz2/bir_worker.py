"""Public BioIR candidate retaining the current Boltz2 HTTP request schema."""
import json
import os
import tempfile
import time
from pathlib import Path

import torch
import server
from bionemo_ir.data.schemas import InputRequest, MSARecord, Polymer
from bionemo_ir.data.utils import get_all_atom_types, get_all_residue_types
from bionemo_ir.data.writers import CIFWriter
from bionemo_ir.pipeline.processor.engine_proc import EngineProcessorConfig, build_processor
from bionemo_ir.pipeline.stages.configs import FeatureGeneratorStageConfig
from bionemo_ir.pipeline.stages.base import unpack_pipeline_row
from bionemo_ir.models.optimize_module_setter import AcceleratedConfig

PROCESSOR = None
ENGINE = None
REQUEST_INDEX = 0


def graph_state():
    result = []
    for name, module in ENGINE.model.named_modules():
        states = getattr(module, "graph_state_by_key", None)
        if states is not None:
            result.append({"module": name, "class": type(module).__name__,
                           "states": [{"state": str(getattr(value, "preparation_state", "unknown")),
                                       "calls": getattr(value, "num_prev_calls_by_input_key", None),
                                       "captured": getattr(value, "graph", None) is not None,
                                       "working_set_bytes": getattr(value, "working_set_bytes", None)}
                                      for value in states.values()]})
    return result


def prepare():
    global PROCESSOR, ENGINE
    started = time.monotonic()
    config = EngineProcessorConfig(
        model_source="boltz-2",
        runtime_args={"recycling_steps": 3, "num_sampling_steps": 200, "diffusion_samples": 1},
        feature_generator_stage=FeatureGeneratorStageConfig(init_context={"random_seed": 42}),
        writer_stage=False,
        engine_kwargs={"profile_inference": True,
                       "accelerated_configs": {"diffusion_module": AcceleratedConfig(backend="torch")}},
    )
    PROCESSOR = build_processor(config)
    # In public0.1.0 the builder appends WriterStage even when its config is
    # false. Our HTTP writer must consume all samples from the engine.
    PROCESSOR.stages.pop("WriterStage")
    for name, stage in PROCESSOR.stages.items():
        udf = PROCESSOR._get_or_create_udf(name, stage)
        if hasattr(udf, "folding"):
            ENGINE = udf.folding.engine
    if ENGINE is None:
        raise RuntimeError("BioIR folding engine missing")
    original_postprocess = ENGINE.postprocessor

    def all_samples(batch, output):
        # The public pipeline selects one best sample; the current HTTP API
        # returns every requested sample. Select each existing sample without
        # rerunning the model or changing the diffusion sample count.
        confidence = output["confidence_score"]
        samples = []
        for sample_index in range(output["coords"].shape[1]):
            selector = torch.full_like(confidence, -1e6)
            selector[0, sample_index] = 1e6
            row = original_postprocess(batch, {**output, "confidence_score": selector})
            iptm = row["iptm"]
            row["confidence_score"] = (4 * row["complex_plddt"] + (iptm if iptm else row["ptm"])) / 5
            samples.append(dict(row))
        samples.sort(key=lambda item: item["confidence_score"], reverse=True)
        return {"samples": samples}

    ENGINE.postprocessor = all_samples
    server.RUNTIME.startup_seconds = time.monotonic() - started
    server.RUNTIME.compute_capability = ".".join(map(str, torch.cuda.get_device_capability()))
    server.RUNTIME.artifact_sha256 = {"boltz2_conf.ckpt": os.environ["BOLTZ_CONF_SHA256"]}
    server.RUNTIME.ready = True
    print("EVAL_READY " + json.dumps({"startup_seconds": server.RUNTIME.startup_seconds,
                                      "engine_config": config.model_dump(mode="json")}), flush=True)


def predict(request):
    global REQUEST_INDEX
    started = time.monotonic()
    seed = 42 + REQUEST_INDEX
    REQUEST_INDEX += 1
    for udf in PROCESSOR._udf_instances.values():
        if hasattr(udf, "init_context"):
            udf.init_context = {"random_seed": seed}
    ENGINE.runtime_args.update(recycling_steps=request.recycling_steps,
                               num_sampling_steps=request.sampling_steps,
                               diffusion_samples=request.diffusion_samples)
    record = InputRequest(input_id="request", polymers=[
        Polymer(polymer_type="protein", chain_id=[p.id], sequence=p.sequence,
                msas=[MSARecord(content=p.msa.msa_search.a3m.alignment)]) for p in request.polymers])
    result = unpack_pipeline_row(PROCESSOR([{"record": record, "__record_id": "request"}])[0])
    pipeline_done = time.monotonic()
    structures, confidence_scores, ptm_scores = [], [], []
    with tempfile.TemporaryDirectory(prefix="bioir-boltz2-") as temporary:
        for index, sample in enumerate(result["samples"]):
            writer = CIFWriter(res_type_mapping=dict(enumerate(get_all_residue_types("boltz-2"))),
                               atom_type_mapping=dict(enumerate(get_all_atom_types("boltz-2"))),
                               output_path=str(Path(temporary) / f"sample-{index}.cif"))
            structures.append({"format": "mmcif", "structure": writer.write(sample)})
            confidence_scores.append(sample["confidence_score"])
            ptm_scores.append(sample["ptm"])
    print("EVAL_PHASES " + json.dumps({"stage_timing_s": result.get("stage_timing_s"),
          "evaluation_seed": seed,
          "model_inference_time": result.get("model_inference_time"),
          "runtime_args": ENGINE.runtime_args,
          "graph_state": graph_state(),
          "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
          "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
          "pipeline_seconds": pipeline_done - started,
          "writer_seconds": time.monotonic() - pipeline_done}), flush=True)
    return {"structures": structures, "confidence_scores": confidence_scores, "ptm_scores": ptm_scores}


server._prepare_runtime = prepare
server._predict = predict
app = server.app
