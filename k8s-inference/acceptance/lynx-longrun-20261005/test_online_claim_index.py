import ast

from online_claim_index import IMAGE, NAME, manifest


def test_online_index_job_reuses_existing_owner_and_never_records_schema_or_updates_jobs():
    job = manifest()
    assert job["metadata"]["name"] == NAME
    spec = job["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "fs2-serve-control-plane-migration"
    assert spec["automountServiceAccountToken"] is False
    container = spec["containers"][0]
    assert container["image"] == IMAGE and container["command"] == ["python", "-c"]
    assert (
        container["env"][0]["valueFrom"]["secretKeyRef"]["name"]
        == "fs2-serve-database-migrations"
    )
    program = container["args"][0]
    tree = ast.parse(program)
    compile(tree, "online-index", "exec")
    literal_sql = next(
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and node.targets[0].id == "INDEX_SQL"
    )
    assert (
        "CREATE INDEX CONCURRENTLY fs2_scientific_batches_pending_claim_idx"
        in literal_sql
    )
    assert "IF NOT EXISTS" not in literal_sql.split("CREATE INDEX", 1)[-1]
    assert "DROP INDEX" not in program
    assert "INSERT INTO fs2_schema_migrations" not in program
    assert "UPDATE fs2_scientific_batches" not in program
    assert '"EXPLAIN (FORMAT JSON) "' in program
    assert "EXPLAIN (ANALYZE" not in program
    assert "len(before) != 37" in program
