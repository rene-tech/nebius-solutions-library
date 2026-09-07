import cache_copy


def test_copy_is_cpu_only_source_readonly_and_destination_digest_verified():
    pod = cache_copy.manifest("source-volume-node")
    spec = pod["spec"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "source-volume-node"}
    assert spec["restartPolicy"] == "Never" and spec["activeDeadlineSeconds"] == 7200
    source, destination = spec["volumes"]
    assert source["persistentVolumeClaim"] == {"claimName": cache_copy.SOURCE, "readOnly": True}
    assert destination["persistentVolumeClaim"] == {"claimName": cache_copy.DESTINATION}
    container = spec["containers"][0]
    assert "nvidia.com/gpu" not in container["resources"]["limits"]
    assert container["volumeMounts"][0]["readOnly"] is True
    command = container["command"][-1]
    assert cache_copy.CHECKPOINT_SHA256 in command
    assert command.index("sha256sum -c -") < command.index("mv /destination/")
    assert "test ! -e /destination/evo2_40b.pt" in command
    assert "rm " not in command
