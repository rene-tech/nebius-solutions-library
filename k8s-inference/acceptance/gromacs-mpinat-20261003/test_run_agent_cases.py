import unittest

from run_agent_cases import IMAGE, QA_KEY_ID, gate_state, selected_cases


class AgentAdmissionTests(unittest.TestCase):
    def policy(self):
        return {'id': QA_KEY_ID, 'tenant_id': 'system', 'principal_id': 'qa', 'max_concurrency': 2}

    def test_both_parent_gates_required(self):
        self.assertTrue(all(gate_state(self.policy(), {'all_verified': True}).values()))
        self.assertFalse(all(gate_state({}, {'all_verified': True}).values()))
        self.assertFalse(all(gate_state(self.policy(), {'mpi': [{'verified': True}]}).values()))

    def test_other_key_or_policy_is_not_baseline_handoff(self):
        for change in ({'id': 'customer'}, {'max_concurrency': 3}, {'principal_id': 'development'}):
            self.assertFalse(all(gate_state(self.policy() | change, {'all_verified': True}).values()))

    def test_only_exact_starter(self):
        manifest = {'candidate_image': IMAGE, 'cases': [{'case_id': 'assumed-hosted-gromacs'}]}
        self.assertEqual(len(selected_cases(manifest, 'alanine')), 1)
        with self.assertRaises(ValueError):
            selected_cases(manifest | {'candidate_image': 'old-image'}, 'alanine')

    def test_completed_or_duplicate_not_replayed(self):
        cases = [{'case_id': 'mpinat-' + str(index)} for index in range(19)]
        self.assertEqual(len(selected_cases({'candidate_image': IMAGE, 'cases': cases}, 'benchmarks')), 19)
        for replacement in ('mpinat-benchsnc', 'mpinat-0'):
            invalid = cases[:-1] + [{'case_id': replacement}]
            with self.assertRaises(ValueError):
                selected_cases({'candidate_image': IMAGE, 'cases': invalid}, 'benchmarks')


if __name__ == '__main__':
    unittest.main()
