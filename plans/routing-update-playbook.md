# Hermes Routing Update Playbook

This repo has local routing-system modifications on top of upstream Hermes.
Do not update the live working branch by running `git pull` directly and hoping for the best.

## Local Setup

This checkout is configured for safer recurring upstream merges:

- dedicated integration branch: `codex/routing-integration`
- repo-local `rerere.enabled=true`
- repo-local `rerere.autoupdate=true`
- repo-local `merge.conflictstyle=zdiff3`
- focused routing regression script: `scripts/test-routing-contract.ps1`
- disposable update-worktree helper: `scripts/prepare-hermes-update.ps1`

## Safe Update Workflow

1. Make sure your main Hermes worktree is clean.
2. From the Hermes repo root, run:

  ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\prepare-hermes-update.ps1
   ```

   That fetches upstream and shows ahead/behind counts without mutating anything.

3. When ready, create a disposable update worktree:

  ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\prepare-hermes-update.ps1 -Apply
   ```

4. In the newly created worktree:

   ```powershell
   git merge --no-ff origin/main
   ```

   Or use rebase if you intentionally want a rebased history:

  ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\prepare-hermes-update.ps1 -Apply -Rebase
   ```

5. Resolve conflicts in routing-critical files first:

- `agent/routing_guard.py`
- `model_tools.py`
- `run_agent.py`
- `agent/skill_commands.py`
- `cli.py`
- `gateway/run.py`
- `hermes_cli/config.py`
- `agent/prompt_builder.py`
- `skills/routing-layer/SKILL.md`
- `../SOUL.md`

6. Run the routing contract suite:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\test-routing-contract.ps1
   ```

7. Run one live routed conversation after the tests pass.

8. If the update worktree is good, fast-forward the integration branch:

   ```powershell
   git merge --ff-only codex/upstream-sync-<timestamp>
   ```

9. Clear the banner update cache after a manual Git-based update so new Hermes sessions do not show a stale
   "commits behind" warning:

   ```powershell
   Remove-Item ~/.hermes/.update_check -ErrorAction SilentlyContinue
   ```

   `hermes update` does this automatically. A custom manual merge workflow does not.

## Why This Workflow

- keeps upstream merges out of your live working tree
- makes conflict resolution repeatable
- ensures routing enforcement stays tested after each update
- gives `rerere` a chance to learn recurring merge resolutions

## Current Constraint

The routing system is still specialized enough that upstream changes touching tool dispatch, the agent loop, or prompt assembly can break it.
Treat the routing contract suite as mandatory before accepting an upstream update.
