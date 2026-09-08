from aging.benchmark_runtime import process_memory
from aging.fixtures import clinical_payload


def test_process_memory_uses_current_linux_address_space(monkeypatch):
    monkeypatch.setattr(
        "aging.benchmark_runtime.Path.read_text",
        lambda self: "Name:\tpython\nVmRSS:\t1234 kB\nVmHWM:\t4321 kB\n",
    )
    assert process_memory() == {"VmRSS": 1234 * 1024, "VmHWM": 4321 * 1024}


def test_synthetic_clinical_batch_has_distinct_names_and_ages():
    values = clinical_payload(2)["samples"]
    assert values[0]["sample_id"] != values[1]["sample_id"]
    assert values[0]["age_years"] != values[1]["age_years"]
