# Hermes Routing Update Playbook

This fork is maintained through the routing-aware updater. The canonical entrypoint is:

```powershell
hermes routing update run
```

Or, from PowerShell wrappers:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-routing-auto-update.ps1
```

## Canonical Topology

- live branch: `codex/routing-integration`
- upstream remote: `origin`
- writable fork remote: `fork`
- promotion target: `fork/main`

## Public Commands

- `hermes routing update install`
- `hermes routing update run`
- `hermes routing update status`
- `hermes routing update doctor`

Advanced repair/finalization:

- `hermes routing update finalize`

## Trust Gate

Promotion to both `fork/codex/routing-integration` and `fork/main` only happens after:

```powershell
python -m pytest tests/ -q --ignore=tests/integration --ignore=tests/e2e --tb=short -n auto
powershell -ExecutionPolicy Bypass -File .\scripts\test-routing-contract.ps1
```

## Scheduled Behavior

- cadence: every 4 hours
- timezone: `America/Vancouver`
- `noop` runs stay silent
- successful runs summarize briefly
- degraded runs report the retained worktree and repair manifest

## Repair Flow

When `hermes routing update run --json` reports `repair_required` or `verification_failed`:

1. inspect `latest.json` and `latest.md`
2. if `repair_eligible == true`, route a guarded maintenance repair over the retained worktree
3. apply the smallest repair
4. rerun the targeted failing verification command
5. call `hermes routing update finalize`

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

Manual backup remains available:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\export-routing-backup.ps1
```
