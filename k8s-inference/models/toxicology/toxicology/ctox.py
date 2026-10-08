"""CToxPred2 inference with pinned upstream features, transforms and weights."""

from __future__ import annotations

import os
import math
import sys
from pathlib import Path


class CToxRuntime:
    model_id = "ctoxpred2"
    endpoints = {
        "hERG": {"name": "hERG cardiac potassium-channel liability", "task_type": "classification", "units": "unitless model score"},
        "Nav1.5": {"name": "Nav1.5 cardiac sodium-channel liability", "task_type": "classification", "units": "unitless model score"},
        "Cav1.2": {"name": "Cav1.2 cardiac calcium-channel liability", "task_type": "classification", "units": "unitless model score"},
    }

    def __init__(self):
        import joblib
        import torch
        from mordred import Calculator, descriptors

        self.torch = torch
        torch.set_num_threads(int(os.getenv("TOX_CPU_THREADS", "2")))
        self.root = Path(os.getenv("CTOXPRED_ROOT", "/opt/ctoxpred2")) / "CToxPred2"
        sys.path.insert(0, str(self.root))
        # Pickled upstream pipelines import this module by its original name.
        import pairwise_correlation  # noqa: F401
        # The released sklearn pipelines were pickled by a notebook with this
        # exact upstream transformer in __main__. Bind that original class;
        # never substitute a reimplementation or rewrite the fitted pipeline.
        import __main__
        __main__.CorrelationThreshold = pairwise_correlation.CorrelationThreshold
        from utils import compute_fingerprint_features
        from hERG_model import hERGClassifier
        from nav15_model import Nav15Classifier
        from cav12_model import Cav12Classifier

        self.fingerprints = compute_fingerprint_features
        self.descriptors = Calculator(descriptors, ignore_3D=True)
        models = self.root / "models"
        transforms = models / "decriptors_preprocessing"
        self.rf_transform = joblib.load(transforms / "global_preprocessing_pipeline.sav")
        self.rf = {
            "hERG": joblib.load(models / "random_forest/hERG/_ssl_herg_model.joblib"),
            "Nav1.5": joblib.load(models / "random_forest/Nav1.5/_ssl_nav_model.joblib"),
            "Cav1.2": joblib.load(models / "random_forest/Cav1.2/_ssl_cav_model.joblib"),
        }
        for model in self.rf.values():
            model.n_jobs = int(os.getenv("TOX_CPU_THREADS", "2"))
            if list(model.classes_) != [0, 1]:
                raise RuntimeError("unexpected upstream class ordering")
        self.dnn = {"hERG": hERGClassifier(1905, 2, 0.2), "Nav1.5": Nav15Classifier(2453, 2, 0.2), "Cav1.2": Cav12Classifier(2586, 2, 0.2)}
        for name, filename in {"hERG": "hERG/_herg_checkpoint.model", "Nav1.5": "Nav1.5/_nav15_checkpoint.model", "Cav1.2": "Cav1.2/_cav12_checkpoint.model"}.items():
            self.dnn[name].load(str(models / "model_weights" / filename))
            self.dnn[name].train()  # MC dropout is part of the upstream method.
        self.dnn_transform = {
            "Nav1.5": joblib.load(transforms / "Nav1.5/nav_descriptors_preprocessing_pipeline.sav"),
            "Cav1.2": joblib.load(transforms / "Cav1.2/cav_descriptors_preprocessing_pipeline.sav"),
        }

    def metadata(self):
        import numpy as np

        transforms = {"rf-ssl": self.rf_transform, **self.dnn_transform}
        training_empty = {
            name: list(pipeline.named_steps["imputer"].feature_names_in_[np.isnan(pipeline.named_steps["imputer"].statistics_)])
            for name, pipeline in transforms.items()
        }
        return {
            "model_id": self.model_id,
            "model_version": "2a31aa119e27b6b69a5588d18a01f2a27fef4524",
            "upstream_revision": "2a31aa119e27b6b69a5588d18a01f2a27fef4524",
            "device": "cpu", "endpoints": self.endpoints,
            "methods": ["rf-ssl", "dl-sl"], "mc_dropout_iterations": 100,
            "positive_class_definition": "Ion-channel blocker: experimental IC50 <= 10 micromolar (pIC50 >= 5), per the upstream training labels. Not clinical cardiotoxicity.",
            "preprocessing": "Pinned upstream ECFP2 (1024 bits), PubChem (881 bits), Mordred 2D and released sklearn pipelines; no extra molecular standardization.",
            "training_empty_descriptors": training_empty,
            "limitations": ["Cardiac ion-channel screening, not a clinical arrhythmia prediction.", "Model probabilities and dropout dispersion are not calibrated human-safety confidence.", "No validated applicability-domain estimate is supplied."],
        }

    def predict(self, smiles, *, endpoints=None, method="rf-ssl", seed=0):
        import numpy as np
        from rdkit import Chem

        if method not in {"rf-ssl", "dl-sl"}:
            raise ValueError("method must be rf-ssl or dl-sl")
        selected = endpoints or list(self.endpoints)
        fp = self.fingerprints(smiles)
        desc = self.descriptors.pandas([Chem.MolFromSmiles(s) for s in smiles], nproc=1, quiet=True)
        output = [{"predictions": {}, "method": method} for _ in smiles]
        if method == "rf-ssl":
            features = np.concatenate((fp, self.rf_transform.transform(desc)), axis=1)
            for name in selected:
                scores = self.rf[name].predict_proba(features)
                for result, score in zip(output, scores):
                    result["predictions"][name] = {"positive_class_score": float(score[1]), "predicted_class": int(np.argmax(score)), "uncertainty": None}
        else:
            torch = self.torch
            with torch.random.fork_rng(devices=[]), torch.inference_mode():
                torch.manual_seed(seed)
                for name in selected:
                    features = fp if name == "hERG" else np.concatenate((fp, self.dnn_transform[name].transform(desc)), axis=1)
                    tensor = torch.from_numpy(features).float()
                    scores = torch.stack([self.dnn[name](tensor) for _ in range(100)])
                    mean, std = scores.mean(dim=0), scores[:, :, 1].std(dim=0, unbiased=False)
                    for i, result in enumerate(output):
                        result["predictions"][name] = {"positive_class_score": float(mean[i, 1]), "predicted_class": int(mean[i].argmax()), "uncertainty": {"kind": "mc_dropout_std", "value": float(std[i]), "calibrated": False}}
        for result in output:
            for value in result["predictions"].values():
                score = value["positive_class_score"]
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise ValueError("upstream returned an invalid classification score")
        return output
