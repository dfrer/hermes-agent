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


def test_probe_backend_uses_resolved_commit_shas_for_push_checks():
    calls = []

    class FakeBackend:
        def run(self, args, check=False):
            calls.append(list(args))
            if args[:2] == ["ls-remote", "--heads"]:
                return _completed(args)
            if args[:3] == ["rev-parse", "--verify", "refs/remotes/fork/codex/routing-integration^{commit}"]:
                return _completed(args, stdout="live-sha\n")
            if args[:3] == ["rev-parse", "--verify", "refs/remotes/fork/main^{commit}"]:
                return _completed(args, stdout="main-sha\n")
            if args[:2] == ["rev-parse", "--verify"]:
                return _completed(args, returncode=1)
            if args[:3] == ["push", "--porcelain", "--dry-run"]:
                return _completed(args)
            raise AssertionError(f"Unexpected git args: {args}")

    fetch_ready, push_ready, push_targets, errors = rug._probe_backend(
        FakeBackend(),
        upstream_remote="origin",
        push_remote="fork",
        live_branch="codex/routing-integration",
        promotion_branch="main",
        live_source_refs=("refs/remotes/fork/codex/routing-integration",),
        promotion_source_refs=("refs/remotes/fork/main",),
    )

    push_calls = [args for args in calls if args[:3] == ["push", "--porcelain", "--dry-run"]]

    assert fetch_ready is True
    assert push_ready is True
    assert push_targets == {"codex/routing-integration": True, "main": True}
    assert errors == {}
    assert push_calls == [
        ["push", "--porcelain", "--dry-run", "fork", "live-sha:refs/heads/codex/routing-integration"],
        ["push", "--porcelain", "--dry-run", "fork", "main-sha:refs/heads/main"],
    ]


def _completed(args=None, stdout="", stderr="", returncode=0):
    import subprocess

    return subprocess.CompletedProcess(args or [], returncode, stdout=stdout, stderr=stderr)
