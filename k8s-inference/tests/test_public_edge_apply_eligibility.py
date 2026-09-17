from __future__ import annotations

import copy
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "stages/foundation/scripts/verify-public-edge-node-eligibility.py"
)
SPEC = importlib.util.spec_from_file_location("public_edge_apply_eligibility", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)

CLUSTER_ID = "mk8scluster-test123"
GROUP_ID = "mk8snodegroup-test123"
RUN_ID = "r12345"
SELECTOR = {
    "workload.fs2.nebius/system": "true",
    "capacity.fs2.nebius/type": "regular",
    "capacity.fs2.nebius/pool": "system",
    "lifecycle.fs2.nebius/run": RUN_ID,
    "nebius.com/node-group-id": GROUP_ID,
}


def node_group(resource_version: str = "11") -> dict[str, object]:
    return {
        "metadata": {
            "id": GROUP_ID,
            "parent_id": CLUSTER_ID,
            "resource_version": resource_version,
        },
        "spec": {
            "fixed_node_count": 3,
            "template": {
                "metadata": {
                    "labels": {
                        key: value
                        for key, value in SELECTOR.items()
                        if key != "nebius.com/node-group-id"
                    }
                }
            },
        },
        "status": {
            "state": "RUNNING",
            "target_node_count": 3,
            "node_count": 3,
            "outdated_node_count": 0,
            "ready_node_count": 3,
            "reconciling": False,
        },
    }


def complete_list(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "items": items,
        "pagination": {
            "complete": True,
            "page_count": 1,
            "item_count": len(items),
            "terminal_next_page_token": "",
        },
    }


def provider_instance(ordinal: int, resource_version: str | None = None) -> dict[str, object]:
    instance_id = f"computeinstance-test{ordinal}"
    return {
        "metadata": {
            "id": instance_id,
            "parent_id": "project-test123",
            # Deliberately not a NodeGroup-derived name. Membership authority
            # comes from the signed provider relation, never this convention.
            "name": f"managed-system-member-{ordinal}",
            "resource_version": resource_version or str(20 + ordinal),
            "created_at": f"2026-09-17T11:0{ordinal}:00Z",
        },
        "spec": {"stopped": False},
        "status": {"state": "RUNNING", "reconciling": False},
    }


def provider_instances() -> dict[str, object]:
    return complete_list([provider_instance(ordinal) for ordinal in range(1, 4)])


def provider_instance_gets() -> dict[str, dict[str, object]]:
    return {
        f"computeinstance-test{ordinal}": provider_instance(ordinal)
        for ordinal in range(1, 4)
    }


def provider_member_set() -> set[str]:
    return {f"computeinstance-test{ordinal}" for ordinal in range(1, 4)}


def membership_subject() -> dict[str, object]:
    return GATE.membership_subject(
        project_id="project-test123",
        cluster_id=CLUSTER_ID,
        group_id=GROUP_ID,
        run_id=RUN_ID,
        expected_count=3,
        minimum_domains=3,
        selector=SELECTOR,
    )


def membership_receipt() -> tuple[dict[str, object], dict[str, object], bytes]:
    evidence = {
        "schema": "fs2-serve.nebius.ai/provider-node-group-membership-export/v1",
        "provider": "nebius",
        "project_id": "project-test123",
        "cluster_id": CLUSTER_ID,
        "node_group_id": GROUP_ID,
        "node_group_resource_version": "11",
        "relation_api": "nebius-managed-kubernetes-node-group-membership/v1",
        "member_instance_ids": sorted(provider_member_set()),
        "collected_at": "2026-09-17T12:00:00Z",
        "adapter_sha256": "a" * 64,
    }
    evidence_raw = GATE.canonical_bytes(evidence) + b"\n"
    key = b"k" * 32
    key_id = "sha256:" + hashlib.sha256(key).hexdigest()
    toolchain = {
        name: {
            "path": f"/usr/bin/{name}",
            "resolved_path": f"/usr/bin/{name}",
            "sha256": "b" * 64,
        }
        for name in ("python3", "nebius", "kubectl")
    }
    payload = {
        "schema": GATE.MEMBERSHIP_PAYLOAD_SCHEMA,
        "issuer": {
            "id": "provider-membership-review",
            "role": GATE.MEMBERSHIP_ISSUER_ROLE,
            "key_id": key_id,
        },
        "nonce": "c" * 64,
        "issued_at": "2026-09-17T12:00:00Z",
        "expires_at": "2026-09-17T13:00:00Z",
        "subject": membership_subject(),
        "provider_membership": {
            "relation_api": "nebius-managed-kubernetes-node-group-membership/v1",
            "node_group_resource_version": "11",
            "member_instance_ids": sorted(provider_member_set()),
            "kubernetes_node_controller_username": "system:serviceaccount:provider:node-controller",
        },
        "toolchain": toolchain,
        "evidence": {
            "provider_membership_export_sha256": hashlib.sha256(evidence_raw).hexdigest()
        },
    }
    receipt = {
        "schema": GATE.MEMBERSHIP_RECEIPT_SCHEMA,
        "algorithm": "ed25519",
        "payload": payload,
        "payload_sha256": hashlib.sha256(GATE.canonical_bytes(payload)).hexdigest(),
        "signature": base64.urlsafe_b64encode(b"s" * 64).rstrip(b"=").decode("ascii"),
    }
    trust = {
        "schema": GATE.MEMBERSHIP_TRUST_SCHEMA,
        "issuers": [
            {
                "id": "provider-membership-review",
                "role": GATE.MEMBERSHIP_ISSUER_ROLE,
                "key_id": key_id,
                "public_key": base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii"),
            }
        ],
    }
    return receipt, trust, evidence_raw


def node(ordinal: int) -> dict[str, object]:
    instance_id = f"computeinstance-test{ordinal}"
    return {
        "metadata": {
            "name": instance_id,
            "uid": f"00000000-0000-4000-8000-00000000000{ordinal}",
            "resourceVersion": str(100 + ordinal),
            "annotations": {
                "cluster.x-k8s.io/cluster-name": CLUSTER_ID,
                "cluster.x-k8s.io/owner-kind": "MachineSet",
                "cluster.x-k8s.io/owner-name": GROUP_ID,
                "cluster.x-k8s.io/machine": f"{GROUP_ID}-node{ordinal}",
            },
            "labels": {
                **SELECTOR,
                "kubernetes.io/hostname": instance_id,
            },
        },
        "spec": {
            "providerID": f"nebius://{instance_id}",
            "taints": [],
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def node_list(resource_version: str = "200") -> dict[str, object]:
    return {
        "metadata": {"resourceVersion": resource_version},
        "items": [node(ordinal) for ordinal in range(1, 4)],
    }


def test_current_provider_group_and_three_owned_ready_nodes_are_accepted() -> None:
    group = node_group()
    revision, digest = GATE.validate_node_group(
        complete_list([copy.deepcopy(group)]),
        complete_list([copy.deepcopy(group)]),
        copy.deepcopy(group),
        copy.deepcopy(group),
        cluster_id=CLUSTER_ID,
        group_id=GROUP_ID,
        expected_count=3,
        selector=SELECTOR,
    )
    node_revision, count, domains, node_digest = GATE.validate_nodes(
        node_list("200"),
        node_list("201"),
        cluster_id=CLUSTER_ID,
        group_id=GROUP_ID,
        run_id=RUN_ID,
        selector=SELECTOR,
        provider_member_ids=provider_member_set(),
        expected_count=3,
        minimum_domains=3,
    )

    assert revision == "11"
    assert len(digest) == 64
    assert node_revision == "201"
    assert count == domains == 3
    assert len(node_digest) == 64


def test_provider_members_are_enumerated_from_compute_and_exact_gets() -> None:
    members, digest = GATE.validate_provider_members(
        provider_instances(),
        provider_instance_gets(),
        provider_instances(),
        provider_instance_gets(),
        project_id="project-test123",
        signed_instance_ids=sorted(provider_member_set()),
        expected_count=3,
    )
    assert members == provider_member_set()
    assert len(digest) == 64


def test_signed_provider_relation_is_the_only_membership_authority(monkeypatch) -> None:
    receipt, trust, evidence_raw = membership_receipt()
    evidence = json.loads(evidence_raw)
    monkeypatch.setattr(GATE, "verify_ed25519", lambda *_args: None)
    monkeypatch.setattr(
        GATE,
        "checked_executable",
        lambda _record, name: sys.executable if name == "python3" else f"/usr/bin/{name}",
    )

    result = GATE.validate_membership_receipt(
        receipt,
        trust,
        membership_subject(),
        evidence,
        hashlib.sha256(evidence_raw).hexdigest(),
        validation_time=datetime(2026, 9, 17, 12, 1, tzinfo=timezone.utc),
    )

    assert result["member_instance_ids"] == sorted(provider_member_set())
    assert result["node_group_resource_version"] == "11"
    assert result["kubernetes_node_controller_username"].endswith("node-controller")


def test_empty_source_issuer_registry_fails_closed() -> None:
    receipt, _trust, evidence_raw = membership_receipt()
    with pytest.raises(GATE.GateError, match="source-trusted authority"):
        GATE.validate_membership_receipt(
            receipt,
            {"schema": GATE.MEMBERSHIP_TRUST_SCHEMA, "issuers": []},
            membership_subject(),
            json.loads(evidence_raw),
            hashlib.sha256(evidence_raw).hexdigest(),
            validation_time=datetime(2026, 9, 17, 12, 1, tzinfo=timezone.utc),
        )


def test_signed_relation_cannot_be_replaced_by_a_name_derived_member(monkeypatch) -> None:
    receipt, trust, evidence_raw = membership_receipt()
    receipt["payload"]["provider_membership"]["member_instance_ids"][0] = (
        "computeinstance-attacker"
    )
    receipt["payload"]["provider_membership"]["member_instance_ids"].sort()
    receipt["payload_sha256"] = hashlib.sha256(
        GATE.canonical_bytes(receipt["payload"])
    ).hexdigest()
    monkeypatch.setattr(GATE, "verify_ed25519", lambda *_args: None)
    monkeypatch.setattr(
        GATE,
        "checked_executable",
        lambda _record, name: sys.executable if name == "python3" else f"/usr/bin/{name}",
    )
    with pytest.raises(GATE.GateError, match="does not bind the signed exact relation"):
        GATE.validate_membership_receipt(
            receipt,
            trust,
            membership_subject(),
            json.loads(evidence_raw),
            hashlib.sha256(evidence_raw).hexdigest(),
            validation_time=datetime(2026, 9, 17, 12, 1, tzinfo=timezone.utc),
        )


def test_checked_executable_uses_and_closes_the_pinned_descriptor(monkeypatch) -> None:
    read_descriptor, write_descriptor = os.pipe()
    os.close(write_descriptor)
    monkeypatch.setattr(
        GATE,
        "pin_executable",
        lambda *_args: ("/usr/bin/tool", "/usr/bin/tool", read_descriptor),
    )

    assert GATE.checked_executable({}, "provider CLI") == "/usr/bin/tool"
    with pytest.raises(OSError):
        os.fstat(read_descriptor)


def test_checked_kubeconfig_is_reused_only_through_retained_descriptor(
    monkeypatch, tmp_path: Path
) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    kubeconfig = run_root / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    kubeconfig.chmod(0o600)
    monkeypatch.setattr(GATE, "validate_parent_chain", lambda *_args: None)

    resolved_root, pinned_path, descriptor = GATE.checked_local_inputs(
        str(run_root), str(kubeconfig)
    )
    try:
        assert resolved_root == run_root.resolve()
        assert pinned_path == f"/proc/self/fd/{descriptor}"
        assert os.fstat(descriptor).st_ino == kubeconfig.stat().st_ino
    finally:
        os.close(descriptor)


def test_provider_reads_receive_only_pinned_fds_and_sanitized_environment(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")

    monkeypatch.setattr(GATE.subprocess, "run", fake_run)
    monkeypatch.setattr(
        GATE.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir="/home/exact-user")
    )
    monkeypatch.setattr(GATE, "PINNED_COMMAND_FDS", (41, 42, 43))

    assert GATE.run_json(["/proc/self/fd/42", "get"], "provider read") == {
        "ok": True
    }
    assert observed["command"] == ["/proc/self/fd/42", "get"]
    assert observed["pass_fds"] == (41, 42, 43)
    assert observed["close_fds"] is True
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["cwd"] == "/"
    assert observed["env"] == {
        "HOME": "/home/exact-user",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def test_complete_pagination_follows_every_provider_token(monkeypatch) -> None:
    pages = iter(
        (
            {"items": [provider_instance(1)], "next_page_token": "page-2"},
            {"items": [provider_instance(2), provider_instance(3)]},
        )
    )
    commands: list[list[str]] = []

    def fake_run_json(command: list[str], _label: str) -> dict[str, object]:
        commands.append(command)
        return next(pages)

    monkeypatch.setattr(GATE, "run_json", fake_run_json)
    result = GATE.paginated_list(
        ["nebius", "compute", "instance"],
        ["list", "--parent-id", "project-test123"],
        "Compute instances",
    )

    assert result["pagination"] == {
        "complete": True,
        "page_count": 2,
        "item_count": 3,
        "terminal_next_page_token": "",
    }
    assert "--page-token" not in commands[0]
    assert commands[1][-2:] == ["--page-token", "page-2"]


def test_pagination_rejects_a_repeated_continuation_token(monkeypatch) -> None:
    pages = iter(
        (
            {"items": [provider_instance(1)], "next_page_token": "repeat"},
            {"items": [provider_instance(2)], "next_page_token": "repeat"},
        )
    )
    monkeypatch.setattr(GATE, "run_json", lambda _command, _label: next(pages))
    with pytest.raises(GATE.GateError, match="repeated a pagination token"):
        GATE.paginated_list(
            ["nebius", "compute", "instance"],
            ["list", "--parent-id", "project-test123"],
            "Compute instances",
        )


def test_provider_list_and_exact_get_must_have_the_same_revision() -> None:
    changed_gets = provider_instance_gets()
    changed_gets["computeinstance-test1"] = provider_instance(1, "99")
    with pytest.raises(GATE.GateError, match="list and exact-ID get projections differ"):
        GATE.validate_provider_members(
            provider_instances(),
            changed_gets,
            provider_instances(),
            provider_instance_gets(),
            project_id="project-test123",
            signed_instance_ids=sorted(provider_member_set()),
            expected_count=3,
        )


def test_provider_membership_rejects_kubernetes_only_foreign_node() -> None:
    foreign = node_list("201")
    foreign["items"][0]["metadata"]["name"] = "computeinstance-foreign"
    foreign["items"][0]["metadata"]["labels"][
        "kubernetes.io/hostname"
    ] = "computeinstance-foreign"
    foreign["items"][0]["spec"]["providerID"] = "nebius://computeinstance-foreign"
    with pytest.raises(GATE.GateError, match="absent from provider membership"):
        GATE.validate_nodes(
            node_list("200"),
            foreign,
            cluster_id=CLUSTER_ID,
            group_id=GROUP_ID,
            run_id=RUN_ID,
            selector=SELECTOR,
            provider_member_ids=provider_member_set(),
            expected_count=3,
            minimum_domains=3,
        )


def test_saved_plan_older_than_prerequisite_compatible_window_is_rejected() -> None:
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(GATE.GateError, match="exceeding the 14400s"):
        GATE.parse_plan_timestamp(
            (now - timedelta(seconds=14401)).isoformat(),
            now=now,
            maximum_age=14400,
        )


def test_unsigned_name_convention_cannot_add_a_provider_member() -> None:
    extra = provider_instance(4)
    extra["metadata"]["name"] = f"{GROUP_ID}-abc12-def34"
    listed = provider_instances()
    listed["items"].append(extra)
    listed["pagination"]["item_count"] = 4

    assert GATE.provider_member_ids(
        listed,
        project_id="project-test123",
        signed_instance_ids=sorted(provider_member_set()),
    ) == sorted(provider_member_set())


def test_signed_member_absent_from_complete_provider_list_is_rejected() -> None:
    incomplete = provider_instances()
    incomplete["items"] = incomplete["items"][:-1]
    incomplete["pagination"]["item_count"] = 2
    with pytest.raises(GATE.GateError, match="absent from the complete Compute"):
        GATE.provider_member_ids(
            incomplete,
            project_id="project-test123",
            signed_instance_ids=sorted(provider_member_set()),
        )


def test_spoofed_scheduler_labels_without_provider_membership_are_rejected() -> None:
    after = node_list("201")
    after["items"][0]["metadata"]["annotations"][
        "cluster.x-k8s.io/owner-name"
    ] = "mk8snodegroup-foreign"
    with pytest.raises(GATE.GateError, match="corroborating Cluster API ownership"):
        GATE.validate_nodes(
            node_list("200"),
            after,
            cluster_id=CLUSTER_ID,
            group_id=GROUP_ID,
            run_id=RUN_ID,
            selector=SELECTOR,
            provider_member_ids=provider_member_set(),
            expected_count=3,
            minimum_domains=3,
        )


def test_provider_rollout_machineset_suffix_remains_bound_to_exact_group() -> None:
    before = node_list("200")
    for item in before["items"]:
        annotations = item["metadata"]["annotations"]
        annotations["cluster.x-k8s.io/owner-name"] = f"{GROUP_ID}-abc12"
        annotations["cluster.x-k8s.io/machine"] = f"{GROUP_ID}-abc12-def34"
    after = copy.deepcopy(before)
    after["metadata"]["resourceVersion"] = "201"

    _revision, count, domains, _digest = GATE.validate_nodes(
        before,
        after,
        cluster_id=CLUSTER_ID,
        group_id=GROUP_ID,
        run_id=RUN_ID,
        selector=SELECTOR,
        provider_member_ids=provider_member_set(),
        expected_count=3,
        minimum_domains=3,
    )
    assert count == domains == 3


def test_changed_provider_resource_version_is_rejected() -> None:
    before = node_group("11")
    after = node_group("12")
    with pytest.raises(GATE.GateError, match="changed during"):
        GATE.validate_node_group(
            complete_list([copy.deepcopy(before)]),
            complete_list([copy.deepcopy(after)]),
            before,
            after,
            cluster_id=CLUSTER_ID,
            group_id=GROUP_ID,
            expected_count=3,
            selector=SELECTOR,
        )


def test_no_schedule_taint_removes_node_from_current_eligible_capacity() -> None:
    before = node_list("200")
    before["items"][0]["spec"]["taints"] = [
        {"key": "maintenance", "effect": "NoSchedule"}
    ]
    after = copy.deepcopy(before)
    after["metadata"]["resourceVersion"] = "201"
    with pytest.raises(GATE.GateError, match="fewer provider-owned Nodes"):
        GATE.validate_nodes(
            before,
            after,
            cluster_id=CLUSTER_ID,
            group_id=GROUP_ID,
            run_id=RUN_ID,
            selector=SELECTOR,
            provider_member_ids=provider_member_set(),
            expected_count=3,
            minimum_domains=3,
        )
