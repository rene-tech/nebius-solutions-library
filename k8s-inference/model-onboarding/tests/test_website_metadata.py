"""Offline regression coverage for the cross-repository model-publication check."""

import copy
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "website_metadata", Path(__file__).resolve().parents[1] / "verify_website_metadata.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WebsiteMetadataTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "schema": "scientific-ai/catalog-metadata/v1",
            "models": [{
                "id": "parakeet-realtime-eou-120m-v1",
                "aliases": ["parakeet_realtime_eou_120m-v1"],
                "domain": "speech",
                "homepage": "https://huggingface.co/nvidia/parakeet_realtime_eou_120m-v1",
                "attribution": {
                    "label": "NVIDIA", "relationship": "publisher",
                    "source": "https://huggingface.co/nvidia/parakeet_realtime_eou_120m-v1",
                    "verifiedOn": "2026-09-16",
                },
            }],
        }
        self.ids = {self.payload["models"][0]["id"]}

    def test_known_model_and_alias(self):
        self.assertEqual(MODULE.metadata_issues(self.ids | {"PARAKEET_REALTIME_EOU_120M-V1"}, self.payload), [])

    def test_new_app_without_website_entry_fails(self):
        self.assertEqual(MODULE.metadata_issues({"new-app"}, self.payload), ["new-app: missing deployed website metadata"])

    def test_nvidia_credit_cannot_disappear(self):
        self.payload["models"][0]["attribution"] = None
        self.assertIn("NVIDIA model card has no NVIDIA credit", "\n".join(MODULE.metadata_issues(self.ids, self.payload)))

    def test_other_is_not_an_accepted_onboarding_category(self):
        self.payload["models"][0]["domain"] = "other"
        self.assertIn("invalid category", "\n".join(MODULE.metadata_issues(self.ids, self.payload)))

    def test_physical_ai_category_and_existing_general_purpose_are_accepted(self):
        for domain in ("physical-ai-robotics", "general-ai", "generative-media"):
            with self.subTest(domain=domain):
                self.payload["models"][0]["domain"] = domain
                self.assertEqual(MODULE.metadata_issues(self.ids, self.payload), [])

    def test_explicit_uncredited_community_model_is_valid(self):
        row = self.payload["models"][0]
        row.update(homepage="https://example.org/model", attribution=None)
        self.assertEqual(MODULE.metadata_issues(self.ids, self.payload), [])
        del row["attribution"]
        self.assertIn("explicit attribution", "\n".join(MODULE.metadata_issues(self.ids, self.payload)))

    def test_links_and_attribution_are_checked(self):
        for change in ({"homepage": ""}, {"attribution": {"label": "NVIDIA", "relationship": "ecosystem"}}):
            with self.subTest(change=change):
                payload = copy.deepcopy(self.payload)
                payload["models"][0].update(change)
                self.assertTrue(MODULE.metadata_issues(self.ids, payload))

    def test_duplicate_ids_are_not_silently_overwritten(self):
        self.payload["models"].append(copy.deepcopy(self.payload["models"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            MODULE.metadata_issues(self.ids, self.payload)

    def test_stale_or_wrong_endpoint_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "schema"):
            MODULE.metadata_issues(self.ids, {"models": []})

    def test_all_three_registration_paths_are_discovered(self):
        self.assertEqual(MODULE.ids_in_document({"model": {"id": "http-app"}}), {"http-app"})
        self.assertEqual(MODULE.ids_in_document({"record": {"model": {"id": "native-app"}}}), {"native-app"})
        self.assertEqual(MODULE.ids_in_document({"profiles": [{"model_id": "batch-app"}]}), {"batch-app"})

    def test_unchanged_inventory_still_checks_every_live_app(self):
        live = {"status": "ok", "source": "live", "dropped": 0, "models": [{"id": "forgotten-app"}]}
        with patch.object(MODULE, "ids_at_ref", return_value={"existing-app"}), patch.object(
            MODULE, "urlopen", side_effect=[
                io.BytesIO(json.dumps(live).encode()),
                io.BytesIO(json.dumps(self.payload).encode()),
            ],
        ), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(MODULE.main(["--website-url", "https://example.org", "--base-ref", "HEAD~1"]), 1)
            self.assertIn("forgotten-app: missing deployed website metadata", stderr.getvalue())

    def test_endpoint_failure_is_not_a_successful_check(self):
        with patch.object(MODULE, "urlopen", side_effect=OSError("unavailable")), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(MODULE.main(["--website-url", "https://example.org", "--model-id", "new-app"]), 1)


if __name__ == "__main__":
    unittest.main()
