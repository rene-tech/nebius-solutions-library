import copy
import csv
import io
import json
import shlex
import unittest
from types import SimpleNamespace

from verify_agent_study import sha, verify_study, verify_timing_report, workspace_path


class StudyReportTests(unittest.TestCase):
    def fixture(self):
        log = b'fixture not real MD'
        command = {'step_id': 'repeat-1', 'segment': 1, 'command': ['gmx', 'mdrun', '-nsteps', '10000'],
                   'exit_code': 0, 'performance_ns_per_day': 1.25, 'wall_seconds': 3.5,
                   'checkpoint_step': 10000, 'log': 'run.log'}
        meta = {'path': 'run.log', 'size_bytes': len(log), 'sha256': sha(log)}
        native = {'schema': 'fs2-serve.nebius.ai/gromacs-workflow-result/v1', 'operation_id': 'op',
                  'status': 'succeeded', 'job_id': 'benchmark', 'commands': [command],
                  'completed_steps': ['repeat-1'], 'files': [meta]}
        encoded = json.dumps(native).encode()
        source = {'path': '/workspace/native.artifact', 'sha256': sha(encoded), 'size_bytes': len(encoded)}
        mapping = {'results': [{'source_result_sha256': source['sha256'],
            'files': [{**meta, 'path': '/workspace/native/run.log', 'native_path': 'run.log'}]}]}
        values = {'/workspace/native.artifact': encoded, '/workspace/native/run.log': log,
                  '/workspace/native-files.json': json.dumps(mapping).encode(),
                  '/workspace/receipt.json': json.dumps({'state': 'verified', 'operation_id': 'op',
                                                        'verified_artifacts': [source]}).encode()}
        row = {**command, 'job_id': 'benchmark', 'source_result_sha256': source['sha256'],
               'requested_steps': 10000, 'log_file': '/workspace/native/run.log', 'log_sha256': meta['sha256']}
        report = {'schema': 'scientific-ai/native-md-timing-report/v1', 'operation_id': 'op',
                  'complete': True, 'gaps': [], 'reported_repeats': 1,
                  'receipt_file': '/workspace/receipt.json', 'sources': [source], 'timing_rows': [row]}
        def fetch(path, expected=None):
            value = values[path]
            if expected and (sha(value) != expected['sha256'] or len(value) != expected['size_bytes']):
                raise ValueError('hash mismatch')
            return value
        return report, values, fetch

    def csv(self, report):
        rows = [{**row, 'command': shlex.join(row['command'])} for row in report['timing_rows']]
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue()

    def test_native_source_and_csv_are_verified_not_platform_status(self):
        report, _, fetch = self.fixture()
        result = verify_timing_report(report, self.csv(report),
                                      'repeat-1 | 1.25', fetch, 1)
        self.assertTrue(result['native_report_verified'])
        for mutation in ({'complete': False}, {'timing_rows': []}, {'reported_repeats': 0}):
            with self.assertRaises(ValueError):
                verify_timing_report(report | mutation, '', '', fetch, 1)

    def test_changed_timings_or_invented_counts_fail(self):
        report, _, fetch = self.fixture()
        for field, value in (('wall_seconds', 99), ('executed_steps', 10000), ('requested_steps', 999), ('log_sha256', 'bad')):
            changed = copy.deepcopy(report)
            changed['timing_rows'][0][field] = value
            with self.assertRaises(ValueError):
                verify_timing_report(changed, self.csv(changed), 'repeat-1 | 1.25', fetch, 1)

    def test_changed_source_log_fails(self):
        report, values, fetch = self.fixture()
        values['/workspace/native/run.log'] = b'changed'
        with self.assertRaises(ValueError):
            verify_timing_report(report, self.csv(report), 'repeat-1 | 1.25', fetch, 1)

    def test_csv_cannot_omit_scientific_columns(self):
        report, _, fetch = self.fixture()
        with self.assertRaisesRegex(ValueError, 'omits required'):
            verify_timing_report(report, 'step_id\nrepeat-1\n', 'repeat-1 | 1.25', fetch, 1)

    def test_completed_generic_empty_report_does_not_pass(self):
        client = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(
            content=b'# No native measurements', raise_for_status=lambda: None))
        with self.assertRaises(ValueError, msg='No requested native reports'):
            verify_study(client, {'id': 'study', 'state': 'completed', 'artifacts': [
                {'path': '/workspace/report.md', 'sha256': sha(b'# No native measurements'),
                 'size_bytes': len(b'# No native measurements')}]})

    def test_workspace_paths_do_not_escape(self):
        self.assertEqual(workspace_path('/workspace/a/report.md'), 'a/report.md')
        for value in ('/tmp/file', '/workspace/../secret', 'https://other/file'):
            with self.assertRaises(ValueError):
                workspace_path(value)


if __name__ == '__main__':
    unittest.main()
