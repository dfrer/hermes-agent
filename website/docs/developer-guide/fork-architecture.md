---
title: "Fork Architecture"
description: "Architecture added by this fork on top of upstream Hermes Agent"
---

# Fork Architecture

This fork adds a routing and maintenance layer above the upstream Hermes core. The goal is not to replace upstream architecture, but to add explicit control points where this fork needs stricter execution policy, quota governance, and safer upstream maintenance.

## Design goals

- Keep upstream Hermes usable without carrying a long-lived private reimplementation.
- Make high-risk execution paths explicit instead of hiding them inside generic tool calls.
- Prevent silent paid-spend fallback when a cheaper or already-entitled path should be used instead.
- Treat upstream sync as a deterministic, test-gated workflow rather than an informal `git pull`.

## Layered model

### 1. Canonical routing

`agent/routing_policy.py` defines the canonical route matrix: tiers, paths, default executors, and allowed model families.

`agent/routing_guard.py` persists the task's selected route, approval state, verification state, and selected executor metadata. Once a task is routed, downstream execution should consume that state rather than inventing its own parallel routing logic.

### 2. Ability and visual context

`agent/ability_context.py` turns task requirements into concrete handoff packets, including visual context and lane requirements where needed.

This layer exists so routed execution can be strict about what context is required before a high-risk implementation step runs.

### 3. Routed planning and execution

`tools/routed_plan_tool.py` provides a structured planning surface for the routing layer.

`tools/routed_exec_tool.py` is the guarded implementation path. It consumes the selected route, applies entitlement checks, requests approvals when required, and dispatches the actual executor model or provider path.

### 4. Entitlement and quota gating

`agent/entitlements.py` sits above the canonical route matrix. It does not replace routing policy; it decides whether a routed target is currently allowed, blocked, unknown, or approval-gated.

Key behavior in this fork:

- locked spend classes fail closed when quota state is unknown
- task-scoped approvals are supported for downgrade or paid-spend exceptions
- routed fallback should respect entitlements instead of silently escaping into a paid backend

### 5. Deterministic fork maintenance

`hermes_cli/routing_auto_update.py` owns upstream-sync orchestration for this fork.

`hermes_cli/routing_update_git.py` isolates git backend detection and auth probing so the updater can choose the working transport path for the current environment.

The updater is part of the architecture, not a convenience script. In this fork, upstream maintenance is a first-class subsystem.

## Key modules

| Module | Responsibility |
|---|---|
| `agent/routing_policy.py` | Canonical tier/path matrix and route validation |
| `agent/routing_guard.py` | Task-level routing state, approvals, verification metadata, selected route |
| `agent/ability_context.py` | Lane/ability detection, visual packets, handoff preparation |
| `tools/routed_plan_tool.py` | Structured routed planning surface |
| `tools/routed_exec_tool.py` | Guarded routed execution with approvals and entitlement checks |
| `agent/entitlements.py` | Quota snapshots, spend-class locks, downgrade and paid-spend gating |
| `hermes_cli/routing_auto_update.py` | Retained-worktree updater, trust gate, promotion flow |
| `hermes_cli/routing_update_git.py` | Git backend probing and backend selection |
| `scripts/test-routing-contract.ps1` | Fork-specific post-pytest routing contract validation |

## Invariants

- Routing policy is canonical. Entitlements may block or degrade execution, but they should not silently rewrite the meaning of the original task tier.
- Routed work should flow through `routing_guard` state. A new execution path that bypasses it is a regression.
- Provider fallback must respect entitlements. A generic fallback that silently spends locked credits is a bug in this fork.
- Upstream sync is updater-owned. Manual maintenance can exist for emergency recovery, but it should not become the normal path.

## Where to extend the fork

- Add or change routes in `agent/routing_policy.py` and the routing tests.
- Add or change downgrade / spend policy in `agent/entitlements.py`.
- Add routed plan or execution behavior in `tools/routed_plan_tool.py` and `tools/routed_exec_tool.py`.
- Add maintenance behavior in `hermes_cli/routing_auto_update.py` and `hermes_cli/routing_update_git.py`.
- Keep fork documentation in sync when any of the above changes, especially [Maintained Fork](../getting-started/fork-variant.md) and [Fork Maintenance](./fork-maintenance.md).
