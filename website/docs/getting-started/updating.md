---
sidebar_position: 3
title: "Updating & Uninstalling"
description: "How to update Hermes Agent to the latest version or uninstall it"
---

# Updating & Uninstalling

## Updating

For this fork, the canonical update command is:

```bash
hermes routing update run
```

Compatibility entrypoint:

```bash
hermes update
```

:::info Maintained fork updater
This repository does not use a plain `git pull` update path as its primary maintenance model. The authoritative updater is the routing-aware workflow, which understands the fork topology, retained repair worktrees, trust gate, and promotion rules. See [Maintained Fork](./fork-variant.md) and [Fork Maintenance](../developer-guide/fork-maintenance.md).
:::

:::tip
`hermes update` automatically detects new configuration options and prompts you to add them. If you skipped that prompt, you can manually run `hermes config check` to see missing options, then `hermes config migrate` to interactively add them.
:::

### What happens during an update

When you run `hermes routing update run`, the following steps occur:

1. **Topology + auth probe** — validates `origin`, `fork`, the live branch, and the active git backend
2. **Retained worktree prep** — creates or refreshes a retained `codex/upstream-sync-*` worktree for the merge
3. **Upstream merge** — merges `origin/main` into the retained worktree instead of mutating the live branch directly
4. **Trust gate** — runs the full xdist pytest gate plus the routing contract script
5. **Promotion** — only after validation passes, promotes the result to `fork/codex/routing-integration` and `fork/main`
6. **Report writeout** — stores status, drift, and any repair artifacts under `~/.hermes/cron/output/routing-auto-update/`

Expected output looks like:

```
$ hermes routing update run
# Routing Auto Update: updated
- Message: Applied upstream changes, passed the trust gate, and promoted fork integration + main.
```

### Status, health, and finalize

```bash
hermes routing update status
hermes routing update doctor
hermes routing update finalize
```

- `status` shows the last updater result, branch drift, auth backend, and job state
- `doctor` checks whether the updater environment is healthy before you run it
- `finalize` resumes from a retained repair worktree after a manual fix

### Recommended Post-Update Validation

The routing updater handles the full maintenance path, but a quick post-update check is still useful:

1. `git status --short` — if the tree is unexpectedly dirty, inspect before continuing
2. `hermes routing update status` — confirm the last run is `updated`
3. `hermes doctor` — checks config, dependencies, and service health
4. If you use the gateway: `hermes gateway status`

:::warning Dirty working tree after update
If `git status --short` shows unexpected changes after an updater run, stop and inspect them before continuing. On this fork, the updater expects to own the maintenance flow; a dirty tree usually means local work or a manual change needs to be separated before the next run.
:::

### Checking your current version

```bash
hermes version
```

Compare against the latest promoted head in your fork or check for available updates:

```bash
hermes update --check
```

### Updating from Messaging Platforms

You can also update directly from Telegram, Discord, Slack, or WhatsApp by sending:

```
/update
```

This pulls the latest code, updates dependencies, and restarts the gateway. The bot will briefly go offline during the restart (typically 5–15 seconds) and then resume.

### Manual Update

If you are doing emergency maintenance and intentionally bypassing the deterministic updater:

```bash
cd /path/to/hermes-agent
git fetch origin --prune
git fetch fork --prune
hermes routing update run
```

For this fork, manual maintenance should normally mean "invoke the updater and repair the retained worktree if needed," not "replace the updater with a hand-written merge flow."

### Rollback instructions

If an update introduces a problem, you can roll back to a previous version:

```bash
cd /path/to/hermes-agent

# List recent versions
git log --oneline -10

# Roll back to a specific commit
git checkout <commit-hash>
git submodule update --init --recursive
uv pip install -e ".[all]"

# Restart the gateway if running
hermes gateway restart
```

To roll back to a specific release tag:

```bash
git checkout v0.6.0
git submodule update --init --recursive
uv pip install -e ".[all]"
```

:::warning
Rolling back may cause config incompatibilities if new options were added. Run `hermes config check` after rolling back and remove any unrecognized options from `config.yaml` if you encounter errors.
:::

### Note for Nix users

If you installed via Nix flake, updates are managed through the Nix package manager:

```bash
# Update the flake input
nix flake update hermes-agent

# Or rebuild with the latest
nix profile upgrade hermes-agent
```

Nix installations are immutable — rollback is handled by Nix's generation system:

```bash
nix profile rollback
```

See [Nix Setup](./nix-setup.md) for more details.

---

## Uninstalling

```bash
hermes uninstall
```

The uninstaller gives you the option to keep your configuration files (`~/.hermes/`) for a future reinstall.

### Manual Uninstall

```bash
rm -f ~/.local/bin/hermes
rm -rf /path/to/hermes-agent
rm -rf ~/.hermes            # Optional — keep if you plan to reinstall
```

:::info
If you installed the gateway as a system service, stop and disable it first:
```bash
hermes gateway stop
# Linux: systemctl --user disable hermes-gateway
# macOS: launchctl remove ai.hermes.gateway
```
:::
