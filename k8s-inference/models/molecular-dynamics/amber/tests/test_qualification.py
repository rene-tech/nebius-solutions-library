import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).parents[1] / "qualification/ti_validation.py"
spec = importlib.util.spec_from_file_location("ti_validation", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_all_ti_states_and_scheduled_mbar_blocks_are_required():
    mdin = "ifmbar=1, mbar_states=11, bar_intervall=100, ntpr=100, clambda=0.30,\nmbar_lambda=0.0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0,\n"
    block = "MBAR Energy analysis:\n" + "\n".join(f"Energy at {i / 10:.4f} = {-100 + i:.8f}" for i in range(11))
    output = "DV/DL = 12.4\n" + block + "\n" + block
    result = module.validate_ti(mdin, output, 200)
    assert result["mbar_energy_blocks"] == 2
    assert result["mbar_energy_values"] == 22
    assert result["free_energy_convergence_claimed"] is False
    assert module.validate_ti(mdin.replace("ntpr=100,", "ntpr=1000,"), output, 2000)["mbar_print_interval_steps"] == 1000
    with pytest.raises(ValueError, match="scheduled"):
        module.validate_ti(mdin, block + "\nDV/DL = 12.4", 200)
    with pytest.raises(ValueError, match="coverage"):
        module.validate_ti(mdin, output.replace("Energy at 1.0000 = -90.00000000", ""), 200)
    with pytest.raises(ValueError, match="nonfinite"):
        module.validate_ti(mdin, output.replace("DV/DL = 12.4", "DV/DL = NaN"), 200)
