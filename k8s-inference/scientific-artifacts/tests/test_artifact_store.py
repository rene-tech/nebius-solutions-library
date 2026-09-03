"""Canonical layout, tenant isolation and media-type rules for the result store."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fs2_artifact_store_under_test", MODULE_ROOT / "artifact_store.py"
)
assert SPEC is not None and SPEC.loader is not None
STORE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = STORE
SPEC.loader.exec_module(STORE)

CONTRACT = json.loads((MODULE_ROOT / "artifact-store-contract.json").read_text(encoding="utf-8"))
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def address(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tenant": "acme",
        "operation": "0f8b2c1e-4d5a-4a0e-9c3b-77c1b5a9d2e4",
        "stage": "structure-prediction",
        "shard": 0,
        "attempt": 1,
        "direction": "output",
        "digest": DIGEST,
    }
    base.update(overrides)
    return base


class CanonicalLayoutTests(unittest.TestCase):
    def test_the_key_matches_the_documented_template(self) -> None:
        key = STORE.object_key(**address())
        self.assertEqual(
            key,
            "scientific/v1/tenants/acme/operations/"
            "0f8b2c1e-4d5a-4a0e-9c3b-77c1b5a9d2e4/stages/structure-prediction/"
            f"shards/0/attempts/1/output/sha256/{DIGEST}",
        )
        template = CONTRACT["object_layout"]["object_key"]
        self.assertEqual(len(key.split("/")), len(template.split("/")))

    def test_a_key_round_trips_through_the_parser(self) -> None:
        fields = address(direction="input", shard=1023, attempt=10)
        parsed = STORE.parse_object_key(STORE.object_key(**fields))
        for name, value in fields.items():
            self.assertEqual(getattr(parsed, name), value)

    def test_only_content_identity_distinguishes_two_commits(self) -> None:
        # A rerun that produces identical bytes writes the identical key, so a
        # retry cannot silently fork a stage's committed output.
        first = STORE.object_key(**address())
        second = STORE.object_key(**address())
        self.assertEqual(first, second)
        self.assertNotEqual(first, STORE.object_key(**address(digest=OTHER_DIGEST)))
        self.assertNotEqual(first, STORE.object_key(**address(direction="input")))
        self.assertNotEqual(first, STORE.object_key(**address(attempt=2)))

    def test_every_component_is_confined_to_one_path_segment(self) -> None:
        for name, value in (
            ("tenant", "acme/evil"),
            ("tenant", ".."),
            ("tenant", "Acme"),
            ("operation", "../../reference-data"),
            ("stage", "Stage"),
            ("digest", DIGEST.upper()),
            ("digest", "short"),
        ):
            with self.subTest(component=name, value=value):
                with self.assertRaises(STORE.ArtifactLayoutError):
                    STORE.object_key(**address(**{name: value}))

    def test_shard_and_attempt_stay_inside_the_batch_contract_bounds(self) -> None:
        bounds = CONTRACT["object_layout"]["bounds"]
        self.assertEqual(STORE.MAX_SHARD, bounds["max_shard"])
        self.assertEqual(STORE.MAX_ATTEMPT, bounds["max_attempt"])
        for name, value in (
            ("shard", -1),
            ("shard", bounds["max_shard"] + 1),
            ("attempt", 0),
            ("attempt", bounds["max_attempt"] + 1),
            ("attempt", True),
        ):
            with self.subTest(component=name, value=value):
                with self.assertRaises(STORE.ArtifactLayoutError):
                    STORE.object_key(**address(**{name: value}))

    def test_a_non_canonical_key_is_rejected_rather_than_repaired(self) -> None:
        for key in (
            "scientific/v1/tenants/acme/operations/op/stages/s/shards/00/attempts/1/output/sha256/" + DIGEST,
            "scientific/v2/tenants/acme/operations/op/stages/s/shards/0/attempts/1/output/sha256/" + DIGEST,
            "reference-data/blobs/sha256/" + DIGEST,
            "scientific/v1/tenants/acme/operations/op/stages/s/shards/0/attempts/1/sideways/sha256/" + DIGEST,
        ):
            with self.subTest(key=key):
                with self.assertRaises(STORE.ArtifactLayoutError):
                    STORE.parse_object_key(key)


class TenantIsolationTests(unittest.TestCase):
    def test_one_tenant_prefix_never_contains_another(self) -> None:
        # "acme" is a string prefix of "acme-labs"; the trailing slash is what
        # keeps their key spaces disjoint.
        acme = STORE.object_key(**address(tenant="acme"))
        acme_labs = STORE.object_key(**address(tenant="acme-labs"))
        self.assertTrue(STORE.belongs_to_tenant(acme, "acme"))
        self.assertTrue(STORE.belongs_to_tenant(acme_labs, "acme-labs"))
        self.assertFalse(STORE.belongs_to_tenant(acme_labs, "acme"))
        self.assertFalse(STORE.belongs_to_tenant(acme, "acme-labs"))

    def test_a_tenant_cannot_address_outside_the_scientific_root(self) -> None:
        for tenant in ("acme", "acme-labs"):
            self.assertTrue(STORE.tenant_prefix(tenant).startswith(STORE.ROOT + "/"))
        # The bucket policy scope must cover every key the layout can produce.
        scope = CONTRACT["storage"]["writer_paths"][0]
        self.assertEqual(scope, f"{STORE.ROOT}/*")
        self.assertTrue(STORE.object_key(**address()).startswith(scope[:-1]))

    def test_the_probe_tenant_used_by_the_smoke_test_is_disjoint(self) -> None:
        mine = STORE.object_key(**address(tenant="fs2-acceptance"))
        other = STORE.object_key(**address(tenant="isolation-probe"))
        self.assertFalse(STORE.belongs_to_tenant(other, "fs2-acceptance"))
        self.assertFalse(STORE.belongs_to_tenant(mine, "isolation-probe"))


class MediaTypeTests(unittest.TestCase):
    def test_the_allowlist_is_normalized_and_exact(self) -> None:
        self.assertEqual(
            STORE.normalize_media_types([" Application/JSON ", "application/json", "chemical/x-pdb"]),
            ("application/json", "chemical/x-pdb"),
        )

    def test_an_empty_or_malformed_allowlist_is_refused(self) -> None:
        for allowlist in ([], ["   "], ["application"], ["*/*"], ["application/" + "x" * 200]):
            with self.subTest(allowlist=allowlist):
                with self.assertRaises(STORE.ArtifactLayoutError):
                    STORE.normalize_media_types(allowlist)

    def test_membership_ignores_case_and_padding_only(self) -> None:
        allowlist = ["application/json", "chemical/x-cif"]
        self.assertTrue(STORE.media_type_allowed("Application/JSON", allowlist))
        self.assertFalse(STORE.media_type_allowed("application/xml", allowlist))


class CredentialHandlingTests(unittest.TestCase):
    def test_credentials_are_read_from_the_same_document_the_secret_carries(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(
                json.dumps({"access_key_id": "AKIDEXAMPLE", "secret_access_key": "s3cr3t"}),
                encoding="utf-8",
            )
            self.assertEqual(STORE.read_credentials(path), ("AKIDEXAMPLE", "s3cr3t"))

    def test_an_incomplete_credential_document_is_refused(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            for document in ({"access_key_id": "AKID"}, {"access_key_id": "", "secret_access_key": "s"}, []):
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.subTest(document=document):
                    with self.assertRaises(ValueError):
                        STORE.read_credentials(path)

    def test_the_module_never_accepts_a_secret_on_argv(self) -> None:
        parser = STORE.build_parser()
        smoke = {
            action.dest
            for action in parser._subparsers._group_actions[0].choices["smoke"]._actions  # noqa: SLF001
        }
        self.assertIn("credentials_file", smoke)
        self.assertNotIn("secret_access_key", smoke)
        self.assertNotIn("secret_key", smoke)


class DeletionProofTests(unittest.TestCase):
    """A delete call is not proof; only what the store answers afterwards is."""

    class Denied(Exception):
        def __init__(self, status: int) -> None:
            super().__init__(status)
            self.response = {"ResponseMetadata": {"HTTPStatusCode": status}}

    def test_only_an_exact_404_proves_deletion(self) -> None:
        self.assertTrue(STORE.absence_confirmed(404))
        # 403 is the answer a bucket-scoped writer gets for a key it may not
        # read, which says nothing about whether the object still exists.
        for status in (403, 401, 200, 500, None, "404", "ConnectionError"):
            with self.subTest(status=status):
                self.assertFalse(STORE.absence_confirmed(status))

    def test_a_successful_head_reports_the_object_as_present(self) -> None:
        self.assertEqual(
            STORE.probe_absent(lambda **_: {"ContentLength": 1}),
            {"absent": False, "status": 200},
        )

    def test_a_probe_reports_the_exact_status_the_store_returned(self) -> None:
        for status, proven in ((404, True), (403, False), (500, False)):
            with self.subTest(status=status):
                def head(**_: object) -> None:
                    raise self.Denied(status)

                self.assertEqual(
                    STORE.probe_absent(head), {"absent": proven, "status": status}
                )

    def test_a_transport_failure_is_named_and_never_counted_as_deletion(self) -> None:
        def head(**_: object) -> None:
            raise TimeoutError("no route")

        self.assertEqual(
            STORE.probe_absent(head), {"absent": False, "status": "TimeoutError"}
        )

    def cleanup(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "kept": False,
            "residual": [],
            "verified_absent": {
                name: {"absent": True, "status": 404}
                for name in STORE.REQUIRED_ABSENCE_PROBES
            },
        }
        base.update(overrides)
        return base

    def test_deletion_is_confirmed_only_when_every_probe_returns_404(self) -> None:
        self.assertTrue(STORE.cleanup_confirmed(self.cleanup()))

    def test_a_missing_probe_is_not_silently_treated_as_proof(self) -> None:
        # `all` over an empty or partial mapping is true, so the probe set has
        # to be checked before its contents.
        for probes in (
            {},
            {"current": {"absent": True, "status": 404}},
            {
                "current": {"absent": True, "status": 404},
                "written_version": {"absent": True, "status": 404},
            },
        ):
            with self.subTest(probes=sorted(probes)):
                self.assertFalse(
                    STORE.cleanup_confirmed(self.cleanup(verified_absent=probes))
                )

    def test_a_403_probe_blocks_the_deletion_claim(self) -> None:
        probes = {
            name: {"absent": True, "status": 404}
            for name in STORE.REQUIRED_ABSENCE_PROBES
        }
        probes["previously_signed_handle"] = {"absent": False, "status": 403}
        self.assertFalse(STORE.cleanup_confirmed(self.cleanup(verified_absent=probes)))

    def test_residual_versions_or_a_kept_object_block_the_claim(self) -> None:
        self.assertFalse(STORE.cleanup_confirmed(self.cleanup(residual=["key@1: AccessDenied"])))
        self.assertFalse(STORE.cleanup_confirmed(self.cleanup(kept=True)))

    def test_an_unversioned_write_cannot_prove_its_version_is_gone(self) -> None:
        probes = {
            name: {"absent": True, "status": 404}
            for name in STORE.REQUIRED_ABSENCE_PROBES
        }
        probes["written_version"] = {"absent": False, "status": "no-version-id-returned"}
        self.assertFalse(STORE.cleanup_confirmed(self.cleanup(verified_absent=probes)))


if __name__ == "__main__":
    unittest.main()
