#!/usr/bin/env python3
"""Crash-safe public-edge NetworkPolicy transition coordinator."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

SCHEMA = "fs2-serve.nebius.ai/network-policy-transition-receipt/v3"
BOUNDARY_LABEL = "fs2.nebius.ai/network-policy-boundary"
BOUNDARY_VALUE = "permanent"
ROLE_LABEL = "fs2.nebius.ai/network-policy-role"
CANDIDATE_ANNOTATION = "fs2.nebius.ai/network-policy-candidate-sha256"
NORMAL_ANNOTATION = "fs2.nebius.ai/normal-policy-name"
DENY_ANNOTATION = "fs2.nebius.ai/deny-policy-name"
FENCE_ANNOTATION = "fs2.nebius.ai/network-policy-fence"
LEASE_NAME = "fs2-network-policy-transition"
RECEIPT_NAME = "fs2-network-policy-transition"
TOPOLOGY_NAME = "fs2-network-policy-boundary-topology"
LOCK_SECONDS = 60
LOCK_RENEW_SECONDS = 15
POD_PAGE_SIZE = 100
MAX_POD_PAGES = 10
RELAXED_SELECTOR = {"fs2.nebius.ai/network-policy-deny-relaxed": "true"}
IN_FLIGHT_PHASES = {
    "bootstrap-relaxing",
    "bootstrap-ready",
    "bootstrap-guards-staging",
    "guards-staging",
    "guards-ready",
    "staged",
    "rollback-relaxing",
    "rollback-prepared",
    "rollback-guards-staging",
    "rollback-guards-ready",
    "destroy-relaxing",
    "destroy-prepared",
}


class TransitionError(RuntimeError):
    """A transition invariant failed closed."""


def canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Command:
    def __init__(self, prefix: Sequence[str], *, name: str) -> None:
        self.prefix = list(prefix)
        self.name = name

    def run(
        self,
        *arguments: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        result = subprocess.run(  # noqa: S603 - fixed tools and validated arguments
            [*self.prefix, *arguments],
            input=input_text,
            capture_output=True,
            check=False,
            text=True,
        )
        outcome = CommandResult(result.returncode, result.stdout, result.stderr)
        if check and result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise TransitionError(f"{self.name} command failed{suffix}")
        return outcome


@dataclass
class Candidate:
    candidate_sha256: str
    chart_sha256: str
    values_sha256: str
    complete_render_sha256: str
    network_policy_render_sha256: str
    release: dict[str, Any]
    topology: dict[str, Any]
    proxy: dict[str, Any]
    controller: dict[str, Any]
    deny_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_sha256": self.candidate_sha256,
            "chart_sha256": self.chart_sha256,
            "values_sha256": self.values_sha256,
            "complete_render_sha256": self.complete_render_sha256,
            "network_policy_render_sha256": self.network_policy_render_sha256,
            "release": self.release,
            "topology": self.topology,
            "policies": {
                "public-envoy": self.proxy,
                "envoy-controller": self.controller,
            },
            "deny_name": self.deny_name,
        }


class Transition:
    def __init__(self, arguments: argparse.Namespace) -> None:
        self.arguments = arguments
        self.release = arguments.release
        self.release_namespace = arguments.release_namespace
        self.chart = Path(arguments.chart).resolve()
        self.kubeconfig = Path(arguments.kubeconfig).resolve()
        self.security_owner_kubeconfig = Path(arguments.security_owner_kubeconfig).resolve()
        self.tempdir = Path(tempfile.mkdtemp(prefix="fs2-network-policy-transition."))
        self.holder = f"{os.uname().nodename}-{os.getpid()}-{uuid.uuid4()}"
        self.bootstrap_kubectl = Command(self._kubectl_prefix(self.kubeconfig), name="kubectl")
        self.security_owner_kubectl = Command(
            self._kubectl_prefix(self.security_owner_kubeconfig),
            name="security-owner kubectl",
        )
        helm_prefix = ["helm", "--kubeconfig", str(self.kubeconfig)]
        if arguments.context:
            helm_prefix.extend(["--kube-context", arguments.context])
        self.helm = Command(helm_prefix, name="helm")
        self.value_sources: list[dict[str, str]] = []
        self.helm_values = self._helm_values()
        self.guarded_kubectl: Command | None = None
        self.fence_transitions: int | None = None

    def close(self) -> None:
        shutil.rmtree(self.tempdir)

    def _kubectl_prefix(self, kubeconfig: Path) -> list[str]:
        prefix = ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=30s"]
        if self.arguments.context:
            prefix.extend(["--context", self.arguments.context])
        return prefix

    def _helm_values(self) -> list[str]:
        result: list[str] = []
        for path_value in self.arguments.values:
            path = Path(path_value)
            if not path.is_file():
                raise TransitionError(f"values file does not exist: {path}")
            result.extend(["--values", str(path.resolve())])
            self.value_sources.append(
                {"kind": "file", "name": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            )
        for index, name in enumerate(self.arguments.values_env):
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name not in os.environ:
                raise TransitionError("values environment name is invalid or unset")
            path = self.tempdir / f"values-{index}.yaml"
            path.write_text(os.environ[name] + "\n", encoding="utf-8")
            result.extend(["--values", str(path)])
            self.value_sources.append(
                {"kind": "environment", "name": name, "sha256": sha256_text(os.environ[name] + "\n")}
            )
        passthrough = list(self.arguments.helm_value_args)
        if passthrough[:1] == ["--"]:
            passthrough.pop(0)
        result.extend(passthrough)
        self.value_sources.append({"kind": "helm-arguments", "name": "remainder", "sha256": sha256_json(passthrough)})
        return result

    def configure_guarded_client(self) -> None:
        self.verify_external_iam_boundary()
        self.guarded_kubectl = self.security_owner_kubectl

    @staticmethod
    def _cluster_identity(command: Command) -> tuple[str, str]:
        raw_config = command.run("config", "view", "--minify", "--raw", "-o", "json").stdout
        try:
            clusters = json.loads(raw_config)["clusters"]
            if len(clusters) != 1:
                raise TransitionError("kubeconfig does not select exactly one cluster")
            server = clusters[0]["cluster"]["server"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise TransitionError("could not derive the selected Kubernetes API server") from error
        uid = command.run("get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}").stdout.strip()
        if not isinstance(server, str) or not server or not uid:
            raise TransitionError("kubeconfig cluster identity is incomplete")
        return server, uid

    @staticmethod
    def _require_can_i(command: Command, expected: str, *arguments: str) -> None:
        result = command.run("auth", "can-i", *arguments, check=False)
        if result.returncode != 0 or result.stdout.strip() != expected:
            raise TransitionError("Kubernetes authorization does not match the external security boundary")

    def verify_external_iam_boundary(self) -> None:
        """Fail closed unless the rollout identity is outside security ownership."""
        if self.kubeconfig == self.security_owner_kubeconfig:
            raise TransitionError("ordinary and security-owner kubeconfigs must be distinct")
        if stat.S_IMODE(self.security_owner_kubeconfig.stat().st_mode) != 0o600:
            raise TransitionError("security-owner kubeconfig must have mode 0600")
        if self._cluster_identity(self.bootstrap_kubectl) != self._cluster_identity(self.security_owner_kubectl):
            raise TransitionError("ordinary and security-owner kubeconfigs select different clusters")
        topology_result = self.bootstrap_kubectl.run(
            "get", "configmap", TOPOLOGY_NAME, "--namespace", self.release_namespace, "-o", "json"
        )
        topology_resource = cast(dict[str, Any], json.loads(topology_result.stdout))
        try:
            topology = json.loads(topology_resource["data"]["topology.json"])
            security_owner = topology["security_owner_username"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise TransitionError("protected topology has no external security-owner identity") from error
        if not isinstance(security_owner, str) or not re.fullmatch(r"[A-Za-z0-9:@._/-]{3,253}", security_owner):
            raise TransitionError("protected topology has an invalid external security-owner identity")
        owner_result = self.security_owner_kubectl.run("auth", "whoami", "-o", "json")
        try:
            owner_username = json.loads(owner_result.stdout)["status"]["userInfo"]["username"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise TransitionError("security-owner kubeconfig has no exact authenticated identity") from error
        if owner_username != security_owner:
            raise TransitionError("security-owner kubeconfig identity differs from protected topology")
        protected = (
            "validatingadmissionpolicies.admissionregistration.k8s.io",
            "validatingadmissionpolicybindings.admissionregistration.k8s.io",
        )
        for resource in protected:
            for verb in ("get", "patch", "update", "delete"):
                exact = (verb, f"{resource}/fs2-network-policy-boundary")
                self._require_can_i(self.bootstrap_kubectl, "no", *exact)
                self._require_can_i(self.security_owner_kubectl, "yes", *exact)
            self._require_can_i(self.bootstrap_kubectl, "no", "deletecollection", resource)
            self._require_can_i(self.security_owner_kubectl, "no", "deletecollection", resource)
        self._require_can_i(
            self.bootstrap_kubectl,
            "no",
            "impersonate",
            f"users/{security_owner}",
        )
        self._require_can_i(
            self.bootstrap_kubectl,
            "no",
            "create",
            "serviceaccounts/fs2-network-policy-transition",
            "--subresource=token",
            "--namespace",
            self.release_namespace,
        )

    @property
    def guarded(self) -> Command:
        if self.guarded_kubectl is None:
            raise TransitionError("guarded Kubernetes client is not configured")
        return self.guarded_kubectl

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self._acquire_lock()
        body_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            body_error = error
            raise
        finally:
            try:
                self._release_lock()
            except TransitionError:
                if body_error is None:
                    raise

    def _lease(self) -> dict[str, Any]:
        result = self.guarded.run("get", "lease", LEASE_NAME, "--namespace", self.release_namespace, "-o", "json")
        return cast(dict[str, Any], json.loads(result.stdout))

    @staticmethod
    def _lease_expired(lease: dict[str, Any]) -> bool:
        spec = lease.get("spec", {})
        holder = spec.get("holderIdentity", "")
        if not holder:
            return True
        renew = spec.get("renewTime")
        duration = int(spec.get("leaseDurationSeconds", 0))
        if not renew or duration <= 0:
            return False
        renewed = dt.datetime.fromisoformat(renew.replace("Z", "+00:00"))
        return dt.datetime.now(dt.UTC) >= renewed + dt.timedelta(seconds=duration)

    def _acquire_lock(self) -> None:
        for _ in range(3):
            lease = self._lease()
            holder = lease.get("spec", {}).get("holderIdentity", "")
            if holder and holder != self.holder and not self._lease_expired(lease):
                raise TransitionError("another NetworkPolicy transition holds the Lease")
            patch = [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": lease["metadata"]["resourceVersion"],
                },
                {"op": "add", "path": "/spec/holderIdentity", "value": self.holder},
                {"op": "add", "path": "/spec/leaseDurationSeconds", "value": LOCK_SECONDS},
                {
                    "op": "add",
                    "path": "/spec/leaseTransitions",
                    "value": int(lease.get("spec", {}).get("leaseTransitions", 0)) + 1,
                },
                {
                    "op": "add",
                    "path": "/spec/renewTime",
                    "value": dt.datetime.now(dt.UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                },
            ]
            result = self.guarded.run(
                "patch",
                "lease",
                LEASE_NAME,
                "--namespace",
                self.release_namespace,
                "--type=json",
                "--patch",
                canonical(patch),
                "-o",
                "json",
                check=False,
            )
            if result.returncode == 0:
                updated = json.loads(result.stdout)
                if updated.get("spec", {}).get("holderIdentity") != self.holder:
                    raise TransitionError("Lease acquisition did not preserve holder identity")
                self.fence_transitions = int(updated.get("spec", {}).get("leaseTransitions", -1))
                return
        raise TransitionError("could not acquire the NetworkPolicy transition Lease")

    def _renew_fence(self) -> dict[str, Any]:
        if self.fence_transitions is None:
            raise TransitionError("transition mutation attempted without a Lease fence")
        lease = self._lease()
        spec = lease.get("spec", {})
        if (
            spec.get("holderIdentity") != self.holder
            or int(spec.get("leaseTransitions", -1)) != self.fence_transitions
            or self._lease_expired(lease)
        ):
            raise TransitionError("transition Lease fence is stale")
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": lease["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/spec/holderIdentity", "value": self.holder},
            {"op": "test", "path": "/spec/leaseTransitions", "value": self.fence_transitions},
            {
                "op": "add",
                "path": "/spec/renewTime",
                "value": dt.datetime.now(dt.UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            },
        ]
        result = self.guarded.run(
            "patch",
            "lease",
            LEASE_NAME,
            "--namespace",
            self.release_namespace,
            "--type=json",
            "--patch",
            canonical(patch),
            "-o",
            "json",
        )
        renewed = cast(dict[str, Any], json.loads(result.stdout))
        if (
            renewed.get("spec", {}).get("holderIdentity") != self.holder
            or int(renewed.get("spec", {}).get("leaseTransitions", -1)) != self.fence_transitions
        ):
            raise TransitionError("transition Lease renewal lost its fence")
        return renewed

    @property
    def fence_identity(self) -> str:
        if self.fence_transitions is None:
            raise TransitionError("transition Lease fence is unavailable")
        return f"{self.holder}:{self.fence_transitions}"

    def _release_lock(self) -> None:
        lease = self._renew_fence()
        if lease.get("spec", {}).get("holderIdentity") != self.holder:
            raise TransitionError("transition Lease ownership changed before release")
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": lease["metadata"]["resourceVersion"],
            },
            {"op": "add", "path": "/spec/holderIdentity", "value": ""},
        ]
        self.guarded.run(
            "patch",
            "lease",
            LEASE_NAME,
            "--namespace",
            self.release_namespace,
            "--type=json",
            "--patch",
            canonical(patch),
        )
        self.fence_transitions = None

    def run_fenced_helm(self, *arguments: str) -> CommandResult:
        """Run one mutating Helm action while renewing and enforcing the Lease."""
        self._renew_fence()
        process = subprocess.Popen(  # noqa: S603 - fixed Helm prefix and validated arguments
            [*self.helm.prefix, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        while True:
            try:
                stdout, stderr = process.communicate(timeout=LOCK_RENEW_SECONDS)
                break
            except subprocess.TimeoutExpired:
                try:
                    self._renew_fence()
                except TransitionError:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise
        outcome = CommandResult(process.returncode, stdout, stderr)
        if outcome.returncode != 0:
            detail = outcome.stderr.strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise TransitionError(f"fenced helm command failed{suffix}")
        self._renew_fence()
        return outcome

    def _chart_hash(self) -> str:
        digest = hashlib.sha256()
        files = sorted(path for path in self.chart.rglob("*") if path.is_file())
        if not files:
            raise TransitionError("chart contains no files")
        for path in files:
            digest.update(str(path.relative_to(self.chart)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _successful_history_entry(entry: dict[str, Any]) -> bool:
        description = str(entry.get("description", "")).lower()
        return (
            str(entry.get("status", "")).lower() in {"deployed", "superseded"}
            and "complete" in description
            and not any(word in description for word in ("fail", "pending"))
        )

    @staticmethod
    def _release_object_identity(policy: dict[str, Any]) -> dict[str, str]:
        metadata = policy.get("metadata", {})
        if not metadata.get("uid") or not metadata.get("resourceVersion"):
            raise TransitionError("Helm release policy lacks UID/resourceVersion identity")
        return {
            "namespace": metadata["namespace"],
            "name": metadata["name"],
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "spec_sha256": sha256_json(policy.get("spec")),
        }

    def _release_storage_identity(self, revision: str, status: str) -> dict[str, str]:
        """Read only the exact Helm release Secret metadata, never its payload."""
        if not re.fullmatch(r"[1-9][0-9]*", revision):
            raise TransitionError("Helm storage revision is not a positive integer")
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", self.release):
            raise TransitionError("Helm release name cannot identify exact storage")
        name = f"sh.helm.release.v1.{self.release}.v{revision}"
        jsonpath = (
            r'{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.metadata.resourceVersion}'
            r'{"\t"}{.metadata.labels.owner}{"\t"}{.metadata.labels.name}'
            r'{"\t"}{.metadata.labels.version}{"\t"}{.metadata.labels.status}'
        )
        result = self.bootstrap_kubectl.run(
            "get",
            "secret",
            name,
            "--namespace",
            self.release_namespace,
            "-o",
            f"jsonpath={jsonpath}",
        )
        fields = result.stdout.split("\t")
        if len(fields) != 7:
            raise TransitionError("Helm release storage metadata is incomplete")
        storage_name, uid, resource_version, owner, release_name, stored_revision, stored_status = fields
        if (
            storage_name != name
            or not uid
            or not resource_version
            or owner != "helm"
            or release_name != self.release
            or stored_revision != revision
            or stored_status.lower() != status.lower()
        ):
            raise TransitionError("Helm release storage identity does not match list/history")
        return {
            "name": storage_name,
            "uid": uid,
            "resource_version": resource_version,
            "revision": stored_revision,
            "status": stored_status.lower(),
        }

    def _release_identity(
        self,
        proxy: dict[str, Any],
        controller: dict[str, Any],
        *,
        require_deployed: bool = True,
    ) -> dict[str, Any]:
        listing = self.helm.run(
            "list",
            "--namespace",
            self.release_namespace,
            "--all",
            "--filter",
            f"^{re.escape(self.release)}$",
            "-o",
            "json",
        )
        exact = [item for item in json.loads(listing.stdout) if item.get("name") == self.release]
        if not exact:
            return {
                "name": self.release,
                "namespace": self.release_namespace,
                "revision": "absent",
                "status": "absent",
                "deployed_manifest_sha256": None,
                "deployed_values_sha256": None,
                "request_debug_enabled": None,
                "identity_uid": None,
                "storage": None,
                "objects": {},
            }
        if len(exact) != 1:
            raise TransitionError("expected at most one existing exact Helm release")
        revision = str(exact[0].get("revision", ""))
        status = str(exact[0].get("status", "")).lower()
        if not re.fullmatch(r"[1-9][0-9]*", revision):
            raise TransitionError("Helm release revision is not a positive integer")
        if require_deployed and status != "deployed":
            raise TransitionError("existing Helm release is not successfully deployed")
        history = json.loads(
            self.helm.run("history", self.release, "--namespace", self.release_namespace, "-o", "json").stdout
        )
        current = [row for row in history if str(row.get("revision")) == revision]
        if len(current) != 1 or str(current[0].get("status", "")).lower() != status:
            raise TransitionError("Helm list/history identity is inconsistent")
        if require_deployed and not self._successful_history_entry(current[0]):
            raise TransitionError("Helm release is not a successful stable deployment")
        manifest = self.helm.run(
            "get", "manifest", self.release, "--namespace", self.release_namespace, "--revision", revision
        ).stdout
        values = json.loads(
            self.helm.run(
                "get",
                "values",
                self.release,
                "--namespace",
                self.release_namespace,
                "--revision",
                revision,
                "--all",
                "-o",
                "json",
            ).stdout
        )
        storage = self._release_storage_identity(revision, status)
        proxy_policy = self.get_policy(proxy["normal_name"], proxy["namespace"])
        controller_policy = self.get_policy(controller["normal_name"], controller["namespace"])
        objects = {
            "public-envoy": self._release_object_identity(proxy_policy),
            "envoy-controller": self._release_object_identity(controller_policy),
        }
        return {
            "name": self.release,
            "namespace": self.release_namespace,
            "revision": revision,
            "status": status,
            "description": current[0].get("description", ""),
            "chart": current[0].get("chart", ""),
            "app_version": current[0].get("app_version", ""),
            "deployed_manifest_sha256": sha256_json(self._yaml_documents(manifest)),
            "deployed_values_sha256": sha256_json(values),
            "request_debug_enabled": values.get("config", {}).get("requestDebugEnabled"),
            "identity_uid": storage["uid"],
            "storage": storage,
            "objects": objects,
        }

    def _yaml_documents(self, rendered: str) -> list[dict[str, Any]]:
        result = Command(["yq", "eval-all", "-o=json", "[.]", "-"], name="yq").run(input_text=rendered)
        documents = json.loads(result.stdout)
        return [item for item in documents if isinstance(item, dict)]

    def live_topology(self) -> dict[str, Any]:
        result = self.guarded.run(
            "get", "configmap", TOPOLOGY_NAME, "--namespace", self.release_namespace, "-o", "json"
        )
        resource = json.loads(result.stdout)
        metadata = resource.get("metadata", {})
        labels = metadata.get("labels", {})
        if (
            labels.get(BOUNDARY_LABEL) != BOUNDARY_VALUE
            or not metadata.get("uid")
            or not metadata.get("resourceVersion")
        ):
            raise TransitionError("live boundary topology is not externally owned")
        try:
            contract = json.loads(resource.get("data", {})["topology.json"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise TransitionError("live boundary topology contract is invalid") from error
        if contract.get("schema") != "fs2-serve.nebius.ai/network-policy-boundary-topology/v1":
            raise TransitionError("live boundary topology schema is unsupported")
        if contract.get("mode") != "public":
            raise TransitionError("protected live topology does not authorize a public boundary transition")
        if not isinstance(contract.get("security_owner_username"), str) or not re.fullmatch(
            r"[A-Za-z0-9:@._/-]{3,253}", contract["security_owner_username"]
        ):
            raise TransitionError("protected live topology has no valid external security owner")
        return {
            "contract": contract,
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "sha256": sha256_json(contract),
        }

    def render_candidate(self, *, release_identity: dict[str, Any] | None = None) -> Candidate:
        topology = self.live_topology()
        policy_command = [
            "template",
            self.release,
            str(self.chart),
            "--namespace",
            self.release_namespace,
            *self.helm_values,
        ]
        policy_render = self.helm.run(*policy_command, "--show-only", "templates/networkpolicy.yaml").stdout
        policies = [item for item in self._yaml_documents(policy_render) if item.get("kind") == "NetworkPolicy"]

        def exact(component: str) -> dict[str, Any]:
            matching = [
                item
                for item in policies
                if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component") == component
            ]
            if len(matching) != 1:
                raise TransitionError(f"expected one rendered {component} NetworkPolicy")
            policy = matching[0]
            namespace = policy.get("metadata", {}).get("namespace")
            name = policy.get("metadata", {}).get("name")
            spec = policy.get("spec")
            if not isinstance(spec, dict) or not isinstance(spec.get("podSelector"), dict):
                raise TransitionError(f"rendered {component} NetworkPolicy is incomplete")
            if not isinstance(namespace, str) or not namespace or not isinstance(name, str) or not name:
                raise TransitionError(f"rendered {component} NetworkPolicy identity is incomplete")
            return {"namespace": namespace, "normal_name": name, "spec": spec}

        proxy = exact("public-edge")
        controller = exact("envoy-controller-xds")
        proxy["guard_name"] = f"{proxy['normal_name']}-transition-guard"
        controller["guard_name"] = f"{controller['normal_name']}-transition-guard"
        deny_name = proxy["normal_name"].removesuffix("-public-envoy") + "-envoy-default-deny"
        expected = topology["contract"]
        if (
            proxy["namespace"] != expected.get("gateway_namespace")
            or controller["namespace"] != expected.get("controller_namespace")
            or proxy["normal_name"] != expected.get("policy_names", {}).get("proxy_normal")
            or proxy["guard_name"] != expected.get("policy_names", {}).get("proxy_guard")
            or controller["normal_name"] != expected.get("policy_names", {}).get("controller_normal")
            or controller["guard_name"] != expected.get("policy_names", {}).get("controller_guard")
            or deny_name != expected.get("policy_names", {}).get("default_deny")
        ):
            raise TransitionError("caller render does not match the protected live boundary topology")
        release = release_identity or self._release_identity(proxy, controller)
        complete_command = list(policy_command)
        if release["status"] != "absent":
            complete_command.append("--is-upgrade")
        complete_render = self.helm.run(*complete_command).stdout
        chart_hash = self._chart_hash()
        values_hash = sha256_json(self.value_sources)
        complete_render_hash = sha256_json(self._yaml_documents(complete_render))
        policy_hash = sha256_json(self._yaml_documents(policy_render))
        material = {
            "schema": SCHEMA,
            "chart_sha256": chart_hash,
            "values_sha256": values_hash,
            "complete_render_sha256": complete_render_hash,
            "network_policy_render_sha256": policy_hash,
            "release": release,
            "topology": topology,
            "policies": {"public-envoy": proxy, "envoy-controller": controller},
            "deny_name": deny_name,
        }
        return Candidate(
            candidate_sha256=sha256_json(material),
            chart_sha256=chart_hash,
            values_sha256=values_hash,
            complete_render_sha256=complete_render_hash,
            network_policy_render_sha256=policy_hash,
            release=release,
            topology=topology,
            proxy=proxy,
            controller=controller,
            deny_name=deny_name,
        )

    def get_policy(self, name: str, namespace: str) -> dict[str, Any]:
        result = self.guarded.run("get", "networkpolicy", name, "--namespace", namespace, "-o", "json")
        return cast(dict[str, Any], json.loads(result.stdout))

    def get_optional_policy(self, name: str, namespace: str) -> dict[str, Any] | None:
        result = self.guarded.run(
            "get",
            "networkpolicy",
            name,
            "--namespace",
            namespace,
            "--ignore-not-found",
            "-o",
            "json",
            check=False,
        )
        if result.returncode != 0:
            raise TransitionError("default-deny lookup failed without a verified NotFound")
        if not result.stdout.strip():
            return None
        return cast(dict[str, Any], json.loads(result.stdout))

    @staticmethod
    def _verify_boundary_identity(policy: dict[str, Any], *, role: str) -> None:
        metadata = policy.get("metadata", {})
        labels = metadata.get("labels", {})
        if labels.get(BOUNDARY_LABEL) != BOUNDARY_VALUE or labels.get(ROLE_LABEL) != role:
            raise TransitionError(f"{role} boundary is not externally owned")
        if not metadata.get("uid") or not metadata.get("resourceVersion"):
            raise TransitionError(f"{role} boundary lacks Kubernetes identity")

    def patch_guard(self, role: str, policy: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
        current = self.get_policy(policy["guard_name"], policy["namespace"])
        self._verify_boundary_identity(current, role=role)
        annotations = {
            CANDIDATE_ANNOTATION: candidate.candidate_sha256,
            NORMAL_ANNOTATION: policy["normal_name"],
            FENCE_ANNOTATION: self.fence_identity,
        }
        if role == "public-envoy":
            annotations[DENY_ANNOTATION] = candidate.deny_name
        patch = canonical(
            {
                "metadata": {
                    "resourceVersion": current["metadata"]["resourceVersion"],
                    "annotations": annotations,
                },
                "spec": policy["spec"],
            }
        )
        common = (
            "patch",
            "networkpolicy",
            policy["guard_name"],
            "--namespace",
            policy["namespace"],
            "--type=merge",
            "--patch",
            patch,
        )
        self.guarded.run(*common, "--dry-run=server", "-o", "json")
        self._renew_fence()
        result = self.guarded.run(
            *common,
            "-o",
            "json",
        )
        updated = cast(dict[str, Any], json.loads(result.stdout))
        self.verify_guard(role, policy, candidate, updated)
        return updated

    def verify_guard(
        self,
        role: str,
        policy: dict[str, Any],
        candidate: Candidate,
        live: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = live or self.get_policy(policy["guard_name"], policy["namespace"])
        self._verify_boundary_identity(current, role=role)
        if current.get("spec") != policy["spec"]:
            raise TransitionError(f"{role} boundary spec does not match the candidate")
        annotations = current.get("metadata", {}).get("annotations", {})
        if annotations.get(CANDIDATE_ANNOTATION) != candidate.candidate_sha256:
            raise TransitionError(f"{role} boundary is bound to a stale candidate")
        self.verify_ready_pods(policy["namespace"], policy["spec"], role=role)
        return current

    def verify_ready_pods(self, namespace: str, spec: dict[str, Any], *, role: str) -> int:
        selector = spec.get("podSelector", {})
        labels = selector.get("matchLabels")
        if not isinstance(labels, dict) or not labels or selector.get("matchExpressions"):
            raise TransitionError(f"{role} boundary requires an exact matchLabels selector")
        ready = [
            pod
            for pod in self.list_pods_bounded(namespace, labels)
            if any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
        ]
        if not ready:
            raise TransitionError(f"{role} boundary selects zero Ready Pods")
        return len(ready)

    def list_pods_bounded(self, namespace: str, labels: dict[str, str]) -> list[dict[str, Any]]:
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        encoded_namespace = urllib.parse.quote(namespace, safe="")
        encoded_selector = urllib.parse.quote(selector, safe="")
        continuation = ""
        items: list[dict[str, Any]] = []
        for _ in range(MAX_POD_PAGES):
            query = f"limit={POD_PAGE_SIZE}&labelSelector={encoded_selector}"
            if continuation:
                query += f"&continue={urllib.parse.quote(continuation, safe='')}"
            result = self.guarded.run("get", "--raw", f"/api/v1/namespaces/{encoded_namespace}/pods?{query}")
            page = json.loads(result.stdout)
            page_items = page.get("items", [])
            if not isinstance(page_items, list) or len(page_items) > POD_PAGE_SIZE:
                raise TransitionError("Pod list page exceeded its enforced bound")
            items.extend(item for item in page_items if isinstance(item, dict))
            continuation = str(page.get("metadata", {}).get("continue", ""))
            if not continuation:
                return items
        raise TransitionError("Pod selector exceeded bounded pagination")

    def verify_normal(self, role: str, policy: dict[str, Any]) -> None:
        normal = self.get_policy(policy["normal_name"], policy["namespace"])
        if normal.get("spec") != policy["spec"]:
            raise TransitionError(f"{role} Helm policy does not match the staged boundary")
        self.verify_ready_pods(policy["namespace"], normal["spec"], role=role)

    def _deny_patch(
        self,
        candidate: Candidate,
        spec: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        patch = canonical(
            {
                "metadata": {
                    "resourceVersion": current["metadata"]["resourceVersion"],
                    "annotations": {
                        CANDIDATE_ANNOTATION: candidate.candidate_sha256,
                        FENCE_ANNOTATION: self.fence_identity,
                    },
                },
                "spec": spec,
            }
        )
        common = (
            "patch",
            "networkpolicy",
            candidate.deny_name,
            "--namespace",
            candidate.proxy["namespace"],
            "--type=merge",
            "--patch",
            patch,
        )
        self.guarded.run(*common, "--dry-run=server", "-o", "json")
        self._renew_fence()
        result = self.guarded.run(
            *common,
            "-o",
            "json",
        )
        return cast(dict[str, Any], json.loads(result.stdout))

    def activate_deny(self, candidate: Candidate) -> None:
        current = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
        self._verify_boundary_identity(current, role="default-deny")
        expected = {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []}
        if self._deny_patch(candidate, expected, current).get("spec") != expected:
            raise TransitionError("default-deny did not become active")

    def verify_deny_active(self, candidate: Candidate) -> None:
        current = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
        expected = {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []}
        if current.get("spec") != expected:
            raise TransitionError("default-deny is not active")
        if current.get("metadata", {}).get("annotations", {}).get(CANDIDATE_ANNOTATION) != candidate.candidate_sha256:
            raise TransitionError("default-deny is bound to a stale candidate")

    def relax_deny(self, candidate: Candidate) -> str:
        current = self.get_optional_policy(candidate.deny_name, candidate.proxy["namespace"])
        if current is None:
            return "verified-not-found"
        self._verify_boundary_identity(current, role="default-deny")
        expected = {
            "podSelector": {"matchLabels": RELAXED_SELECTOR},
            "policyTypes": ["Ingress"],
            "ingress": [],
        }
        if self._deny_patch(candidate, expected, current).get("spec") != expected:
            raise TransitionError("default-deny was not relaxed")
        if self.list_pods_bounded(candidate.proxy["namespace"], RELAXED_SELECTOR):
            raise TransitionError("relaxed default-deny still selects Pods")
        proof = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
        if proof.get("spec") != expected:
            raise TransitionError("default-deny relaxation proof changed")
        if proof.get("metadata", {}).get("annotations", {}).get(CANDIDATE_ANNOTATION) != candidate.candidate_sha256:
            raise TransitionError("relaxed default-deny is bound to a stale candidate")
        return "relaxed-zero-selected-pods"

    def receipt(self) -> tuple[dict[str, Any], dict[str, Any]]:
        result = self.guarded.run("get", "configmap", RECEIPT_NAME, "--namespace", self.release_namespace, "-o", "json")
        resource = json.loads(result.stdout)
        encoded = resource.get("data", {}).get("receipt.json", "")
        try:
            receipt = json.loads(encoded) if encoded else {}
        except json.JSONDecodeError as error:
            raise TransitionError("transition receipt is not valid JSON") from error
        return resource, receipt

    @staticmethod
    def _guard_receipt(policy: dict[str, Any]) -> dict[str, Any]:
        metadata = policy["metadata"]
        spec = policy["spec"]
        return {
            "namespace": metadata["namespace"],
            "name": metadata["name"],
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "spec_sha256": sha256_json(spec),
            "spec": spec,
        }

    def write_receipt(
        self,
        phase: str,
        candidate: Candidate,
        guards: dict[str, dict[str, Any]],
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resource, previous = self.receipt()
        metadata = resource["metadata"]
        receipt = {
            "schema": SCHEMA,
            "phase": phase,
            "candidate": candidate.as_dict(),
            "boundary_objects": {role: self._guard_receipt(policy) for role, policy in sorted(guards.items())},
            "receipt_object": {
                "namespace": metadata["namespace"],
                "name": metadata["name"],
                "uid": metadata["uid"],
                "prior_resource_version": metadata["resourceVersion"],
            },
            "previous_phase": previous.get("phase", "uninitialized"),
            "fence": {
                "holder_identity": self.holder,
                "lease_transitions": self.fence_transitions,
            },
        }
        deny = self.get_optional_policy(candidate.deny_name, candidate.proxy["namespace"])
        if deny is None:
            receipt["boundary_objects"]["default-deny"] = {
                "namespace": candidate.proxy["namespace"],
                "name": candidate.deny_name,
                "status": "verified-not-found",
            }
        else:
            self._verify_boundary_identity(deny, role="default-deny")
            receipt["boundary_objects"]["default-deny"] = self._guard_receipt(deny)
        if extra:
            receipt.update(extra)
        patch = {
            "metadata": {"resourceVersion": metadata["resourceVersion"]},
            "data": {"receipt.json": canonical(receipt)},
        }
        self._renew_fence()
        result = self.guarded.run(
            "patch",
            "configmap",
            RECEIPT_NAME,
            "--namespace",
            self.release_namespace,
            "--type=merge",
            "--patch",
            canonical(patch),
            "-o",
            "json",
        )
        return cast(dict[str, Any], json.loads(result.stdout))

    @staticmethod
    def verify_receipt_boundaries(receipt: dict[str, Any], boundaries: dict[str, dict[str, Any]]) -> None:
        recorded = receipt.get("boundary_objects", {})
        for role, boundary in boundaries.items():
            expected = recorded.get(role, {})
            metadata = boundary.get("metadata", {})
            if (
                expected.get("uid") != metadata.get("uid")
                or expected.get("resource_version") != metadata.get("resourceVersion")
                or expected.get("spec_sha256") != sha256_json(boundary.get("spec"))
                or expected.get("spec") != boundary.get("spec")
            ):
                raise TransitionError(f"{role} boundary no longer matches the durable receipt")

    @staticmethod
    def verify_receipt_boundary_uids(receipt: dict[str, Any], boundaries: dict[str, dict[str, Any]]) -> None:
        """Permit crash-resume spec/RV drift only for the exact durable objects."""
        recorded = receipt.get("boundary_objects", {})
        for role, boundary in boundaries.items():
            expected = recorded.get(role, {})
            metadata = boundary.get("metadata", {})
            if (
                expected.get("namespace") != metadata.get("namespace")
                or expected.get("name") != metadata.get("name")
                or expected.get("uid") != metadata.get("uid")
            ):
                raise TransitionError(f"{role} boundary identity changed during durable intent recovery")

    def boundary_objects(self, candidate: Candidate) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        guards = {
            "public-envoy": self.get_policy(candidate.proxy["guard_name"], candidate.proxy["namespace"]),
            "envoy-controller": self.get_policy(candidate.controller["guard_name"], candidate.controller["namespace"]),
        }
        deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
        for role, boundary in guards.items():
            self._verify_boundary_identity(boundary, role=role)
        self._verify_boundary_identity(deny, role="default-deny")
        return guards, deny

    @staticmethod
    def intent(operation: str, target: Any) -> dict[str, Any]:
        return {
            "operation": operation,
            "target_sha256": sha256_json(target),
            "target": target,
        }

    def current_guards(self, candidate: Candidate) -> dict[str, dict[str, Any]]:
        return {
            "envoy-controller": self.verify_guard("envoy-controller", candidate.controller, candidate),
            "public-envoy": self.verify_guard("public-envoy", candidate.proxy, candidate),
        }

    @staticmethod
    def candidate_from_receipt(receipt: dict[str, Any]) -> Candidate:
        encoded = receipt.get("candidate")
        if not isinstance(encoded, dict) or encoded.get("candidate_sha256") is None:
            raise TransitionError("transition receipt has no candidate identity")
        policies = encoded.get("policies", {})
        return Candidate(
            candidate_sha256=encoded["candidate_sha256"],
            chart_sha256=encoded["chart_sha256"],
            values_sha256=encoded["values_sha256"],
            complete_render_sha256=encoded["complete_render_sha256"],
            network_policy_render_sha256=encoded["network_policy_render_sha256"],
            release=encoded["release"],
            topology=encoded["topology"],
            proxy=policies["public-envoy"],
            controller=policies["envoy-controller"],
            deny_name=encoded["deny_name"],
        )

    def verify_local_candidate(self, receipt: dict[str, Any]) -> Candidate:
        recorded = self.candidate_from_receipt(receipt)
        rendered = self.render_candidate(release_identity=recorded.release)
        if rendered.as_dict() != recorded.as_dict():
            raise TransitionError("local chart/values/render no longer match the durable candidate")
        return recorded

    def verify_live_topology(self, candidate: Candidate) -> None:
        if self.live_topology() != candidate.topology:
            raise TransitionError("protected live boundary topology changed from the durable receipt")

    def verify_deployed_target(self, candidate: Candidate) -> dict[str, Any]:
        deployed = self._release_identity(candidate.proxy, candidate.controller)
        source = candidate.release
        expected_revision = 1 if source["status"] == "absent" else int(source["revision"]) + 1
        if int(deployed["revision"]) != expected_revision:
            raise TransitionError("deployed Helm revision is not the candidate's exact successor")
        if deployed["deployed_manifest_sha256"] != candidate.complete_render_sha256:
            raise TransitionError("deployed Helm manifest does not match the candidate render")
        if source["status"] != "absent":
            for role, identity in source.get("objects", {}).items():
                if deployed.get("objects", {}).get(role, {}).get("uid") != identity.get("uid"):
                    raise TransitionError(f"Helm release {role} UID changed during upgrade")
        return deployed

    def capture_rollback_source(self, candidate: Candidate) -> dict[str, Any] | None:
        """Bind the pre-upgrade release both before and after Helm supersedes it."""
        source = candidate.release
        if source.get("status") == "absent":
            return None
        if source.get("status") != "deployed":
            raise TransitionError("staged rollback source was not a successful deployed release")
        revision = str(source.get("revision"))
        history = json.loads(
            self.helm.run("history", self.release, "--namespace", self.release_namespace, "-o", "json").stdout
        )
        matches = [entry for entry in history if str(entry.get("revision")) == revision]
        if len(matches) != 1 or not self._successful_history_entry(matches[0]):
            raise TransitionError("staged source is no longer a successful stable Helm revision")
        current_storage = self._release_storage_identity(revision, str(matches[0].get("status", "")))
        staged_storage = source.get("storage", {})
        for field in ("name", "uid", "revision"):
            if current_storage.get(field) != staged_storage.get(field):
                raise TransitionError("staged source Helm storage identity changed across upgrade")
        manifest_sha256 = sha256_json(self._yaml_documents(self._target_manifest(revision)))
        if manifest_sha256 != source.get("deployed_manifest_sha256"):
            raise TransitionError("staged source Helm manifest changed across upgrade")
        values = json.loads(
            self.helm.run(
                "get",
                "values",
                self.release,
                "--namespace",
                self.release_namespace,
                "--revision",
                revision,
                "--all",
                "-o",
                "json",
            ).stdout
        )
        values_sha256 = sha256_json(values)
        if values_sha256 != source.get("deployed_values_sha256"):
            raise TransitionError("staged source Helm values changed across upgrade")
        return {
            "target_revision": revision,
            "target_history": matches[0],
            "target_manifest_sha256": manifest_sha256,
            "target_values_sha256": values_sha256,
            "staged_storage": staged_storage,
            "current_storage": current_storage,
            "request_debug_enabled": values.get("config", {}).get("requestDebugEnabled"),
        }

    @staticmethod
    def staged_rollback_source(candidate: Candidate) -> dict[str, Any]:
        source = candidate.release
        return {
            "target_revision": str(source.get("revision")),
            "target_manifest_sha256": source.get("deployed_manifest_sha256"),
            "target_values_sha256": source.get("deployed_values_sha256"),
            "staged_storage": source.get("storage"),
            "current_storage": source.get("storage"),
            "request_debug_enabled": source.get("request_debug_enabled"),
        }

    def stage(self) -> None:
        with self.lock():
            candidate = self.render_candidate()
            if candidate.release["status"] != "deployed":
                raise TransitionError("stage requires a successful deployed release with Ready boundary Pods")
            self._stage_locked(candidate)
        print(f"network-policy-transition=staged candidate={candidate.candidate_sha256}")

    def _stage_locked(self, candidate: Candidate) -> None:
        _, prior = self.receipt()
        phase = prior.get("phase")
        prior_candidate = prior.get("candidate", {}).get("candidate_sha256")
        if phase in IN_FLIGHT_PHASES and prior_candidate != candidate.candidate_sha256:
            raise TransitionError("a different durable NetworkPolicy transition is still in flight")
        if phase in {"staged", "active"} and prior_candidate == candidate.candidate_sha256:
            guards = self.current_guards(candidate)
            self.verify_deny_active(candidate)
            deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
            self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
            self.write_receipt(phase, candidate, guards)
            return
        guards, deny = self.boundary_objects(candidate)
        if phase == "guards-ready" and prior_candidate == candidate.candidate_sha256:
            self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
            guards = self.current_guards(candidate)
            self.activate_deny(candidate)
            self.write_receipt("staged", candidate, guards)
            return
        if phase == "guards-staging" and prior_candidate == candidate.candidate_sha256:
            self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
        else:
            self.write_receipt(
                "guards-staging",
                candidate,
                guards,
                extra={
                    "intent": self.intent(
                        "stage-guards",
                        {
                            "public-envoy": candidate.proxy["spec"],
                            "envoy-controller": candidate.controller["spec"],
                        },
                    )
                },
            )
        proxy = self.patch_guard("public-envoy", candidate.proxy, candidate)
        controller = self.patch_guard("envoy-controller", candidate.controller, candidate)
        guards = {"public-envoy": proxy, "envoy-controller": controller}
        self.write_receipt(
            "guards-ready",
            candidate,
            guards,
            extra={"intent": self.intent("activate-deny", {"podSelector": {}, "policyTypes": ["Ingress"]})},
        )
        self.activate_deny(candidate)
        self.write_receipt("staged", candidate, guards)

    def prepare(self) -> None:
        with self.lock():
            candidate = self.render_candidate()
            if candidate.release["status"] != "absent":
                if candidate.release["status"] != "deployed":
                    raise TransitionError("prepare requires an absent or successful deployed release")
                self._stage_locked(candidate)
                print(f"network-policy-transition=staged candidate={candidate.candidate_sha256}")
                return
            _, prior = self.receipt()
            phase = prior.get("phase")
            prior_candidate = prior.get("candidate", {}).get("candidate_sha256")
            if phase in {"bootstrap-relaxing", "bootstrap-ready"} and prior_candidate != candidate.candidate_sha256:
                raise TransitionError("bootstrap receipt is bound to a different candidate")
            guards, deny = self.boundary_objects(candidate)
            if phase == "bootstrap-ready":
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                print(f"network-policy-transition=bootstrap-ready candidate={candidate.candidate_sha256} deny=relaxed")
                return
            if phase == "bootstrap-relaxing":
                self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
            else:
                self.write_receipt(
                    "bootstrap-relaxing",
                    candidate,
                    guards,
                    extra={"intent": self.intent("relax-deny", RELAXED_SELECTOR)},
                )
            deny_proof = self.relax_deny(candidate)
            self.write_receipt(
                "bootstrap-ready",
                candidate,
                guards,
                extra={"bootstrap": {"deny_proof": deny_proof}},
            )
        print(f"network-policy-transition=bootstrap-ready candidate={candidate.candidate_sha256} deny=relaxed")

    def complete(self) -> None:
        with self.lock():
            _, prior = self.receipt()
            if prior.get("phase") not in {
                "bootstrap-ready",
                "bootstrap-guards-staging",
                "guards-ready",
                "staged",
                "active",
            }:
                raise TransitionError("transition receipt is not in a resumable completion phase")
            candidate = self.verify_local_candidate(prior)
            deployed_release = self.verify_deployed_target(candidate)
            rollback_source = self.capture_rollback_source(candidate)
            completion = {"deployed_release": deployed_release, "rollback_source": rollback_source}
            phase = prior.get("phase")
            if phase == "bootstrap-ready":
                bootstrap_guards, bootstrap_deny = self.boundary_objects(candidate)
                self.verify_receipt_boundaries(prior, {**bootstrap_guards, "default-deny": bootstrap_deny})
                self.write_receipt(
                    "bootstrap-guards-staging",
                    candidate,
                    bootstrap_guards,
                    extra={
                        **completion,
                        "intent": self.intent(
                            "stage-bootstrap-guards",
                            {
                                "public-envoy": candidate.proxy["spec"],
                                "envoy-controller": candidate.controller["spec"],
                            },
                        ),
                    },
                )
                phase = "bootstrap-guards-staging"
                _, prior = self.receipt()
            if phase == "bootstrap-guards-staging":
                bootstrap_guards, bootstrap_deny = self.boundary_objects(candidate)
                self.verify_receipt_boundary_uids(prior, {**bootstrap_guards, "default-deny": bootstrap_deny})
                proxy = self.patch_guard("public-envoy", candidate.proxy, candidate)
                controller = self.patch_guard("envoy-controller", candidate.controller, candidate)
                guards = {"public-envoy": proxy, "envoy-controller": controller}
                self.write_receipt(
                    "guards-ready",
                    candidate,
                    guards,
                    extra={
                        **completion,
                        "intent": self.intent("activate-deny", {"podSelector": {}, "policyTypes": ["Ingress"]}),
                    },
                )
                phase = "guards-ready"
                _, prior = self.receipt()
            if phase == "guards-ready":
                guards = self.current_guards(candidate)
                deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
                self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
                self.activate_deny(candidate)
                self.write_receipt("staged", candidate, guards, extra=completion)
                phase = "staged"
                _, prior = self.receipt()
            else:
                guards = self.current_guards(candidate)
                if phase == "active" and (
                    prior.get("deployed_release") != deployed_release or prior.get("rollback_source") != rollback_source
                ):
                    raise TransitionError("receipt is not bound to the exact deployed release")
            self.verify_normal("public-envoy", candidate.proxy)
            self.verify_normal("envoy-controller", candidate.controller)
            self.verify_deny_active(candidate)
            deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
            self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
            self.write_receipt("active", candidate, guards, extra=completion)
        print(f"network-policy-transition=active candidate={candidate.candidate_sha256} boundaries=permanent")

    def _target_manifest(self, revision: str) -> str:
        return self.helm.run(
            "get",
            "manifest",
            self.release,
            "--namespace",
            self.release_namespace,
            "--revision",
            revision,
        ).stdout

    def _current_manifest(self) -> str:
        return self.helm.run("get", "manifest", self.release, "--namespace", self.release_namespace).stdout

    def _rollback_target(
        self,
        source: Candidate,
        revision: str,
        bound_source: dict[str, Any],
    ) -> dict[str, Any]:
        if source.release.get("status") != "deployed" or revision != str(source.release.get("revision")):
            raise TransitionError("rollback revision is not the receipt-bound stable source revision")
        if bound_source.get("target_revision") != revision:
            raise TransitionError("rollback source receipt names a different Helm revision")
        if bound_source.get("staged_storage") != source.release.get("storage"):
            raise TransitionError("rollback source receipt lost the exact staged storage resourceVersion")
        if bound_source.get("target_values_sha256") != source.release.get("deployed_values_sha256"):
            raise TransitionError("rollback source receipt lost the exact staged Helm values")
        if bound_source.get("target_manifest_sha256") != source.release.get("deployed_manifest_sha256"):
            raise TransitionError("rollback source receipt lost the exact staged Helm manifest")
        history = json.loads(
            self.helm.run("history", self.release, "--namespace", self.release_namespace, "-o", "json").stdout
        )
        matches = [entry for entry in history if str(entry.get("revision")) == revision]
        if len(matches) != 1 or not self._successful_history_entry(matches[0]):
            raise TransitionError("rollback target is not a successful stable Helm revision")
        target_storage = self._release_storage_identity(revision, str(matches[0].get("status", "")))
        if target_storage != bound_source.get("current_storage"):
            raise TransitionError("rollback target storage UID/resourceVersion/status changed from its receipt")
        manifest_sha256 = sha256_json(self._yaml_documents(self._target_manifest(revision)))
        if manifest_sha256 != source.release.get("deployed_manifest_sha256"):
            raise TransitionError("rollback target manifest differs from the staged source release")
        values = json.loads(
            self.helm.run(
                "get",
                "values",
                self.release,
                "--namespace",
                self.release_namespace,
                "--revision",
                revision,
                "--all",
                "-o",
                "json",
            ).stdout
        )
        values_sha256 = sha256_json(values)
        if values_sha256 != bound_source.get("target_values_sha256"):
            raise TransitionError("rollback target values differ from the staged source receipt")
        if (
            bound_source.get("request_debug_enabled") is not False
            or values.get("config", {}).get("requestDebugEnabled") is not False
        ):
            raise TransitionError("rollback target is ineligible while request debugging is enabled or unknown")
        return {
            "target_revision": revision,
            "target_history": matches[0],
            "target_manifest_sha256": manifest_sha256,
            "target_values_sha256": values_sha256,
            "target_storage": target_storage,
            "staged_storage": bound_source["staged_storage"],
            "request_debug_enabled": False,
            "source_release": source.release,
        }

    def _revalidate_rollback_target(self, rollback: dict[str, Any], revision: str) -> None:
        if rollback.get("target_revision") != revision or rollback.get("request_debug_enabled") is not False:
            raise TransitionError("durable rollback target does not match the request")
        source_release = rollback.get("source_release", {})
        if (
            rollback.get("staged_storage") != source_release.get("storage")
            or rollback.get("target_manifest_sha256") != source_release.get("deployed_manifest_sha256")
            or rollback.get("target_values_sha256") != source_release.get("deployed_values_sha256")
        ):
            raise TransitionError("durable rollback receipt lost its exact staged Helm source binding")
        history = json.loads(
            self.helm.run("history", self.release, "--namespace", self.release_namespace, "-o", "json").stdout
        )
        matches = [entry for entry in history if str(entry.get("revision")) == revision]
        if len(matches) != 1 or not self._successful_history_entry(matches[0]):
            raise TransitionError("durable rollback target is no longer a successful stable revision")
        storage = self._release_storage_identity(revision, str(matches[0].get("status", "")))
        recorded_storage = rollback.get("target_storage", {})
        if storage != recorded_storage:
            raise TransitionError("durable rollback target Helm storage UID/resourceVersion/status changed")
        if sha256_json(self._yaml_documents(self._target_manifest(revision))) != rollback.get("target_manifest_sha256"):
            raise TransitionError("durable rollback target manifest changed")
        values = json.loads(
            self.helm.run(
                "get",
                "values",
                self.release,
                "--namespace",
                self.release_namespace,
                "--revision",
                revision,
                "--all",
                "-o",
                "json",
            ).stdout
        )
        if values.get("config", {}).get("requestDebugEnabled") is not False:
            raise TransitionError("rollback target request-debug state is no longer disabled")
        if sha256_json(values) != rollback.get("target_values_sha256"):
            raise TransitionError("durable rollback target values changed")

    def _candidate_from_live(self, source: Candidate, deployed_release: dict[str, Any]) -> Candidate:
        proxy = dict(source.proxy)
        controller = dict(source.controller)
        proxy["spec"] = self.get_policy(proxy["normal_name"], proxy["namespace"])["spec"]
        controller["spec"] = self.get_policy(controller["normal_name"], controller["namespace"])["spec"]
        material = {
            "schema": SCHEMA,
            "source": "verified-live-rollback",
            "release": deployed_release,
            "topology": source.topology,
            "policies": {"public-envoy": proxy, "envoy-controller": controller},
            "deny_name": source.deny_name,
        }
        return Candidate(
            candidate_sha256=sha256_json(material),
            chart_sha256=source.chart_sha256,
            values_sha256=source.values_sha256,
            complete_render_sha256=deployed_release["deployed_manifest_sha256"],
            network_policy_render_sha256=sha256_json(
                {"public-envoy": proxy["spec"], "envoy-controller": controller["spec"]}
            ),
            release=deployed_release,
            topology=source.topology,
            proxy=proxy,
            controller=controller,
            deny_name=source.deny_name,
        )

    def rollback(self) -> None:
        revision = self.arguments.revision
        with self.lock():
            _, prior = self.receipt()
            candidate = self.candidate_from_receipt(prior)
            self.verify_live_topology(candidate)
            phase = prior.get("phase")
            rollback_state = prior.get("rollback", {})
            if phase == "rolled-back" and rollback_state.get("target_revision") == revision:
                self._revalidate_rollback_target(rollback_state, revision)
                deployed = self._release_identity(candidate.proxy, candidate.controller)
                if deployed != candidate.release:
                    raise TransitionError("rolled-back release identity no longer matches its receipt")
                guards = self.current_guards(candidate)
                self.verify_deny_active(candidate)
                deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                self.write_receipt(
                    "rolled-back",
                    candidate,
                    guards,
                    extra={"rollback": rollback_state},
                )
                print(f"network-policy-transition=rolled-back revision={revision} idempotent=true")
                return
            if phase not in {
                "staged",
                "active",
                "rollback-relaxing",
                "rollback-prepared",
                "rollback-guards-staging",
                "rollback-guards-ready",
            }:
                raise TransitionError("receipt phase does not authorize rollback")
            guards, deny = self.boundary_objects(candidate)
            if phase in {"staged", "active"}:
                bound_source = prior.get("rollback_source") or self.staged_rollback_source(candidate)
                rollback_state = self._rollback_target(candidate, revision, bound_source)
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                current = self._release_identity(candidate.proxy, candidate.controller, require_deployed=False)
                if current["status"] == "absent" or int(current["revision"]) <= int(revision):
                    raise TransitionError("current Helm release is not newer than the rollback target")
                rollback_state["current_release_before_rollback"] = current
                self.write_receipt(
                    "rollback-relaxing",
                    candidate,
                    guards,
                    extra={
                        "rollback": rollback_state,
                        "intent": self.intent("relax-deny-for-rollback", RELAXED_SELECTOR),
                    },
                )
                phase = "rollback-relaxing"
                _, prior = self.receipt()
            if phase == "rollback-relaxing":
                self._revalidate_rollback_target(rollback_state, revision)
                guards, deny = self.boundary_objects(candidate)
                self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
                deny_proof = self.relax_deny(candidate)
                rollback_state["deny_proof"] = deny_proof
                self.write_receipt("rollback-prepared", candidate, guards, extra={"rollback": rollback_state})
                phase = "rollback-prepared"
                _, prior = self.receipt()
            if phase in {"rollback-prepared", "rollback-guards-staging", "rollback-guards-ready"}:
                self._revalidate_rollback_target(rollback_state, revision)
            current_hash = sha256_json(self._yaml_documents(self._current_manifest()))
            target_hash = rollback_state["target_manifest_sha256"]
            if current_hash != target_hash:
                self.run_fenced_helm(
                    "rollback",
                    self.release,
                    revision,
                    "--namespace",
                    self.release_namespace,
                    "--wait",
                    "--wait-for-jobs",
                    "--timeout",
                    self.arguments.timeout,
                )
            if sha256_json(self._yaml_documents(self._current_manifest())) != target_hash:
                raise TransitionError("Helm rollback did not reach the target manifest")
            deployed = self._release_identity(candidate.proxy, candidate.controller)
            if deployed["deployed_manifest_sha256"] != target_hash:
                raise TransitionError("rolled-back release identity has the wrong manifest")
            live_candidate = self._candidate_from_live(candidate, deployed)
            live_guards, live_deny = self.boundary_objects(live_candidate)
            if phase == "rollback-prepared":
                self.write_receipt(
                    "rollback-guards-staging",
                    live_candidate,
                    live_guards,
                    extra={
                        "rollback": rollback_state,
                        "intent": self.intent(
                            "stage-rollback-guards",
                            {
                                "public-envoy": live_candidate.proxy["spec"],
                                "envoy-controller": live_candidate.controller["spec"],
                            },
                        ),
                    },
                )
                phase = "rollback-guards-staging"
                _, prior = self.receipt()
            if phase == "rollback-guards-staging":
                self.verify_receipt_boundary_uids(prior, {**live_guards, "default-deny": live_deny})
                proxy = self.patch_guard("public-envoy", live_candidate.proxy, live_candidate)
                controller = self.patch_guard("envoy-controller", live_candidate.controller, live_candidate)
                live_guards = {"public-envoy": proxy, "envoy-controller": controller}
                self.write_receipt(
                    "rollback-guards-ready",
                    live_candidate,
                    live_guards,
                    extra={
                        "rollback": rollback_state,
                        "intent": self.intent("activate-deny-after-rollback", {"podSelector": {}}),
                    },
                )
                phase = "rollback-guards-ready"
                _, prior = self.receipt()
            if phase == "rollback-guards-ready":
                self.verify_receipt_boundary_uids(prior, {**live_guards, "default-deny": live_deny})
            self.activate_deny(live_candidate)
            self.write_receipt("rolled-back", live_candidate, live_guards, extra={"rollback": rollback_state})
        print(f"network-policy-transition=rolled-back revision={revision} deny=active")

    def destroy(self) -> None:
        with self.lock():
            _, prior = self.receipt()
            candidate = self.candidate_from_receipt(prior)
            self.verify_live_topology(candidate)
            phase = prior.get("phase")
            guards, deny = self.boundary_objects(candidate)
            if phase == "destroy-prepared":
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                print("network-policy-transition=destroy-prepared deny=relaxed boundaries=retained")
                return
            if phase == "destroy-relaxing":
                self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
            else:
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                self.write_receipt(
                    "destroy-relaxing",
                    candidate,
                    guards,
                    extra={
                        "destroy": {},
                        "intent": self.intent("relax-deny-for-destroy", RELAXED_SELECTOR),
                    },
                )
            deny_proof = self.relax_deny(candidate)
            self.write_receipt(
                "destroy-prepared",
                candidate,
                guards,
                extra={"destroy": {"deny_proof": deny_proof}},
            )
        print("network-policy-transition=destroy-prepared deny=relaxed boundaries=retained")


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "stage", "complete", "rollback", "destroy"))
    parser.add_argument("--release", required=True)
    parser.add_argument("--release-namespace", required=True)
    parser.add_argument("--chart", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--security-owner-kubeconfig", required=True)
    parser.add_argument("--context", default="")
    parser.add_argument("--values", action="append", default=[])
    parser.add_argument("--values-env", action="append", default=[])
    parser.add_argument("--revision", default="")
    parser.add_argument("--timeout", default="10m")
    parser.add_argument("helm_value_args", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    if arguments.action == "rollback" and not re.fullmatch(r"[1-9][0-9]*", arguments.revision):
        parser.error("rollback requires a positive --revision")
    for executable in ("helm", "kubectl", "yq"):
        if shutil.which(executable) is None:
            parser.error(f"required executable is unavailable: {executable}")
    if (
        not Path(arguments.chart).is_dir()
        or not Path(arguments.kubeconfig).is_file()
        or not Path(arguments.security_owner_kubeconfig).is_file()
    ):
        parser.error("chart and both kubeconfigs must exist")
    if Path(arguments.kubeconfig).resolve() == Path(arguments.security_owner_kubeconfig).resolve():
        parser.error("ordinary and security-owner kubeconfigs must be distinct")
    if stat.S_IMODE(Path(arguments.security_owner_kubeconfig).stat().st_mode) != 0o600:
        parser.error("security-owner kubeconfig must have mode 0600")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv or sys.argv[1:])
    transition = Transition(arguments)
    try:
        transition.configure_guarded_client()
        getattr(transition, arguments.action)()
    except (TransitionError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"network policy transition failed closed: {error}", file=sys.stderr)
        return 1
    finally:
        transition.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
