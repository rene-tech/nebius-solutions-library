# ruff: noqa: S603, S607 -- every executable is a test-owned fixture or a fixed repository script.
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

from conftest import CONTROL_ROOT

TRANSITION = CONTROL_ROOT / "scripts" / "network-policy-transition.sh"
RELEASE = "test-release"
GATEWAY_NAMESPACE = "edge-custom"


def _guard(role: str, name: str, normal_name: str, spec: dict, *, deny_name: str | None = None) -> dict:
    annotations = {"fs2.nebius.ai/normal-policy-name": normal_name}
    if deny_name is not None:
        annotations["fs2.nebius.ai/deny-policy-name"] = deny_name
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": name,
            "namespace": GATEWAY_NAMESPACE,
            "labels": {
                "app.kubernetes.io/instance": RELEASE,
                "fs2.nebius.ai/network-policy-transition": "guard",
                "fs2.nebius.ai/network-policy-role": role,
            },
            "annotations": annotations,
        },
        "spec": spec,
    }


PROXY_SPEC = {
    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "envoy"}},
    "policyTypes": ["Ingress", "Egress"],
    "ingress": [{"ports": [{"port": 10080, "protocol": "TCP"}]}],
    "egress": [{"ports": [{"port": 53, "protocol": "UDP"}]}],
}
CONTROLLER_SPEC = {
    "podSelector": {"matchLabels": {"control-plane": "envoy-gateway"}},
    "policyTypes": ["Ingress"],
    "ingress": [{"ports": [{"port": 18000, "protocol": "TCP"}]}],
}
DENY_NAME = "fs2-serve-control-plane-envoy-default-deny"
GUARDS = [
    _guard(
        "public-envoy",
        "fs2-serve-control-plane-public-envoy-transition-guard",
        "fs2-serve-control-plane-public-envoy",
        PROXY_SPEC,
        deny_name=DENY_NAME,
    ),
    _guard(
        "envoy-controller",
        "fs2-serve-control-plane-envoy-controller-xds-transition-guard",
        "fs2-serve-control-plane-envoy-controller-xds",
        CONTROLLER_SPEC,
    ),
]


def _write_executable(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).lstrip())
    path.chmod(0o755)


def _fake_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    chart = tmp_path / "chart"
    chart.mkdir()
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    event_log = tmp_path / "events.log"
    state = tmp_path / "state"

    rendered_documents = []
    for guard in GUARDS:
        role = guard["metadata"]["labels"]["fs2.nebius.ai/network-policy-role"]
        normal_name = guard["metadata"]["annotations"]["fs2.nebius.ai/normal-policy-name"]
        deny_name = guard["metadata"]["annotations"].get("fs2.nebius.ai/deny-policy-name")
        deny_annotation = f"    fs2.nebius.ai/deny-policy-name: {deny_name}\n" if deny_name else ""
        rendered_documents.append(
            textwrap.dedent(
                f"""\
                apiVersion: networking.k8s.io/v1
                kind: NetworkPolicy
                metadata:
                  name: {guard["metadata"]["name"]}
                  namespace: {guard["metadata"]["namespace"]}
                  labels:
                    app.kubernetes.io/instance: {RELEASE}
                    fs2.nebius.ai/network-policy-transition: guard
                    fs2.nebius.ai/network-policy-role: {role}
                  annotations:
                    fs2.nebius.ai/normal-policy-name: {normal_name}
                """
            )
            + deny_annotation
            + textwrap.dedent(
                """\
                spec:
                  podSelector: {}
                  policyTypes: [Ingress]
                """
            )
        )
    rendered = "\n---\n".join(rendered_documents)
    helm_fixture = tmp_path / "rendered.yaml"
    helm_fixture.write_text(rendered)

    _write_executable(
        fake_bin / "helm",
        """
        #!/usr/bin/env bash
        set -eu
        printf 'helm %s\n' "$*" >>"$EVENT_LOG"
        case "$1" in
          template) cat "$HELM_FIXTURE" ;;
          upgrade) exit 42 ;;
          rollback) exit 0 ;;
          *) exit 2 ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "kubectl",
        f"""
        #!/usr/bin/env python3
        import json
        import os
        import pathlib
        import sys

        args = sys.argv[1:]
        with open(os.environ["EVENT_LOG"], "a", encoding="utf-8") as stream:
            stream.write("kubectl " + " ".join(args) + "\\n")
        command_index = next(
            index
            for index, value in enumerate(args)
            if value in {{"apply", "get", "patch", "delete"}}
        )
        command = args[command_index]
        tail = args[command_index + 1:]
        state = pathlib.Path(os.environ["FAKE_STATE"])
        guards = {json.dumps(GUARDS)}
        if command == "apply":
            raise SystemExit(0)
        if command == "delete":
            state.write_text("deleted")
            raise SystemExit(0)
        if command == "patch":
            raise SystemExit(0)
        if command == "get" and tail[0] == "pods":
            print(json.dumps({{"items": []}}))
            raise SystemExit(0)
        if command == "get" and tail[0] == "networkpolicy" and "--all-namespaces" in tail:
            print(json.dumps({{"items": [] if state.exists() else guards}}))
            raise SystemExit(0)
        if command == "get" and tail[0] == "networkpolicy":
            name = tail[1]
            if name == {DENY_NAME!r}:
                if "-o" in tail:
                    relaxed = {{
                        "spec": {{
                            "podSelector": {{
                                "matchLabels": {{"fs2.nebius.ai/rollback-relaxed": "true"}}
                            }}
                        }}
                    }}
                    print(json.dumps(relaxed))
                raise SystemExit(0)
            specs = {{
                "fs2-serve-control-plane-public-envoy": {json.dumps(PROXY_SPEC)},
                "fs2-serve-control-plane-envoy-controller-xds": {json.dumps(CONTROLLER_SPEC)},
            }}
            print(json.dumps({{"spec": specs[name]}}))
            raise SystemExit(0)
        raise SystemExit(2)
        """,
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "EVENT_LOG": str(event_log),
            "HELM_FIXTURE": str(helm_fixture),
            "FAKE_STATE": str(state),
        }
    )
    return environment, chart, kubeconfig, event_log


def _transition_command(action: str, chart: Path, kubeconfig: Path, *extra: str) -> list[str]:
    return [
        str(TRANSITION),
        action,
        "--release",
        RELEASE,
        "--release-namespace",
        "fs2-system",
        "--chart",
        str(chart),
        "--kubeconfig",
        str(kubeconfig),
        *extra,
    ]


def test_failed_upgrade_cannot_auto_rollback_and_governed_rollback_relaxes_exact_namespace(tmp_path: Path) -> None:
    environment, chart, kubeconfig, event_log = _fake_environment(tmp_path)
    subprocess.run(_transition_command("stage", chart, kubeconfig), check=True, env=environment, capture_output=True)

    failed_upgrade = subprocess.run(
        ["helm", "upgrade", RELEASE, str(chart), "--wait"],
        check=False,
        env=environment,
        capture_output=True,
    )
    assert failed_upgrade.returncode == 42
    before_rollback = event_log.read_text()
    assert "helm rollback" not in before_rollback

    result = subprocess.run(
        _transition_command("rollback", chart, kubeconfig, "--revision", "7"),
        check=True,
        env=environment,
        capture_output=True,
        text=True,
    )
    events = event_log.read_text()
    dry_run = events.index("apply --server-side --dry-run=server")
    live_apply = events.index("apply --server-side --field-manager")
    injected_failure = events.index("helm upgrade")
    relax = events.index(f"patch networkpolicy {DENY_NAME}")
    rollback = events.index(f"helm rollback {RELEASE} 7")
    assert dry_run < live_apply < injected_failure < relax < rollback
    assert f"--namespace {GATEWAY_NAMESPACE}" in events[relax:rollback]
    assert "gateway-namespace=edge-custom" in result.stdout


def test_successful_release_removes_guards_only_after_normal_specs_match(tmp_path: Path) -> None:
    environment, chart, kubeconfig, event_log = _fake_environment(tmp_path)
    subprocess.run(_transition_command("stage", chart, kubeconfig), check=True, env=environment, capture_output=True)
    result = subprocess.run(
        _transition_command("complete", chart, kubeconfig),
        check=True,
        env=environment,
        capture_output=True,
        text=True,
    )

    events = event_log.read_text()
    proxy_get = events.index("get networkpolicy fs2-serve-control-plane-public-envoy")
    controller_get = events.index("get networkpolicy fs2-serve-control-plane-envoy-controller-xds")
    delete = events.index("delete -f")
    assert proxy_get < delete and controller_get < delete
    assert "network-policy-transition=complete guards=0" in result.stdout
