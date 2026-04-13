from __future__ import annotations

from pathlib import Path

from hermes_cli import routing_update_git as rug


def test_probe_git_backend_preserves_windows_bridge_probe_details(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    monkeypatch.setattr(rug, "_is_wsl_runtime", lambda: True)
    monkeypatch.setattr(rug, "_candidate_windows_git_paths", lambda: ["/mnt/c/Program Files/Git/cmd/git.exe"])

    def fake_probe(backend, **kwargs):
        if backend.kind == "native":
            return False, False, {"main": False, "codex/routing-integration": False}, {"fetch:origin": "native denied"}
        return True, True, {"main": True, "codex/routing-integration": True}, {}

    monkeypatch.setattr(rug, "_probe_backend", fake_probe)

    probe = rug.probe_git_backend(
        repo_root,
        upstream_remote="origin",
        push_remote="fork",
        live_branch="codex/routing-integration",
        promotion_branch="main",
    )

    assert probe.backend == "windows-bridge"
    assert probe.details["windows-bridge"]["fetch_ready"] is True
    assert probe.details["windows-bridge"]["push_ready"] is True
    assert probe.details["windows-bridge"]["executable"] == "/mnt/c/Program Files/Git/cmd/git.exe"


def test_probe_git_backend_prefers_native_when_both_backends_work(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    monkeypatch.setattr(rug, "_is_wsl_runtime", lambda: True)
    monkeypatch.setattr(rug, "_candidate_windows_git_paths", lambda: ["/mnt/c/Program Files/Git/cmd/git.exe"])
    monkeypatch.setattr(
        rug,
        "_probe_backend",
        lambda backend, **kwargs: (
            True,
            True,
            {"main": True, "codex/routing-integration": True},
            {},
        ),
    )

    probe = rug.probe_git_backend(
        repo_root,
        upstream_remote="origin",
        push_remote="fork",
        live_branch="codex/routing-integration",
        promotion_branch="main",
    )

    assert probe.backend == "native"
