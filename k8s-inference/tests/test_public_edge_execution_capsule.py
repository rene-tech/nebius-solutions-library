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
    assert "FS2_EXPECTED_BOOTSTRAP_SHA256" in source
    assert "FS2_EXPECTED_PYTHON_SHA256" in source
    assert "require_fd_digest(bootstrap_fd" in source
    assert "require_fd_digest(python_fd" in source
    assert '#include "fs2-frozen-runtime.h"' in source
    assert "require_static_frozen_python(python_fd)" in source
    assert 'child[output++] = "-S"' in source
    assert "sys.path[:]=[]" in source


def test_bootstrap_manifest_binds_release_launcher_tools_and_providers() -> None:
    source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    for required in (
        '"accepted_commit"',
        '"accepted_tree"',
        '"launcher_sha256"',
        '"bootstrap_sha256"',
        '"installer_sha256"',
        '"nebius_auth"',
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


def test_operator_auth_and_secrets_are_brokered_not_ambient() -> None:
    bootstrap = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    launcher = (
        ROOT / "stages/foundation/scripts/public-edge-capsule-launcher.c"
    ).read_text(encoding="utf-8")
    stack = (ROOT / "inference-stack").read_text(encoding="utf-8")
    infrastructure_provider = (
        ROOT / "stages/infrastructure/versions.tf"
    ).read_text(encoding="utf-8")
    workloads_provider = (
        ROOT / "stages/workloads/providers.tf"
    ).read_text(encoding="utf-8")
    assert "public-edge-nebius-auth.sock" in bootstrap
    assert "SO_PEERCRED" in bootstrap
    assert '"caller_uid"' in bootstrap
    assert '"caller_real_gid"' in bootstrap
    assert '"caller_effective_gid"' in bootstrap
    assert '"peer_observed_caller_gid"' in bootstrap
    assert '"operator_identity"' in bootstrap
    assert 'profile_record["operators"]' in bootstrap
    assert 'profile_record["subject_id"]' in bootstrap
    assert 'profile_record["project_id"]' in bootstrap
    assert 'profile_record["tenant_id"]' in bootstrap
    assert 'record["broker_executable_sha256"]' in bootstrap
    assert 'record["broker_config_sha256"]' in bootstrap
    assert 'record["peer_runtime_review_sha256"]' in bootstrap
    assert "token_sha256" in bootstrap
    assert "minimum_remaining_seconds" in bootstrap
    assert "PT_DYNAMIC or PT_INTERP" in bootstrap
    assert "trusted-public-edge-auth-broker-authorities.json" in bootstrap
    assert "FS2_CAPSULE_SECRET_BROKER_FD" in launcher
    assert "operator_secret_slots" in launcher
    for forbidden in ("AWS_", "GITHUB_", "OPENAI_", "NEBIUS_"):
        assert forbidden in launcher
    assert "operator_secret(" in stack
    assert 'result["NEBIUS_IAM_TOKEN"] = brokered_nebius_token()' in stack
    assert "refresh_brokered_auth" in stack
    assert "MAXIMUM_MUTATION_SECONDS = 90 * 60" in stack
    assert "terraform-mutation-started/v2" in stack
    assert "mutation-ledger-receipt/v2" in stack
    assert "record_mutation_started(" in stack
    assert stack.index("record_mutation_started(", stack.index("def apply_plan(")) < stack.index(
        "subprocess.Popen(", stack.index("def apply_plan(")
    )
    assert '_request_mutation_ledger_event(\n        action="started"' in stack
    assert 'MUTATION_SETTLEMENT_ROOT.glob("*.started.json")' in stack
    assert 'f"{record[\'attempt_id\']}.terminal.json"' in stack
    assert "external mutation terminal does not close its exact intent" in stack
    assert "fcntl.flock(run_root_descriptor, fcntl.LOCK_EX)" in stack
    assert "terraform-mutation-reconciliation-evidence/v2" in stack
    assert "public-edge-mutation-settlements" in stack
    assert '"apply", "-input=false", "-lock=true", str(refresh_plan)' in stack
    assert 'or (stage == "infrastructure" and not operations)' in stack
    assert "include_secrets=True" in stack
    assert '!= "fs2-serve.nebius.ai/short-lived-nebius-auth/v3"' in stack
    assert "token = chomp(file(var.nebius_iam_token_file))" in infrastructure_provider
    assert "token = chomp(file(var.nebius_iam_token_file))" in workloads_provider
    assert "profile = {" not in infrastructure_provider
    assert "profile = {" not in workloads_provider


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
        "debug-proxy",
        "debug-view",
        "debug-export",
        "activate-debug",
        "disable-debug",
    ):
        assert f'"{command}"' in source[source.index("CAPSULE_COMMANDS") : source.index("class DeploymentError")]
    for name in ("terraform", "kubectl", "nebius", "crane"):
        assert f'args.{name} = CAPSULE_TOOL_PATHS["{name}"]' in main


def test_authenticated_local_debugging_does_not_require_cloud_broker() -> None:
    bootstrap = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    stack = (ROOT / "inference-stack").read_text(encoding="utf-8")
    assert '"activate-debug",' in bootstrap
    assert '"disable-debug",' in bootstrap
    assert '"debug-view",' in bootstrap
    assert '"debug-export",' in bootstrap
    assert '"local-read-only" if read_only_operator else "brokered-cloud"' in bootstrap
    assert 'if token_fd >= 0:' in bootstrap
    assert '"debug-proxy",\n            "debug-view",\n            "debug-export",\n            "activate-debug",' in stack
    assert '"disable-debug",' in stack
    assert '"debug-view",' in stack
    assert '"debug-export",' in stack
    assert 'CAPSULE_ACCESS_MODE != "local-read-only"' in stack
    assert '"nebius_token" in CAPSULE_TOOL_PATHS' in stack
    assert 'if not local_read_only:\n            require_brokered_target' in stack
    assert "brokered_proxy_session(" in stack
    assert '"host_listeners") != []' in stack
    assert '"kubeconfig_custody")\n            != "root-broker-only"' in stack
    assert "proxy credentials remain in the root broker" in stack


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
    assert "fs2-public-edge-installer-openssl-static" in source
    assert "PT_DYNAMIC or PT_INTERP" in source
    assert "manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()" in source
    assert 'return descriptor, f"/proc/self/fd/{descriptor}"' in source
    assert "pass_fds=(openssl_fd,)" in source
    assert "installation receipt signature is invalid" in source
    assert "fs2-public-edge-installer-openssl-static" in source
    assert "PT_DYNAMIC or PT_INTERP" in source
    assert "bundle file set differs" in source


def test_privileged_installer_pins_inputs_and_atomically_preserves_prior_unit() -> None:
    source = (
        ROOT / "stages/foundation/scripts/install-public-edge-capsule.py"
    ).read_text(encoding="utf-8")
    assert 'FIXED_INSTALLER = Path("/usr/local/sbin/fs2-install-public-edge-capsule")' in source
    assert 'FIXED_INSTALLER_SOURCE = Path("/usr/local/libexec/fs2-public-edge-installer.py")' in source
    assert "reject_symlink_argument" in source
    assert 'getattr(os, "O_NOFOLLOW", 0)' in source
    assert "enumerate_bundle(bundle_fd)" in source
    assert "bundle_files" in source
    assert "os.O_EXCL" in source
    assert "installer_sha256" in source
    assert "installation receipt signature is invalid" in source
    assert "RENAME_EXCHANGE" in source
    assert "RENAME_NOREPLACE" in source
    assert "fs2-public-edge-previous-" in source
    assert "RELEASE_ATTEMPT_ROOT" in source
    assert "ACTIVATION_ATTEMPT_ROOT" in source
    assert "public-edge-release-completion/v1" in source
    assert "public-edge-activation-completion/v1" in source
    assert "verify_release_tree(destination, manifest, gid)" in source
    assert "verify_activation_unit(" in source
    assert "shutil.rmtree" not in source
    assert "os.unlink" not in source
    assert "os.remove" not in source
    assert "Path.unlink" not in source
    assert "fsync_tree_directories(payload)" in source
    assert "fsync_directory(RELEASE_ROOT)" in source
    assert "fsync_directory(ACTIVATION_ROOT)" in source
    assert "activation-exchange-prepared/v1" in source
    assert "recover_activation_transitions(gid)" in source


def test_privileged_installer_has_static_pre_python_gate() -> None:
    source = (
        ROOT / "stages/foundation/scripts/public-edge-capsule-installer-launcher.c"
    ).read_text(encoding="utf-8")
    assert "FS2_EXPECTED_INSTALLER_SOURCE_SHA256" in source
    assert "FS2_EXPECTED_INSTALLER_PYTHON_SHA256" in source
    assert '#include "fs2-frozen-runtime.h"' in source
    assert "/usr/local/libexec/fs2-public-edge-installer.py" in source
    assert "require_static_python(python)" in source
    assert 'open("/proc/self/exe", O_RDONLY)' in source
    assert 'open("/proc/self/exe", O_RDONLY | O_NOFOLLOW)' not in source
    assert "sys.path[:]=[]" in source
    assert 'child[output++] = "-S"' in source
    launcher = (
        ROOT / "stages/foundation/scripts/public-edge-capsule-launcher.c"
    ).read_text(encoding="utf-8")
    assert '#include "fs2-frozen-runtime.h"' in launcher
    assert "require_static_frozen_python(python_fd)" in launcher
    assert "FS2_FROZEN_STDLIB_SHA256" not in launcher


def test_frozen_python_contract_binds_static_pie_file_bytes_to_runtime_bytes() -> None:
    runtime = (
        ROOT / "stages/foundation/scripts/fs2-frozen-runtime.h"
    ).read_text(encoding="utf-8")
    offline = (
        ROOT / "stages/foundation/scripts/verify-public-edge-frozen-runtime.py"
    ).read_text(encoding="utf-8")
    for source in (runtime, offline):
        assert "ET_DYN" in source or "elf_type != 3" in source
        assert "PT_DYNAMIC" in source
        assert "PT_INTERP" in source
        assert "DT_NEEDED" in source or "static-PIE runtime declares an external" in source
        assert "R_X86_64_RELATIVE" in source or "relocation_type != 8" in source
        assert "R_X86_64_IRELATIVE" in source or "relocation_type != 37" in source
        assert "p_filesz" in source or "program[5]" in source
        assert "sh_offset" in source or "section[4]" in source
        assert "sh_addr" in source or "section[3]" in source
        assert "PT_GNU_RELRO" in source or "0x6474E552" in source
        assert "PyImport_FrozenModules" in source
        assert "FS2ATT1" in source
    assert "section->sh_offset - program.p_offset" in runtime
    assert "section->sh_addr - program.p_vaddr" in runtime
    assert "fs2_require_disjoint_sections" in runtime
    assert "require_disjoint(mapping_sections)" in offline
    assert "fs2_require_authority_storage_disjoint" in runtime
    assert "require_authority_storage_disjoint" in offline
    assert "authority_storage[5]" in runtime
    assert "[*pointer_authorities, builtin_authority]" in offline


def test_debug_lifecycle_and_broker_handshakes_are_bounded_and_serialized() -> None:
    source = (ROOT / "inference-stack").read_text(encoding="utf-8")
    assert "def debug_lifecycle_lock" in source
    assert "os.O_RDONLY\n        | os.O_DIRECTORY" in source
    assert 'stat.S_IMODE(details.st_mode) != 0o700' in source
    assert "fcntl.LOCK_EX | fcntl.LOCK_NB" in source
    assert 'with debug_lifecycle_lock(run_root):\n                install_debug_activation' in source
    assert 'with debug_lifecycle_lock(run_root):\n                disable_debug_activation' in source
    assert "disable_deadline = time.monotonic()" in source
    assert 'bound_disable_io("connect", 5.0)' in source
    assert 'bound_disable_io("send", 5.0)' in source
    assert 'bound_disable_io("receive", 20.0)' in source
    assert "handshake_deadline = time.monotonic()" in source
    assert 'bound_handshake_io("connect", 5.0)' in source
    assert 'bound_handshake_io("send", 5.0)' in source
    assert 'bound_handshake_io("receive", 20.0)' in source
    assert "_debug_activation_history(run_root, allow_absent=True)" in source
    assert '"schema": "fs2-serve.nebius.ai/internal-debug-cli-read/v1"' in source
    assert '"fs2-serve.nebius.ai/internal-debug-cli-result/v1"' in source
    assert '"audit_event_sha256"' in source


def test_cloud_token_descriptor_is_scoped_to_authenticated_children() -> None:
    source = (ROOT / "inference-stack").read_text(encoding="utf-8")
    assert "{auth_refresh_fd, secret_broker_fd, token_fd}" in source
    assert "def brokered_auth_pass_fds(" in source
    assert '"NEBIUS_IAM_TOKEN" in environment' in source
    assert "*brokered_auth_pass_fds(environment)" in source
    assert "*brokered_auth_pass_fds(refreshed_environment)" in source


def test_reconciliation_requires_external_signed_settlement_and_acceptance() -> None:
    source = (ROOT / "inference-stack").read_text(encoding="utf-8")
    assert 'MUTATION_SETTLEMENT_ROOT = Path("/var/lib/fs2/public-edge-mutation-settlements")' in source
    assert 'f"{receipt_sha256}.settlement.json"' in source
    assert 'f"{receipt_sha256}.reconciled.json"' in source
    assert 'f"mutation-reconciliation-evidence-{receipt_sha256}.json"' in source
    assert '"-keyform",\n                "DER"' in source
    assert 'run_root.glob("mutation-reconciled-*.json")' not in source
    assert '"outstanding_operation_ids"] != []' in source
    assert 'no_drift.returncode != 0' in source
    assert "refreshed_state_sha256" in source
    assert '"provider_settlement_sha256", "schema", "stage"' in source
    assert "payload[key] != settlement.get(key) for key in provider_binding_keys" in source
    assert 'provider.get("class") not in {"terraform-local", "nebius", "kubernetes-helm"}' in source
    assert 'MUTATION_LEDGER_SOCKET = Path("/run/fs2/public-edge-mutation-ledger.sock")' in source
    assert "socket.SO_PEERCRED" in source
    assert 'payload["conflicting_active_count"] != 0' in source
    assert 'persisted != payload' in source


def test_installer_durably_publishes_candidate_before_prepared_journal() -> None:
    source = (
        ROOT / "stages/foundation/scripts/install-public-edge-capsule.py"
    ).read_text(encoding="utf-8")
    candidate_fsync = source.index(
        "# The candidate directory entry must be durable before a durable journal"
    )
    prepared_journal = source.index(
        'attempt / "activation-exchange-prepared.json"', candidate_fsync
    )
    assert source.index("fsync_directory(CURRENT_LINK.parent)", candidate_fsync) < prepared_journal
    assert 'disposition = "candidate-absent-before-exchange"' in source
    assert "os.readlink(CURRENT_LINK) if CURRENT_LINK.is_symlink() else None" in source


def test_every_stage_test_supplies_mock_only_token_descriptor() -> None:
    stage_tests = sorted((ROOT / "stages/infrastructure/tests").glob("*.tftest.hcl"))
    stage_tests += sorted((ROOT / "stages/workloads/tests").glob("*.tftest.hcl"))
    assert len(stage_tests) == 10
    for path in stage_tests:
        assert 'nebius_iam_token_file = "/proc/self/fd/0"' in path.read_text(
            encoding="utf-8"
        )


def test_launcher_opens_one_atomically_selected_activation_unit() -> None:
    source = (
        ROOT / "stages/foundation/scripts/public-edge-capsule-launcher.c"
    ).read_text(encoding="utf-8")
    assert "/usr/local/libexec/fs2-public-edge-current/launcher" in source
    assert "/usr/local/libexec/fs2-public-edge-activations" in source
    assert 'open_protected_at(activation_directory, "bootstrap.py"' in source
    assert 'open_protected_at(activation_directory, "manifest.json"' in source
    assert 'open_protected_at(activation_directory, "python3"' in source
    assert "fixed activation does not select this exact launcher" in source
