"""Verified online backups of the EXP-2026-11-B database.

    python -m soccerlab expb backup --source D:\\soccerlab-research\\expb\\exp_b_odds.sqlite3 \\
                                    --dest C:\\Users\\louis\\soccerlab-backups\\expb --keep 14
    python -m soccerlab expb backup-verify <backup.sqlite3>
    python -m soccerlab expb restore <backup.sqlite3> <new-target.sqlite3>

Procedure (each step must pass or nothing is published):

1. refuse a destination on the same volume as the source, inside a Dropbox
   folder, or inside the production checkout; require 3x the source size free;
2. copy with SQLite's online backup API from a read-only source connection
   (safe while the logger writes: WAL readers never block it);
3. convert the copy to a self-contained rollback-journal file;
4. verify the copy: PRAGMA integrity_check == ok, expected tables present,
   experiment id, and every append-only table's row count >= the previous
   backup's (a decrease means corruption or the wrong source);
5. fsync, hash, atomically rename into place, then write the manifest
   atomically -- a backup counts only once its manifest exists;
6. prune: keep the newest `keep` complete backups; delete only files matching
   this tool's naming pattern; never delete the backup just made.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from ..hashing import atomic_write_json, sha256_file
from ..paths import assert_not_production
from ..timeutil import iso, utcnow
from .db import TABLES
from .volume import volume_id

NAME_RE = re.compile(r"^exp_b_odds_(\d{8}T\d{6}Z)\.sqlite3$")
# Volumes that must never receive a backup: the USB stick that logged NTFS corruption and
# repeated controller errors on 2026-10-21..2026-11-19 (EXP-2026-11-B data-protection report).
DAMAGED_VOLUMES = {"3CF6E1FA"}
EXPERIMENT = "EXP-2026-11-B"


class BackupError(RuntimeError):
    pass


# ------------------------------------------------------------ approved sync services
# A folder synced by an approved cloud service counts as off-device even though its
# local copy shares the source's volume. Only OneDrive (Personal) is approved, and only
# when the destination is inside its folder and the client is running.
SYNC_SERVICES = ("onedrive",)


def onedrive_root():
    root = os.environ.get("OneDriveConsumer") or os.environ.get("OneDrive")
    return Path(root) if root else None


def onedrive_running():
    if os.name != "nt":
        return False
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OneDrive.exe", "/NH"], capture_output=True, text=True)
    return "OneDrive.exe" in r.stdout


def check_sync_service(dest, service):
    if service not in SYNC_SERVICES:
        raise BackupError(f"unsupported off-device service {service!r}")
    root = onedrive_root()
    if root is None or not root.is_dir():
        raise BackupError("OneDrive folder not found (OneDrive / OneDriveConsumer environment variable)")
    d, r = Path(dest).resolve(), root.resolve()
    if d != r and r not in d.parents:
        raise BackupError(f"{dest} is not inside the OneDrive folder {root}")
    if not onedrive_running():
        raise BackupError("OneDrive is not running; the backup would not leave this machine")
    return r


def default_dest():
    return Path(os.environ.get("SOCCERLAB_EXPB_BACKUP", Path.home() / "soccerlab-backups" / "expb"))


def _in_dropbox(path):
    p = Path(path).resolve()
    for anc in (p, *p.parents):
        if anc.name.lower() == "dropbox" or (anc / ".dropbox").exists() or (anc / ".dropbox.device").exists():
            return anc
    return None


def _counts(conn):
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in TABLES if t not in have]
    if missing:
        raise BackupError(f"backup is missing tables {missing}")
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}  # noqa: S608 (fixed names)


def _check(conn):
    res = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    if res != ["ok"]:
        raise BackupError(f"integrity_check failed: {res[:5]}")
    exp = conn.execute("SELECT value FROM meta WHERE key='experiment'").fetchone()
    if not exp or exp[0] != EXPERIMENT:
        raise BackupError(f"not an {EXPERIMENT} database")
    return _counts(conn)


def list_backups(dest):
    dest = Path(dest)
    out = []
    if not dest.exists():
        return out
    for f in sorted(dest.iterdir()):
        m = NAME_RE.match(f.name)
        if m and f.with_suffix(".json").exists():
            out.append(f)
    return out


def _fsync(path):
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))     # Windows FlushFileBuffers needs write access
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make_backup(source, dest=None, keep=14, now=None, offsite=None, staging=None):
    """offsite="onedrive": dest must be inside the running OneDrive folder; the copy is built and
    verified in a local staging directory outside the synced tree and moved in only when complete,
    so the sync client never sees a partial file."""
    src, dest = Path(source), Path(dest or default_dest())
    now = now or utcnow()
    if not src.is_file():
        raise BackupError(f"source database not found: {src}")
    if keep < 2:
        raise BackupError("keep must be >= 2")
    assert_not_production(dest)
    if (db := _in_dropbox(dest)) is not None:
        raise BackupError(f"refusing a backup target inside a Dropbox folder ({db})")
    if volume_id(dest) in DAMAGED_VOLUMES:
        raise BackupError(f"backup target is on volume {volume_id(dest)}, recorded as damaged; use a healthy device")
    if offsite:
        sync_root = check_sync_service(dest, offsite)
        stage = Path(staging or Path(tempfile.gettempdir()) / "soccerlab-expb-staging")
        if stage.resolve() == sync_root or sync_root in stage.resolve().parents:
            raise BackupError("staging directory must be outside the synced folder")
    else:
        if volume_id(src) == volume_id(dest):
            raise BackupError("backup target is on the same volume as the source; use a physically separate volume")
        stage = dest
    dest.mkdir(parents=True, exist_ok=True)
    stage.mkdir(parents=True, exist_ok=True)
    if volume_id(stage) != volume_id(dest):
        raise BackupError("staging and destination must share a volume so publication is an atomic rename")
    need = 3 * (src.stat().st_size + (src.with_name(src.name + "-wal").stat().st_size
                                      if src.with_name(src.name + "-wal").exists() else 0))
    if shutil.disk_usage(dest).free < need:
        raise BackupError(f"less than {need} bytes free at {dest}")

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    final = dest / f"exp_b_odds_{stamp}.sqlite3"
    if final.exists():
        raise BackupError(f"{final.name} already exists")
    tmp = stage / f".tmp-{stamp}-{os.getpid()}.sqlite3"
    prev = list_backups(dest)
    prev_counts = json.loads(prev[-1].with_suffix(".json").read_text(encoding="utf-8"))["row_counts"] if prev else None
    try:
        s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
        d = sqlite3.connect(tmp)
        try:
            s.backup(d, pages=256)
            d.execute("PRAGMA journal_mode=DELETE")     # self-contained single file
            counts = _check(d)
        finally:
            s.close()
            d.close()                                   # Windows cannot delete an open file
        if prev_counts:
            dropped = {t: (prev_counts.get(t, 0), counts[t]) for t in TABLES if counts[t] < prev_counts.get(t, 0)}
            if dropped:
                raise BackupError(f"append-only row counts decreased since last backup: {dropped}")
        _fsync(tmp)
        digest = sha256_file(tmp)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    manifest = {"experiment": EXPERIMENT, "created_at": iso(now), "source": str(src),
                "source_volume": volume_id(src), "backup_volume": volume_id(dest), "file": final.name,
                "bytes": final.stat().st_size, "sha256": digest, "integrity_check": "ok", "row_counts": counts,
                "offsite": offsite}
    if offsite:                                          # publish the manifest the same way: staged, then renamed
        staged = stage / f".tmp-{stamp}-{os.getpid()}.json"
        atomic_write_json(staged, manifest)
        os.replace(staged, final.with_suffix(".json"))
    else:
        atomic_write_json(final.with_suffix(".json"), manifest)
    pruned = prune(dest, keep, protect=final)
    for d_ in {dest, stage}:                             # leftovers from killed runs
        for stale in d_.glob(".tmp-*"):
            if stale != tmp:
                stale.unlink(missing_ok=True)
    return dict(manifest, pruned=[p.name for p in pruned])


def prune(dest, keep, protect=None):
    complete = list_backups(dest)
    doomed = [f for f in complete[:-keep] if f != protect] if len(complete) > keep else []
    for f in doomed:
        f.unlink()
        f.with_suffix(".json").unlink(missing_ok=True)
    return doomed


def verify_backup(path):
    path = Path(path)
    man_p = path.with_suffix(".json")
    if not man_p.exists():
        raise BackupError("no manifest: backup incomplete or not produced by this tool")
    man = json.loads(man_p.read_text(encoding="utf-8"))
    if sha256_file(path) != man["sha256"]:
        raise BackupError("sha256 mismatch: backup file changed or damaged")
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        counts = _check(c)
    finally:
        c.close()
    if counts != man["row_counts"]:
        raise BackupError("row counts differ from manifest")
    return man


def restore(backup, target):
    """Restore into a NEW file (never overwrites). Point the logger at it
    explicitly afterwards (`expb init-store` on its directory)."""
    backup, target = Path(backup), Path(target)
    verify_backup(backup)
    if target.exists() or target.with_name(target.name + "-wal").exists():
        raise BackupError(f"refusing to overwrite existing {target}")
    assert_not_production(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".restore-{os.getpid()}-{target.name}")
    try:
        s = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
            _check(d)
        finally:
            s.close()
            d.close()
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target
