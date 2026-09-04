from __future__ import annotations

import hashlib
import inspect
import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

from conftest import CONTROL_ROOT

from fs2_serve import cli
from fs2_serve.settings import Settings


def test_wheel_installs_control_plane_and_canonical_catalog_packages() -> None:
    project = tomllib.loads((CONTROL_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    readme = project["project"]["readme"]
    assert readme == "README.md"
    assert (CONTROL_ROOT / readme).is_file()
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/fs2_serve"]
    assert wheel["force-include"] == {
        "../../catalog/runtime/fs2_serve_catalog": "fs2_serve_catalog",
        "migrations": "fs2_serve/migrations",
    }


def test_container_imports_installed_packages_without_pythonpath_or_source_shadowing() -> None:
    dockerfile = (CONTROL_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert re.fullmatch(r"# syntax=docker/dockerfile:1\.12@sha256:[a-f0-9]{64}", dockerfile.splitlines()[0])
    assert "PYTHONPATH" not in dockerfile
    assert "import fs2_serve, fs2_serve_catalog" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "uv export --frozen --only-group build" in dockerfile
    assert "uv build --wheel" in dockerfile and "--build-constraints" in dockerfile and "--require-hashes" in dockerfile
    assert "uv pip install --python .venv/bin/python --no-deps --no-index" in dockerfile
    assert "uv sync --check --frozen --no-dev --no-install-project" in dockerfile
    assert "uv pip check --python .venv/bin/python" in dockerfile
    assert "RUN apk upgrade" not in dockerfile and "RUN apk add --no-cache --upgrade" not in dockerfile
    assert "pip install ." not in dockerfile
    for runtime_build_tool in (
        "/usr/local/bin/pip",
        "/usr/local/bin/pip3",
        "/usr/local/bin/pip3.13",
        "/usr/local/lib/python3.13/ensurepip",
        "/usr/local/lib/python3.13/site-packages/pip",
        "/usr/local/lib/python3.13/site-packages/pip-26.2.1.dist-info",
    ):
        assert runtime_build_tool in dockerfile
    assert "'libcrypto3=3.5.8-r0'" in dockerfile
    assert "'libssl3=3.5.8-r0'" in dockerfile
    assert "'sqlite-libs=3.53.4-r0'" in dockerfile
    assert "k8s-inference/components/control-plane/uv.lock" in dockerfile
    assert ".venv/bin/fs2-serve --help >/dev/null" in dockerfile
    assert "COPY k8s-inference/components/control-plane/contracts ./contracts" in dockerfile
    assert "COPY k8s-inference/catalog/runtime/catalog.json /workspace/runtime-catalog/catalog.json" in dockerfile
    assert "k8s-inference/catalog/runtime/pyproject.toml k8s-inference/catalog/runtime/uv.lock" in dockerfile
    assert "repo_root=Path('/workspace/runtime-catalog/packaged-repository')" in dockerfile
    assert "COPY --from=builder --chown=65532:65532 /workspace/runtime-catalog /opt/fs2/catalog" in dockerfile
    assert "load_catalog(Path('/workspace/runtime-catalog')" in dockerfile
    assert not (CONTROL_ROOT / ".dockerignore").exists()
    dockerignore_path = CONTROL_ROOT / f"{(CONTROL_ROOT / 'Dockerfile').name}.dockerignore"
    dockerignore = dockerignore_path.read_text(encoding="utf-8")
    assert dockerignore.startswith("#") and "\n**\n" in dockerignore
    assert "!k8s-inference/components/control-plane/uv.lock" in dockerignore
    assert "!k8s-inference/components/control-plane/src/**" in dockerignore
    assert "!k8s-inference/catalog/runtime/fs2_serve_catalog/**" in dockerignore
    assert "!k8s-inference/catalog/runtime/pyproject.toml" in dockerignore
    assert "!k8s-inference/catalog/runtime/uv.lock" in dockerignore
    assert "!k8s-inference/catalog/runtime/models/**" in dockerignore
    assert "!k8s-inference/catalog/runtime/kubernetes/**" in dockerignore
    assert "!k8s-inference/catalog/runtime/schema/**" in dockerignore
    assert "!k8s-inference/catalog/runtime/sql/**" in dockerignore
    assert "!k8s-inference/catalog/runtime/packaged-repository/**" in dockerignore
    assert "**/evidence/**" in dockerignore and "**/*secret*" in dockerignore
    assert "**/.env.*" in dockerignore and "**/*.pem" in dockerignore and "**/*.untracked" in dockerignore
    assert "FROM scratch AS context-audit\nCOPY . /" in dockerfile
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ") and "scratch" not in line]
    assert from_lines and all(re.search(r"@sha256:[a-f0-9]{64}(?: AS [a-z]+)?$", line) for line in from_lines)
    fixed_python_base = (
        "python:3.13.15-alpine3.23@sha256:7ea3f82de8ea6d4fb7e5d2bbe3fe3c9d931700b7a529f1fe5769e42abe514ca1"
    )
    assert dockerfile.count(f"FROM {fixed_python_base}") == 2
    assert 'org.opencontainers.image.base.name="docker.io/library/python:3.13.15-alpine3.23"' in dockerfile
    assert (
        'org.opencontainers.image.base.digest="sha256:7ea3f82de8ea6d4fb7e5d2bbe3fe3c9d931700b7a529f1fe5769e42abe514ca1"'
    ) in dockerfile
    for label in (
        "org.opencontainers.image.revision",
        "org.opencontainers.image.base.digest",
        "ai.nebius.fs2-serve.source-tree",
        "ai.nebius.fs2-serve.uv-lock-sha256",
        "ai.nebius.fs2-serve.dockerfile-sha256",
        "ai.nebius.fs2-serve.context-policy-sha256",
    ):
        assert label in dockerfile
    assert (CONTROL_ROOT / "contracts" / "federation-routes.schema.json").is_file()
    assert (CONTROL_ROOT / "contracts" / "postgresql-release-contract.json").is_file()
    assert Path(CONTROL_ROOT / "README.md").is_file()


def test_docker_engine_applies_the_dockerfile_specific_root_context_policy(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    assert docker is not None, "docker is required for the build-context gate"
    context = tmp_path / "repository"
    control = context / "k8s-inference" / "components" / "control-plane"
    catalog = context / "k8s-inference" / "catalog" / "runtime" / "fs2_serve_catalog"
    (control / "src").mkdir(parents=True)
    (control / "migrations").mkdir()
    (control / "contracts").mkdir()
    catalog.mkdir(parents=True)
    shutil.copy2(CONTROL_ROOT / "Dockerfile", control / "Dockerfile")
    shutil.copy2(CONTROL_ROOT / "Dockerfile.dockerignore", control / "Dockerfile.dockerignore")
    allowed = {
        control / "pyproject.toml": "[project]\nname='fixture'\nversion='0.0.0'\n",
        control / "uv.lock": "version = 1\n",
        control / "README.md": "fixture\n",
        control / "src" / "allowed.py": "ALLOWED = True\n",
        control / "migrations" / "0001.sql": "SELECT 1;\n",
        control / "contracts" / "contract.json": "{}\n",
        catalog / "__init__.py": "\n",
    }
    for path, value in allowed.items():
        path.write_text(value, encoding="utf-8")
    excluded = (
        context / "evidence" / "raw.json",
        control / "src" / ".env",
        control / "src" / "tenant-secret.txt",
        control / "src" / "private.pem",
        control / "src" / "rogue.untracked",
        control / "tests" / "test_private.py",
        context / "k8s-inference" / "components" / "private-helper" / "must-stay-out.yaml",
        context / "k8s-training" / "must-stay-out.tf",
    )
    for path in excluded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("MUST_NOT_ENTER_CONTEXT\n", encoding="utf-8")
    output = tmp_path / "filtered-context"
    subprocess.run(  # noqa: S603 - fixed local Docker command and test-owned paths.
        [
            docker,
            "buildx",
            "build",
            "--target",
            "context-audit",
            "--output",
            f"type=local,dest={output}",
            "--file",
            str(control / "Dockerfile"),
            str(context),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    actual = {path.relative_to(output) for path in output.rglob("*") if path.is_file()}
    expected = {path.relative_to(context) for path in allowed}
    assert actual == expected


def test_application_context_policies_reclose_solution_siblings() -> None:
    policies = (
        CONTROL_ROOT / "Dockerfile.dockerignore",
        CONTROL_ROOT.parent / "admin-console" / "Dockerfile.dockerignore",
    )
    for policy in policies:
        lines = policy.read_text(encoding="utf-8").splitlines()
        assert lines.index("!k8s-inference/") < lines.index("k8s-inference/*")
        assert lines.index("k8s-inference/*") < lines.index("!k8s-inference/components/")
        assert lines.index("!k8s-inference/components/") < lines.index("k8s-inference/components/*")


def test_exact_source_build_wrapper_archives_git_and_requires_context_and_label_provenance() -> None:
    source = (CONTROL_ROOT / "scripts" / "build_image.py").read_text(encoding="utf-8")
    assert '"git", "archive", "--format=tar"' in source
    assert '"ls-tree", "-r", "--name-only"' in source
    assert "actual != expected" in source
    assert '"--provenance=mode=max"' in source and 'f"type=sbom,generator={SBOM_GENERATOR}"' in source
    assert "docker/buildkit-syft-scanner@" in source
    assert '"type=oci,dest={args.oci_file}"' in source
    assert '"skopeo",' in source and '"oci-archive:{args.oci_file}"' in source
    assert "EXPECTED_ATTESTATIONS" in source and "_verify_attestations(args.oci_file)" in source
    assert "SUPPORTED_STATEMENT_TYPES" in source and "attestation_statement_types" in source
    assert "subject is not None" in source
    assert '"docker", "image", "inspect"' in source
    assert "local image provenance label mismatch" in source


def test_catalog_credential_contract_schemas_are_explicit_context_exceptions() -> None:
    policy = (CONTROL_ROOT / "Dockerfile.dockerignore").read_text()
    assert "**/*credential*" in policy and "**/*secret*" in policy
    for path in (
        "external-secrets-provider-build-eligibility-receipt.schema.json",
        "ngc-credential-materialization.schema.json",
    ):
        assert f"!k8s-inference/catalog/runtime/schema/{path}" in policy


def test_default_migration_path_resolves_the_source_tree_and_runtime_has_no_ddl() -> None:
    migration_dir = CONTROL_ROOT / "migrations"
    assert Settings.model_fields["migrations_dir"].default == migration_dir
    migration_names = sorted(path.name for path in migration_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    assert migration_names == [
        "0001_initial.sql",
        "0002_bound_operation_attempts.sql",
        "0003_bound_operation_identifiers.sql",
        "0004_index_queued_deadlines.sql",
        "0005_terminal_accounting.sql",
        "0006_activation_controller.sql",
        "0007_activation_controller_health.sql",
        "0008_activation_fencing_identity.sql",
        "0009_maintenance_least_privilege.sql",
        "0010_admin_access_accounting.sql",
        "0011_admin_configuration.sql",
        "0012_model_deployments.sql",
        "0013_durable_dynamic_dispatch.sql",
        "0014_scientific_artifact_results.sql",
        "0015_scientific_batch_controller.sql",
        "0016_scientific_batch_state_v7.sql",
        "0017_scientific_batch_state_v8.sql",
        "0018_workload_lifecycle_telemetry.sql",
        "0019_scientific_deployment_authorization.sql",
        "0020_scientific_atomic_admission.sql",
        "0021_scientific_admission_outbox_runtime_grant.sql",
    ]
    assert hashlib.sha256((migration_dir / "0005_terminal_accounting.sql").read_bytes()).hexdigest() == (
        "fedb6789a4839d42645c5ffb6905ce46525c213d81f15d9d987eacc109614197"
    )
    assert hashlib.sha256((migration_dir / "0006_activation_controller.sql").read_bytes()).hexdigest() == (
        "ac15d435e5fefb03da2780011e059736da803e5ded482414a5d1012ee265b022"
    )
    assert hashlib.sha256((migration_dir / "0007_activation_controller_health.sql").read_bytes()).hexdigest() == (
        "b60a2976b366acc55c652f2e1cbdc4a06f2a38933930aa8d2805b48e063e150d"
    )
    assert hashlib.sha256((migration_dir / "0008_activation_fencing_identity.sql").read_bytes()).hexdigest() == (
        "b7ea9fe6497df00fa4a0d4c6bfb2b01dfa3e699f7b4392979b1d24f13a8fcd3d"
    )
    assert hashlib.sha256((migration_dir / "0009_maintenance_least_privilege.sql").read_bytes()).hexdigest() == (
        "3bf0342f7b9ef8b5dc1e88aeda985d6d8856f5cf111b82aded3d0c56e44d0c23"
    )
    assert hashlib.sha256((migration_dir / "0010_admin_access_accounting.sql").read_bytes()).hexdigest() == (
        "113d7ff18906fd7af94f14e8751c6d9480eba25b711440b528ff7dde9157c5e5"
    )
    assert hashlib.sha256((migration_dir / "0011_admin_configuration.sql").read_bytes()).hexdigest() == (
        "fa8ab57dcf32bba741c149352e796cb261341df535d0b972af432792bbd8da43"
    )
    assert hashlib.sha256((migration_dir / "0012_model_deployments.sql").read_bytes()).hexdigest() == (
        "bf4dfbff463a88f3be1cc04e452900d4eff9c18024161069d7beb281229f3eef"
    )
    assert hashlib.sha256((migration_dir / "0013_durable_dynamic_dispatch.sql").read_bytes()).hexdigest() == (
        "4daf1a47abd864c04f30dc48149a0c74b46aac1332c12ef40df518b2dea8b9ad"
    )
    assert hashlib.sha256((migration_dir / "0014_scientific_artifact_results.sql").read_bytes()).hexdigest() == (
        "97be6f57c64944418fa9719a58bacd8040402d21fa874f7af3a773c02b68b675"
    )
    assert hashlib.sha256((migration_dir / "0015_scientific_batch_controller.sql").read_bytes()).hexdigest() == (
        "48b41100f3c9b25595ee6e9835ad4d44d964161bf05fa3227d6697bf7f085578"
    )
    assert hashlib.sha256((migration_dir / "0016_scientific_batch_state_v7.sql").read_bytes()).hexdigest() == (
        "fa25cc0b11f388e91e1eff7778da882dd5e2df8e8dac8392db7342c82d625207"
    )
    assert hashlib.sha256((migration_dir / "0017_scientific_batch_state_v8.sql").read_bytes()).hexdigest() == (
        "402af8eb08057ae564798af3880c14a62c5f13873787f2aa25745990eaa9b5aa"
    )
    dockerfile = (CONTROL_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.count("WORKDIR /workspace/k8s-inference/components/control-plane") == 2
    assert "COPY k8s-inference/components/control-plane/migrations ./migrations" in dockerfile
    assert "Settings.model_fields['migrations_dir'].default" in dockerfile
    assert "migration_dir.glob('[0-9][0-9][0-9][0-9]_*.sql'))) == 21" in dockerfile
    assert "store.migrate" not in inspect.getsource(cli.build_runtime)
    assert "store.migrate" not in inspect.getsource(cli.maintain)
    assert "PostgresStore.migrate_database" in inspect.getsource(cli.migrate)
    assert "PostgresStore.wait_for_schema" in inspect.getsource(cli.wait_schema)


def test_isolated_uv_run_installs_the_cli_and_catalog_package() -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the isolated CLI packaging gate"
    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONPATH", None)
    clean_environment["PYTHONNOUSERSITE"] = "1"
    completed = subprocess.run(  # noqa: S603 - executable and arguments are locally resolved, never user input.
        [uv, "run", "--isolated", "--no-editable", "--no-dev", "--frozen", "fs2-serve", "--help"],
        cwd=CONTROL_ROOT,
        env=clean_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    # uv may reuse an immutable same-version wheel in its isolated cache; the
    # clean-wheel gate below proves the new command from a freshly built wheel.
    assert "{serve," in completed.stdout
    assert '"activation-controller"' not in inspect.getsource(cli.main)
    assert '"wait-schema"' in inspect.getsource(cli.main)
    assert '"postgresql-release-contract"' in inspect.getsource(cli.main)


def test_clean_wheel_imports_catalog_without_repository_pythonpath(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the clean-wheel packaging gate"
    dist = tmp_path / "dist"
    subprocess.run(  # noqa: S603 - executable and arguments are locally resolved, never user input.
        [uv, "build", "--wheel", "--out-dir", str(dist)],
        cwd=CONTROL_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    wheels = list(dist.glob("fs2_serve_control_plane-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel_archive:
        names = set(wheel_archive.namelist())
        assert "fs2_serve/__init__.py" in names
        assert "fs2_serve_catalog/__init__.py" in names
        assert "fs2_serve/activation_controller.py" not in names
        assert "fs2_serve/kubernetes_activation.py" not in names
        migration_names = sorted(
            name for name in names if name.startswith("fs2_serve/migrations/") and name.endswith(".sql")
        )
        assert migration_names == [
            "fs2_serve/migrations/0001_initial.sql",
            "fs2_serve/migrations/0002_bound_operation_attempts.sql",
            "fs2_serve/migrations/0003_bound_operation_identifiers.sql",
            "fs2_serve/migrations/0004_index_queued_deadlines.sql",
            "fs2_serve/migrations/0005_terminal_accounting.sql",
            "fs2_serve/migrations/0006_activation_controller.sql",
            "fs2_serve/migrations/0007_activation_controller_health.sql",
            "fs2_serve/migrations/0008_activation_fencing_identity.sql",
            "fs2_serve/migrations/0009_maintenance_least_privilege.sql",
            "fs2_serve/migrations/0010_admin_access_accounting.sql",
            "fs2_serve/migrations/0011_admin_configuration.sql",
            "fs2_serve/migrations/0012_model_deployments.sql",
            "fs2_serve/migrations/0013_durable_dynamic_dispatch.sql",
            "fs2_serve/migrations/0014_scientific_artifact_results.sql",
            "fs2_serve/migrations/0015_scientific_batch_controller.sql",
            "fs2_serve/migrations/0016_scientific_batch_state_v7.sql",
            "fs2_serve/migrations/0017_scientific_batch_state_v8.sql",
            "fs2_serve/migrations/0018_workload_lifecycle_telemetry.sql",
            "fs2_serve/migrations/0019_scientific_deployment_authorization.sql",
            "fs2_serve/migrations/0020_scientific_atomic_admission.sql",
            "fs2_serve/migrations/0021_scientific_admission_outbox_runtime_grant.sql",
        ]
        entry_point_files = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        assert len(entry_point_files) == 1
        entry_points = wheel_archive.read(entry_point_files[0]).decode()
        assert "fs2-serve = fs2_serve.cli:main" in entry_points
    environment = tmp_path / "environment"
    subprocess.run(  # noqa: S603 - executable and arguments are locally resolved, never user input.
        [uv, "venv", "--python", sys.executable, str(environment)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    python = environment / "bin" / "python"
    subprocess.run(  # noqa: S603 - executable and wheel path are generated by this test.
        [uv, "pip", "install", "--python", str(python), str(wheels[0])],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONPATH", None)
    clean_environment["PYTHONNOUSERSITE"] = "1"
    cli = environment / "bin" / "fs2-serve"
    completed = subprocess.run(  # noqa: S603 - executable is installed from the wheel built above.
        [str(cli), "--help"],
        cwd=tmp_path,
        env=clean_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        "{serve,maintenance,migrate,wait-schema,bootstrap-access,validate,postgresql-release-contract,"
        "model-controller,scientific-materialize,scientific-collect,scientific-prepare-workspace,"
        "scientific-verify-runtime-artifacts}" in completed.stdout
    )
    emitted_contract = subprocess.run(  # noqa: S603 - clean-wheel CLI and fixed command.
        [str(cli), "postgresql-release-contract"],
        cwd=tmp_path,
        env=clean_environment,
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    assert emitted_contract == (CONTROL_ROOT / "contracts" / "postgresql-release-contract.json").read_bytes()
    subprocess.run(  # noqa: S603 - interpreter and code are fixed by this test.
        [
            str(python),
            "-I",
            "-c",
            (
                "import pathlib,sys,fs2_serve,fs2_serve_catalog;"
                "from fs2_serve.registry import Registry;"
                "from fs2_serve.settings import Settings;"
                "from fs2_serve_catalog.consumer import load_gateway_catalog;"
                "root=pathlib.Path(sys.prefix).resolve();"
                "assert pathlib.Path(fs2_serve.__file__).resolve().is_relative_to(root);"
                "assert pathlib.Path(fs2_serve_catalog.__file__).resolve().is_relative_to(root);"
                "migration_dir=Settings.model_fields['migrations_dir'].default;"
                "assert migration_dir.parent == pathlib.Path(fs2_serve.__file__).resolve().parent;"
                "assert len(list(migration_dir.glob('[0-9][0-9][0-9][0-9]_*.sql'))) == 21;"
                "assert Registry and load_gateway_catalog"
            ),
        ],
        cwd=tmp_path,
        env=clean_environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
