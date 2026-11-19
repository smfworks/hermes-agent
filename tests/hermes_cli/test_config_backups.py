"""config.yaml backups: one dir, deduped, bounded — never a pile of siblings in HERMES_HOME."""
import os
from pathlib import Path

import pytest

from hermes_cli.config_backups import backup_config, list_config_backups


def test_repeat_backups_dedupe_and_rotate(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model: a\n")
    stamps = iter(f"2026010100000{i}" for i in range(10))
    monkeypatch.setattr("hermes_cli.config_backups.time.strftime", lambda _fmt: next(stamps))

    first = backup_config(cfg, "pre-setup", keep=2)
    assert first is not None and first.parent == tmp_path / "backups" / "config"
    # Same bytes again → no new file (the hermes-setup-three-times case).
    assert backup_config(cfg, "pre-setup", keep=2) is None
    for i in range(3):
        cfg.write_text(f"model: {i}\n")
        backup_config(cfg, "pre-setup", keep=2)
    kept = list_config_backups(cfg, "pre-setup")
    assert len(kept) == 2 and kept[0].read_text() == "model: 2\n"
    # Nothing left beside config.yaml in the home root.
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["config.yaml"]


def test_same_size_edit_within_one_mtime_tick_is_still_backed_up(tmp_path: Path, monkeypatch):
    # Coarse-timestamp filesystems leave an immediate rewrite with the old mtime; dedupe must
    # compare content, not trust a (size, mtime) signature.
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model: a\n")
    stamps = iter(f"2026010100000{i}" for i in range(10))
    monkeypatch.setattr("hermes_cli.config_backups.time.strftime", lambda _fmt: next(stamps))

    assert backup_config(cfg, "pre-setup") is not None
    assert backup_config(cfg, "pre-setup") is None
    before = cfg.stat()
    cfg.write_text("model: b\n")
    os.utime(cfg, ns=(before.st_atime_ns, before.st_mtime_ns))

    second = backup_config(cfg, "pre-setup")
    assert second is not None and second.read_text() == "model: b\n"


def test_legacy_siblings_move_but_user_named_copies_stay(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model: a\n")
    for name in ("config.yaml.bak.1778718391", "config.yaml.corrupt.20260729-093706.bak",
                 "config.yaml.bak-pre-migrate-xai-20260515-120000"):
        (tmp_path / name).write_text("old")
    (tmp_path / "config.yaml.bak-my-note").write_text("mine")

    backup_config(cfg, "corrupt")

    root = tmp_path / "backups" / "config"
    assert (root / "config.yaml.bak.1778718391").exists()
    assert (root / "config.yaml.corrupt.20260729-093706.bak").exists()
    assert (tmp_path / "config.yaml.bak-my-note").read_text() == "mine"
    assert not list(tmp_path.glob("config.yaml.bak.*")) and not list(tmp_path.glob("config.yaml.corrupt.*"))


@pytest.mark.platforms("posix")
def test_backup_of_world_readable_config_is_owner_only(tmp_path: Path, monkeypatch):
    # copy2 preserves the source mode; a config.yaml left 0644 by an editor must not yield a
    # world-readable backup of the user's credentials (#148317).
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.config._container_or_chmod_skipped", lambda: False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("api_key: secret\n")
    os.chmod(cfg, 0o644)

    dest = backup_config(cfg, "pre-setup")

    assert dest is not None
    assert dest.stat().st_mode & 0o777 == 0o600


@pytest.mark.platforms("posix")
def test_swept_legacy_backup_becomes_owner_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.config._container_or_chmod_skipped", lambda: False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("api_key: secret\n")
    legacy = tmp_path / "config.yaml.bak.20260101-000000"
    legacy.write_text("api_key: old\n")
    os.chmod(legacy, 0o644)

    backup_config(cfg, "pre-setup")

    moved = tmp_path / "backups" / "config" / legacy.name
    assert moved.stat().st_mode & 0o777 == 0o600


@pytest.mark.platforms("posix")
def test_backups_written_before_the_fix_lose_world_access(tmp_path: Path, monkeypatch):
    # A reason that never fires again never rotates its old 0644 copy out; any later backup secures it.
    # Group bits stay (shared-group homes), as for config.yaml writes (#59726).
    monkeypatch.setattr("hermes_constants.get_managed_system", lambda *a, **k: None)
    monkeypatch.setattr("hermes_constants._container_or_chmod_skipped", lambda: False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("api_key: secret\n")
    root = tmp_path / "backups" / "config"
    root.mkdir(parents=True)
    world = root / "config.yaml.pre-setup.20260101-000000"
    world.write_text("api_key: old\n")
    os.chmod(world, 0o644)
    shared = root / "config.yaml.corrupt.20260101-000000"
    shared.write_text("api_key: old\n")
    os.chmod(shared, 0o640)

    backup_config(cfg, "good")

    assert world.stat().st_mode & 0o777 == 0o640
    assert shared.stat().st_mode & 0o777 == 0o640


@pytest.mark.platforms("posix")
def test_backup_pass_never_chmods_through_a_planted_link(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_managed_system", lambda *a, **k: None)
    monkeypatch.setattr("hermes_constants._container_or_chmod_skipped", lambda: False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("api_key: secret\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    os.chmod(outside, 0o644)
    root = tmp_path / "backups" / "config"
    root.mkdir(parents=True)
    (root / "config.yaml.pre-setup.20260101-000000").symlink_to(outside)

    backup_config(cfg, "good")

    assert outside.stat().st_mode & 0o777 == 0o644


# --- #157943: the container skip in _secure_file left container backups holding the source
# mode, so a 0644 config.yaml (or .env snapshot) yielded a world-readable copy of credentials.
# A backup is a new file nobody else is meant to open: outside containers it is 0600 as before;
# inside a container only the world bits are stripped (0644 -> 0640), keeping group access for
# split gateway/dashboard UIDs — the same policy the rotation sweep already applies to old copies.


@pytest.mark.platforms("posix")
def test_managed_backup_keeps_its_deliberate_mode(tmp_path: Path, monkeypatch):
    # Managed installs set their own mode at activation; the backup must not churn it
    # (the managed early-return in _secure_backup_file, flagged in review of #157943).
    # Note: the rotation sweep's drop_other_access ALSO early-returns when managed, so
    # the only chmod that could touch the new copy is _secure_backup_file itself.
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_constants.get_managed_system", lambda *a, **k: "nix")
    monkeypatch.setattr("hermes_cli.config._container_or_chmod_skipped", lambda: False)
    monkeypatch.setattr("hermes_constants._container_or_chmod_skipped", lambda: False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("api_key: secret\n")
    os.chmod(cfg, 0o640)

    dest = backup_config(cfg, "pre-setup")

    assert dest is not None
    assert dest.stat().st_mode & 0o777 == 0o640  # copy2 carried the source mode; no chmod


@pytest.mark.platforms("posix")
def test_container_backup_of_world_readable_config_drops_world_bits(tmp_path: Path, monkeypatch):
    # The container skip must not skip the backup itself: 0644 in, 0640 out (group stays for
    # split UIDs, world goes — never a world-readable credentials copy, #157943).
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_constants.get_managed_system", lambda *a, **k: None)
    monkeypatch.setattr("hermes_constants._container_or_chmod_skipped", lambda: True)
    monkeypatch.setattr("hermes_cli.config._container_or_chmod_skipped", lambda: True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("api_key: secret\n")
    os.chmod(cfg, 0o644)

    dest = backup_config(cfg, "pre-setup")

    assert dest is not None
    assert dest.stat().st_mode & 0o777 == 0o640


@pytest.mark.platforms("posix")
def test_container_backup_of_group_readable_config_is_unchanged(tmp_path: Path, monkeypatch):
    # 0640 already has no world bits; the container path must not widen or churn it.
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_constants.get_managed_system", lambda *a, **k: None)
    monkeypatch.setattr("hermes_constants._container_or_chmod_skipped", lambda: True)
    monkeypatch.setattr("hermes_cli.config._container_or_chmod_skipped", lambda: True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("api_key: secret\n")
    os.chmod(cfg, 0o640)

    dest = backup_config(cfg, "pre-setup")

    assert dest is not None
    assert dest.stat().st_mode & 0o777 == 0o640


@pytest.mark.platforms("posix")
def test_container_swept_legacy_backup_loses_world_bits(tmp_path: Path, monkeypatch):
    # The legacy-sibling sweep used _secure_file too, so container sweeps kept 0644 copies (#157943).
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_constants.get_managed_system", lambda *a, **k: None)
    monkeypatch.setattr("hermes_constants._container_or_chmod_skipped", lambda: True)
    monkeypatch.setattr("hermes_cli.config._container_or_chmod_skipped", lambda: True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("api_key: secret\n")
    legacy = tmp_path / "config.yaml.bak.20260101-000000"
    legacy.write_text("api_key: old\n")
    os.chmod(legacy, 0o644)

    backup_config(cfg, "pre-setup")

    moved = tmp_path / "backups" / "config" / legacy.name
    assert moved.stat().st_mode & 0o777 == 0o640


@pytest.mark.platforms("posix")
def test_container_backup_of_0666_source_loses_world_bits(tmp_path: Path, monkeypatch):
    # Worst case: a 0666 .env snapshot. The container path strips exactly the world bits
    # (the issue's own sketch: mode & ~0o007), so 0666 -> 0660: world gone; group bits pass
    # through unchanged, matching drop_other_access and the rotation sweep's policy.
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_constants.get_managed_system", lambda *a, **k: None)
    monkeypatch.setattr("hermes_constants._container_or_chmod_skipped", lambda: True)
    monkeypatch.setattr("hermes_cli.config._container_or_chmod_skipped", lambda: True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("api_key: secret\n")
    os.chmod(cfg, 0o666)

    dest = backup_config(cfg, "pre-setup")

    assert dest is not None
    mode = dest.stat().st_mode & 0o777
    assert mode & 0o007 == 0  # no world access, ever
    assert mode & 0o770 == 0o660  # owner+group bits pass through, nothing widened
