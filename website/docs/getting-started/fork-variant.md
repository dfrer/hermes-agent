---
title: "Maintained Fork"
description: "What this fork adds on top of upstream Hermes Agent and how to operate it safely"
---

# Maintained Fork

This repository is a maintained fork of `NousResearch/hermes-agent`, not a plain mirror. It follows upstream closely, but it intentionally carries additional routing, quota-governance, and self-maintenance architecture that upstream Hermes does not ship.

If you want the vanilla upstream project, use `NousResearch/hermes-agent`. If you are using this repository directly, treat this page as the starting point for the fork-specific behavior.

## What this fork adds

| Area | Main modules | Purpose |
|---|---|---|
| Routing layer | `agent/routing_policy.py`, `agent/routing_guard.py`, `agent/ability_context.py` | Classifies work into explicit lanes and preserves that decision through execution |
| Routed planning and execution | `tools/routed_plan_tool.py`, `tools/routed_exec_tool.py`, `tools/ability_context_tool.py`, `tools/visual_context_tool.py` | Gives the agent structured planning and guarded implementation paths for high-risk work |
| Entitlement-aware quota gating | `agent/entitlements.py`, `/quota` | Prevents paid or locked spend classes from being used implicitly and supports task-scoped approvals |
| Deterministic fork updater | `hermes_cli/routing_auto_update.py`, `hermes_cli/routing_update_git.py` | Merges upstream into a retained worktree, runs the trust gate, and only promotes if validation passes |

## Canonical topology

- live integration branch: `codex/routing-integration`
- upstream remote: `origin`
- writable fork remote: `fork`
- promoted branch: `fork/main`
- retained repair branches: `codex/upstream-sync-*`

The important distinction is that `fork/main` is a promoted result, not the day-to-day development branch. Routine work should branch from `codex/routing-integration`, and upstream sync should go through the routing-aware updater rather than ad hoc merges.

## Installing this fork

Use the fork installer, not the upstream installer:

```bash
curl -fsSL https://raw.githubusercontent.com/dfrer/hermes-agent/main/scripts/install.sh | bash
```

If you want the upstream project instead, use:

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

## Updating this fork

The user-facing update command for this repository is:

```bash
hermes update
```

The explicit maintenance entrypoint is:

```bash
hermes-dev routing update run
```

In this fork, `hermes update` delegates into the dev runtime and the authoritative maintenance flow is still the routing-aware updater because it understands the fork topology, retained repair worktrees, trust gate, and promotion rules.

Useful companion commands:

```bash
hermes-dev routing update status
hermes-dev routing update doctor
hermes-dev routing update finalize
```

## Read next

- [Updating & Uninstalling](./updating.md)
- [Fork Architecture](../developer-guide/fork-architecture.md)
- [Fork Maintenance](../developer-guide/fork-maintenance.md)
