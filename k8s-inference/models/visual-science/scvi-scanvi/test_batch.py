"""Run in the pinned batch image: python -m unittest discover -s /tests."""

import tempfile
import unittest
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
from scipy import sparse

from fs2_scvi import PARAMETER_SCHEMA
from fs2_scvi.contracts import normalize
from fs2_scvi.data import inspect_counts, load_counts, validate_values


class ContractTests(unittest.TestCase):
    def test_defaults_and_no_demo_epoch_cap(self):
        params = normalize({"schema": PARAMETER_SCHEMA})
        self.assertIsNone(params["max_epochs"])
        self.assertEqual(params["scanvi_max_epochs"], 20)
        self.assertEqual(
            normalize({"schema": PARAMETER_SCHEMA, "max_epochs": 400})["max_epochs"],
            400,
        )

    def test_annotation_requires_column_but_mapping_does_not(self):
        with self.assertRaises(ValueError):
            normalize({"schema": PARAMETER_SCHEMA, "method": "scanvi"})
        normalize({"schema": PARAMETER_SCHEMA, "method": "scanvi", "mode": "map-query"})

    def test_unknown_and_invalid_fields(self):
        for field, value in (
            ("no_such_option", True),
            ("batch_size", 0),
            ("max_epochs", 0),
            ("output_prefix", "../customer"),
            ("counts_source", "layers/../../raw"),
        ):
            with self.subTest(field=field), self.assertRaises(Exception):
                normalize({"schema": PARAMETER_SCHEMA, field: value})


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "input.h5ad"
        self.adata = ad.AnnData(
            sparse.csr_matrix(np.ones((200, 10), dtype=np.float32)),
            obs=pd.DataFrame(
                {"batch": ["a", "b"] * 100, "label": ["T", "B"] * 100},
                index=[f"cell-{i}" for i in range(200)],
            ),
        )
        self.parameters = normalize(
            {"schema": PARAMETER_SCHEMA, "gene_selection": "all", "batch_key": "batch"}
        )

    def save(self):
        ad.settings.allow_write_nullable_strings = True
        self.adata.write_h5ad(self.path)

    def test_sparse_and_dense(self):
        for dense in (False, True):
            if dense:
                self.adata.X = self.adata.X.toarray()
            self.save()
            loaded, info = load_counts(
                self.path, self.parameters, budget_bytes=128 * 1024**3
            )
            self.assertEqual(loaded.shape, (200, 10))
            self.assertEqual(info["cells"], 200)
            self.assertEqual(info["selected_genes"], 10)

    def test_raw_counts_not_processed_x(self):
        self.adata.raw = self.adata.copy()
        self.adata.X *= 0.1
        self.save()
        with self.assertRaises(ValueError):
            load_counts(self.path, self.parameters, budget_bytes=128 * 1024**3)
        loaded, _ = load_counts(
            self.path,
            {**self.parameters, "counts_source": "raw.X"},
            budget_bytes=128 * 1024**3,
        )
        self.assertEqual(loaded.X.sum(), 2000)

    def test_select_counts_layer_and_ignore_other_layers(self):
        self.adata.layers["counts"] = self.adata.X.copy()
        self.adata.layers["unused"] = self.adata.X.copy() * 0.1
        self.adata.X *= 0.1
        self.save()
        loaded, _ = load_counts(
            self.path,
            {**self.parameters, "counts_source": "layers/counts"},
            budget_bytes=128 * 1024**3,
        )
        self.assertEqual(loaded.X.sum(), 2000)
        self.assertEqual(len(loaded.layers), 0)

    def test_validates_beyond_old_128_row_sample(self):
        self.adata.X.data[-1] = 0.5
        self.save()
        with self.assertRaisesRegex(ValueError, "integer raw counts"):
            load_counts(self.path, self.parameters, budget_bytes=128 * 1024**3)

    def test_budget_is_checked_before_loading(self):
        self.save()
        self.assertGreater(inspect_counts(self.path, "X")["estimated_host_bytes"], 0)
        with self.assertRaisesRegex(ValueError, "larger-memory profile"):
            load_counts(self.path, self.parameters, budget_bytes=1024)

    def test_preselected_genes(self):
        self.adata.var["highly_variable"] = [True] * 4 + [False] * 6
        self.save()
        loaded, info = load_counts(
            self.path,
            {**self.parameters, "gene_selection": "provided"},
            budget_bytes=128 * 1024**3,
        )
        self.assertEqual(loaded.n_vars, 4)
        self.assertEqual(info["input_genes"], 10)

    def test_fully_labeled_scanvi_accepted(self):
        self.save()
        params = normalize(
            {"schema": PARAMETER_SCHEMA, "method": "scanvi", "labels_key": "label"}
        )
        loaded, _ = load_counts(self.path, params, budget_bytes=128 * 1024**3)
        self.assertNotIn(params["unlabeled_category"], loaded.obs.label.unique())

    def test_nonfinite_negative_fractional_values(self):
        for value in (float("nan"), float("inf"), -1, 0.01):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_values(sparse.csr_matrix([[1.0, value]]))


if __name__ == "__main__":
    unittest.main()
