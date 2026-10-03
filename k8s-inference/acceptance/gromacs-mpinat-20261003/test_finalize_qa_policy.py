import unittest

from finalize_qa_policy import QA_KEY_ID, validate_policy


class FinalizePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = dict(id=QA_KEY_ID, tenant_id="system", principal_id="qa", expires_at=None,
                           max_concurrency=2, models=["gromacs", "gromacs-mpi"], scopes=["inference:read"])

    def test_only_original_or_temporary_limit(self):
        for count in (2, 3):
            validate_policy(self.policy, {**self.policy, "max_concurrency": count})

    def test_unrelated_changes_are_not_overwritten(self):
        for change in ({"id": "another"}, {"tenant_id": "customer"}, {"principal_id": "another"},
                       {"expires_at": "tomorrow"}, {"models": []}, {"scopes": []}, {"max_concurrency": 8}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_policy(self.policy, {**self.policy, **change})

    def test_baseline_must_be_two(self):
        with self.assertRaises(ValueError):
            validate_policy({**self.policy, "max_concurrency": 3}, self.policy)
