"""Backup, restore, drive identity and the read-only report, on isolated fixtures."""

import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from soccerlab import provider
from soccerlab.expb import backup as B, config as C, db as dbm, logger as L, report24
from soccerlab.expb.db import DB
from soccerlab.timeutil import parse_utc
from tests.test_expb import KO, T0, Clock, FakeAPI, book, match


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A populated pilot DB built by the real runner against a fake API."""
    monkeypatch.setattr(C, "LEAGUES", ("epl",))
    clock = Clock(T0)
    api = FakeAPI(clock)
    api.schedules["epl"] = [match(1, KO), match(2, KO + timedelta(hours=3))]
    api.odds[("epl", 1)] = [book(1, "draftkings"), book(1, "fanduel", 155, 235, 185)]
    api.odds[("epl", 2)] = [book(2, "draftkings")]
    src = tmp_path / "src" / "exp_b_odds.sqlite3"
    db = DB(src)
    r = L.Runner(db, provider.Client("test-key", transport=api, sleep=lambda s: None), now_fn=clock,
                 sleep=lambda s: None, monotonic=lambda: 0.0)
    r.run_once()
    clock.t += timedelta(minutes=5)
    r.run_once()
    yield src, db, r, clock, api
    db.close()


@pytest.fixture(autouse=True)
def _same_volume_is_different(monkeypatch):
    """tmp_path lives on one volume; pretend source and backup dirs differ
    unless a test opts into the real check."""
    monkeypatch.setattr(B, "volume_id", lambda p: "SRC" if "src" in Path(p).parts else "BAK")


def test_backup_is_verified_atomic_and_restorable(live, tmp_path):
    src, db, r, clock, api = live
    dest = tmp_path / "bak"
    m = B.make_backup(src, dest, keep=3, now=parse_utc("2026-11-21T06:15:00Z"))
    f = dest / m["file"]
    assert f.exists() and f.with_suffix(".json").exists()
    assert m["integrity_check"] == "ok" and m["row_counts"]["quotes"] > 0
    assert not list(dest.glob(".tmp-*"))
    assert B.verify_backup(f)["sha256"] == m["sha256"]
    # backup is a self-contained file (no -wal needed) and opens read-only
    assert sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True).execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    target = tmp_path / "restored" / "exp_b_odds.sqlite3"
    B.restore(f, target)
    with DB(target) as rdb:                                             # restored DB works with the logger's DB class
        assert rdb.c.execute("SELECT count(*) FROM quotes").fetchone()[0] == m["row_counts"]["quotes"]
    with pytest.raises(B.BackupError):
        B.restore(f, target)                                            # never overwrites


def test_backup_while_logger_writes_and_monotonic_counts(live, tmp_path):
    src, db, r, clock, api = live
    dest = tmp_path / "bak"
    B.make_backup(src, dest, now=parse_utc("2026-11-21T06:00:00Z"))
    clock.t += timedelta(hours=3)
    r.run_once()                                                        # more rows appended
    m2 = B.make_backup(src, dest, now=parse_utc("2026-11-22T06:00:00Z"))
    assert len(B.list_backups(dest)) == 2 and m2["row_counts"]["runs"] == 3


def test_backup_refuses_decreasing_counts(live, tmp_path):
    src, db, r, clock, api = live
    dest = tmp_path / "bak"
    B.make_backup(src, dest, now=parse_utc("2026-11-21T06:00:00Z"))
    other = tmp_path / "src" / "other.sqlite3"
    DB(other).close()                                                   # empty DB = wrong source / lost data
    with pytest.raises(B.BackupError, match="decreased"):
        B.make_backup(other, dest, now=parse_utc("2026-11-22T06:00:00Z"))
    assert len(B.list_backups(dest)) == 1 and not list(dest.glob(".tmp-*"))


def test_retention_keeps_newest_and_only_own_files(live, tmp_path):
    src, *_ = live
    dest = tmp_path / "bak"
    dest.mkdir()
    (dest / "notes.txt").write_text("user file")
    names = [B.make_backup(src, dest, keep=3, now=parse_utc(f"2026-11-{d}T06:00:00Z"))["file"] for d in range(20, 26)]
    kept = [f.name for f in B.list_backups(dest)]
    assert kept == names[-3:] and (dest / "notes.txt").exists()
    assert not (dest / (names[0][:-8] + ".json")).exists()


def test_backup_refuses_same_volume_dropbox_and_missing_source(live, tmp_path, monkeypatch):
    src, *_ = live
    monkeypatch.setattr(B, "volume_id", lambda p: "SAME")
    with pytest.raises(B.BackupError, match="same volume"):
        B.make_backup(src, tmp_path / "bak")
    monkeypatch.setattr(B, "volume_id", lambda p: "SRC" if "src" in Path(p).parts else "BAK")
    with pytest.raises(B.BackupError, match="Dropbox"):
        B.make_backup(src, tmp_path / "Dropbox" / "bak")
    marked = tmp_path / "usb"
    marked.mkdir()
    (marked / ".dropbox.device").write_bytes(b"x")
    with pytest.raises(B.BackupError, match="Dropbox"):
        B.make_backup(src, marked / "bak")
    with pytest.raises(B.BackupError, match="not found"):
        B.make_backup(tmp_path / "src" / "nope.sqlite3", tmp_path / "bak")


def test_verify_detects_tampering(live, tmp_path):
    src, *_ = live
    dest = tmp_path / "bak"
    f = dest / B.make_backup(src, dest)["file"]
    with open(f, "r+b") as fh:
        fh.seek(5000)
        fh.write(b"\x00garbage\x00")
    with pytest.raises(B.BackupError):
        B.verify_backup(f)
    (dest / "exp_b_odds_20990101T000000Z.sqlite3").write_bytes(f.read_bytes())
    with pytest.raises(B.BackupError, match="manifest"):
        B.verify_backup(dest / "exp_b_odds_20990101T000000Z.sqlite3")


def test_interrupted_backup_leaves_nothing_published(live, tmp_path, monkeypatch):
    src, *_ = live
    dest = tmp_path / "bak"

    def boom(conn):
        raise RuntimeError("power cut during verification")
    monkeypatch.setattr(B, "_check", boom)
    with pytest.raises(RuntimeError):
        B.make_backup(src, dest)
    assert B.list_backups(dest) == [] and not list(dest.glob(".tmp-*")) and not list(dest.glob("exp_b_odds_*"))


def test_real_volume_check_refuses_same_volume(live, tmp_path, monkeypatch):
    from soccerlab.expb.volume import volume_id
    monkeypatch.setattr(B, "volume_id", volume_id)                      # real check: tmp_path is one volume
    src, *_ = live
    with pytest.raises(B.BackupError, match="same volume"):
        B.make_backup(src, tmp_path / "bak")


# ------------------------------------------------------------ drive identity

def test_store_identity_pin_refuses_other_volume(tmp_path, monkeypatch):
    from soccerlab.expb import volume
    store = tmp_path / "usb" / "expb"
    dbm.init_store(store)
    monkeypatch.setattr(volume, "volume_id", lambda p: "3CF6E1FA")
    d, vid = dbm.pin_store_identity()
    assert vid == "3CF6E1FA" and dbm.store_dir() == store
    monkeypatch.setattr(volume, "volume_id", lambda p: "DEADBEEF")    # another stick, marker copied onto it
    with pytest.raises(dbm.StoreUnavailable, match="expected 3CF6E1FA"):
        dbm.store_dir()
    assert L.main() == 4


def test_no_identity_file_keeps_marker_only_behaviour(tmp_path):
    store = tmp_path / "usb2" / "expb"
    dbm.init_store(store)
    assert dbm.store_dir() == store                                     # unchanged behaviour of the live pilot code
    (store / dbm.STORE_MARKER).unlink()
    with pytest.raises(dbm.StoreUnavailable):
        dbm.store_dir()


# ------------------------------------------------------------ report

def test_report_is_read_only_and_honest_about_snapshots(live, tmp_path):
    src, db, r, clock, api = live
    before = src.stat().st_mtime_ns, os.path.getsize(src)
    R = report24.build(src, start=T0, hours=24, now=T0 + timedelta(hours=1))
    assert (src.stat().st_mtime_ns, os.path.getsize(src)) == before
    assert R["complete_window"] is False and R["runs"]["recorded"] == 2
    assert R["snapshots"]["note"].startswith("no eligible fixture")
    assert R["integrity"]["quick_check"] == "ok" and R["api"]["auth_errors"] == 0
    assert R["quotes"]["complete_book_coverage"]["draftkings"] == 1.0
    text = report24.render(R)
    assert "window complete: False" in text and "lower bounds" in text
    R2 = report24.build(src, start=T0, hours=24, now=T0 + timedelta(hours=30))
    assert R2["snapshots"]["T-60"]["windows_closed_in_period"] == 2
    assert R2["snapshots"]["T-60"].get("missed", 0) + R2["snapshots"]["T-60"].get("unrecorded", 0) == 2


def test_backup_installer_defaults_to_dry_run():
    import shutil
    import subprocess
    if shutil.which("powershell") is None:
        pytest.skip("Windows PowerShell not available")
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(root / "research_tasks" / "install_expb_backup_task.ps1")],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "DRY RUN - nothing registered." in out.stdout


def test_manifest_json_is_valid(live, tmp_path):
    src, *_ = live
    dest = tmp_path / "bak"
    m = B.make_backup(src, dest)
    on_disk = json.loads((dest / m["file"]).with_suffix(".json").read_text(encoding="utf-8"))
    assert on_disk["sha256"] == m["sha256"] and on_disk["source_volume"] != on_disk["backup_volume"]


def test_damaged_volume_is_refused_as_destination(live, tmp_path, monkeypatch):
    src, *_ = live
    monkeypatch.setattr(B, "volume_id", lambda p: "SRC" if "src" in Path(p).parts else "3CF6E1FA")
    with pytest.raises(B.BackupError, match="damaged"):
        B.make_backup(src, tmp_path / "bak")


# ------------------------------------------------------------ approved off-device service (OneDrive)

@pytest.fixture
def onedrive(tmp_path, monkeypatch):
    root = tmp_path / "OneDrive"
    root.mkdir()
    monkeypatch.setattr(B, "onedrive_root", lambda: root)
    monkeypatch.setattr(B, "onedrive_running", lambda: True)
    monkeypatch.setattr(B, "volume_id", lambda p: "C")                 # source, staging and OneDrive share C:
    return root


def test_onedrive_same_volume_allowed_only_with_offsite(live, tmp_path, onedrive):
    src, *_ = live
    dest = onedrive / "soccerlab-backups" / "expb" / "daily"
    with pytest.raises(B.BackupError, match="same volume"):
        B.make_backup(src, dest)                                        # plain mode still refuses
    m = B.make_backup(src, dest, offsite="onedrive", staging=tmp_path / "stage")
    assert m["offsite"] == "onedrive" and B.verify_backup(dest / m["file"])
    assert sorted(p.name for p in dest.iterdir()) == sorted([m["file"], m["file"][:-8] + ".json"])   # nothing partial lands
    assert not list((tmp_path / "stage").glob(".tmp-*"))


def test_onedrive_refusals(live, tmp_path, onedrive, monkeypatch):
    src, *_ = live
    with pytest.raises(B.BackupError, match="not inside the OneDrive"):
        B.make_backup(src, tmp_path / "elsewhere", offsite="onedrive", staging=tmp_path / "stage")
    with pytest.raises(B.BackupError, match="outside the synced folder"):
        B.make_backup(src, onedrive / "bak", offsite="onedrive", staging=onedrive / "stage")
    monkeypatch.setattr(B, "onedrive_running", lambda: False)
    with pytest.raises(B.BackupError, match="not running"):
        B.make_backup(src, onedrive / "bak", offsite="onedrive", staging=tmp_path / "stage")
    monkeypatch.setattr(B, "onedrive_root", lambda: None)
    with pytest.raises(B.BackupError, match="OneDrive folder not found"):
        B.make_backup(src, onedrive / "bak", offsite="onedrive", staging=tmp_path / "stage")
    with pytest.raises(B.BackupError, match="unsupported"):
        B.make_backup(src, onedrive / "bak", offsite="dropbox", staging=tmp_path / "stage")


def test_onedrive_failed_backup_leaves_nothing_in_synced_folder(live, tmp_path, onedrive, monkeypatch):
    src, *_ = live
    dest = onedrive / "bak"

    def boom(conn):
        raise RuntimeError("crash during verification")
    monkeypatch.setattr(B, "_check", boom)
    with pytest.raises(RuntimeError):
        B.make_backup(src, dest, offsite="onedrive", staging=tmp_path / "stage")
    assert not dest.exists() or list(dest.iterdir()) == []
    assert not list((tmp_path / "stage").glob(".tmp-*"))


def test_onedrive_retention_never_touches_other_folders(live, tmp_path, onedrive):
    src, *_ = live
    dest = onedrive / "daily"
    keepme = onedrive / "emergency"
    keepme.mkdir()
    (keepme / "exp_b_odds_20261119T222716Z.sqlite3").write_bytes(b"x")
    (keepme / "exp_b_odds_20261119T222716Z.json").write_text("{}")
    for d in range(20, 25):
        B.make_backup(src, dest, keep=2, offsite="onedrive", staging=tmp_path / "stage",
                      now=parse_utc(f"2026-11-{d}T06:15:00Z"))
    assert len(B.list_backups(dest)) == 2 and len(list(keepme.iterdir())) == 2


def test_sync_status_parsing_and_newest_check(live, tmp_path, onedrive):
    src, *_ = live
    dest = onedrive / "daily"
    m = B.make_backup(src, dest, offsite="onedrive", staging=tmp_path / "stage")
    db_name, js = m["file"], m["file"][:-8] + ".json"
    ok, newest, st = B.newest_backup_synced(dest, runner=lambda d: f"{db_name}|2\n{js}|2\n")
    assert ok and newest == db_name and st[db_name] == "in_sync"
    ok, _, st = B.newest_backup_synced(dest, runner=lambda d: f"{db_name}|4\n{js}|2\n")
    assert not ok and st[db_name] == "pending_upload"
    ok, _, _ = B.newest_backup_synced(dest, runner=lambda d: "")
    assert not ok
    assert B.newest_backup_synced(tmp_path / "empty", runner=lambda d: "") == (False, None, {})
