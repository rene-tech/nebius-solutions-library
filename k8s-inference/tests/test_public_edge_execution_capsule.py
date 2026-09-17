from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = (
    ROOT
    / "stages/foundation/scripts/public-edge-capsule-bootstrap.py"
)
SPEC = importlib.util.spec_from_file_location("public_edge_capsule", BOOTSTRAP_PATH)
assert SPEC is not None and SPEC.loader is not None
CAPSULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPSULE)


def test_launcher_has_no_caller_source_path_or_digest_contract() -> None:
    source = (
        ROOT
        / "stages/foundation/scripts/public-edge-capsule-launcher.c"
    ).read_text(encoding="utf-8")
    assert '"inference-stack"' in source
    assert '"public-edge-verifier"' in source
    assert '"edge-client-identity-verifier"' in source
    assert '"jobset-chart-materializer"' in source
    assert "expected absolute source path, digest" not in source
    assert "argv[1][0] != '/'" not in source
    assert "fs2-public-edge-capsule" in source
    assert "getresgid" in source
    assert "getgroups" in source
    assert "02755" in source


def test_bootstrap_manifest_binds_release_launcher_tools_and_providers() -> None:
    source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    for required in (
        '"accepted_commit"',
        '"accepted_tree"',
        '"launcher_sha256"',
        '"bootstrap_sha256"',
        '"release_files"',
        '"provider_files"',
        '"provider_mirror_relative_path"',
        '"provider_overrides"',
        "FS2_CAPSULE_PASS_FDS",
        'os.environ["PATH"] = paths["tool_bin"]',
    ):
        assert required in source
    assert "observed_files" in source
    assert "accepted release file set differs" in source
    assert "SOURCE_PATHS" in source
    assert "is not mapped to its canonical release path" in source
    assert "direct" in source and "network_mirror" in source


def test_apply_reexecs_before_argument_parsing_or_terraform_probe() -> None:
    source = (ROOT / "inference-stack").read_text(encoding="utf-8")
    main = source[source.index("def main(") :]
    assert main.index("enter_accepted_capsule(arguments)") < main.index(
        "args = parse_args(arguments)"
    )
    assert main.index("load_capsule_contract()") < main.index(
        "require_terraform_version(args.terraform)"
    )
    protected_prefix = main[: main.index("require_terraform_version(args.terraform)")]
    assert 'if args.command == "apply":' not in protected_prefix
    for command in (
        "validate",
        "preflight",
        "plan",
        "apply",
        "destroy",
        "status",
        "output",
        "proxy",
    ):
        assert f'"{command}"' in source[source.index("CAPSULE_COMMANDS") : source.index("class DeploymentError")]
    for name in ("terraform", "kubectl", "nebius", "crane"):
        assert f'args.{name} = CAPSULE_TOOL_PATHS["{name}"]' in main


def test_terraform_helpers_use_only_finite_capsule_entrypoints() -> None:
    module = (ROOT / "modules/jobset-controller/main.tf").read_text(encoding="utf-8")
    foundation = (ROOT / "stages/foundation/releases.tf").read_text(encoding="utf-8")
    edge = (ROOT / "stages/workloads/edge_client_identity.tf").read_text(
        encoding="utf-8"
    )
    assert 'program = ["${path.module}/scripts/materialize-chart.sh"]' not in module
    assert 'command = "\\"${path.module}/scripts/' not in module
    for source in (
        "jobset-chart-materializer",
        "jobset-crd-upgrade",
        "jobset-release-verifier",
        "jobset-api-gate",
    ):
        assert source in module
    assert "jobset-chart-materializer" in foundation
    assert '"python3"' not in edge.split('data "external" "edge_client_identity_receipt"', 1)[1].split("}", 1)[0]
    assert "edge-client-identity-verifier" in edge


def test_direct_environment_markers_cannot_replace_setgid_proof(monkeypatch) -> None:
    monkeypatch.setenv("FS2_CAPSULE_LAUNCHER", "fs2-public-edge-capsule-v1")
    monkeypatch.setattr(CAPSULE.os, "getgid", lambda: 1000)
    monkeypatch.setattr(CAPSULE.os, "getegid", lambda: 1000)
    monkeypatch.setattr(CAPSULE.os, "getgroups", lambda: [])
    with pytest.raises(CAPSULE.CapsuleError, match="setgid launcher proof"):
        CAPSULE.require_capsule_process()


def test_install_verifier_uses_only_fixed_root_acceptance_authorities() -> None:
    source = (
        ROOT
        / "stages/foundation/scripts/verify-public-edge-capsule-install.py"
    ).read_text(encoding="utf-8")
    assert '/etc/fs2/public-edge-capsule-issuers.json' in source
    assert '/etc/fs2/public-edge-capsule-acceptance.json' in source
    assert 'parser.add_argument("--trust-store"' not in source
    assert 'parser.add_argument("--expected-trust-store-sha256"' not in source
    assert "differs from the root acceptance policy" in source
    assert '"openssl_sha256"' in source
    assert "manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()" in source
    assert 'return descriptor, f"/proc/self/fd/{descriptor}"' in source
    assert "pass_fds=(openssl_fd,)" in source
    assert "installation receipt signature is invalid" in source
    assert "bundle file set differs" in source
