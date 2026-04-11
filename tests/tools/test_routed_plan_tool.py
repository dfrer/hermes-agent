import json
import time
from unittest.mock import MagicMock, patch

import pytest

from agent.routing_guard import activate_for_task, deactivate_for_task, record_routing_decision
from agent.routing_plan_store import set_plan_store_db
from hermes_state import SessionDB
from tools.routed_plan_tool import routed_plan_tool


@pytest.fixture(autouse=True)
def _routed_plan_store(tmp_path):
    session_db = SessionDB(tmp_path / "routed_plan_state.db")
    set_plan_store_db(session_db)
    try:
        yield session_db
    finally:
        set_plan_store_db(None)
        session_db.close()


def _parse(result: str) -> dict:
    return json.loads(result)


def _activate(task_id: str, decision: str) -> None:
    activate_for_task(task_id, session_id=f"session-{task_id}", skills=["routing-layer"])
    assert record_routing_decision(task_id, decision, session_id=f"session-{task_id}")


def _node(
    node_id: str,
    *,
    goal: str = "Apply the scoped fix",
    tier: str = "3C",
    path: str = "quick-edit",
    model: str = "Codex CLI (gpt-5.4-mini)",
    write_scope: list[str] | None = None,
    depends_on: list[str] | None = None,
    workdir: str = "",
) -> dict:
    node = {
        "id": node_id,
        "goal": goal,
        "tier": tier,
        "path": path,
        "model": model,
        "write_scope": write_scope if write_scope is not None else ["src/example.py"],
        "depends_on": depends_on or [],
        "verification": "Run targeted tests",
    }
    if workdir:
        node["workdir"] = workdir
    return node


def _plan(tmp_path, nodes: list[dict], **extra) -> dict:
    plan = {"summary": "Split implementation", "workdir": str(tmp_path), "nodes": nodes}
    plan.update(extra)
    return plan


def test_submit_status_and_reset(tmp_path):
    task_id = "routed-plan-submit"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: small scoped work | CONFIDENCE: high",
    )
    try:
        submitted = _parse(
            routed_plan_tool("submit", _plan(tmp_path, [_node("a")]), task_id=task_id)
        )
        assert submitted["success"] is True
        assert submitted["ordered_nodes"] == ["a"]
        assert submitted["next_node"]["id"] == "a"

        status = _parse(routed_plan_tool("status", task_id=task_id))
        assert status["success"] is True
        assert status["plan"]["nodes"][0]["status"] == "pending"

        reset = _parse(routed_plan_tool("reset", task_id=task_id))
        assert reset["success"] is True
        assert reset["status"] == "reset"
        assert reset["plan"] is None
        assert reset["persistent"] is True
        assert reset["resume_key"]["plan_id"]
    finally:
        deactivate_for_task(task_id)


def test_validates_missing_dependency_and_cycle(tmp_path):
    task_id = "routed-plan-graph-validation"
    _activate(
        task_id,
        "TIER: 3A | PATH: high-risk | MODEL: Codex CLI (gpt-5.4) | REASON: larger task | CONFIDENCE: high",
    )
    try:
        missing = _parse(
            routed_plan_tool(
                "submit",
                _plan(tmp_path, [_node("a", depends_on=["missing"])]),
                task_id=task_id,
            )
        )
        assert missing["success"] is False
        assert "missing dependency id(s): missing" in " ".join(missing["errors"])

        cycle = _parse(
            routed_plan_tool(
                "submit",
                _plan(
                    tmp_path,
                    [
                        _node("a", depends_on=["b"]),
                        _node("b", depends_on=["a"]),
                    ],
                ),
                task_id=task_id,
            )
        )
        assert cycle["success"] is False
        assert "plan dependency cycle" in " ".join(cycle["errors"])
    finally:
        deactivate_for_task(task_id)


def test_validates_route_path_model_mismatch(tmp_path):
    task_id = "routed-plan-route-mismatch"
    _activate(
        task_id,
        "TIER: 3B | PATH: marathon | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium task | CONFIDENCE: high",
    )
    try:
        result = _parse(
            routed_plan_tool(
                "submit",
                _plan(
                    tmp_path,
                    [
                        _node(
                            "a",
                            tier="3B",
                            path="long-context",
                            model="Hermes CLI (glm-5.1 via zai)",
                        )
                    ],
                ),
                task_id=task_id,
            )
        )
        assert result["success"] is False
        assert "not allowed for 3B/long-context" in " ".join(result["errors"])
    finally:
        deactivate_for_task(task_id)


def test_validates_missing_workdir_and_write_scope(tmp_path):
    task_id = "routed-plan-required-fields"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: small scoped work | CONFIDENCE: high",
    )
    try:
        plan = {
            "summary": "Missing required node fields",
            "nodes": [_node("a", write_scope=[])],
        }
        result = _parse(routed_plan_tool("submit", plan, task_id=task_id))
        assert result["success"] is False
        errors = " ".join(result["errors"])
        assert "workdir is required" in errors
        assert "write_scope must name at least one" in errors
    finally:
        deactivate_for_task(task_id)


def test_validates_parent_tier_underrouting(tmp_path):
    task_id = "routed-plan-parent-tier"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: small scoped work | CONFIDENCE: high",
    )
    try:
        result = _parse(
            routed_plan_tool(
                "submit",
                _plan(
                    tmp_path,
                    [
                        _node(
                            "a",
                            tier="3A",
                            path="high-risk",
                            model="Codex CLI (gpt-5.4)",
                        )
                    ],
                ),
                task_id=task_id,
            )
        )
        assert result["success"] is False
        assert "exceeds parent route tier 3C" in " ".join(result["errors"])
    finally:
        deactivate_for_task(task_id)


def test_run_next_executes_first_runnable_node(tmp_path):
    task_id = "routed-plan-run-next"
    _activate(
        task_id,
        "TIER: 3A | PATH: high-risk | MODEL: Codex CLI (gpt-5.4) | REASON: larger task | CONFIDENCE: high",
    )
    try:
        routed_plan_tool(
            "submit",
            _plan(
                tmp_path,
                [
                    _node("a", tier="3A", path="high-risk", model="Codex CLI (gpt-5.4)"),
                    _node("b", depends_on=["a"]),
                ],
            ),
            task_id=task_id,
        )
        with patch(
            "tools.routed_plan_tool.execute_routed_context",
            return_value={
                "success": True,
                "executors_attempted": ["Codex CLI (gpt-5.4)"],
                "summary": "node a done",
                "verification": "tests passed",
                "warnings": [],
                "attempts": [],
                "status": "success",
            },
        ) as mock_execute:
            result = _parse(routed_plan_tool("run_next", task_id=task_id))

        assert mock_execute.call_count == 1
        assert result["success"] is True
        assert result["executed_nodes"][0]["id"] == "a"
        assert result["plan"]["nodes"][0]["status"] == "completed"
        assert result["plan"]["next_node"]["id"] == "b"
    finally:
        deactivate_for_task(task_id)


def test_run_all_stops_on_failure_and_blocks_dependents(tmp_path):
    task_id = "routed-plan-run-all-failure"
    _activate(
        task_id,
        "TIER: 3A | PATH: high-risk | MODEL: Codex CLI (gpt-5.4) | REASON: larger task | CONFIDENCE: high",
    )
    try:
        routed_plan_tool(
            "submit",
            _plan(
                tmp_path,
                [
                    _node("a", tier="3A", path="high-risk", model="Codex CLI (gpt-5.4)"),
                    _node("b", depends_on=["a"]),
                    _node("c"),
                ],
            ),
            task_id=task_id,
        )
        with patch(
            "tools.routed_plan_tool.execute_routed_context",
            return_value={
                "success": False,
                "executors_attempted": ["Codex CLI (gpt-5.4)"],
                "summary": "node a failed",
                "verification": "",
                "warnings": ["failure"],
                "attempts": [{"failure_kind": "execution_failure"}],
                "failure_kind": "execution_failure",
                "status": "failed",
            },
        ):
            result = _parse(routed_plan_tool("run_all", task_id=task_id, max_nodes=3))

        statuses = {node["id"]: node["status"] for node in result["plan"]["nodes"]}
        assert result["success"] is False
        assert result["status"] == "failed"
        assert statuses == {"a": "failed", "b": "blocked", "c": "pending"}
    finally:
        deactivate_for_task(task_id)


def test_run_all_uses_expected_route_selection(tmp_path):
    task_id = "routed-plan-route-selection"
    _activate(
        task_id,
        "TIER: 3A | PATH: high-risk | MODEL: Codex CLI (gpt-5.4) | REASON: full routed plan | CONFIDENCE: high",
    )
    try:
        routed_plan_tool(
            "submit",
            _plan(
                tmp_path,
                [
                    _node("high", tier="3A", path="high-risk", model="Codex CLI (gpt-5.4)"),
                    _node("marathon", tier="3B", path="marathon", model="Hermes CLI (glm-5.1 via zai)"),
                    _node("context", tier="3B", path="long-context", model="Hermes CLI (xiaomi/mimo-v2-pro via nous)"),
                    _node("quick", tier="3C", path="quick-edit", model="Hermes CLI (MiniMax-M2.7 via minimax)"),
                ],
            ),
            task_id=task_id,
        )
        success_payload = 'HERMES_ROUTED_RESULT: {"status":"success","summary":"done","verification":"ok","warnings":[]}'
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                side_effect=[
                    MagicMock(returncode=0, stdout=success_payload, stderr=""),
                    MagicMock(returncode=1, stdout="provider failed", stderr=""),
                    MagicMock(returncode=0, stdout=success_payload, stderr=""),
                    MagicMock(returncode=0, stdout=success_payload, stderr=""),
                    MagicMock(returncode=0, stdout=success_payload, stderr=""),
                ],
            ) as mock_run,
            patch(
                "tools.routed_exec_tool._resolve_effective_route_plan",
                side_effect=lambda task_id, decision, plan: (plan, {}, ""),
            ),
            patch("tools.routed_exec_tool._find_executable", side_effect=lambda name: name),
            patch("tools.routed_exec_tool.resolve_api_key_provider_credentials", return_value={}),
        ):
            result = _parse(routed_plan_tool("run_all", task_id=task_id, max_nodes=4))

        assert result["status"] == "complete"
        commands = [call.args[0] for call in mock_run.call_args_list]
        assert commands[0][commands[0].index("-m") + 1] == "gpt-5.4"
        assert commands[1][commands[1].index("-m") + 1] == "glm-5.1"
        assert commands[1][commands[1].index("--provider") + 1] == "zai"
        assert commands[2][commands[2].index("-m") + 1] == "gpt-5.4-mini"
        assert commands[3][commands[3].index("-m") + 1] == "xiaomi/mimo-v2-pro"
        assert commands[3][commands[3].index("--provider") + 1] == "nous"
        assert commands[4][commands[4].index("-m") + 1] == "MiniMax-M2.7"
        assert commands[4][commands[4].index("--provider") + 1] == "minimax"
    finally:
        deactivate_for_task(task_id)


def test_persists_submit_status_reset_roundtrip(tmp_path, _routed_plan_store):
    task_id = "routed-plan-persist-roundtrip"
    session_id = "session-routed-plan-persist-roundtrip"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: small scoped work | CONFIDENCE: high",
    )
    try:
        submitted = _parse(
            routed_plan_tool(
                "submit",
                _plan(tmp_path, [_node("a")], plan_id="persist-plan"),
                task_id=task_id,
                session_id=session_id,
            )
        )
        assert submitted["persistent"] is True
        assert submitted["resume_key"] == {"session_id": session_id, "plan_id": "persist-plan"}
        assert _routed_plan_store.get_routed_plan("persist-plan")["status"] == "submitted"

        deactivate_for_task(task_id)
        activate_for_task(task_id, session_id=session_id, skills=["routing-layer"])
        status = _parse(routed_plan_tool("status", plan_id="persist-plan", task_id=task_id, session_id=session_id))
        assert status["persistent"] is True
        assert status["plan"]["nodes"][0]["status"] == "pending"

        reset = _parse(routed_plan_tool("reset", plan_id="persist-plan", task_id=task_id, session_id=session_id))
        assert reset["status"] == "reset"
        assert _routed_plan_store.get_routed_plan("persist-plan")["status"] == "reset"
    finally:
        deactivate_for_task(task_id)


def test_recovers_interrupted_running_node_from_persistence(tmp_path, _routed_plan_store):
    task_id = "routed-plan-running-recovery"
    session_id = "session-routed-plan-running-recovery"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: small scoped work | CONFIDENCE: high",
    )
    try:
        routed_plan_tool(
            "submit",
            _plan(tmp_path, [_node("a")], plan_id="recovery-plan"),
            task_id=task_id,
            session_id=session_id,
        )
        record = _routed_plan_store.get_routed_plan("recovery-plan")
        plan = record["plan"]
        plan["nodes"][0]["status"] = "running"
        plan["nodes"][0]["lease"] = {"id": "interrupted", "mode": "parallel"}
        _routed_plan_store.save_routed_plan(
            plan_id="recovery-plan",
            session_id=session_id,
            task_id=task_id,
            status="running",
            parent_decision=record["parent_decision"],
            plan=plan,
        )

        deactivate_for_task(task_id)
        activate_for_task(task_id, session_id=session_id, skills=["routing-layer"])
        status = _parse(routed_plan_tool("status", plan_id="recovery-plan", task_id=task_id, session_id=session_id))
        node = status["plan"]["nodes"][0]
        assert node["status"] == "pending"
        assert "recovered_after_interrupted_run" in node["warnings"]
        assert _routed_plan_store.get_routed_plan("recovery-plan")["status"] == "submitted"
    finally:
        deactivate_for_task(task_id)


def test_completed_plan_reloads_from_persistence(tmp_path):
    task_id = "routed-plan-completed-reload"
    session_id = "session-routed-plan-completed-reload"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: small scoped work | CONFIDENCE: high",
    )
    try:
        routed_plan_tool(
            "submit",
            _plan(tmp_path, [_node("a")], plan_id="completed-plan"),
            task_id=task_id,
            session_id=session_id,
        )
        with patch(
            "tools.routed_plan_tool.execute_routed_context",
            return_value={
                "success": True,
                "executors_attempted": ["Codex CLI (gpt-5.4-mini)"],
                "summary": "done",
                "verification": "ok",
                "warnings": [],
                "attempts": [],
                "status": "success",
            },
        ):
            routed_plan_tool("run_next", plan_id="completed-plan", task_id=task_id, session_id=session_id)

        deactivate_for_task(task_id)
        activate_for_task(task_id, session_id=session_id, skills=["routing-layer"])
        status = _parse(routed_plan_tool("status", plan_id="completed-plan", task_id=task_id, session_id=session_id))
        assert status["plan"]["complete"] is True
        assert status["plan"]["status"] == "completed"
    finally:
        deactivate_for_task(task_id)


def _success_execution(summary: str) -> dict:
    return {
        "success": True,
        "executors_attempted": ["Codex CLI (gpt-5.4-mini)"],
        "summary": summary,
        "verification": "ok",
        "warnings": [],
        "attempts": [],
        "status": "success",
    }


def _failure_execution(summary: str) -> dict:
    return {
        "success": False,
        "executors_attempted": ["Codex CLI (gpt-5.4-mini)"],
        "summary": summary,
        "verification": "",
        "warnings": ["failed"],
        "attempts": [{"failure_kind": "execution_failure"}],
        "failure_kind": "execution_failure",
        "status": "failed",
    }


def test_run_parallel_uses_ready_disjoint_nodes_and_plan_order(tmp_path, _routed_plan_store):
    task_id = "routed-plan-parallel-ready"
    session_id = "session-routed-plan-parallel-ready"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: scoped parallel work | CONFIDENCE: high",
    )
    try:
        routed_plan_tool(
            "submit",
            _plan(
                tmp_path,
                [
                    _node("a", write_scope=["src/a.py"]),
                    _node("b", write_scope=["src/b.py"], depends_on=["a"]),
                    _node("c", write_scope=["docs/c.md"]),
                ],
                plan_id="parallel-ready-plan",
            ),
            task_id=task_id,
            session_id=session_id,
        )

        def fake_execute(prompt, *_args, **_kwargs):
            if "Node id: a" in prompt:
                time.sleep(0.05)
                return _success_execution("a done")
            return _success_execution("c done")

        with patch("tools.routed_plan_tool.execute_routed_context", side_effect=fake_execute) as mock_execute:
            result = _parse(
                routed_plan_tool(
                    "run_parallel",
                    plan_id="parallel-ready-plan",
                    task_id=task_id,
                    session_id=session_id,
                    max_nodes=3,
                    max_concurrency=2,
                )
            )

        assert mock_execute.call_count == 3
        assert [node["id"] for node in result["executed_nodes"]] == ["a", "b", "c"]
        statuses = {node["id"]: node["status"] for node in result["plan"]["nodes"]}
        assert statuses == {"a": "completed", "b": "completed", "c": "completed"}
    finally:
        deactivate_for_task(task_id)


def test_run_parallel_conservatively_skips_conflicting_write_scopes(tmp_path):
    task_id = "routed-plan-parallel-conflict"
    session_id = "session-routed-plan-parallel-conflict"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: scoped parallel work | CONFIDENCE: high",
    )
    try:
        routed_plan_tool(
            "submit",
            _plan(
                tmp_path,
                    [
                        _node("a", write_scope=["src"]),
                        _node("b", write_scope=["src/app.py"]),
                        _node("c", write_scope=["docs/c.md"]),
                        _node("d", write_scope=["docs/*.md"]),
                    ],
                plan_id="parallel-conflict-plan",
            ),
            task_id=task_id,
            session_id=session_id,
        )
        with patch("tools.routed_plan_tool.execute_routed_context", return_value=_success_execution("done")) as mock_execute:
            result = _parse(
                routed_plan_tool(
                    "run_parallel",
                    plan_id="parallel-conflict-plan",
                    task_id=task_id,
                    session_id=session_id,
                    max_nodes=2,
                    max_concurrency=4,
                )
            )

        assert mock_execute.call_count == 2
        assert [node["id"] for node in result["executed_nodes"]] == ["a", "c"]
        statuses = {node["id"]: node["status"] for node in result["plan"]["nodes"]}
        assert statuses["b"] == "pending"
        assert statuses["d"] == "pending"
    finally:
        deactivate_for_task(task_id)


def test_run_parallel_persists_lease_and_stops_after_wave_failure(tmp_path, _routed_plan_store):
    task_id = "routed-plan-parallel-failure"
    session_id = "session-routed-plan-parallel-failure"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: scoped parallel work | CONFIDENCE: high",
    )
    try:
        routed_plan_tool(
            "submit",
            _plan(
                tmp_path,
                [
                    _node("a", write_scope=["src/a.py"]),
                    _node("b", write_scope=["src/b.py"]),
                    _node("c", write_scope=["src/c.py"], depends_on=["a"]),
                ],
                plan_id="parallel-failure-plan",
            ),
            task_id=task_id,
            session_id=session_id,
        )

        def fake_execute(prompt, *_args, **_kwargs):
            row = _routed_plan_store.get_routed_plan("parallel-failure-plan")
            running = [node for node in row["plan"]["nodes"] if node["status"] == "running"]
            assert running
            assert all("lease" in node for node in running)
            if "Node id: a" in prompt:
                return _failure_execution("a failed")
            return _success_execution("b done")

        with patch("tools.routed_plan_tool.execute_routed_context", side_effect=fake_execute):
            result = _parse(
                routed_plan_tool(
                    "run_parallel",
                    plan_id="parallel-failure-plan",
                    task_id=task_id,
                    session_id=session_id,
                    max_nodes=3,
                    max_concurrency=2,
                )
            )

        statuses = {node["id"]: node["status"] for node in result["plan"]["nodes"]}
        assert statuses == {"a": "failed", "b": "completed", "c": "blocked"}
        assert [node["id"] for node in result["executed_nodes"]] == ["a", "b"]
        assert result["status"] == "failed"
    finally:
        deactivate_for_task(task_id)


def test_critique_uses_moa_read_only_without_mutating_plan(tmp_path):
    task_id = "routed-plan-critique"
    session_id = "session-routed-plan-critique"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: scoped work | CONFIDENCE: high",
    )
    try:
        routed_plan_tool(
            "submit",
            _plan(tmp_path, [_node("a")], plan_id="critique-plan"),
            task_id=task_id,
            session_id=session_id,
        )
        before = _parse(routed_plan_tool("status", plan_id="critique-plan", task_id=task_id, session_id=session_id))
        with (
            patch("tools.mixture_of_agents_tool.mixture_of_agents_tool", return_value=object()) as mock_moa,
            patch("model_tools._run_async", return_value='{"success": true, "response": "check dependencies"}'),
        ):
            result = _parse(
                routed_plan_tool(
                    "critique",
                    plan_id="critique-plan",
                    task_id=task_id,
                    session_id=session_id,
                    target="plan",
                )
            )
        after = _parse(routed_plan_tool("status", plan_id="critique-plan", task_id=task_id, session_id=session_id))

        assert mock_moa.call_count == 1
        assert result["success"] is True
        assert result["critique"]["response"] == "check dependencies"
        assert before["plan"]["nodes"] == after["plan"]["nodes"]
    finally:
        deactivate_for_task(task_id)


def test_critique_returns_unavailable_without_mutating_plan(tmp_path):
    task_id = "routed-plan-critique-unavailable"
    _activate(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Codex CLI (gpt-5.4-mini) | REASON: scoped work | CONFIDENCE: high",
    )
    try:
        routed_plan_tool("submit", _plan(tmp_path, [_node("a")], plan_id="critique-unavailable"), task_id=task_id)
        with (
            patch("tools.mixture_of_agents_tool.mixture_of_agents_tool", return_value=object()),
            patch("model_tools._run_async", side_effect=RuntimeError("moa missing")),
        ):
            result = _parse(routed_plan_tool("critique", plan_id="critique-unavailable", task_id=task_id))
        assert result["success"] is False
        assert result["status"] == "unavailable"
        assert "moa missing" in result["error"]
        assert result["plan"]["nodes"][0]["status"] == "pending"
    finally:
        deactivate_for_task(task_id)
