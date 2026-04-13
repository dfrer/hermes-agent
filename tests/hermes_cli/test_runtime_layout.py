from __future__ import annotations

import json
from pathlib import Path

from hermes_cli import runtime_layout as rl


def test_bootstrap_split_runtime_moves_root_runtime_and_seeds_dev(tmp_path):
    admin_root = tmp_path / ".hermes"
    admin_root.mkdir()
    (admin_root / "state.db").write_text("live-db", encoding="utf-8")
    (admin_root / "config.yaml").write_text("model: nous", encoding="utf-8")
    (admin_root / ".env").write_text("OPENAI_API_KEY=test", encoding="utf-8")
    (admin_root / "auth.json").write_text('{"version": 1}', encoding="utf-8")
    (admin_root / "SOUL.md").write_text("# Soul", encoding="utf-8")
    (admin_root / "sessions").mkdir()
    (admin_root / "sessions" / "one.json").write_text("{}", encoding="utf-8")
    (admin_root / "skills" / "routing-layer").mkdir(parents=True)
    (admin_root / "skills" / "routing-layer" / "SKILL.md").write_text("routing skill", encoding="utf-8")

    main_home = rl.get_profile_home(rl.MAIN_RUNTIME_PROFILE, admin_root)
    dev_home = rl.get_profile_home(rl.DEV_RUNTIME_PROFILE, admin_root)
    main_home.mkdir(parents=True)
    dev_home.mkdir(parents=True)
    (main_home / "config.yaml").write_text("placeholder: true", encoding="utf-8")

    result = rl.bootstrap_split_runtime(admin_root)

    assert result.status == "migrated"
    assert (main_home / "state.db").read_text(encoding="utf-8") == "live-db"
    assert (main_home / "SOUL.md").read_text(encoding="utf-8") == "# Soul"
    assert (main_home / "sessions" / "one.json").read_text(encoding="utf-8") == "{}"
    assert (dev_home / "config.yaml").read_text(encoding="utf-8") == "model: nous"
    assert (dev_home / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=test"
    assert json.loads((dev_home / "auth.json").read_text(encoding="utf-8")) == {"version": 1}
    assert (dev_home / "SOUL.md").read_text(encoding="utf-8") == "# Soul"
    assert (dev_home / "skills" / "routing-layer" / "SKILL.md").read_text(encoding="utf-8") == "routing skill"
    assert not (admin_root / "state.db").exists()
    assert not (admin_root / "config.yaml").exists()
    assert (admin_root / rl.RUNTIME_LAYOUT_MARKER).exists()
    assert (rl.get_runtime_backup_root(admin_root) / Path(result.backup_dir).name / "archived-root-runtime" / "state.db").exists()


def test_bootstrap_split_runtime_is_noop_once_root_runtime_is_archived(tmp_path):
    admin_root = tmp_path / ".hermes"
    admin_root.mkdir()

    result = rl.bootstrap_split_runtime(admin_root)

    assert result.status == "noop"
    assert (admin_root / rl.RUNTIME_LAYOUT_MARKER).exists()


def test_bootstrap_split_runtime_repairs_missing_dev_policy_files_after_migration(tmp_path):
    admin_root = tmp_path / ".hermes"
    admin_root.mkdir()
    main_home = rl.get_profile_home(rl.MAIN_RUNTIME_PROFILE, admin_root)
    dev_home = rl.get_profile_home(rl.DEV_RUNTIME_PROFILE, admin_root)
    (main_home / "skills" / "routing-layer").mkdir(parents=True)
    dev_home.mkdir(parents=True)
    (main_home / "SOUL.md").write_text("# Main Soul", encoding="utf-8")
    (main_home / "skills" / "routing-layer" / "SKILL.md").write_text("routing skill", encoding="utf-8")
    (admin_root / rl.RUNTIME_LAYOUT_MARKER).write_text("{}", encoding="utf-8")

    result = rl.bootstrap_split_runtime(admin_root)

    assert result.status == "repaired"
    assert "SOUL.md" in result.seeded_dev_paths
    assert "skills/routing-layer/SKILL.md" in result.seeded_dev_paths
    assert (dev_home / "SOUL.md").read_text(encoding="utf-8") == "# Main Soul"
    assert (dev_home / "skills" / "routing-layer" / "SKILL.md").read_text(encoding="utf-8") == "routing skill"
