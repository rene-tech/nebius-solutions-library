from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gromacs-mpinat-20261003"))
from recover_bucket import WORKSPACE_PREFIXES, owned_workspace_prefix


def test_old_default_and_explicit_lynx_family_remain_distinct():
    old, lynx = WORKSPACE_PREFIXES
    assert owned_workspace_prefix("runs/fs2-mpinat-example/case", old)
    assert owned_workspace_prefix("runs/fs2-lynx-performance-20261005-single-rest/case", lynx)
    assert not owned_workspace_prefix("runs/fs2-lynx-performance-20261005-single-rest/case", old)
    assert not owned_workspace_prefix("runs/fs2-mpinat-example/case", lynx)


@pytest.mark.parametrize("base", ["runs/customer/case", "runs/fs2-lynx-performance-20261006-case/x",
                                      "runs/fs2-lynx-performance-20261005-x/../other",
                                      "/runs/fs2-lynx-performance-20261005-x", None])
def test_unrelated_recovery_prefix_rejected(base):
    assert not owned_workspace_prefix(base, WORKSPACE_PREFIXES[1])
    assert not owned_workspace_prefix(base, "runs/")
