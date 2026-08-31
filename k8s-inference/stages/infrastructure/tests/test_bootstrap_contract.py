from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BootstrapContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lock = json.loads(
            (ROOT / "bootstrap/components.lock.json").read_text(encoding="utf-8")
        )
        cls.script = (ROOT / "bootstrap/bootstrap.sh").read_text(encoding="utf-8")

    def test_component_lock_is_complete_and_digest_pinned(self) -> None:
        components = self.lock["components"]
        self.assertEqual(1, self.lock["schema_version"])
        self.assertEqual(
            {
                "gateway-api",
                "cert-manager",
                "envoy-gateway",
                "kueue",
                "kserve-crd",
                "kserve-resources",
                "kube-prometheus-stack",
                "dcgm-exporter",
            },
            {component["name"] for component in components},
        )
        for component in components:
            self.assertRegex(component["sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("latest", component["version"].lower())
            if component["kind"] == "helm-oci":
                self.assertRegex(component["oci_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_live_modes_have_cluster_identity_guard(self) -> None:
        self.assertIn('readonly CLUSTER_ID_PREFIX="mk8scluster-"', self.script)
        self.assertIn('readonly RETAINED_CLUSTER_ID="${CLUSTER_ID_PREFIX}', self.script)
        self.assertIn('readonly PROHIBITED_CLUSTER_ID="${CLUSTER_ID_PREFIX}', self.script)
        self.assertIn("current context is not the run-scoped disposable context", self.script)
        self.assertRegex(self.script, re.compile(r"install_components\(\).*?validate_run", re.DOTALL))
        self.assertRegex(self.script, re.compile(r"remove_components\(\).*?validate_run", re.DOTALL))

    def test_bootstrap_has_install_verify_and_reverse_cleanup(self) -> None:
        self.assertIn("install_components()", self.script)
        self.assertIn("verify_components()", self.script)
        self.assertIn("remove_components()", self.script)
        self.assertIn("bootstrap.receipt.json", self.script)
        self.assertIn("helm list --all-namespaces", self.script)

    def test_gateway_api_crds_have_one_installer(self) -> None:
        install_body = self.script.split("install_components() {", 1)[1].split(
            "\nverify_components() {", 1
        )[0]
        gateway_apply = (
            'kubectl apply --server-side --field-manager="$prefix-bootstrap" '
            '-f "$gateway_manifest"'
        )
        envoy_install = (
            'install_chart "$prefix-envoy" envoy-gateway-system envoy-gateway '
            "--skip-crds"
        )

        self.assertEqual(install_body.count(gateway_apply), 1)
        self.assertEqual(install_body.count(envoy_install), 1)
        self.assertEqual(install_body.count("--skip-crds"), 1)
        self.assertIn(
            'gateway_api:{crd_source:"pinned-manifest",envoy_chart_skip_crds:true}',
            self.script,
        )

    def test_remove_deletes_run_scoped_roles_and_retained_crds(self) -> None:
        remove_body = self.script.split("remove_components() {", 1)[1].split(
            "\nmain() {", 1
        )[0]
        self.assertIn('"$prefix-kueue-batch-admin-role"', remove_body)
        self.assertIn('"$prefix-kueue-batch-user-role"', remove_body)
        self.assertEqual(
            remove_body.count(
                '"$prefix-envoy-gateway-helm-certgen:envoy-gateway-system"'
            ),
            2,
        )

        retained_crds = {
            "alertmanagerconfigs.monitoring.coreos.com",
            "alertmanagers.monitoring.coreos.com",
            "podmonitors.monitoring.coreos.com",
            "probes.monitoring.coreos.com",
            "prometheusagents.monitoring.coreos.com",
            "prometheuses.monitoring.coreos.com",
            "prometheusrules.monitoring.coreos.com",
            "scrapeconfigs.monitoring.coreos.com",
            "servicemonitors.monitoring.coreos.com",
            "thanosrulers.monitoring.coreos.com",
            "tcproutes.gateway.networking.k8s.io",
            "udproutes.gateway.networking.k8s.io",
            "xbackendtrafficpolicies.gateway.networking.x-k8s.io",
            "xmeshes.gateway.networking.x-k8s.io",
            "challenges.acme.cert-manager.io",
            "orders.acme.cert-manager.io",
            "certificaterequests.cert-manager.io",
            "certificates.cert-manager.io",
            "clusterissuers.cert-manager.io",
            "issuers.cert-manager.io",
        }
        for crd in retained_crds:
            self.assertEqual(remove_body.count(f"\n    {crd} " + "\\"), 1, crd)
        self.assertGreaterEqual(
            remove_body.count("kubectl delete customresourcedefinition"), 3
        )


if __name__ == "__main__":
    unittest.main()
