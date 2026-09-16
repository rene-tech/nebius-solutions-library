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
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "fs2-serve.nebius.ai/network-policy-transition-receipt/v2"
BOUNDARY_LABEL = "fs2.nebius.ai/network-policy-boundary"
BOUNDARY_VALUE = "permanent"
ROLE_LABEL = "fs2.nebius.ai/network-policy-role"
CANDIDATE_ANNOTATION = "fs2.nebius.ai/network-policy-candidate-sha256"
NORMAL_ANNOTATION = "fs2.nebius.ai/normal-policy-name"
DENY_ANNOTATION = "fs2.nebius.ai/deny-policy-name"
SERVICE_ACCOUNT = "fs2-network-policy-transition"
LEASE_NAME = "fs2-network-policy-transition"
RECEIPT_NAME = "fs2-network-policy-transition"
LOCK_SECONDS = 3600
RELAXED_SELECTOR = {"fs2.nebius.ai/network-policy-deny-relaxed": "true"}


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
    network_policy_render_sha256: str
    release: dict[str, Any]
    proxy: dict[str, Any]
    controller: dict[str, Any]
    deny_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_sha256": self.candidate_sha256,
            "chart_sha256": self.chart_sha256,
            "values_sha256": self.values_sha256,
            "network_policy_render_sha256": self.network_policy_render_sha256,
            "release": self.release,
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
        self.tempdir = Path(tempfile.mkdtemp(prefix="fs2-network-policy-transition."))
        self.holder = f"{os.uname().nodename}-{os.getpid()}-{uuid.uuid4()}"
        self.bootstrap_kubectl = Command(self._kubectl_prefix(self.kubeconfig), name="kubectl")
        helm_prefix = ["helm", "--kubeconfig", str(self.kubeconfig)]
        if arguments.context:
            helm_prefix.extend(["--kube-context", arguments.context])
        self.helm = Command(helm_prefix, name="helm")
        self.helm_values = self._helm_values()
        self.guarded_kubectl: Command | None = None

    def close(self) -> None:
        shutil.rmtree(self.tempdir)

    def _kubectl_prefix(self, kubeconfig: Path) -> list[str]:
        prefix = ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=30s"]
        if self.arguments.context and kubeconfig == self.kubeconfig:
            prefix.extend(["--context", self.arguments.context])
        return prefix

    def _helm_values(self) -> list[str]:
        result: list[str] = []
        for path_value in self.arguments.values:
            path = Path(path_value)
            if not path.is_file():
                raise TransitionError(f"values file does not exist: {path}")
            result.extend(["--values", str(path.resolve())])
        for index, name in enumerate(self.arguments.values_env):
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name not in os.environ:
                raise TransitionError("values environment name is invalid or unset")
            path = self.tempdir / f"values-{index}.yaml"
            path.write_text(os.environ[name] + "\n", encoding="utf-8")
            result.extend(["--values", str(path)])
        passthrough = list(self.arguments.helm_value_args)
        if passthrough[:1] == ["--"]:
            passthrough.pop(0)
        result.extend(passthrough)
        return result

    def configure_guarded_client(self) -> None:
        token = self.bootstrap_kubectl.run(
            "create",
            "token",
            SERVICE_ACCOUNT,
            "--namespace",
            self.release_namespace,
            f"--duration={LOCK_SECONDS}s",
        ).stdout.strip()
        if not token:
            raise TransitionError("transition ServiceAccount TokenRequest returned no token")
        raw_config = self.bootstrap_kubectl.run("config", "view", "--minify", "--raw", "-o", "json").stdout
        try:
            cluster = json.loads(raw_config)["clusters"][0]["cluster"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise TransitionError("could not derive the active Kubernetes cluster") from error
        guarded_path = self.tempdir / "guarded-kubeconfig.json"
        guarded_path.write_text(
            canonical(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "clusters": [{"name": "target", "cluster": cluster}],
                    "users": [{"name": "transition", "user": {"token": token}}],
                    "contexts": [
                        {
                            "name": "transition",
                            "context": {
                                "cluster": "target",
                                "user": "transition",
                                "namespace": self.release_namespace,
                            },
                        }
                    ],
                    "current-context": "transition",
                }
            ),
            encoding="utf-8",
        )
        guarded_path.chmod(0o600)
        self.guarded_kubectl = Command(self._kubectl_prefix(guarded_path), name="guarded kubectl")

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
        return json.loads(result.stdout)

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
                check=False,
            )
            if result.returncode == 0:
                return
        raise TransitionError("could not acquire the NetworkPolicy transition Lease")

    def _release_lock(self) -> None:
        lease = self._lease()
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

    def _release_identity(self) -> dict[str, Any]:
        listing = self.helm.run(
            "list",
            "--namespace",
            self.release_namespace,
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
            }
        if len(exact) != 1:
            raise TransitionError("expected at most one existing exact Helm release")
        manifest = self.helm.run("get", "manifest", self.release, "--namespace", self.release_namespace).stdout
        return {
            "name": self.release,
            "namespace": self.release_namespace,
            "revision": str(exact[0].get("revision", "")),
            "status": exact[0].get("status", ""),
            "deployed_manifest_sha256": sha256_text(manifest),
        }

    def _yaml_documents(self, rendered: str) -> list[dict[str, Any]]:
        result = Command(["yq", "eval-all", "-o=json", "[.]", "-"], name="yq").run(input_text=rendered)
        documents = json.loads(result.stdout)
        return [item for item in documents if isinstance(item, dict)]

    def public_boundary_enabled(self) -> bool:
        rendered = self.helm.run(
            "template",
            self.release,
            str(self.chart),
            "--namespace",
            self.release_namespace,
            *self.helm_values,
            "--show-only",
            "templates/networkpolicy.yaml",
        ).stdout
        components = {
            item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
            for item in self._yaml_documents(rendered)
            if item.get("kind") == "NetworkPolicy"
        }
        boundary_components = components & {"public-edge", "envoy-controller-xds"}
        if not boundary_components:
            return False
        if boundary_components != {"public-edge", "envoy-controller-xds"}:
            raise TransitionError("public release must render both ordinary boundary allows")
        return True

    def internal_rollback(self) -> None:
        self.helm.run(
            "rollback",
            self.release,
            self.arguments.revision,
            "--namespace",
            self.release_namespace,
            "--wait",
            "--wait-for-jobs",
            "--timeout",
            self.arguments.timeout,
        )
        print(f"network-policy-transition=rolled-back revision={self.arguments.revision} public-gateway=disabled")

    def render_candidate(self) -> Candidate:
        common = [
            "template",
            self.release,
            str(self.chart),
            "--namespace",
            self.release_namespace,
            *self.helm_values,
        ]
        complete_render = self.helm.run(*common).stdout
        policy_render = self.helm.run(*common, "--show-only", "templates/networkpolicy.yaml").stdout
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
        chart_hash = self._chart_hash()
        values_hash = sha256_text(complete_render)
        policy_hash = sha256_text(policy_render)
        release = self._release_identity()
        material = {
            "schema": SCHEMA,
            "chart_sha256": chart_hash,
            "values_sha256": values_hash,
            "network_policy_render_sha256": policy_hash,
            "release": {"name": self.release, "namespace": self.release_namespace},
            "policies": {"public-envoy": proxy, "envoy-controller": controller},
            "deny_name": deny_name,
        }
        return Candidate(
            candidate_sha256=sha256_json(material),
            chart_sha256=chart_hash,
            values_sha256=values_hash,
            network_policy_render_sha256=policy_hash,
            release=release,
            proxy=proxy,
            controller=controller,
            deny_name=deny_name,
        )

    def get_policy(self, name: str, namespace: str) -> dict[str, Any]:
        result = self.guarded.run("get", "networkpolicy", name, "--namespace", namespace, "-o", "json")
        return json.loads(result.stdout)

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
        return json.loads(result.stdout)

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
        }
        if role == "public-envoy":
            annotations[DENY_ANNOTATION] = candidate.deny_name
        patch = canonical({"metadata": {"annotations": annotations}, "spec": policy["spec"]})
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
        result = self.guarded.run(
            *common,
            "-o",
            "json",
        )
        updated = json.loads(result.stdout)
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
        label_selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        result = self.guarded.run("get", "pods", "--namespace", namespace, "--selector", label_selector, "-o", "json")
        ready = [
            pod
            for pod in json.loads(result.stdout).get("items", [])
            if any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
        ]
        if not ready:
            raise TransitionError(f"{role} boundary selects zero Ready Pods")
        return len(ready)

    def verify_normal(self, role: str, policy: dict[str, Any]) -> None:
        normal = self.get_policy(policy["normal_name"], policy["namespace"])
        if normal.get("spec") != policy["spec"]:
            raise TransitionError(f"{role} Helm policy does not match the staged boundary")
        self.verify_ready_pods(policy["namespace"], normal["spec"], role=role)

    def _deny_patch(self, candidate: Candidate, spec: dict[str, Any]) -> dict[str, Any]:
        patch = canonical(
            {
                "metadata": {"annotations": {CANDIDATE_ANNOTATION: candidate.candidate_sha256}},
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
        result = self.guarded.run(
            *common,
            "-o",
            "json",
        )
        return json.loads(result.stdout)

    def activate_deny(self, candidate: Candidate) -> None:
        current = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
        self._verify_boundary_identity(current, role="default-deny")
        expected = {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []}
        if self._deny_patch(candidate, expected).get("spec") != expected:
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
        if self._deny_patch(candidate, expected).get("spec") != expected:
            raise TransitionError("default-deny was not relaxed")
        selector = ",".join(f"{key}={value}" for key, value in RELAXED_SELECTOR.items())
        pods = self.guarded.run(
            "get",
            "pods",
            "--namespace",
            candidate.proxy["namespace"],
            "--selector",
            selector,
            "-o",
            "json",
        )
        if json.loads(pods.stdout).get("items"):
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
        return json.loads(result.stdout)

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

    def current_guards(self, candidate: Candidate) -> dict[str, dict[str, Any]]:
        return {
            "envoy-controller": self.verify_guard("envoy-controller", candidate.controller, candidate),
            "public-envoy": self.verify_guard("public-envoy", candidate.proxy, candidate),
        }

    def stage(self) -> None:
        candidate = self.render_candidate()
        if candidate.release["status"] == "absent":
            raise TransitionError("stage requires an existing release with Ready boundary Pods")
        with self.lock():
            _, prior = self.receipt()
            if (
                prior.get("phase") in {"staged", "active"}
                and prior.get("candidate", {}).get("candidate_sha256") == candidate.candidate_sha256
            ):
                guards = self.current_guards(candidate)
                self.verify_deny_active(candidate)
                deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                self.write_receipt(prior["phase"], candidate, guards)
                print(
                    f"network-policy-transition={prior['phase']} candidate={candidate.candidate_sha256} idempotent=true"
                )
                return
            proxy = self.patch_guard("public-envoy", candidate.proxy, candidate)
            controller = self.patch_guard("envoy-controller", candidate.controller, candidate)
            guards = {"public-envoy": proxy, "envoy-controller": controller}
            self.write_receipt("guards-ready", candidate, guards)
            self.activate_deny(candidate)
            self.write_receipt("staged", candidate, guards)
        print(f"network-policy-transition=staged candidate={candidate.candidate_sha256}")

    def prepare(self) -> None:
        candidate = self.render_candidate()
        if candidate.release["status"] != "absent":
            self.stage()
            return
        with self.lock():
            _, prior = self.receipt()
            guards = {
                "public-envoy": self.get_policy(candidate.proxy["guard_name"], candidate.proxy["namespace"]),
                "envoy-controller": self.get_policy(
                    candidate.controller["guard_name"], candidate.controller["namespace"]
                ),
            }
            deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
            for role, boundary in guards.items():
                self._verify_boundary_identity(boundary, role=role)
            self._verify_boundary_identity(deny, role="default-deny")
            if prior.get("phase") == "bootstrap-ready":
                if prior.get("candidate", {}).get("candidate_sha256") != candidate.candidate_sha256:
                    raise TransitionError("bootstrap receipt is bound to a different candidate")
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
            deny_proof = self.relax_deny(candidate)
            self.write_receipt(
                "bootstrap-ready",
                candidate,
                guards,
                extra={"bootstrap": {"deny_proof": deny_proof}},
            )
        print(f"network-policy-transition=bootstrap-ready candidate={candidate.candidate_sha256} deny=relaxed")

    def complete(self) -> None:
        candidate = self.render_candidate()
        with self.lock():
            _, prior = self.receipt()
            if prior.get("candidate", {}).get("candidate_sha256") != candidate.candidate_sha256:
                raise TransitionError("receipt is not bound to the rendered candidate")
            if prior.get("phase") not in {"bootstrap-ready", "staged", "active"}:
                raise TransitionError("transition receipt is not bootstrap-ready, staged, or active")
            if prior.get("phase") == "bootstrap-ready":
                bootstrap_guards = {
                    "public-envoy": self.get_policy(candidate.proxy["guard_name"], candidate.proxy["namespace"]),
                    "envoy-controller": self.get_policy(
                        candidate.controller["guard_name"], candidate.controller["namespace"]
                    ),
                }
                bootstrap_deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
                self.verify_receipt_boundaries(prior, {**bootstrap_guards, "default-deny": bootstrap_deny})
                proxy = self.patch_guard("public-envoy", candidate.proxy, candidate)
                controller = self.patch_guard("envoy-controller", candidate.controller, candidate)
                guards = {"public-envoy": proxy, "envoy-controller": controller}
                self.write_receipt("guards-ready", candidate, guards)
                self.activate_deny(candidate)
                self.write_receipt("staged", candidate, guards)
                _, prior = self.receipt()
            else:
                guards = self.current_guards(candidate)
            self.verify_normal("public-envoy", candidate.proxy)
            self.verify_normal("envoy-controller", candidate.controller)
            self.verify_deny_active(candidate)
            deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
            self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
            self.write_receipt("active", candidate, guards)
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

    def _candidate_from_live(self, source: Candidate, target_manifest_sha256: str) -> Candidate:
        proxy = dict(source.proxy)
        controller = dict(source.controller)
        proxy["spec"] = self.get_policy(proxy["normal_name"], proxy["namespace"])["spec"]
        controller["spec"] = self.get_policy(controller["normal_name"], controller["namespace"])["spec"]
        release = dict(source.release)
        release["deployed_manifest_sha256"] = target_manifest_sha256
        release["revision"] = f"rollback:{self.arguments.revision}"
        material = {
            "schema": SCHEMA,
            "source": "verified-live-rollback",
            "target_manifest_sha256": target_manifest_sha256,
            "release": release,
            "policies": {"public-envoy": proxy, "envoy-controller": controller},
            "deny_name": source.deny_name,
        }
        return Candidate(
            candidate_sha256=sha256_json(material),
            chart_sha256=source.chart_sha256,
            values_sha256=target_manifest_sha256,
            network_policy_render_sha256=target_manifest_sha256,
            release=release,
            proxy=proxy,
            controller=controller,
            deny_name=source.deny_name,
        )

    def rollback(self) -> None:
        candidate = self.render_candidate()
        revision = self.arguments.revision
        target_hash = sha256_text(self._target_manifest(revision))
        with self.lock():
            _, prior = self.receipt()
            rollback_state = prior.get("rollback", {})
            current_hash = sha256_text(self._current_manifest())
            if (
                prior.get("phase") == "rolled-back"
                and rollback_state.get("target_revision") == revision
                and rollback_state.get("target_manifest_sha256") == target_hash
                and current_hash == target_hash
            ):
                live_candidate = self._candidate_from_live(candidate, target_hash)
                guards = self.current_guards(live_candidate)
                self.verify_deny_active(live_candidate)
                deny = self.get_policy(live_candidate.deny_name, live_candidate.proxy["namespace"])
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                self.write_receipt(
                    "rolled-back",
                    live_candidate,
                    guards,
                    extra={"rollback": rollback_state},
                )
                print(f"network-policy-transition=rolled-back revision={revision} idempotent=true")
                return
            if prior.get("phase") not in {
                "staged",
                "active",
                "rollback-prepared",
                "rollback-guards-ready",
            }:
                raise TransitionError("receipt phase does not authorize rollback")
            if (
                prior.get("phase") not in {"rollback-prepared", "rollback-guards-ready"}
                and prior.get("candidate", {}).get("candidate_sha256") != candidate.candidate_sha256
            ):
                raise TransitionError("rollback receipt is not bound to the rendered candidate")
            guards = {
                "public-envoy": self.get_policy(candidate.proxy["guard_name"], candidate.proxy["namespace"]),
                "envoy-controller": self.get_policy(
                    candidate.controller["guard_name"], candidate.controller["namespace"]
                ),
            }
            if prior.get("phase") in {"staged", "active"}:
                current_deny = self.get_optional_policy(candidate.deny_name, candidate.proxy["namespace"])
                boundaries = dict(guards)
                if current_deny is not None:
                    boundaries["default-deny"] = current_deny
                self.verify_receipt_boundaries(prior, boundaries)
            deny_proof = self.relax_deny(candidate)
            rollback = {
                "target_revision": revision,
                "target_manifest_sha256": target_hash,
                "deny_proof": deny_proof,
            }
            self.write_receipt("rollback-prepared", candidate, guards, extra={"rollback": rollback})
            if current_hash != target_hash:
                self.helm.run(
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
            if sha256_text(self._current_manifest()) != target_hash:
                raise TransitionError("Helm rollback did not reach the target manifest")
            live_candidate = self._candidate_from_live(candidate, target_hash)
            proxy = self.patch_guard("public-envoy", live_candidate.proxy, live_candidate)
            controller = self.patch_guard("envoy-controller", live_candidate.controller, live_candidate)
            live_guards = {"public-envoy": proxy, "envoy-controller": controller}
            self.write_receipt(
                "rollback-guards-ready",
                live_candidate,
                live_guards,
                extra={"rollback": rollback},
            )
            self.activate_deny(live_candidate)
            self.write_receipt("rolled-back", live_candidate, live_guards, extra={"rollback": rollback})
        print(f"network-policy-transition=rolled-back revision={revision} deny=active")

    def destroy(self) -> None:
        with self.lock():
            _, prior = self.receipt()
            encoded = prior.get("candidate")
            if not isinstance(encoded, dict):
                raise TransitionError("destroy requires a durable candidate receipt")
            policies = encoded.get("policies", {})
            candidate = Candidate(
                candidate_sha256=encoded["candidate_sha256"],
                chart_sha256=encoded["chart_sha256"],
                values_sha256=encoded["values_sha256"],
                network_policy_render_sha256=encoded["network_policy_render_sha256"],
                release=encoded["release"],
                proxy=policies["public-envoy"],
                controller=policies["envoy-controller"],
                deny_name=encoded["deny_name"],
            )
            guards = {
                "public-envoy": self.get_policy(candidate.proxy["guard_name"], candidate.proxy["namespace"]),
                "envoy-controller": self.get_policy(
                    candidate.controller["guard_name"], candidate.controller["namespace"]
                ),
            }
            deny = self.get_optional_policy(candidate.deny_name, candidate.proxy["namespace"])
            boundaries = dict(guards)
            if deny is not None:
                boundaries["default-deny"] = deny
            self.verify_receipt_boundaries(prior, boundaries)
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
    if not Path(arguments.chart).is_dir() or not Path(arguments.kubeconfig).is_file():
        parser.error("chart and kubeconfig must exist")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv or sys.argv[1:])
    transition = Transition(arguments)
    try:
        if arguments.action != "destroy" and not transition.public_boundary_enabled():
            if arguments.action == "rollback":
                transition.internal_rollback()
            else:
                print(f"network-policy-transition=disabled release={arguments.release} public-gateway=false")
            return 0
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
