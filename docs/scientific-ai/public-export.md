# Reusable source and operator history

The working repository retains historical benchmark, deployment and acceptance
records. Their exact bytes can be referenced by qualification hashes. They must
not be rewritten to make a portability check pass or presented as fresh tests of
a different runtime.

Run from the repository root:

```sh
python3 k8s-inference/public_export.py
python3 k8s-inference/public_export.py --output /tmp/inference-source-export
python3 -m unittest discover -s k8s-inference/tests -p test_public_export.py -v
```

The output directory must not already exist. This creates a source-staging tree
for Solutions Library integration, not a separately qualified release or a full
repository clone. Run repository tests in the original checkout: some historical
tests intentionally consume operator records that are not in the staging tree.

The selection includes the inference solution, its shared GPU/device/network
modules, root README and inference CI workflow. Training code is untouched.
`public-export-manifest.json` records the SHA-256 of every exported file (or the
link text for a symlink). It does not claim deployment or customer acceptance.

`public-export-history.json` is the reviewed, exact-file inventory of operator
records excluded from that staging tree. Every entry pins its bytes and gives a
reason. Originals remain in Git. Missing or changed inventory entries, new
private references and attempts to exclude application source/contracts fail
validation. Do not add broad directory exemptions or regenerate this inventory
uncritically when a check fails; either make reusable code portable or review the
specific historical record. The CI path filter includes inventory changes.

The portability scan distinguishes the intentional container account from a
developer checkout and registry image names from the obsolete source layout.
It is not a credential scanner, a legal/publication approval or an exhaustive
link/dependency audit. Historical links and qualification evidence need a separate
publication decision before an upstream release; the staging tree alone must not
be described as release-ready.
