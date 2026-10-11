# Scientific AI repository rules

- `main` is the only maintained branch. Default to detached task worktrees. Temporary branches are allowed; retire completed branches after integration and preserve unfinished work with explicit follow-up.
- Follow [docs/development-workflow.md](docs/development-workflow.md), including serialized integration, validation and closeout.
- Preserve deployed sibling work, dirty files, evidence and existing clients. Git consolidation does not authorize a live rollout.
- Push only to the configured personal fork. Nebius-owned upstreams are read-only for this consolidation.
- Keep chat summaries within 120 words and progress updates within two short sentences.

## Active Serverless 2 migration exception

Owner instruction, 11 October 2026: keep unfinished Scientific AI migration work
on `scientific-ai-serverless2-migration`, not `main`. This branch is active work,
not a completed branch to retire. Continue in its existing worktree. Do not
integrate it automatically at turn closeout or deploy it to the customer cluster.
Use only the shared test fleet. Follow `k8s-inference/docs/SERVERLESS2-MIGRATION.md`
for integration gates and the eventual explicit release/cutover decision.
