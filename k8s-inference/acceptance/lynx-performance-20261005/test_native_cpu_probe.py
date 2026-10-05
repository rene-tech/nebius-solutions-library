from types import SimpleNamespace

import pytest

from native_cpu_probe import pod_spec


@pytest.mark.parametrize("cpus", [8, 16, 32])
def test_cpu_probe_resource_envelope_is_explicit_and_one_gpu(cpus):
    pod = pod_spec(SimpleNamespace(cpus=cpus, name="fs2-lynx-perf-cpu-test", node="exact-node",
                                  image="example/gromacs@sha256:" + "a" * 64))
    spec = pod["spec"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "exact-node"}
    assert spec["activeDeadlineSeconds"] == 1800
    resources = spec["containers"][0]["resources"]
    assert resources["requests"] == resources["limits"]
    assert resources["requests"]["cpu"] == str(cpus)
    assert resources["requests"]["nvidia.com/gpu"] == "1"


@pytest.mark.parametrize("name,image,cpus", [("other", "x@sha256:a", 8),
                                              ("fs2-lynx-perf-cpu-x", "x:latest", 8),
                                              ("fs2-lynx-perf-cpu-x", "x@sha256:a", 128)])
def test_rejects_unbounded_or_unowned_cpu_probe(name, image, cpus):
    with pytest.raises(ValueError):
        pod_spec(SimpleNamespace(cpus=cpus, name=name, node="n", image=image))
