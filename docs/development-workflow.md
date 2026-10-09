# Scientific AI development workflow

`main` is the maintained trunk for the three personal Scientific AI repositories.
Temporary task branches are allowed. Finished work must not leave accumulating
branches: integrate useful changes, preserve unfinished experiments with an
explicit disposition, and retire completed local and personal-fork branches.
Keep branches used by active work or open PRs. Nebius-owned upstream repositories
are read-only for this consolidation.

## Start isolated work

Install local main-history guards once:

```sh
python3 scripts/single_main.py install
python3 scripts/single_main.py start /absolute/path/to/task-worktree
```

Task Deck defaults to detached worktrees from current main, so ordinary tasks
create no branch backlog. Old ticket branch fields are historical references.
A short-lived branch is also fine when useful: give it an owner and purpose,
keep it current with main, and record its closeout. Do not add a branch for every
review revision or use branches as an archive. Never edit main concurrently.

## Integrate and close

One integrator owns main at a time. Incorporate current main and deployed sibling
features, review conflicts and run appropriate checks. Commit in the isolated
task worktree, then save an external receipt naming the exact validated commit:

```json
{"commit": "<full SHA>", "checks": [{"command": "<actual check>", "exit_code": 0}]}
```

Publish through the serialized helper:

```sh
python3 scripts/single_main.py integrate /absolute/path/to/task-worktree --evidence /absolute/path/to/checks.json --push
```

It refuses dirty worktrees, stale validation and divergent main. When this task
uses a branch, successful integration retires that exact local/personal-fork
branch, preserves a closeout receipt and leaves the worktree files intact in
detached HEAD. An open PR, unavailable PR state or a moved remote branch is preserved for review. No unmerged work
is automatically deleted. Remove only a clean, finished worktree after its
session exits. A task is complete after publication and branch closeout, or an
explicit recoverable archive and follow-up disposition for unqualified work.

Run `python3 scripts/single_main.py check --remote` at closeout. Temporary
branches do not fail the audit. Already merged leftovers and counts above 20
require an ownership/cleanup review; creating new branches is not blocked.
Retain open PR branches until their reviews finish. Never force-push main.

## Preservation and deployment

Before retiring unique work, preserve verified Git bundles, branch-to-SHA maps,
staged/unstaged patches and untracked files outside the repositories. Record
integrated, superseded, rejected, or qualification-pending work distinctly;
archiving does not count as merging a feature. Restore into a detached worktree.

Git integration does not authorize a live rollout. Record deployed image/source
provenance and preserve clients, models, credentials, storage, Gateway routing
and monitoring. `forge.nebius.cloud` is intentionally unavailable; use
`https://89.169.99.188` for current checks and leave its DNS/redirect unchanged.

Local hooks protect main against deletion/history rewrites and restrict pushes
to the authorized personal fork. They allow temporary branches. GitHub rulesets
also protect main in the two public forks; the private website plan does not
support rulesets. Other machines must install the local hooks and follow this
closeout policy. Explicit user deployment instructions take precedence.
