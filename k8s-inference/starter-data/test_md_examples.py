"""Native science controls and resumable large-file delivery are explicit."""

import asyncio
from contextlib import asynccontextmanager

import md_examples as md
import pytest
import run_example as runner


def test_archives_are_reproducible_and_preserve_every_byte():
    files = {"b/coordinates.bin": b"\x00\x01", "a.mdp": b"nsteps = 10000\n"}
    first = md.archive(files)
    assert first == md.archive(dict(reversed(list(files.items()))))
    assert md.unpack(first) == files


@pytest.mark.parametrize("name", ["../escape", "/absolute"])
def test_unsafe_native_archives_rejected(name):
    with pytest.raises(ValueError, match="invalid_input_archive_member"):
        md.unpack(md.archive({name: b"not accepted"}))


def test_quick_gromacs_keeps_physics_and_changes_only_declared_duration():
    files = {
        name: b"nsteps = 50000\ndt = 0.002\nconstraints = h-bonds\n"
        for name in ("nvt.mdp", "npt.mdp", "production.mdp")
    }
    files["system.top"] = b"canonical force field"
    actual, _request = md.quick_variant("gromacs", files, {"jobs": [{"steps": []}]})
    assert actual["system.top"] == files["system.top"]
    assert (
        actual["production.mdp"]
        == b"nsteps = 10000\ndt = 0.002\nconstraints = h-bonds\n"
    )
    assert files["production.mdp"].startswith(b"nsteps = 50000")


def test_lammps_quick_targets_are_cumulative_not_three_identical_stops():
    files = {
        stage + suffix: f"units real\nrun {target} upto\n".encode()
        for stage, target in (("nvt", 50000), ("npt", 100000), ("production", 600000))
        for suffix in (".in", "-resume.in")
    }
    request = {
        "jobs": [
            {
                "steps": [
                    {"id": stage, "continuation": {"target_step": target}}
                    for stage, target in (
                        ("nvt", 50000),
                        ("npt", 100000),
                        ("production", 600000),
                    )
                ]
            }
        ]
    }
    actual, changed = md.quick_variant("lammps", files, request)
    assert b"run 30000 upto" in actual["production-resume.in"]
    assert [s["continuation"]["target_step"] for s in changed["jobs"][0]["steps"]] == [
        10000,
        20000,
        30000,
    ]
    assert request["jobs"][0]["steps"][-1]["continuation"]["target_step"] == 600000


def test_batch_has_independent_seed_and_directory_context():
    files = {"alanine/nvt.namd": b"seed 20260923\nrun 10000\n"}
    request = {
        "jobs": [{"id": "original", "steps": [{"id": "nvt", "directory": "alanine"}]}]
    }
    bundled, actual, expected = md.batch_variant("namd", files, request)
    assert (
        bundled["replica-1/alanine/nvt.namd"] != bundled["replica-2/alanine/nvt.namd"]
    )
    assert [j["steps"][0]["directory"] for j in actual["jobs"]] == [
        "replica-1/alanine",
        "replica-2/alanine",
    ]
    assert expected["jobs"] == 2
    assert request["jobs"][0]["id"] == "original"


class Stream:
    def __init__(self, blocks):
        self.blocks = blocks

    def raise_for_status(self):
        pass

    async def aiter_bytes(self):
        for block in self.blocks:
            yield block


class Http:
    def __init__(self, blocks):
        self.blocks = blocks

    @asynccontextmanager
    async def stream(self, method, url):
        assert method == "GET"
        yield Stream(self.blocks)


def test_large_result_is_streamed_then_atomically_published(tmp_path):
    target = tmp_path / "output.bin"
    data = b"native trajectory"
    asyncio.run(
        runner.download_verified(
            Http([data[:4], data[4:]]),
            "https://example.invalid/artifact",
            target,
            {"size_bytes": len(data), "sha256": runner.sha(data)},
        )
    )
    assert target.read_bytes() == data
    assert not target.with_suffix(".bin.partial").exists()


@pytest.mark.parametrize(
    "data,expected_size", [(b"truncated", 99), (b"oversized", 2), (b"wrong", 5)]
)
def test_bad_download_never_replaces_existing_verified_file(
    tmp_path, data, expected_size
):
    target = tmp_path / "output.bin"
    target.write_bytes(b"original")
    with pytest.raises(ValueError, match="download_(size|checksum)_mismatch"):
        asyncio.run(
            runner.download_verified(
                Http([data]),
                "https://example.invalid/artifact",
                target,
                {"size_bytes": expected_size, "sha256": runner.sha(b"other")},
            )
        )
    assert target.read_bytes() == b"original"
