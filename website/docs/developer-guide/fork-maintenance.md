---
title: "Fork Maintenance"
description: "How this fork tracks upstream Hermes safely and predictably"
---

# Fork Maintenance

This fork uses a deterministic maintenance workflow. The updater is responsible for merging upstream, validating the result, and promoting the fork only after the trust gate passes.

## Branch and remote model

| Role | Name |
|---|---|
| upstream remote | `origin` |
| writable fork remote | `fork` |
| live integration branch | `codex/routing-integration` |
| promoted branch | `fork/main` |
| retained merge/repair branches | `codex/upstream-sync-*` |

`fork/main` is the promoted output. `codex/routing-integration` is the long-lived integration branch that carries the fork-specific architecture.

## Canonical update flow

Use:

```bash
hermes-dev routing update run
```

The updater does the following:

1. verifies the repo topology and git auth backend
2. creates or refreshes a retained update worktree
3. merges `origin/main` into a retained `codex/upstream-sync-*` branch
4. runs the trust gate
5. promotes the result into `fork/codex/routing-integration`
6. promotes the same validated head into `fork/main`
7. records status and repair artifacts under `~/.hermes/profiles/dev/cron/output/routing-auto-update/`

## Trust gate

The authoritative trust gate for this fork is:

```bash
python -m pytest -o addopts= tests/ -q --ignore=tests/integration --ignore=tests/e2e --tb=short -n auto
powershell -ExecutionPolicy Bypass -File .\scripts\test-routing-contract.ps1
```

The first command validates the Python test suite under the same xdist mode the updater uses. The second command validates routing-specific contract assumptions that should not silently drift.

## Operational commands

```bash
hermes-dev routing update install
hermes-dev routing update run
hermes-dev routing update status
hermes-dev routing update doctor
hermes-dev routing update finalize
```

- `install`: installs the scheduled updater job
- `run`: executes a normal upstream-sync cycle
- `status`: shows the last report, drift, and job state
- `doctor`: checks the updater environment and auth path
- `finalize`: resumes from a retained repair worktree after a manual fix

## Failure model

Common statuses:

- `noop`: nothing to do
- `updated`: upstream sync, trust gate, and promotion all succeeded
- `dirty_worktree`: live repo was not clean enough to start
- `auth_failed`: updater could not find a valid fetch/push path
- `repair_required`: retained worktree needs a merge or verification repair
- `verification_failed`: merge completed, but trust gate failed
- `finalize_failed`: retained repair exists, but promotion could not complete yet

When a run fails after worktree creation, the retained worktree is authoritative. Repairs should happen there, not in the live repo.

## Repair rules

- Repair the retained worktree, not the live branch.
- Start with the smallest failing validation slice before rerunning anything broad.
- Preserve the updater's topology and reports. Do not improvise a different merge/promotion flow unless you are doing emergency recovery.
- After a retained repair is committed, use `hermes routing update finalize`.

## Future-proofing checklist

- If a new fork-only subsystem changes execution policy, update [Fork Architecture](./fork-architecture.md).
- If maintenance or promotion behavior changes, update this page, [Maintained Fork](../getting-started/fork-variant.md), and `plans/routing-update-playbook.md`.
- If install or onboarding behavior changes, update README plus the getting-started pages so the documented install source still matches the actual repo source.
- If a new trust-gate step is added, document it here and keep the command paths deterministic.
