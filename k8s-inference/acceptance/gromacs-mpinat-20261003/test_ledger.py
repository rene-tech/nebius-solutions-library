import tempfile
from pathlib import Path
import unittest
from ledger import Ledger, protocol, union_steps


class LedgerTests(unittest.TestCase):
    def test_retry_intervals_are_not_double_counted(self):
        self.assertEqual(union_steps([(0, 100), (80, 150), (0, 100), (200, 250)]), 200)
        self.assertEqual(union_steps([]), 0)

    def test_unknown_is_not_a_zero(self):
        self.assertIsNone(protocol("no native log")['particle_count'])
        self.assertIsNone(protocol("   dt = 0.002\n")['initial_step'])

    def test_protocol_is_not_guessed(self):
        value = protocol("   dt = 0.002\n   init-step = 0\n   fep-lambdas = 0 0.5 1\nThere are: 81743 Atoms\n")
        self.assertEqual(value['particle_count'], 81743)
        self.assertEqual(value['timestep_ps'], .002)
        self.assertEqual(value['native_input_parameters']['fep-lambdas'], '0 0.5 1')
        self.assertIsNone(value['force_field_name'])

    def test_null_requires_explicit_unknown_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / 'evidence.sqlite')
            with self.assertRaises(ValueError):
                ledger.measure('op', 'attempt', 'job', 'replica', 'steps', None, 'steps', 'measured', 'none')
            ledger.db.close()


if __name__ == '__main__':
    unittest.main()
