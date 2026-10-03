import copy
import json
from types import SimpleNamespace
import unittest

from verify_agent_case import canonical, direct_delivery, is_terminal, normalize, runtime_canonical, sha, validate_native, validate_plan, validate_recovery_identity


class SelectedCaseTests(unittest.TestCase):
    def test_direct_delivery_is_distinct_from_saved_study_and_admission(self):
        verifier = SimpleNamespace(read_tool_output=json.loads)
        payload = {'results': [{'kind': 'native-md', 'path': '/workspace/receipt'},
            *[{'kind': 'file', 'path': '/workspace/report/' + name} for name in
              ('native-timing-report.json', 'native-timing-report.md', 'native-timings.csv')]]}
        call = {'args': json.dumps(payload), 'output': json.dumps({
            'schema': 'scientific-verified-delivery/v1', 'status': 'completed', 'report_markdown': 'actual report'})}
        self.assertEqual(direct_delivery([call], verifier)['receipt_directory'], '/workspace/receipt')
        for invalid in ([], [call, call], [dict(call, output=json.dumps({'state': 'accepted'}))],
                        [dict(call, args=json.dumps({'results': payload['results'][:-1]}))]):
            with self.assertRaises(ValueError):
                direct_delivery(invalid, verifier)

    def test_observation_expiry_is_not_terminal_native_failure(self):
        for state in ('queued', 'running', 'observation_expired', 'cancelling'):
            self.assertFalse(is_terminal(state))
        for state in ('completed', 'failed', 'cancelled', 'needs_attention'):
            self.assertTrue(is_terminal(state))

    def fixture(self):
        parameters = {'schema': 'fs2-serve.nebius.ai/gromacs-workflow-request/v1',
                      'jobs': [{'id': 'benchmark', 'steps': [
                          {'id': 'finite-tpr', 'command': 'convert-tpr',
                           'args': ['-s', 'original.tpr', '-o', 'benchmark.tpr', '-nsteps', '10000']},
                          *[
                          {'id': 'repeat-' + str(i), 'command': 'mdrun',
                           'args': ['-s', 'benchmark.tpr']}
                          for i in range(1, 4)]]}], 'output_prefix': 'runs/qa-case'}
        encoded = canonical(parameters)
        expected = {'case_id': 'mpinat-case', 'source': {'path': '/workspace/input.tar.gz',
                    'sha256': 'bundle', 'size_bytes': 123}, 'parameter_path': '/workspace/parameters.json',
                    'parameter_file_sha256': sha(encoded), 'parameter_size_bytes': len(encoded),
                    'parameters_sha256': sha(encoded), 'parameters': parameters,
                    'idempotency_key': 'qa-case', 'provenance': {'tpr_sha256': sha(b'tpr'), 'tpr_bytes': 3}}
        plan = {'steps': [{'id': 'simulate', 'kind': 'batch', 'model': 'gromacs',
                 'tool': 'submit_gromacs_workflow', 'operation': 'run-workflow',
                 'source': expected['source']['path'], 'parameters': expected['parameter_path'],
                 'compression': 'gzip', 'idempotency_key': 'qa-case'}]}
        record = {'id': 'study', 'state': 'completed', 'owner': 'qa', 'output_directory': '/workspace/out',
                  'steps': {'simulate': {'operation_id': 'op'}},
                  'inputs': {expected['source']['path']: expected['source'],
                             expected['parameter_path']: {'sha256': sha(encoded), 'size_bytes': len(encoded)}}}
        record['identity'] = sha(canonical({'plan': plan, 'output': '/workspace/out', 'owner': 'qa'}))
        return expected, {'record': record, 'plan': plan}, copy.deepcopy(record)

    def test_admitted_case_binds_original_files_and_operation(self):
        expected, frozen, study = self.fixture()
        result = validate_plan(expected, frozen, study)
        self.assertEqual(result['operation_id'], 'op')
        for key in ('sha256', 'size_bytes'):
            altered = copy.deepcopy(frozen)
            altered['record']['inputs'][expected['source']['path']][key] = 'wrong'
            with self.assertRaisesRegex(ValueError, 'Frozen study inputs'):
                validate_plan(expected, altered, study)

    def test_valid_other_case_or_recovered_mem_is_not_selected_benchmark(self):
        expected, frozen, study = self.fixture()
        for key, value in (('source', '/workspace/mem.tar.gz'), ('parameters', '/workspace/mem.json'),
                           ('idempotency_key', 'prior-MEM'), ('compression', 'none')):
            altered = copy.deepcopy(frozen)
            altered['plan']['steps'][0][key] = value
            altered['record']['identity'] = sha(canonical({'plan': altered['plan'],
                'output': '/workspace/out', 'owner': 'qa'}))
            with self.assertRaisesRegex(ValueError, 'Study changed'):
                validate_plan(expected, altered, study)

    def test_omitted_encoding_uses_contract_binding_but_native_must_be_gzip(self):
        expected, frozen, study = self.fixture()
        del frozen['plan']['steps'][0]['compression']
        frozen['record']['identity'] = sha(canonical({'plan': frozen['plan'],
            'output': '/workspace/out', 'owner': 'qa'}))
        self.assertEqual(validate_plan(expected, frozen, study)['operation_id'], 'op')
        args = self.native_fixture()
        args[3]['request_descriptor']['compression'] = 'none'
        with self.assertRaisesRegex(ValueError, 'requested gzip'):
            validate_native(*args[:-1])

    def native_fixture(self):
        expected, frozen, study = self.fixture()
        binding = validate_plan(expected, frozen, study)
        original = {'path': 'original.tpr', 'sha256': sha(b'tpr'), 'size_bytes': 3}
        native = {'job_id': 'benchmark', 'engine_id': 'exact-engine', 'files': [original]}
        native['recipe_sha256'] = sha(runtime_canonical({'request': normalize(expected['parameters']),
                                                       'job': 'benchmark', 'image': 'exact-engine'}))
        body = canonical(native)
        source = {'path': '/workspace/native.json', 'size_bytes': len(body), 'sha256': sha(body)}
        report = {'operation_id': 'op', 'timing_rows': [{'requested_steps': 10000}] * 3,
                  'sources': [source]}
        receipt = {'operation_id': 'op', 'identity': {'model_id': 'gromacs', 'source_sha256': 'bundle',
                   'parameters_sha256': expected['parameters_sha256'], 'idempotency_key': 'qa-case'},
                   'request_descriptor': {'compression': 'gzip'}, 'verified_artifacts': [source]}
        mapping = {'results': [{'source_result_sha256': source['sha256'],
                   'files': [{'native_path': 'original.tpr', 'path': '/workspace/native/original.tpr'}]}]}
        files = {source['path']: body, '/workspace/native/original.tpr': b'tpr'}
        def fetch(path, reference):
            value = files[path]
            if sha(value) != reference['sha256'] or len(value) != reference['size_bytes']:
                raise ValueError('Changed bytes')
            return value
        return expected, binding, report, receipt, mapping, fetch, files

    def test_exact_native_recipe_and_original_tpr_pass(self):
        args = self.native_fixture()
        self.assertTrue(validate_native(*args[:-1])['selected_case_verified'])

    def test_recovery_preserves_original_submission_identity_without_mutation(self):
        expected, binding, report, receipt, mapping, fetch, files = self.native_fixture()
        submission = copy.deepcopy(receipt)
        submission['identity'].update(endpoint='https://example.test/mcp', caller_fingerprint='qa')
        recovery = copy.deepcopy(receipt)
        recovery['identity'] = {'operation_id': 'op', 'endpoint': 'https://example.test/mcp',
                                'caller_fingerprint': 'qa'}
        recovery.pop('request_descriptor')
        recovery['state'] = 'verified'
        untouched = copy.deepcopy(recovery)
        validate_recovery_identity(submission, recovery)
        self.assertTrue(validate_native(expected, binding, report, recovery, mapping, fetch,
                                       submission)['selected_case_verified'])
        self.assertEqual(recovery, untouched)
        for key, value in (('operation_id', 'other-op'), ('endpoint', 'https://other.test'),
                           ('caller_fingerprint', 'customer')):
            altered = copy.deepcopy(recovery)
            altered['identity'][key] = value
            with self.assertRaises(ValueError):
                validate_recovery_identity(submission, altered)
        for field, value in (('operation_id', 'other-op'), ('state', 'running')):
            altered = dict(recovery, **{field: value})
            with self.assertRaises(ValueError):
                validate_recovery_identity(submission, altered)
        altered = copy.deepcopy(submission)
        altered['identity']['source_sha256'] = 'other-bundle'
        with self.assertRaisesRegex(ValueError, 'source_sha256'):
            validate_native(expected, binding, report, recovery, mapping, fetch, altered)

    def test_wrong_operation_input_parameters_steps_or_tpr_fail(self):
        for kind in ('operation', 'input', 'parameters', 'steps', 'tpr', 'recipe'):
            expected, binding, report, receipt, mapping, fetch, files = self.native_fixture()
            if kind == 'operation':
                report['operation_id'] = 'completed-MEM'
            elif kind == 'input':
                receipt['identity']['source_sha256'] = 'other-bundle'
            elif kind == 'parameters':
                receipt['identity']['parameters_sha256'] = 'other-physics'
            elif kind == 'steps':
                report['timing_rows'][0]['requested_steps'] = 10
            elif kind == 'tpr':
                files['/workspace/native/original.tpr'] = b'bad'
            else:
                native = json.loads(files['/workspace/native.json'])
                native['recipe_sha256'] = 'other-physics'
                body = canonical(native)
                files['/workspace/native.json'] = body
                report['sources'][0].update(sha256=sha(body), size_bytes=len(body))
                mapping['results'][0]['source_result_sha256'] = sha(body)
            with self.assertRaises(ValueError, msg=kind):
                validate_native(expected, binding, report, receipt, mapping, fetch)


if __name__ == '__main__':
    unittest.main()
