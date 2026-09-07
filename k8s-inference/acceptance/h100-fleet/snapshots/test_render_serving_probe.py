import importlib.util
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).with_name("render_serving_probe.py")
spec = importlib.util.spec_from_file_location("render_serving_probe", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def source():
    return {
        "metadata": {"namespace": "models"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "runtime",
                            "image": "registry/model@sha256:" + "a" * 64,
                            "command": ["model-server"],
                            "volumeMounts": [],
                        }
                    ],
                    "initContainers": [],
                    "volumes": [],
                }
            }
        },
    }


def args(mode):
    return SimpleNamespace(
        mode=mode,
        name="snapshot-probe",
        node="gpu-node",
        model_id="model",
        model_revision="revision",
        container="runtime",
        tools_image="registry/tools@sha256:" + "b" * 64,
        source_configmap="snapshot-source",
        pvc="snapshot-cache",
        run="model-r1",
        entrypoint_json="[]",
        python="python3",
        fallback="fail",
        allow_device_remap=True,
        asyncio_loop=False,
        request_uid=1000,
    )


def test_sys_resource_is_capture_only():
    donor = module.render(source(), args("donor"))
    restore = module.render(source(), args("restore"))
    donor_caps = donor["spec"]["containers"][0]["securityContext"]["capabilities"]["add"]
    restore_caps = restore["spec"]["containers"][0]["securityContext"]["capabilities"]["add"]
    assert "SYS_RESOURCE" in donor_caps
    assert "SYS_RESOURCE" not in restore_caps
    assert set(donor_caps) - {"SYS_RESOURCE"} == set(restore_caps)
