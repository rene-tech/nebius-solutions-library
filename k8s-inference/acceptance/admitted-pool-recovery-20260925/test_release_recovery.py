from copy import deepcopy

import pytest
from release_recovery import ENV, NAME, normalize, validate_delta


def example():
    old_image, new_image = "registry/control@sha256:old", "registry/control@sha256:new"
    gateway = {"spec": {"template": {"spec": {
        "serviceAccountName": "runtime",
        "initContainers": [{"name": "starter", "image": "registry/seed@sha256:fixed"}],
        "containers": [{"name": "control-plane", "image": old_image, "env": [{"name": "PRESERVED", "value": "yes"}]}],
    }}}}
    key = ("Deployment", "fs2-system", NAME)
    permission = ("ClusterRole", "fs2-system", NAME + "-scientific-batch-flavors")
    old = {key: gateway, permission: {"rules": [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get"]}]}}
    new = deepcopy(old)
    container = new[key]["spec"]["template"]["spec"]["containers"][0]
    container["image"] = new_image
    container["env"] += [{"name": name, "value": value} for name, value in ENV.items()]
    new[permission]["rules"][0]["verbs"].append("list")
    role = ("Role", "kube-system", NAME + "-scientific-pool-health")
    new[role] = {"rules": [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["cluster-autoscaler-status"], "verbs": ["get"]}]}
    new[("RoleBinding", "kube-system", role[2])] = {
        "subjects": [{"kind": "ServiceAccount", "name": "runtime", "namespace": "fs2-system"}],
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role[2]},
    }
    return old, new, old_image, new_image


def test_only_exact_recovery_delta_allowed_and_validation_does_not_mutate_render():
    old, new, before, after = example()
    frozen = deepcopy(new)
    assert len(validate_delta(old, new, before, after)) == 4
    assert new == frozen


@pytest.mark.parametrize("change", ["seed", "resources", "other-env", "node-write", "wildcard-cm", "wrong-sa", "extra-resource"])
def test_unrelated_or_excess_mutations_refused(change):
    old, new, before, after = example()
    pod = new[("Deployment", "fs2-system", NAME)]["spec"]["template"]["spec"]
    if change == "seed":
        pod["initContainers"][0]["image"] = "unapproved"
    elif change == "resources":
        pod["containers"][0]["resources"] = {"requests": {"cpu": "100"}}
    elif change == "other-env":
        pod["containers"][0]["env"][0]["value"] = "changed"
    elif change == "node-write":
        new[("ClusterRole", "fs2-system", NAME + "-scientific-batch-flavors")]["rules"][0]["verbs"].append("patch")
    elif change == "wildcard-cm":
        new[("Role", "kube-system", NAME + "-scientific-pool-health")]["rules"][0].pop("resourceNames")
    elif change == "wrong-sa":
        new[("RoleBinding", "kube-system", NAME + "-scientific-pool-health")]["subjects"][0]["name"] = "other"
    else:
        new[("Secret", "fs2-system", "unexpected")] = {}
    with pytest.raises(ValueError):
        validate_delta(old, new, before, after)


def test_changed_recovery_values_cannot_be_hidden_by_normalization():
    with pytest.raises(ValueError, match="unexpected_recovery_env"):
        normalize({"env": [{"name": next(iter(ENV)), "value": "1"}]}, "image")
