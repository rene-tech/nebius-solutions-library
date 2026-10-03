import unittest
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent_cases import IMAGE, R5_IMAGE, R5_REMAINING, QA_KEY_ID, gate_state, selected_cases, refresh_browser_session, wait_operator_pause, admitted_studies


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

    def test_browser_refresh_only_uses_account_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'login.json').write_text(json.dumps({'email': 'qa@example.invalid', 'password': 'test-only'}))
            client = MagicMock()
            client.headers = {'Authorization': 'Bearer synthetic-browser-session'}
            constructor = MagicMock()
            constructor.return_value.__enter__.return_value = client
            harness = SimpleNamespace(httpx=SimpleNamespace(Client=constructor), UA='qa',
                                      authenticate=MagicMock(), save=MagicMock())
            refresh_browser_session(harness, root, root, 'http://127.0.0.1:13207')
            harness.authenticate.assert_called_once_with(client, {'email': 'qa@example.invalid', 'password': 'test-only'})
            harness.save.assert_called_once_with(root / 'session.json', {'token': 'synthetic-browser-session'})
            self.assertFalse(json.loads((root / 'browser-session-refreshes.jsonl').read_text())['inference_key_changed'])

    def test_r5_excludes_completed_mem_and_requires_eighteen(self):
        cases = [{'case_id': name} for name in sorted(R5_REMAINING)]
        self.assertEqual(len(selected_cases({'candidate_image': R5_IMAGE, 'cases': cases}, 'benchmarks')), 18)
        with self.assertRaises(ValueError):
            selected_cases({'candidate_image': R5_IMAGE, 'cases': cases + [{'case_id': 'mpinat-benchmem'}]}, 'benchmarks')
        with self.assertRaises(ValueError):
            selected_cases({'candidate_image': R5_IMAGE, 'cases': cases[:-1]}, 'benchmarks')
        with self.assertRaises(ValueError):
            selected_cases({'candidate_image': R5_IMAGE, 'cases': cases[:-1] + [{'case_id': 'mpinat-unknown'}]}, 'benchmarks')

    def test_only_actual_accepted_durable_study_is_observed(self):
        identifier = '96b89a35-9e48-5986-92ce-5bcdab33dad6'
        verifier = SimpleNamespace(read_tool_output=json.loads)
        call = {'name': 'run_scientific_workflow_mcp_environment-execution', 'output': json.dumps({
            'id': identifier, 'study_admission': 'accepted', 'durable_study': True})}
        self.assertEqual(admitted_studies([call, call], verifier), [identifier])
        self.assertEqual(admitted_studies([{'name': 'upload_workspace_files', 'output': call['output']}], verifier), [])
        self.assertEqual(admitted_studies([dict(call, output='{}')], verifier), [])

    def test_operator_pause_waits_without_cancelling_or_mutating_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flag = root / 'pause.json'
            flag.write_text('{}')
            async def release(_seconds):
                flag.unlink()  # Represents the operator's explicit release.
            with patch('run_agent_cases.asyncio.sleep', side_effect=release) as sleeping:
                asyncio.run(wait_operator_pause(flag, root, 45))
            sleeping.assert_called_once_with(45)
            self.assertEqual([json.loads(line)['state'] for line in
                              (root / 'operator-pauses.jsonl').read_text().splitlines()], ['paused', 'resumed'])


if __name__ == '__main__':
    unittest.main()
