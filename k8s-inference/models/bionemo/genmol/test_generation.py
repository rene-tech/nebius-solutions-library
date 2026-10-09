"""CPU-only yield and sampling-contract regression tests; no model download."""

import unittest
from unittest.mock import patch

from generation import GenerationExhausted, generate_valid_molecules


class Sampler:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.calls = []

    def de_novo_generation(self, count, temperature, randomness, token_length):
        self.calls.append((count, temperature, randomness, token_length))
        return next(self.batches)


class GenerationTest(unittest.TestCase):
    def run_sampler(self, sampler, **overrides):
        params = dict(requested=2, temperature=0.7, randomness=1.1, token_length=20,
                      scoring="QED", unique=False)
        params.update(overrides)
        return generate_valid_molecules(sampler, **params)

    def test_invalid_candidate_is_replaced_without_changing_sampling_parameters(self):
        sampler = Sampler([["CCO", ""], ["CCN"]])
        molecules, metrics = self.run_sampler(sampler)
        self.assertEqual(len(molecules), 2)
        self.assertEqual(sampler.calls, [(2, 0.7, 1.1, 20), (1, 0.7, 1.1, 20)])
        self.assertEqual(metrics["invalid_candidates"], 1)
        self.assertEqual(metrics["sampling_attempts"], 2)
        self.assertEqual(metrics["requested_molecules"], metrics["returned_molecules"])

    def test_canonical_duplicates_are_replaced_when_unique_requested(self):
        sampler = Sampler([["CCO", "OCC"], ["CCN"]])
        molecules, metrics = self.run_sampler(sampler, unique=True)
        self.assertEqual({item["smiles"] for item in molecules}, {"CCO", "CCN"})
        self.assertEqual(metrics["duplicate_candidates"], 1)

    def test_duplicates_remain_valid_when_unique_false(self):
        sampler = Sampler([["CCO", "OCC"]])
        molecules, metrics = self.run_sampler(sampler)
        self.assertEqual(len(molecules), 2)
        self.assertEqual(metrics["sampling_attempts"], 1)
        self.assertEqual(metrics["duplicate_candidates"], 0)

    def test_partial_yield_is_explicit_exhaustion_not_success(self):
        sampler = Sampler([["CCO", "CCO"], ["CCO"], ["CCO"]])
        with self.assertRaises(GenerationExhausted) as caught:
            self.run_sampler(sampler, unique=True, max_sampling_attempts=3)
        metrics = caught.exception.metrics
        self.assertEqual(metrics["accepted_molecules"], 1)
        self.assertEqual(metrics["returned_molecules"], 0)
        self.assertEqual(metrics["sampling_attempts"], 3)
        self.assertEqual(metrics["candidate_requests"], 4)

    def test_empty_sampler_yield_is_bounded(self):
        sampler = Sampler([[], [], []])
        with self.assertRaises(GenerationExhausted) as caught:
            self.run_sampler(sampler, max_sampling_attempts=3)
        self.assertEqual(len(sampler.calls), 3)
        self.assertEqual(caught.exception.metrics["sampled_candidates"], 0)

    def test_candidate_budget_is_not_exceeded(self):
        sampler = Sampler([["", ""]])
        with self.assertRaises(GenerationExhausted) as caught:
            self.run_sampler(sampler, candidate_multiplier=1)
        self.assertEqual(len(sampler.calls), 1)
        self.assertEqual(caught.exception.metrics["candidate_requests"], 2)

    def test_short_upstream_batch_is_refilled(self):
        sampler = Sampler([["CCO"], ["CCN"]])
        molecules, metrics = self.run_sampler(sampler)
        self.assertEqual(len(molecules), 2)
        self.assertEqual(metrics["candidate_requests"], 3)
        self.assertEqual(metrics["sampled_candidates"], 2)
        self.assertEqual(metrics["upstream_unreturned_candidates"], 1)

    def test_logp_scoring_unchanged(self):
        from rdkit import Chem
        from rdkit.Chem import Crippen
        molecules, _ = self.run_sampler(Sampler([["CCO", "CCN"]]), scoring="LOGP")
        for item in molecules:
            self.assertAlmostEqual(item["score"], Crippen.MolLogP(Chem.MolFromSmiles(item["smiles"])))

    def test_nonfinite_score_is_replaced(self):
        sampler = Sampler([["CCO", "CCN"], ["CCC"]])
        with patch("generation.QED.qed", side_effect=[float("nan"), 0.2, 0.3]):
            molecules, metrics = self.run_sampler(sampler)
        self.assertEqual(len(molecules), 2)
        self.assertEqual(metrics["nonfinite_score_candidates"], 1)

    def test_low_token_length_does_not_mean_heavy_atom_limit(self):
        molecules, _ = self.run_sampler(Sampler([["CCCCCCCC", "CCN"]]), token_length=2)
        self.assertEqual(len(molecules), 2)


if __name__ == "__main__":
    unittest.main()
