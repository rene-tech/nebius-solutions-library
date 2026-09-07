import copy
import unittest

import public_verify as verifier


class PublicFixtureTests(unittest.TestCase):
    def test_hopper_profile_keeps_archived_b300_oracle_unchanged_and_strict(self):
        hopper = verifier.evo_validator()
        original = hopper.base.build_probes(("fs2-test-original-a", "fs2-test-original-b"))
        current = hopper.build_probes(("fs2-test-hopper-a", "fs2-test-hopper-b"))
        self.assertEqual([probe.expected_sequence for probe in original], ["ATTTTTTTTTTTTTTTTTTT", "TGTTTTTTTTTTTTTTTTTT"])
        self.assertEqual([probe.expected_sequence for probe in current], ["ATCGATCGATCGATCGATCG", "GATTACAGATTACAGATTAC"])
        response = {"sequence": current[0].expected_sequence, "logits": None, "sampled_probs": None,
            "elapsed_ms": 1000, "elapsed_ms_per_token": [50] * 20}
        self.assertEqual(hopper.validate_response(response, current[0])["token_count"], 20)
        with self.assertRaises(hopper.base.SemanticFailure):
            hopper.validate_response({**response, "sequence": original[0].expected_sequence}, current[0])
        with self.assertRaises(hopper.base.SemanticFailure):
            hopper.validate_response({**response, "elapsed_ms_per_token": [float("nan")] * 20}, current[0])

    def test_evo_uses_original_deterministic_probes(self):
        _, cases = verifier.evo_cases("pinned-hf-revision")
        originals = verifier.evo_validator().build_probes(("fs2-test-evo-a", "fs2-test-evo-b"))
        self.assertEqual(len(cases), 2)
        for original, case in zip(originals, cases, strict=True):
            self.assertEqual(original.payload, case.payload)
            self.assertEqual(case.revision, "pinned-hf-revision")
            self.assertEqual(case.operation, "generate-sequence")
        self.assertEqual(len({case.payload_sha256 for case in cases}), 2)

    def test_cxr_wire_inputs_unchanged(self):
        contract, cases = verifier.cases_for("nv-reason-cxr-3b")
        self.assertEqual(len(cases), 2)
        for original, case in zip(contract["requests"], cases, strict=True):
            self.assertEqual(original["wire_request"], case.payload)
            self.assertEqual(original["payload_sha256"], case.payload_sha256)

    def test_segment_generated_inputs_keep_pinned_hashes(self):
        contract, cases = verifier.cases_for("nv-segment-ct")
        self.assertEqual([item["payload_sha256"] for item in contract["requests"]], [case.payload_sha256 for case in cases])
        self.assertEqual(len({case.payload_sha256 for case in cases}), 2)

    def test_sdxl_only_output_transport_changes(self):
        contract, cases = verifier.cases_for("sdxl")
        for original, case in zip(contract["requests"], cases, strict=True):
            expected = copy.deepcopy(original["request"])
            expected["response_format"] = "b64_json"
            self.assertEqual(expected, case.payload)
        self.assertEqual(len({case.payload_sha256 for case in cases}), 2)

    def test_route_revision_is_distinct_from_weight_revision(self):
        expected = {"image": "registry/runtime@sha256:abc", "model_revision": "hf123"}
        pod = {"metadata": {"uid": "pod123", "name": "model-hot", "namespace": "fs2-models",
            "annotations": {"fs2.nebius/model-revision": "hf123",
                "fs2-serve.nebius.ai/spec-digest": "sha256:route"}},
            "status": {"containerStatuses": [{"imageID": "registry/runtime@sha256:abc"}]}}
        operation = {"runtime": {"pod_uid": "pod123"}, "model_revision": "dynamic:sha256:route"}
        result = verifier.runtime_binding([pod], operation, expected)
        self.assertEqual(result["model_revision"], "hf123")
        self.assertEqual(result["route_revision"], "dynamic:sha256:route")
        for field, wrong in (("image", "registry/runtime@sha256:wrong"), ("model_revision", "wrong")):
            with self.subTest(field=field), self.assertRaises(verifier.AcceptanceError):
                verifier.runtime_binding([pod], operation, {**expected, field: wrong})
        with self.assertRaises(verifier.AcceptanceError):
            verifier.runtime_binding([pod], {**operation, "model_revision": "hf123"}, expected)


if __name__ == "__main__":
    unittest.main()
