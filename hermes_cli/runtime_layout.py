#!/usr/bin/env python3
"""Helpers for the split live/dev Hermes runtime layout."""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from hermes_constants import get_default_hermes_root


MAIN_RUNTIME_PROFILE = "main"
DEV_RUNTIME_PROFILE = "dev"
RUNTIME_LAYOUT_BACKUP_DIRNAME = "runtime-layout-backups"
RUNTIME_LAYOUT_MARKER = ".split-runtime-layout-v1.json"

MAIN_RUNTIME_PATHS: tuple[str, ...] = (
    "SOUL.md",
    "auth.json",
    "config.yaml",
    ".env",
    "gateway.pid",
    "gateway_state.json",
    "processes.json",
    "state.db",
    "state.db-shm",
    "state.db-wal",
    "response_store.db",
    "response_store.db-shm",
    "response_store.db-wal",
    "logs",
    "memories",
    "sessions",
    "skills",
    "skins",
    "plans",
    "workspace",
    "home",
)

DEV_SEED_PATHS: tuple[str, ...] = (
    "auth.json",
    "config.yaml",
    ".env",
    "SOUL.md",
    "skills/routing-layer/SKILL.md",
)


@dataclass
class RuntimeLayoutResult:
    status: str
    admin_root: str
    main_home: str
    dev_home: str
    backup_dir: str = ""
    migrated_paths: list[str] = field(default_factory=list)
    seeded_dev_paths: list[str] = field(default_factory=list)
    archived_root_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    message: str = ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def get_admin_root(root: Path | str | None = None) -> Path:
    candidate = Path(root).expanduser() if root is not None else get_default_hermes_root()
    return candidate.resolve()


def get_profile_home(profile: str, root: Path | str | None = None) -> Path:
    return get_admin_root(root) / "profiles" / profile


def get_runtime_backup_root(root: Path | str | None = None) -> Path:
    return get_admin_root(root) / RUNTIME_LAYOUT_BACKUP_DIRNAME


def _marker_path(root: Path) -> Path:
    return root / RUNTIME_LAYOUT_MARKER


def _copy_path(src: Path, dest: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)


def _replace_with_copy(src: Path, dest: Path, *, backup_dest_root: Path | None = None) -> None:
    if dest.exists() and backup_dest_root is not None:
        backup_dest = backup_dest_root / dest.name
        if not backup_dest.exists():
            _copy_path(dest, backup_dest)
    _remove_path(dest)
    _copy_path(src, dest)


def _iter_existing(root: Path, relative_paths: Iterable[str]) -> list[tuple[str, Path]]:
    items: list[tuple[str, Path]] = []
    for relative in relative_paths:
        src = root / relative
        if src.exists():
            items.append((relative, src))
    return items


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _seed_profile_material(
    source_root: Path,
    dest_root: Path,
    relative_paths: Iterable[str],
    *,
    overwrite: bool,
    backup_dest_root: Path | None = None,
) -> tuple[list[str], list[str]]:
    seeded: list[str] = []
    skipped: list[str] = []
    for relative in relative_paths:
        seed_src = source_root / relative
        if not seed_src.exists():
            skipped.append(relative)
            continue
        dest = dest_root / relative
        if dest.exists() and not overwrite:
            continue
        _replace_with_copy(seed_src, dest, backup_dest_root=backup_dest_root)
        seeded.append(relative)
    return seeded, skipped


def bootstrap_split_runtime(root: Path | str | None = None) -> RuntimeLayoutResult:
    admin_root = get_admin_root(root)
    main_home = get_profile_home(MAIN_RUNTIME_PROFILE, admin_root)
    dev_home = get_profile_home(DEV_RUNTIME_PROFILE, admin_root)
    main_home.mkdir(parents=True, exist_ok=True)
    dev_home.mkdir(parents=True, exist_ok=True)

    source_entries = _iter_existing(admin_root, MAIN_RUNTIME_PATHS)
    result = RuntimeLayoutResult(
        status="noop",
        admin_root=str(admin_root),
        main_home=str(main_home),
        dev_home=str(dev_home),
        message="Split runtime layout already bootstrapped.",
    )
    if not source_entries:
        seeded, skipped = _seed_profile_material(main_home, dev_home, DEV_SEED_PATHS, overwrite=False)
        result.seeded_dev_paths.extend(seeded)
        result.skipped_paths.extend([f"dev-seed:{item}" for item in skipped])
        if seeded:
            result.status = "repaired"
            result.message = "Split runtime layout was already migrated; repaired missing dev automation files."
        marker = _marker_path(admin_root)
        if not marker.exists():
            _write_json(
                marker,
                {
                    **asdict(result),
                    "updated_at": _iso(_utc_now()),
                },
            )
        elif seeded:
            _write_json(
                marker,
                {
                    **asdict(result),
                    "updated_at": _iso(_utc_now()),
                },
            )
        return result

    stamp = _utc_now().strftime("%Y%m%d-%H%M%S")
    backup_dir = get_runtime_backup_root(admin_root) / stamp
    root_snapshot_dir = backup_dir / "root-runtime"
    archived_root_dir = backup_dir / "archived-root-runtime"
    preexisting_main_dir = backup_dir / "preexisting-main-runtime"
    preexisting_dev_dir = backup_dir / "preexisting-dev-seed"
    backup_dir.mkdir(parents=True, exist_ok=True)
    result.backup_dir = str(backup_dir)

    # Copy the root runtime into profiles/main first, then archive the old root payload.
    for relative, src in source_entries:
        _copy_path(src, root_snapshot_dir / relative)
        _replace_with_copy(src, main_home / relative, backup_dest_root=preexisting_main_dir)
        result.migrated_paths.append(relative)

    # Seed dev auth/config/policy material from the newly-migrated main runtime.
    seeded, skipped = _seed_profile_material(
        main_home,
        dev_home,
        DEV_SEED_PATHS,
        overwrite=False,
        backup_dest_root=preexisting_dev_dir,
    )
    result.seeded_dev_paths.extend(seeded)
    result.skipped_paths.extend([f"dev-seed:{item}" for item in skipped])

    for relative, src in source_entries:
        archived_dest = archived_root_dir / relative
        archived_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(archived_dest))
        result.archived_root_paths.append(relative)

    result.status = "migrated"
    result.message = "Moved the legacy root runtime into profiles/main and seeded profiles/dev automation files."
    _write_json(
        _marker_path(admin_root),
        {
            **asdict(result),
            "updated_at": _iso(_utc_now()),
        },
    )
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap the split Hermes runtime layout")
    parser.add_argument("command", choices=("bootstrap",))
    parser.add_argument("--root", default="", help="Override the Hermes admin root")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = bootstrap_split_runtime(args.root or None)
    payload = asdict(result)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(result.message)
        if result.backup_dir:
            print(f"backup_dir={result.backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
