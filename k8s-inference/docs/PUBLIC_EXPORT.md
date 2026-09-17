# Provenance-preserving public export

The canonical repository retains immutable acceptance evidence even when a
record contains a private checkout path, a historical private-source name, or
an opaque provider resource identifier. Public publication must use
`public_export.py`; copying the canonical tree directly is not an approved
export path.

The exporter reads blobs directly from a named Git commit, applies the bounded
redaction rules in memory, and creates a new destination that must not already
exist and must be outside the source repository. It never edits or deletes the
canonical evidence. `PUBLIC_EXPORT_PROVENANCE.json` records the source commit,
source and exported SHA-256 for every file, file mode, projected path, and the
name/count of each redaction rule. It never records the matched private value.

Example for a separately reviewed destination:

```bash
python3 k8s-inference/public_export.py /NEW/EMPTY/PUBLIC_EXPORT \
  --repository-root /PATH/TO/nebius-solutions-library \
  --source-ref EXACT_REVIEWED_COMMIT
```

The source ref must resolve to a commit. Publication automation must bind the
resulting manifest and exact commit, run the public-export regression against
the projection, and reject any remaining forbidden reference. Do not use the
tool to rewrite the working tree, conceal failed evidence, or substitute an
unreviewed commit.
