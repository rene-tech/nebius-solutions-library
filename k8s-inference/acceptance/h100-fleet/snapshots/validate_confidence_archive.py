"""Use the production collector to validate isolated ESM snapshot outputs."""

import io
from pathlib import Path
import tarfile
import tempfile


def validate_confidence_archive(content, command, model_revision):
    from fs2_serve.scientific_batch.adapters.secondary_structure import _validated_confidence_entries

    def argument(name):
        if command.count(name) != 1:
            raise ValueError(f"expected one original {name} argument")
        return command[command.index(name) + 1]

    with tempfile.TemporaryDirectory(prefix="fs2-snapshot-confidence-") as directory:
        workspace = Path(directory)
        outputs = workspace / "outputs"
        outputs.mkdir()
        with tarfile.open(fileobj=io.BytesIO(content)) as archive:
            archive.extractall(outputs, filter="data")
        _, validation = _validated_confidence_entries(
            workspace,
            expected_runtime_id=argument("--variant"),
            expected_model_revision=model_revision,
            expected_seeds=(int(argument("--seed")),),
            expected_samples_per_seed=1,
            expected_input_artifact_id=argument("--expected-raw-input-artifact-id"),
            expected_raw_input_sha256=argument("--expected-raw-input-sha256"),
        )
        return validation
