"""Evaluation adapter using the deployed parser/model/writer with a resident model."""
import contextlib
import io
import json
import os
import subprocess
import time

import torch
import boltz.main as upstream
import server
from lightning_fabric.utilities.apply_func import move_data_to_device

MODELS = {}
PHASES = {}
ORIGINAL_LOADER = upstream.Boltz2.load_from_checkpoint
ORIGINAL_RUN = subprocess.run
NO_KERNELS = os.environ.get("EVAL_NO_KERNELS", "1") == "1"
REQUEST_INDEX = 0


def resident_load(checkpoint, **kwargs):
    start = time.monotonic()
    key = str(checkpoint)
    if key not in MODELS:
        MODELS[key] = ORIGINAL_LOADER(checkpoint, **kwargs).eval().cuda()
        MODELS[key].requires_grad_(False)
    else:
        MODELS[key].predict_args = kwargs["predict_args"]
    torch.cuda.synchronize()
    PHASES["model_load_seconds"] = time.monotonic() - start
    return MODELS[key]


class ResidentPredictor:
    """Preserve upstream predict_step and writer without Lightning teardown."""
    def __init__(self, callbacks, **kwargs):
        self.callbacks = callbacks

    def predict(self, model, datamodule, **kwargs):
        start = time.monotonic()
        datamodule.setup("predict")
        loader = datamodule.predict_dataloader()
        for index, batch in enumerate(loader):
            device_batch = datamodule.transfer_batch_to_device(batch, torch.device("cuda:0"), 0)
            torch.cuda.synchronize()
            forward_start = time.monotonic()
            with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.bfloat16):
                prediction = model.predict_step(device_batch, index)
            torch.cuda.synchronize()
            PHASES["forward_seconds"] = time.monotonic() - forward_start
            write_start = time.monotonic()
            for writer in self.callbacks:
                writer.write_on_batch_end(self, model, prediction, None, device_batch, index, 0)
            PHASES["writer_seconds"] = time.monotonic() - write_start
        PHASES["dataloader_predict_writer_seconds"] = time.monotonic() - start


def inprocess_run(command, **kwargs):
    global REQUEST_INDEX
    if command[:2] != ["boltz", "predict"]:
        return ORIGINAL_RUN(command, **kwargs)
    PHASES.clear()
    args = list(command[2:])
    seed = 42 + REQUEST_INDEX
    REQUEST_INDEX += 1
    args += ["--seed", str(seed)]
    PHASES["evaluation_seed"] = seed
    if not NO_KERNELS:
        args.remove("--no_kernels")
    buffer = io.StringIO()
    started = time.monotonic()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        upstream.predict.main(args=args, standalone_mode=False)
    PHASES["inprocess_total_seconds"] = time.monotonic() - started
    print("EVAL_PHASES " + json.dumps(PHASES), flush=True)
    return subprocess.CompletedProcess(command, 0, buffer.getvalue(), "")


upstream.Boltz2.load_from_checkpoint = resident_load
upstream.Trainer = ResidentPredictor
server.subprocess.run = inprocess_run
app = server.app
