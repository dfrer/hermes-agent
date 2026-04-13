# Hermes Routing Update Playbook

The routing updater operates across two isolated repo worktrees with distinct
profiles.  Operational updater runs are driven from the **dev profile/worktree**;
live `main` is **not** the updater's working branch.

## Canonical Topology

| Role | Path | Branch | Profile |
|------|------|--------|---------|
| live | `/home/hunter/.hermes/hermes-agent` | `main` | `default` |
| dev  | `/home/hunter/.hermes/hermes-agent-dev` | `codex/routing-integration` | `dev` |

- upstream remote: `origin`
- writable fork remote: `fork`
- live backup target: `fork/main`
- dev backup target: `fork/codex/routing-integration`

## Public Commands

- `hermes routing update install`
- `hermes routing update run`
- `hermes routing update status`
- `hermes routing update doctor`
- `hermes routing update finalize`

All operational updater runs for this setup use the dev profile (`-p dev`).
Live `main` is not the updater's working branch.

## Promotion Flow

Promotion uses **cherry-pick**, not merge:

1. develop on `codex/routing-integration`
2. optionally push dev backup to `fork/codex/routing-integration`
3. create a temporary promote-check branch from `main`
4. cherry-pick approved commit(s) onto the temp branch
5. run targeted validation appropriate to the change
6. require explicit user approval
7. cherry-pick approved commit(s) onto `main`
8. optionally push `fork/main`

## Scheduled Behavior

- cadence: every 4 hours
- timezone: `America/Vancouver`
- default profile cron is **paused**
- dev profile cron is the **active** updater path
- `noop` runs stay silent
- successful runs summarize briefly
- degraded runs report the retained worktree and repair manifest

### Running the dev gateway under WSL

Do not use `gateway install` under WSL.  Run the gateway in a tmux session instead:

```
tmux new-session -d -s hermes-dev-gateway \
  "cd /home/hunter/.hermes/hermes-agent-dev && \
   /home/hunter/.hermes/hermes-agent-dev/venv/bin/python -m hermes_cli.main -p dev gateway run"
```

Attach later with: `tmux attach -t hermes-dev-gateway`

## Repair Flow

When `hermes routing update run --json` reports `repair_required` or `verification_failed`:

1. inspect `latest.json` and `latest.md`
2. if `repair_eligible == true`, route a guarded maintenance repair over the retained worktree
3. apply the smallest repair
4. rerun the targeted failing verification command
5. call `hermes routing update finalize`

Updater repairs happen against **dev retained worktrees only**.
Retained worktrees are treated as narrow-scope recovery state.
Archived retired retained worktrees (e.g. `archive/updater-retained-*`) are
historical reference only and must not be modified.

The deterministic updater remains authoritative for:

- worktree preparation
- trust-gate reruns
- live-branch fast-forward
- fork pushes
- report writing

## Required Repo Defaults

The updater expects:

- `rerere.enabled=true`
- `rerere.autoupdate=true`
- `merge.conflictstyle=zdiff3`
- `git safe.directory` configured for the live repo

## Backups and Policy Sync

The updater automatically:

- exports a routing backup under `~/.hermes/routing-backups/`
- syncs `~/.hermes/SOUL.md`
- syncs `~/.hermes/skills/routing-layer/SKILL.md`
- records policy history under `~/.hermes/routing-policy-history/`
