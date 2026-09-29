"""Behavior tests; no live identities, cloud buckets, or secrets are created."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import lifecycle as life
import seed_data as seed


class API:
    """Stateful contract fake, intentionally not a cloud-acceptance claim."""

    def __init__(self):
        self.calls, self.rows, self.keys, self.storage = [], [], {}, {}
        self.policies = {}

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(payload)))
        url = urlsplit(path)
        parts = url.path.split("/")
        if url.path == "/admin/api/v1/users" and method == "GET":
            tenant = parse_qs(url.query).get("tenant_id", [None])[0]
            return {
                "items": copy.deepcopy(
                    [u for u in self.rows if tenant is None or u["tenant_id"] == tenant]
                ),
                "truncated": False,
            }
        if "/tenants/" in path:
            tenant = parts[5]
            if method == "PUT":
                self.policies[tenant] = copy.deepcopy(payload)
            return self.policies.get(
                tenant, {"mode": "tenant", "quota_bytes": 5_000_000_000}
            )
        if url.path == "/admin/api/v1/users" and method == "POST":
            user = {**payload, "id": str(len(self.rows) + 1)}
            self.rows.append(user)
            self.keys[user["id"]] = []
            policy = self.policies.get(
                user["tenant_id"], {"mode": "tenant", "quota_bytes": 5_000_000_000}
            )
            self.storage[user["id"]] = {
                **policy,
                "state": "ready",
                "bucket_name": life.bucket_name(
                    "project-test",
                    user["tenant_id"],
                    user["principal_id"] if policy["mode"] == "user" else None,
                ),
            }
            return copy.deepcopy(user)
        if "/keys/" in path and method == "DELETE":
            for values in self.keys.values():
                for key in values:
                    if key["id"] == parts[-1]:
                        key["revoked_at"] = "2026-09-29T00:00:00Z"
            return {}
        user_id = parts[5]
        user = next(u for u in self.rows if u["id"] == user_id)
        if parts[-1] == "keys":
            if method == "POST":
                key = {
                    **payload,
                    "id": f"key-{user_id}-{len(self.keys[user_id])}",
                    "revoked_at": None,
                }
                self.keys[user_id].append(key)
                return {"key": key, "secret": "test-only-not-a-live-key"}
            return {"items": copy.deepcopy(self.keys[user_id])}
        if parts[-1] == "storage":
            return copy.deepcopy(self.storage[user_id])
        if method == "PATCH":
            user.update(payload)
            self.storage[user_id]["state"] = "ready" if user["enabled"] else "disabled"
            return copy.deepcopy(user)
        return {
            "user": copy.deepcopy(user),
            "keys": copy.deepcopy(self.keys[user_id]),
            "storage": copy.deepcopy(self.storage[user_id]),
        }


class UsersTests(unittest.TestCase):
    def setUp(self):
        self.api = API()

    def create(self, tenant="system", principal="qa", **kwargs):
        return life.create_user(
            self.api,
            tenant,
            principal,
            principal,
            "service",
            kwargs.get("mode", "tenant"),
            5_000_000_000,
            timeout=0,
        )

    def test_alias_preserves_customer_identity(self):
        policy = {"tenants": {"koprabio": {"id": "kopra", "aliases": ["KopraBio"]}}}
        for name in ("koprabio", "kopra", "KopraBio"):
            self.assertEqual(life.resolve_tenant(name, policy), "kopra")

    def test_private_name_shared_name_and_legacy_binding(self):
        self.assertEqual(
            life.bucket_name("project-e00rene", "kopra"), "fs2-kopra-b559903873831e20"
        )
        self.assertIn("-alice-", life.bucket_name("p", "customer", "alice"))
        self.assertNotIn("alice", life.bucket_name("p", "customer"))
        self.assertLessEqual(len(life.bucket_name("p", "T" * 120, "U" * 160)), 63)

    def test_normalization_does_not_merge_distinct_owners(self):
        self.assertNotEqual(life.bucket_name("p", "a_b"), life.bucket_name("p", "a-b"))
        self.assertNotEqual(
            life.bucket_name("p", "customer", "qa"),
            life.bucket_name("p2", "customer", "qa"),
        )

    def test_shared_users_reuse_bucket(self):
        one, two = self.create(), self.create(principal="development")
        self.assertEqual(one["storage"]["bucket_name"], two["storage"]["bucket_name"])

    def test_private_users_have_different_buckets(self):
        one, two = (
            self.create(mode="user"),
            self.create(principal="development", mode="user"),
        )
        self.assertNotEqual(
            one["storage"]["bucket_name"], two["storage"]["bucket_name"]
        )
        methods = [(m, p) for m, p, _ in self.api.calls]
        self.assertLess(
            next(i for i, x in enumerate(methods) if x[0] == "PUT"),
            next(i for i, x in enumerate(methods) if x[0] == "POST"),
        )

    def test_repeated_create_does_not_create_user_or_key(self):
        first = self.create()
        self.api.calls.clear()
        second = self.create()
        self.assertFalse(second["created"])
        self.assertEqual(first["user_id"], second["user_id"])
        self.assertTrue(all(m == "GET" for m, _, _ in self.api.calls))

    def test_existing_mode_mismatch_does_not_migrate(self):
        self.create()
        self.api.calls.clear()
        with self.assertRaises(life.LifecycleError):
            self.create(mode="user")
        self.assertTrue(all(m == "GET" for m, _, _ in self.api.calls))

    def test_disabled_user_is_not_implicitly_reactivated(self):
        self.create()
        self.api.rows[0]["enabled"] = False
        with self.assertRaises(life.LifecycleError):
            self.create()
        self.assertFalse(self.api.rows[0]["enabled"])

    def test_retirement_dry_run_is_read_only(self):
        self.create()
        self.api.calls.clear()
        plan = life.retire_user(self.api, "system", "qa")
        self.assertEqual(
            plan["bucket_action"], "retain all data and shared-user access"
        )
        self.assertTrue(all(m == "GET" for m, _, _ in self.api.calls))

    def test_retire_requires_draining_and_preserves_shared_peer(self):
        self.create()
        self.create(principal="development")
        self.api.keys["1"] = [{"id": "test-key", "revoked_at": None}]
        with self.assertRaises(life.LifecycleError):
            life.retire_user(self.api, "system", "qa", apply=True)
        result = life.retire_user(
            self.api, "system", "qa", apply=True, drained=True, timeout=0
        )
        self.assertEqual(result["status"], "access_retired_data_retained")
        self.assertFalse(self.api.rows[0]["enabled"])
        self.assertTrue(self.api.rows[1]["enabled"])
        self.assertEqual(self.api.storage["2"]["state"], "ready")
        self.assertEqual(
            self.api.storage["1"]["bucket_name"], self.api.storage["2"]["bucket_name"]
        )
        self.assertIsNotNone(self.api.keys["1"][0]["revoked_at"])
        self.assertEqual(len(self.api.rows), 2)

    def test_active_work_blocks_key_revocation(self):
        self.create()
        self.api.rows[0]["usage"] = {"running": 1}
        with self.assertRaises(life.LifecycleError):
            life.retire_user(self.api, "system", "qa", apply=True, drained=True)
        self.assertTrue(self.api.rows[0]["enabled"])

    def test_pending_storage_is_not_success(self):
        self.create()
        self.api.storage["1"]["state"] = "pending"
        with self.assertRaises(life.LifecycleError):
            self.create()

    def test_key_secret_private_file_no_duplicate(self):
        self.create()
        spec = {"name": "qa", "models": ["qwen"], "scopes": ["inference.invoke"]}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "key.json"
            result = life.issue_key(self.api, "system", "qa", spec, output)
            self.assertNotIn("secret", result)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(output.read_text())["secret"], "test-only-not-a-live-key"
            )
            with self.assertRaises(life.LifecycleError):
                life.issue_key(
                    self.api, "system", "qa", spec, Path(directory) / "duplicate.json"
                )
            self.assertEqual(len(self.api.keys["1"]), 1)

    def test_existing_handover_prevents_key_issuance(self):
        self.create()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "key.json"
            output.touch()
            with self.assertRaises(FileExistsError):
                life.issue_key(
                    self.api,
                    "system",
                    "qa",
                    {"name": "test", "models": ["*"], "scopes": ["mcp.invoke"]},
                    output,
                )
            self.assertEqual(self.api.keys["1"], [])

    def test_origin_rejects_plain_remote_and_embedded_credentials(self):
        for url in (
            "http://example.com",
            "https://admin:secret@example.com",
            "https://example.com?token=x",
        ):
            with self.assertRaises(life.LifecycleError):
                life.validate_origin(url)
        for url in ("https://example.com", "http://127.0.0.1:8000"):
            life.validate_origin(url)

    def test_offline_create_dry_run_does_not_authenticate(self):
        with patch.object(
            life, "AdminClient", side_effect=AssertionError("must not connect")
        ):
            result = life.main(["create-user", "--tenant", "system", "--user", "qa"])
        self.assertFalse(result["apply"])

    def test_truncated_inventory_cannot_be_used(self):
        with patch.object(
            self.api, "request", return_value={"items": [], "truncated": True}
        ):
            with self.assertRaises(life.LifecycleError):
                life.users(self.api)

    def test_policy_private_mode_is_used_without_a_cli_override(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory) / "policy.json"
            policy.write_text(
                json.dumps({"tenants": {"lab": {"id": "lab", "mode": "user"}}})
            )
            result = life.main(
                [
                    "--policy",
                    str(policy),
                    "create-user",
                    "--tenant",
                    "lab",
                    "--user",
                    "alice",
                ]
            )
        self.assertEqual(result["storage_mode"], "user")
        self.assertEqual(result["bucket_owner"], "alice")


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "bucket_name": "fs2-system-source",
            "region": "eu-north1",
            "access_key_id": "test-id",
            "secret_access_key": "test-secret",
        }
        self.target = {**self.source, "bucket_name": "fs2-customer-target"}
        self.spec = seed.transfer_spec(
            "project-test", self.source, self.target, "v3", "a" * 64
        )

    def test_non_destructive_versioned_prefix_and_no_secret_in_plan(self):
        spec = self.spec["spec"]
        self.assertEqual(spec["overwrite_strategy"], "NEVER")
        self.assertFalse(spec["enable_deletes_in_destination"])
        self.assertFalse(spec["touch_unmanaged"])
        self.assertIn("after_one_iteration", spec)
        self.assertEqual(spec["source"]["prefix"], spec["destination"]["prefix"])
        self.assertEqual(spec["source"]["prefix"], "examples/v3/")
        self.assertNotIn("test-secret", json.dumps(self.spec))
        self.assertNotIn("test-id", json.dumps(self.spec))

    def test_retry_has_stable_identity_and_other_destination_does_not(self):
        self.assertEqual(
            self.spec,
            seed.transfer_spec(
                "project-test", self.source, self.target, "v3", "a" * 64
            ),
        )
        other = seed.transfer_spec(
            "project-test",
            self.source,
            {**self.target, "bucket_name": "fs2-another-target"},
            "v3",
            "a" * 64,
        )
        self.assertNotEqual(self.spec["metadata"]["name"], other["metadata"]["name"])

    def test_self_copy_and_unversioned_source_refused(self):
        with self.assertRaises(life.LifecycleError):
            seed.transfer_spec("p", self.source, self.source, "v3", "a" * 64)
        with self.assertRaises(life.LifecycleError):
            seed.transfer_spec("p", self.source, self.target, "latest", "a" * 64)

    def test_existing_transfer_wrong_binding_or_deletes_refused(self):
        for field in ("enable_deletes_in_destination", "touch_unmanaged"):
            bad = copy.deepcopy(self.spec)
            bad["spec"][field] = True
            with self.assertRaises(life.LifecycleError):
                seed.verify_binding(bad, self.spec)
        bad = copy.deepcopy(self.spec)
        bad["spec"]["source"]["prefix"] = "customer-data/"
        with self.assertRaises(life.LifecycleError):
            seed.verify_binding(bad, self.spec)

    def test_stopped_is_not_automatically_success(self):
        class CLI:
            def find(inner, *args):
                return {
                    **self.spec,
                    "metadata": {**self.spec["metadata"], "id": "transfer-test"},
                    "status": {
                        "state": "STOPPED",
                        "last_iteration": {"state": "INTERRUPTED"},
                    },
                }

        with self.assertRaises(life.LifecycleError):
            seed.start_transfer(CLI(), self.spec, self.source, self.target, 0)

    def test_completed_copy_still_requires_verification(self):
        class CLI:
            def find(inner, *args):
                return {
                    **self.spec,
                    "metadata": {**self.spec["metadata"], "id": "transfer-test"},
                    "status": {
                        "state": "STOPPED",
                        "last_iteration": {"state": "COMPLETED"},
                    },
                }

        self.assertEqual(
            seed.start_transfer(CLI(), self.spec, self.source, self.target, 0)[
                "verification_state"
            ],
            "required",
        )

    def test_missing_v1_cli_stops_before_create(self):
        with patch.object(seed.subprocess, "run") as run:
            run.return_value.returncode = 1
            with self.assertRaises(life.LifecycleError):
                seed.TransferCLI("nebius", "test")
            self.assertEqual(run.call_count, 1)

    def test_mismatched_manifest_is_not_verified(self):
        class Pack:
            prefix = "examples/v3/"
            manifest = b"{}"
            digest = "a" * 64

        class Writer:
            def _matches(self, *args):
                return False

        with self.assertRaises(life.LifecycleError):
            seed.verify_pack(Writer(), Pack())

    def test_existing_equal_objects_need_no_extra_quota(self):
        class Pack:
            prefix = "examples/v3/"
            manifest = b"{}"
            digest = "a" * 64
            objects = ()

        class Writer:
            def _matches(self, *args):
                return True

        self.assertEqual(seed.required_headroom(Writer(), Pack()), 0)

    def test_destination_edits_are_preserved_before_transfer(self):
        class Pack:
            prefix = "examples/v3/"
            manifest = b"{}"
            digest = "a" * 64
            objects = ()

        class Writer:
            def _matches(self, *args):
                return False

        with self.assertRaises(life.LifecycleError):
            seed.required_headroom(Writer(), Pack())


if __name__ == "__main__":
    unittest.main()
