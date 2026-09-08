"""Namespaced copies of qualified templates for independent serving apps.

Artifact PVCs and service accounts are shared dependencies, not owned by the
new app. Runtime Deployments, Services and configuration are independent.
No model arguments or snapshot payloads are rewritten: the gateway translates
the public app route into the original runtime model identity.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from .model_deployment import AppDeploymentIdentity, LegacyTemplateBundle


def instantiate_app_template(
    bundle: LegacyTemplateBundle,
    deployment_name: str,
    *,
    identity: AppDeploymentIdentity,
) -> LegacyTemplateBundle:
    names = {
        item["metadata"]["name"]: (
            deployment_name[:45].rstrip("-") + "-" + hashlib.sha256(item["metadata"]["name"].encode()).hexdigest()[:12]
        )
        for item in bundle.resources
        if item["kind"] not in {"PersistentVolumeClaim", "ServiceAccount"}
    }

    services = {item["metadata"]["name"] for item in bundle.resources if item["kind"] == "Service"}
    reference_fields = {"configMap", "configMapRef", "configMapKeyRef", "scaleTargetRef"}

    def rewrite(value: Any, parent: str = "") -> Any:
        if isinstance(value, str):
            # Rewrite an actual Service DNS/URL reference, never a model name
            # or an arbitrary runtime argument that happens to equal a name.
            for service in services:
                value = re.sub(
                    rf"(?<=://){re.escape(service)}(?=[:/]|$)",
                    names[service],
                    value,
                )
                value = value.replace(
                    f"{service}.{bundle.resources[0]['metadata']['namespace']}.svc",
                    (f"{names[service]}.{bundle.resources[0]['metadata']['namespace']}.svc"),
                )
            return value
        if isinstance(value, list):
            return [rewrite(item, parent) for item in value]
        if isinstance(value, dict):
            return {
                key: names.get(item, item)
                if key == "name" and isinstance(item, str) and parent in {"metadata", *reference_fields}
                else rewrite(item, key)
                for key, item in value.items()
            }
        return value

    resources = [
        rewrite(copy.deepcopy(item))
        for item in bundle.resources
        if item["kind"] not in {"PersistentVolumeClaim", "ServiceAccount"}
    ]
    app_label = {"fs2-serve.nebius.ai/app-id": str(identity.app_id)}
    deployment_sources = {
        names[item["metadata"]["name"]]: item for item in bundle.resources if item["kind"] == "Deployment"
    }
    deployments = {item["metadata"]["name"]: item for item in resources if item["kind"] == "Deployment"}
    for name, workload in deployments.items():
        pod = workload["spec"]["template"]["metadata"]
        original_selector = deployment_sources[name]["spec"]["selector"].get("matchLabels", {})
        pod["labels"] = {key: value for key, value in pod.get("labels", {}).items() if key not in original_selector}
        selector = {**app_label, "fs2-serve.nebius.ai/app-template": name}
        pod["labels"].update(selector)
        workload["spec"]["selector"] = {"matchLabels": selector}
    for service in (item for item in resources if item["kind"] == "Service"):
        original_selector = service["spec"].get("selector", {})
        selector_key = (
            "fs2-serve.nebius.ai/route-" + hashlib.sha256(service["metadata"]["name"].encode()).hexdigest()[:12]
        )
        matching = [
            name
            for name, source in deployment_sources.items()
            if original_selector
            and all(
                source["spec"]["template"]["metadata"].get("labels", {}).get(key) == value
                for key, value in original_selector.items()
            )
        ]
        if not matching:
            raise ValueError("app template Service has no exact owned Pod selector")
        service["spec"]["selector"] = {**app_label, selector_key: "true"}
        for name in matching:
            deployments[name]["spec"]["template"]["metadata"]["labels"][selector_key] = "true"
    return bundle.model_copy(
        update={
            "resources": resources,
            "primary_workload_name": names[bundle.primary_workload_name],
            "primary_service_name": names[bundle.primary_service_name],
        }
    )
