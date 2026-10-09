import hashlib
import json

import pytest

from qualify_portable_analysis import compare_csv, complete_timeline, equal_science, verify_package


def counter():
    return {"numeric_values_compared": 0, "maximum_absolute_difference": 0., "non_bit_identical_numeric_values": 0}


def test_scientific_values_match_with_only_explicit_boundary_path_relocation():
    left = {"temperature_K": {"mean": 300.}, "next_segment_file": "/delivery/part2", "frames": [1000, 6598]}
    right = {**left, "next_segment_file": "/original/part2"}
    stats = counter()
    equal_science(left, right, stats)
    assert stats == {"numeric_values_compared": 3, "maximum_absolute_difference": 0., "non_bit_identical_numeric_values": 0}


def test_records_machine_rounding_without_hiding_it():
    stats = counter()
    equal_science(300. + 1e-11, 300., stats)
    assert stats["non_bit_identical_numeric_values"] == 1
    assert stats["maximum_absolute_difference"] > 0


def timeline():
    return {"engine": "namd", "common_frame_count": 1000, "production_steps": 500000,
            "production_duration_ps": 1000., "first_common_time_ps": 1.0000006290766237,
            "last_time_ps": 1000.000003607501}


def test_native_dcd_finite_precision_time_is_not_an_incomplete_trajectory():
    complete_timeline(timeline())


@pytest.mark.parametrize("field,value", [("common_frame_count", 999), ("production_steps", 499500),
                                       ("production_duration_ps", 999), ("first_common_time_ps", 2),
                                       ("last_time_ps", 999), ("last_time_ps", float("nan"))])
def test_incomplete_native_timeline_still_fails(field, value):
    with pytest.raises(ValueError, match="incomplete"):
        complete_timeline({**timeline(), field: value})


@pytest.mark.parametrize("left,right", [
    (301., 300.), (999, 1000), (float("nan"), 300), (float("inf"), 300),
    ([1, 2], [1, 2, 3]), ({"temperature": 300}, {"pressure": 300}),
    ({"temperature": 300, "extra": 1}, {"temperature": 300}), ("native", "synthetic"),
])
def test_rejects_changed_nonfinite_missing_or_extra_science(left, right):
    with pytest.raises(ValueError):
        equal_science(left, right, counter())


def test_csv_all_rows_and_columns_are_checked(tmp_path):
    left, right = tmp_path / "left.csv", tmp_path / "right.csv"
    left.write_text("time,temperature,engine\n1,300,real\n2,301,real\n")
    right.write_text("time,temperature,engine\n1.0,300.0,real\n2.0,301.0,real\n")
    stats = counter()
    compare_csv(left, right, stats)
    assert stats["numeric_values_compared"] == 4
    right.write_text("time,temperature,engine\n1,300,real\n2,999,real\n")
    with pytest.raises(ValueError):
        compare_csv(left, right, counter())


@pytest.fixture
def bundle(tmp_path):
    inputs = tmp_path / "analysis-inputs"
    inputs.mkdir()
    raw = tmp_path / "native.dat"
    raw.write_bytes(b"native scientific data\n")
    spec = inputs / "spec.json"
    spec.write_text('{}\n')
    receipt = {"path_base": "spec-directory", "portable_spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
               "files": [{"path": "native.dat", "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(), "bytes": raw.stat().st_size}]}
    (inputs / "packaging-receipt.json").write_text(json.dumps(receipt))
    return tmp_path


def test_verifies_all_packaged_dependencies(bundle):
    assert verify_package(bundle)["sha256"]
    (bundle / "native.dat").write_text("changed native bytes\n")
    with pytest.raises(ValueError, match="changed"):
        verify_package(bundle)


def test_rejects_changed_portable_spec(bundle):
    (bundle / "analysis-inputs/spec.json").write_text('{"changed": true}\n')
    with pytest.raises(ValueError, match="spec changed"):
        verify_package(bundle)


def test_rejects_path_escaping_delivered_bundle(bundle):
    path = bundle / "analysis-inputs/packaging-receipt.json"
    receipt = json.loads(path.read_text())
    receipt["files"][0]["path"] = "/etc/hostname"
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="outside"):
        verify_package(bundle)
