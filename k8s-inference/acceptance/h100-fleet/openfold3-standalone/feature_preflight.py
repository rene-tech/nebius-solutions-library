"""Run exact original Preview2 feature preparation without consuming a GPU."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/opt/fs2/openfold3-preview2")
from server import ARTIFACTS, Runtime, prepare_request
from biotite.structure.info.ccd import set_ccd_path
from openfold3.projects.of3_all_atom.config.inference_query_format import InferenceQuerySet

set_ccd_path(str(ARTIFACTS / "components.bcif"))
with tempfile.TemporaryDirectory(prefix="preview2-cpu-features-") as temporary:
    directory = Path(temporary)
    payload = json.loads(Path("/fixture/request.json").read_text())
    _, _, query = prepare_request(payload, directory)
    runner = Runtime().runner(directory / "output")
    runner.inference_query_set = InferenceQuerySet.model_validate(query)
    data = runner.lightning_data_module
    data.prepare_data()
    data.setup("predict")
    loader = data.predict_dataloader()
    if isinstance(loader, list):
        loader = loader[0]
    batch = next(iter(loader))
    if batch is None:
        raise RuntimeError("Upstream feature preprocessing returned None")
    print(json.dumps({"status": "passed", "batch_keys": sorted(batch), "source": "unchanged original 20-aa inline-A3M fixture"}), flush=True)
