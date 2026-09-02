"""Contract tests for the cancer-immunotherapy model source qualification manifest.

The manifest is a research artifact that downstream onboarding reads as its
source of truth for model identity, licensing and H100 fit. These tests enforce
the invariants that keep it decision-grade: that every requested model name is
covered, that revisions are immutable, that no claim of hardware support or NIM
availability is made without evidence, and that ambiguity is surfaced as an
answerable question rather than silently guessed.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "model-source-qualification.json"
SCHEMA_PATH = ROOT / "model-source-qualification.schema.json"

# The eight names exactly as the customer wrote them in the workload definition.
REQUESTED_NAMES = (
    "Proteina-Complexa",
    "BoltzGen",
    "mosaic",
    "BindCraft",
    "RFdiffusion",
    "ESMFold2",
    "ESMFold2-Fast",
    "Protenix v2",
    "AlphaFold3",
)

# A 40-character lowercase hex string is a git commit SHA. Anything shorter, or
# a branch name, is not a reproducible revision.
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# Substrings that would indicate a credential leaked into a committed artifact.
CREDENTIAL_MARKERS = (
    "hf_",
    "nvapi-",
    "ghp_",
    "AKIA",
    "-----BEGIN",
    "api_key=",
    "password",
)


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


class ManifestSchemaTests(unittest.TestCase):
    """The manifest must satisfy its own published schema."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(MANIFEST_PATH)
        cls.schema = load_json(SCHEMA_PATH)

    def test_schema_is_itself_valid(self) -> None:
        jsonschema.Draft202012Validator.check_schema(self.schema)

    def test_manifest_validates_against_schema(self) -> None:
        validator = jsonschema.Draft202012Validator(self.schema)
        errors = sorted(validator.iter_errors(self.manifest), key=lambda e: list(e.path))
        details = "\n".join(
            f"  {'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
            for err in errors
        )
        self.assertEqual([], errors, f"manifest violates its schema:\n{details}")


class CoverageTests(unittest.TestCase):
    """Every name the customer asked for must be accounted for."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(MANIFEST_PATH)
        cls.models = cls.manifest["models"]

    def test_every_requested_name_is_covered(self) -> None:
        covered = " | ".join(m["requested_name"] for m in self.models.values())
        for name in REQUESTED_NAMES:
            with self.subTest(requested=name):
                self.assertIn(
                    name.lower(),
                    covered.lower(),
                    f"requested model {name!r} has no entry in the manifest",
                )

    def test_primary_and_secondary_roles_are_assigned(self) -> None:
        """The customer split the request into primary and secondary volume tiers."""
        roles = {m["role"] for m in self.models.values()}
        self.assertIn("primary", roles)
        self.assertIn("secondary", roles)

    def test_alternatives_are_never_presented_as_requested_models(self) -> None:
        """A substitute must not be silently reported as the thing that was asked for."""
        for model_id, model in self.models.items():
            if model["role"] != "alternative":
                continue
            with self.subTest(model=model_id):
                self.assertNotIn(
                    model["identity"]["canonical_name"],
                    REQUESTED_NAMES,
                    f"{model_id} is flagged as an alternative but carries a requested "
                    "model's canonical name, which would let onboarding mistake it "
                    "for the real thing",
                )


class RevisionImmutabilityTests(unittest.TestCase):
    """Pinning must be reproducible: a tag or branch alone is not enough."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.models = load_json(MANIFEST_PATH)["models"]

    def test_commit_revisions_are_full_shas_or_explain_why_not(self) -> None:
        for model_id, model in self.models.items():
            code = model["code"]
            if code["revision_kind"] not in ("commit", "release-tag-and-commit"):
                continue
            with self.subTest(model=model_id):
                revision = code["revision"]
                if FULL_SHA.match(revision):
                    continue
                # A non-SHA revision is tolerated only when the label states
                # plainly that onboarding must resolve and pin one itself.
                label = (code.get("revision_label") or "").lower()
                self.assertTrue(
                    "resolve" in label and "pin" in label,
                    f"{model_id} pins {revision!r}, which is not an immutable commit "
                    "SHA, and its revision_label does not instruct onboarding to "
                    "resolve and pin one",
                )

    def test_weight_revisions_are_recorded(self) -> None:
        for model_id, model in self.models.items():
            for weight in model["weights"]:
                with self.subTest(model=model_id, artifact=weight["artifact_id"]):
                    self.assertTrue(
                        weight["revision"].strip(),
                        "every weight artifact needs a revision or an explicit "
                        "statement that none is obtainable",
                    )


class EvidenceDisciplineTests(unittest.TestCase):
    """No capability or support claim without evidence behind it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.models = load_json(MANIFEST_PATH)["models"]

    def test_documented_h100_support_names_the_hardware(self) -> None:
        """`documented-supported` is reserved for an upstream statement about Hopper."""
        for model_id, model in self.models.items():
            fit = model["h100_fit"]
            if fit["state"] != "documented-supported":
                continue
            with self.subTest(model=model_id):
                evidence = fit["evidence"].lower()
                self.assertTrue(
                    "h100" in evidence or "hopper" in evidence,
                    f"{model_id} claims documented H100 support but its evidence "
                    "never names H100 or Hopper",
                )

    def test_untested_fit_is_not_dressed_up_as_support(self) -> None:
        for model_id, model in self.models.items():
            fit = model["h100_fit"]
            if fit["state"] != "expected-compatible-untested":
                continue
            with self.subTest(model=model_id):
                self.assertTrue(
                    fit["evidence"].strip(),
                    "an untested expectation must still say what it is based on",
                )

    def test_nim_choice_requires_nim_availability(self) -> None:
        """A NIM runtime cannot be recommended for a model that has no NIM."""
        for model_id, model in self.models.items():
            rec = model["runtime_recommendation"]
            if rec["choice"] != "existing-nim":
                continue
            with self.subTest(model=model_id):
                self.assertTrue(
                    rec.get("nim_available"),
                    f"{model_id} recommends an existing NIM while nim_available is "
                    "not set, which would be an assumed rather than observed NIM",
                )

    def test_every_model_carries_sources(self) -> None:
        for model_id, model in self.models.items():
            with self.subTest(model=model_id):
                self.assertGreaterEqual(
                    len(model["sources"]),
                    1,
                    "every model needs at least one primary source",
                )
                for source in model["sources"]:
                    self.assertTrue(
                        source["established"].strip(),
                        f"{model_id} cites {source['url']} without saying what it "
                        "established",
                    )

    def test_semantic_criteria_are_scientific_not_transport_level(self) -> None:
        """An HTTP status or exit code alone never proves a model works."""
        weak = ("http 200", "status 200", "exit code 0", "returns 200")
        for model_id, model in self.models.items():
            criteria = model["semantic_validator"]["success_criteria"]
            with self.subTest(model=model_id):
                self.assertGreaterEqual(
                    len(criteria), 2, "one criterion is not a semantic validator"
                )
                joined = " ".join(criteria).lower()
                for phrase in weak:
                    self.assertNotIn(
                        phrase,
                        joined,
                        f"{model_id} leans on a transport-level check ({phrase!r}) "
                        "as a success criterion",
                    )


class BlockerAndGateTests(unittest.TestCase):
    """Blocking gates must be visible, and ambiguity must be answerable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(MANIFEST_PATH)
        cls.models = cls.manifest["models"]

    def test_blocking_gates_are_actionable(self) -> None:
        for model_id, model in self.models.items():
            for gate in model["access_gates"]:
                if not gate["blocking"]:
                    continue
                with self.subTest(model=model_id, gate=gate["gate_id"]):
                    self.assertGreater(
                        len(gate["action"]), 80,
                        "a blocking gate must spell out the exact one-time step, "
                        "not merely assert that a gate exists",
                    )

    def test_unresolved_names_have_a_question_and_a_default(self) -> None:
        questioned = {
            model_id
            for question in self.manifest["open_questions"]
            for model_id in question["models"]
        }
        for model_id, model in self.models.items():
            state = model["resolution"]["state"]
            if state == "resolved":
                continue
            with self.subTest(model=model_id, state=state):
                self.assertIn(
                    model_id,
                    questioned,
                    f"{model_id} is {state} but no open question would resolve it, "
                    "so onboarding would have to guess",
                )

    def test_open_questions_never_block_silently(self) -> None:
        for question in self.manifest["open_questions"]:
            with self.subTest(question=question["question_id"]):
                self.assertTrue(
                    question["default_if_unanswered"].strip(),
                    "every question needs a stated default so unanswered questions "
                    "delay nothing",
                )
                self.assertTrue(
                    question["question"].rstrip().endswith("?"),
                    "an open question must actually be phrased as a question",
                )

    def test_license_verdicts_match_the_declared_use_class(self) -> None:
        """A prohibited-commercial artifact is only acceptable under academic use."""
        use_class = self.manifest["use_class"]
        for model_id, model in self.models.items():
            for weight in model["weights"]:
                if weight["commercial_use"] != "prohibited":
                    continue
                with self.subTest(model=model_id, artifact=weight["artifact_id"]):
                    self.assertEqual(
                        "academic-non-commercial",
                        use_class,
                        f"{model_id} depends on weights that forbid commercial use, "
                        "which is incompatible with the declared use class",
                    )
                    self.assertTrue(
                        model["blockers"],
                        f"{model_id} carries non-commercial weights but records no "
                        "blocker warning that the lane cannot carry over to "
                        "commercial work",
                    )


class DiscoveryTests(unittest.TestCase):
    """Observed state must be recorded precisely and never over-read."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.discovery = load_json(MANIFEST_PATH)["discovery"]

    def test_method_states_the_access_was_read_only(self) -> None:
        method = self.discovery["method"].lower()
        self.assertIn("read-only", method)

    def test_reachable_cluster_records_concrete_identity(self) -> None:
        """Claiming reachability obliges us to say exactly what we reached."""
        cluster = self.discovery["cluster"]
        if not cluster["reachable"]:
            self.skipTest("cluster recorded as unreachable")
        for field in ("context", "project", "region"):
            with self.subTest(field=field):
                self.assertTrue(
                    (cluster.get(field) or "").strip(),
                    f"cluster is recorded as reachable but {field} is empty, which "
                    "makes the claim unverifiable",
                )
        self.assertGreaterEqual(len(cluster["observations"]), 1)

    def test_cluster_and_build_host_observations_stay_separate(self) -> None:
        """A workstation cache is not a cluster volume; the two must not blur."""
        cluster_text = " ".join(self.discovery["cluster"]["observations"]).lower()
        host_text = " ".join(self.discovery["build_host"]["observations"]).lower()
        self.assertNotIn(
            "local model cache on this build host",
            cluster_text,
            "a build-host observation leaked into the cluster section",
        )
        self.assertTrue(
            "build host" in host_text,
            "build-host observations must name the build host so they are never "
            "read as cluster state",
        )

    def test_registry_failure_is_scoped_not_generalised(self) -> None:
        """A host credential probe must not be reported as the cluster's verdict."""
        host_text = " ".join(self.discovery["build_host"]["observations"]).lower()
        if "403" not in host_text:
            self.skipTest("no registry failure recorded")
        self.assertIn(
            "build host",
            host_text,
            "a registry failure must be scoped to where it was observed",
        )
        unverified = " ".join(self.discovery.get("unverified", [])).lower()
        self.assertIn(
            "cluster",
            unverified,
            "if a registry probe failed on the build host, the manifest must say "
            "explicitly that in-cluster pull remains separately unverified",
        )

    def test_unverified_list_is_present_when_cluster_was_inspected(self) -> None:
        if not self.discovery["cluster"]["reachable"]:
            self.skipTest("cluster recorded as unreachable")
        self.assertTrue(
            self.discovery.get("unverified"),
            "a live inspection must record what it deliberately did not conclude",
        )


class HygieneTests(unittest.TestCase):
    """The manifest is committed to git, so it must never carry secrets."""

    def test_no_credentials_are_committed(self) -> None:
        raw = MANIFEST_PATH.read_text(encoding="utf-8")
        for marker in CREDENTIAL_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(
                    marker,
                    raw,
                    f"manifest appears to contain a credential ({marker!r}); "
                    "credentials and licensed weights must never be committed",
                )

    def test_retrieval_date_is_recorded(self) -> None:
        manifest = load_json(MANIFEST_PATH)
        self.assertRegex(manifest["retrieved_at"], r"^\d{4}-\d{2}-\d{2}$")

    def test_evidence_policy_disclaims_deployment(self) -> None:
        """This task is read-only; the artifact must not imply otherwise."""
        policy = load_json(MANIFEST_PATH)["evidence_policy"].lower()
        self.assertIn("read-only", policy)
        self.assertTrue(
            "not assert" in policy or "nothing in this manifest asserts" in policy,
            "the evidence policy must state that no deployment claim is made",
        )


if __name__ == "__main__":
    unittest.main()
