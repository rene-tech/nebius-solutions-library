import unittest

from prepare_agent_continuation import IMAGE, MEM_OPERATION, REMAINING, select


class ContinuationTests(unittest.TestCase):
    def fixtures(self):
        return ({'candidate_image': IMAGE, 'cases': [{'case_id': name} for name in
                sorted(REMAINING | {'mpinat-benchmem'})]},
                {'operation_id': MEM_OPERATION, 'native_report_verified': True, 'verified_repeats': 3})

    def test_complete_mem_is_excluded_without_discarding_unsubmitted_pep(self):
        manifest, proof = self.fixtures()
        cases = select(manifest, proof)
        self.assertEqual({row['case_id'] for row in cases}, REMAINING)
        self.assertEqual(len(cases), 18)
        self.assertEqual(len(manifest['cases']), 19)

    def test_unproven_completion_does_not_shrink_cohort(self):
        manifest, proof = self.fixtures()
        for update in ({'operation_id': 'other'}, {'native_report_verified': False}, {'verified_repeats': 0}):
            with self.assertRaises(ValueError):
                select(manifest, proof | update)

    def test_missing_or_extra_cases_require_reconciliation(self):
        manifest, proof = self.fixtures()
        for cases in (manifest['cases'][:-1], manifest['cases'] + [{'case_id': 'mpinat-benchsnc'}]):
            with self.assertRaises(ValueError):
                select(manifest | {'cases': cases}, proof)


if __name__ == '__main__':
    unittest.main()
