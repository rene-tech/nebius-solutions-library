import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "combine_serving_pair_receipts",
    Path(__file__).with_name("combine_serving_pair_receipts.py"),
)
combine_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(combine_module)


def test_combines_split_sessions_and_retains_source_repetitions(tmp_path):
    paths = []
    for index, pair_count in enumerate((1, 2), 1):
        runs = []
        for repetition in range(1, pair_count + 1):
            for mode in ("normal", "restore"):
                runs.append(
                    {
                        "repetition": repetition,
                        "mode": mode,
                        "status": "passed",
                    }
                )
        path = tmp_path / f"receipt-{index}.json"
        path.write_text(
            json.dumps(
                {"status": "passed", "model": "genmol", "cache": "same", "runs": runs}
            )
        )
        paths.append(path)

    result = combine_module.combine(paths)
    assert [row["repetition"] for row in result["runs"]] == [1, 1, 2, 2, 3, 3]
    assert [row["source_receipt_index"] for row in result["runs"]] == [1, 1, 2, 2, 2, 2]
    assert all(item["sha256"] for item in result["source_receipts"])
