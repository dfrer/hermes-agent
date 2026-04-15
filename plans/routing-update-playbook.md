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

## Split-Topology Awareness

`hermes routing update status` and `hermes routing update doctor` detect the
repo role automatically based on the repo root basename:

- `hermes-agent` → role `live`, expected branch `main`
- `hermes-agent-dev` → role `dev`, expected branch `codex/routing-integration`

Both commands work from **either** repo.  Doctor reports which role is being
inspected and uses correct terminology:

- `main` is the **protected live branch**
- `codex/routing-integration` is the **dev/updater branch**
- promotion candidates are counted via `git cherry` for the fast path and
  treated as advisory once reconcile mode has converged both fork branches onto
  one promoted head

## Public Commands

- `hermes routing update install`
- `hermes routing update run`
- `hermes routing update status`
- `hermes routing update doctor`
- `hermes routing update finalize`
- `hermes routing update reconcile`

All operational updater runs for this setup use the dev profile (`-p dev`).
Live `main` is not the updater's working branch.

### Dev-repo-only guard

`run`, `install`, `finalize`, and `reconcile` **must** run from the dev repo
(`/home/hunter/.hermes/hermes-agent-dev`) on `codex/routing-integration`.
If invoked against the live repo, they refuse with a clear error explaining
that updater operations are dev-repo-only.

`status` and `doctor` work from both repos.

## Promotion Flow

The updater now supports two promotion modes:

### Fast path: cherry-pick / fast-forward promotion

Use this when `fork/main` and the validated dev head are still clean
ancestor-related.

1. develop on `codex/routing-integration`
2. optionally push dev backup to `fork/codex/routing-integration`
3. run the updater trust gate
4. promote the validated head to `fork/codex/routing-integration`
5. promote the same head to `fork/main`

### Recovery path: reconcile-mode promotion

Use this when `fork/main` and `codex/routing-integration` have drifted too far
for a clean cherry-pick replay.

1. run `hermes-dev routing update reconcile`
2. the updater creates a disposable reconcile worktree from `fork/main`
3. it merges the validated dev head with `--no-ff`
4. if conflicts fall outside the narrow repair allowlist, it emits
   `reconcile_required` plus a repair manifest and stops
5. otherwise it aligns the resulting merge commit tree to the validated dev
   head exactly
6. it creates durable rollback refs on the fork
7. it pushes the same reconciled head to both `fork/main` and
   `fork/codex/routing-integration`

Rollback refs under `archive/routing-auto-update/<stamp>/...` are now a
standard part of every promotion.

## Scheduled Behavior

- cadence: every 4 hours
- timezone: `America/Vancouver`
- default profile cron is **paused**
- dev profile cron is the **active** updater path
- `noop` runs stay silent
- successful runs summarize briefly
- degraded runs report the retained worktree and repair manifest

### Running the dev gateway under WSL

Do not use `gateway install` under WSL.  Use the profile-aware tmux helpers
instead:

```
hermes -p dev gateway tmux-start    # start the dev gateway in tmux
hermes -p dev gateway tmux-status   # check session, PID, and health
hermes -p dev gateway tmux-attach   # attach to the tmux session
hermes -p dev gateway stop          # graceful gateway shutdown for profile handoff
hermes -p dev gateway tmux-stop     # tmux session cleanup only
```

The session is automatically named `hermes-dev-gateway` for the dev profile.
The default profile uses `hermes-gateway`.

For sequential handoff between dev and live under WSL, use `gateway stop`
before `tmux-stop`. `tmux-stop` only removes the session; it does not guarantee
that the gateway process has released profile-scoped credentials such as the
Telegram bot token.

## Repair Flow

When `hermes routing update run --json` reports `repair_required`,
`verification_failed`, or `reconcile_required`:

1. inspect `latest.json` and `latest.md`
2. if `repair_eligible == true`, route a guarded maintenance repair over the retained worktree
3. apply the smallest repair
4. rerun the targeted failing verification command
5. call `hermes routing update finalize`

If `doctor` reports branch-graph drift without a retained worktree, use
`hermes routing update reconcile` instead of improvising manual branch surgery.

Updater repairs happen against **dev retained worktrees only**.
Retained worktrees are treated as narrow-scope recovery state.
Archived retired retained worktrees (e.g. `archive/updater-retained-*`) are
historical reference only and must not be modified.

The deterministic updater remains authoritative for:

- worktree preparation
- trust-gate reruns
- reconcile-mode promotion
- live-branch fast-forward
- fork pushes
- report writing

## Managed Live Sync

The canonical live checkout remains on `main`, but the updater can now preserve
known local runtime-only edits during a live sync.

Default preserve allowlist:

- `gateway/status.py`
- `plugins/memory/honcho/client.py`

Behavior:

- if the live checkout is dirty only in the allowlisted paths, the updater
  stashes those paths as rollback-only preservation, fast-forwards `main`, and
  records `live_sync_state=preserved_local_changes`
- if the promoted `fork/main` already contains the equivalent content, the
  stash is intentionally left untouched for rollback instead of being
  auto-reapplied
- if any non-allowlisted tracked or untracked path is dirty, live sync is
  refused and reported as `live_sync_state=blocked_dirty`

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
