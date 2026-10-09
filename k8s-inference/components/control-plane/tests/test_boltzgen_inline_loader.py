"""Inline input loading avoids worker tensor transport through 64 MiB shm."""

import pytest

from fs2_serve.scientific_batch.adapters import boltzgen


@pytest.mark.parametrize("protocol", sorted(boltzgen.PROTOCOLS))
def test_all_advertised_protocols_request_inline_prediction_loading(protocol):
    parameters = boltzgen.BoltzGenParameters.parse({
        "protocol": protocol,
        "batches": [{"shard_id": "protocol", "num_designs": 20,
                     "budget": 1, "reuse_completed": False}],
    })
    argv = boltzgen._configure_argv(parameters, parameters.batches[0], "qualification")
    assert argv[argv.index("--num_workers") + 1] == "0"
    assert argv[argv.index("--devices") + 1] == "1"
    assert argv[argv.index("--num_designs") + 1] == "20"
    assert argv[argv.index("--budget") + 1] == "1"
    assert argv[argv.index("--protocol") + 1] == protocol
    assert argv[argv.index("--config") + 1:] == (
        "analysis", "num_processes=1", "data.cfg.num_workers=0", "data.cfg.pin_memory=false",
    )
    assert not {"--diffusion_steps", "--step_scale", "--noise_scale", "--diffusion_batch_size"} & set(argv)
    assert "--reuse" not in argv


def test_inline_loading_keeps_explicit_reuse_and_public_bounds():
    values = {"protocol": "antibody-anything", "batches": [
        {"shard_id": "first", "num_designs": 20, "budget": 3, "reuse_completed": True},
        {"shard_id": "second", "num_designs": 4, "budget": 1, "reuse_completed": False},
    ]}
    parameters = boltzgen.BoltzGenParameters.parse(values)
    assert boltzgen._configure_argv(parameters, parameters.batches[0], "qualification")[-1] == "--reuse"
    assert boltzgen.MAX_DESIGNS_PER_BATCH == 20
    assert boltzgen.MAX_TOTAL_DESIGNS == 24
    assert boltzgen.MAX_BUDGET_PER_BATCH == 3
