# EXP-2026-11-B — data protection (2026-11-19)

> **Update 2026-11-19 22:37 UTC — store migrated to C:.** Live store:
> `C:\Users\louis\soccerlab-research\expb`. Recovery copies:
> `C:\Users\louis\soccerlab-backups\expb\emergency\` and `...\migration\` (verified,
> sha256 manifests), plus the untouched original on the damaged USB `D:`. The daily
> backup task is not installed because no separate healthy device exists yet, so
> off-device protection is **incomplete**. Event log:
> `registry/experiments/EXP-2026-11-B_operations.json`. The rest of this document
> describes the pre-migration state.

The live pilot runs code frozen at `94f8c56` (worktree
`api-football-data-research`). Everything here lives on branch
`research/quant-audit-next` (worktree `api-football-data-research-dev`) and
is **not active**.

## 1. USB store: findings (read-only inspection)

| item | value |
|---|---|
| volume | `D:` NTFS, 117 GB, 112 GB free, serial `3CF6E1FA`, removable; physical disk 1 "VendorC ProductCode" (USB) |
| Windows health | **HealthStatus Warning, OperationalStatus "Full Repair Needed"** |
| System log, last 30 days | 22 × `disk` Event 11 "controller error on \Device\Harddisk1" (10-21 → 11-18), 1 × Event 51 paging error, device number rising DR12 → DR46 (repeated disconnect/re-enumeration) |
| today | 12 × NTFS Event 55 "corruption discovered" + self-repairs at 21:57:45 UTC, the minute the research folder was created |
| SQLite | journal_mode `wal`, synchronous `2` (FULL; per-connection default, not set by the logger), page_size 4096, `quick_check` ok |
| protections in the live code | marker file `.soccerlab-expb-store` must exist, or the run exits 4 and writes nothing. **No drive-identity check** in the live code |

Assessment: the stick has a history of controller errors and filesystem
corruption. SQLite's own checks pass today, but the store sits on hardware that
has repeatedly failed. That is the main risk to the pilot.

## 2. Dropbox

* No Dropbox client, service, startup entry, scheduled task, registry entry or
  app data on this PC. **Nothing on this machine monitors `D:\soccerlab-research\expb`.**
* `D:\.dropbox.device` (2024-10-22) and `D:\.dbxignore` (Dropbox-generated
  ignore list for external-drive backup) are leftovers from registering the
  stick with Dropbox Backup, presumably on another computer. Neither was modified.
* `C:\Users\louis\Dropbox` exists (an old sync folder). The backup tool refuses
  any target inside a folder named `Dropbox` or marked with `.dropbox`/`.dropbox.device`.
* If the stick is ever connected to a machine running Dropbox Backup, exclude
  `soccerlab-research` in that app's backup settings first. The exact current
  UI steps were not verified here.

## 3. Backup mechanism (prepared, not scheduled)

`python -m soccerlab expb backup` (see `soccerlab/expb/backup.py`):
- SQLite online backup from a read-only connection (the logger keeps writing);
- converted to a self-contained file;
- `integrity_check`, experiment id, and every append-only table's count must
  be ≥ the previous backup's;
- fsync, then atomic rename, then an atomic sha256 manifest;
- keeps the newest 14;
- deletes only its own files;
- refuses: same volume as the source, Dropbox folders, the production
  checkout, a missing source, less than 3× the source size free.

`backup-verify` re-hashes and re-checks a backup. `restore` writes a **new**
file only and never repoints the logger.

Default target: `C:\Users\louis\soccerlab-backups\expb` (internal NVMe, a
physically separate device from the USB; not a Dropbox folder).

Tested on isolated fixtures (`tests/test_expb_backup.py`, 13 tests): backup
while the logger writes, restore, refusal on decreasing counts, retention,
tampering detection, interrupted backup leaves nothing published, same-volume
and Dropbox refusal, drive-identity pin.

### Commands (none run against the live store yet)

```powershell
cd C:\Users\louis\api-football-data-research-dev
$py = "C:\Users\louis\api-football-data-research\var\venv\Scripts\python.exe"
& $py -m soccerlab expb backup --source D:\soccerlab-research\expb\exp_b_odds.sqlite3 --dest C:\Users\louis\soccerlab-backups\expb --keep 14
& $py -m soccerlab expb backup-list --dest C:\Users\louis\soccerlab-backups\expb
& $py -m soccerlab expb backup-verify C:\Users\louis\soccerlab-backups\expb\exp_b_odds_<stamp>.sqlite3
powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_backup_task.ps1            # dry run
powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_backup_task.ps1 -Install   # needs authorization
```

## 4. Drive identity (prepared, not active)

`expb pin-identity` records the store's volume serial in
`var\expb\STORE_IDENTITY` on C:. When that file exists, a run whose store is on
any other volume exits 4, even if the marker file was copied across. Without
the file, behaviour is identical to the live code. Activating it in the live
pilot requires moving this code onto the pilot worktree, which changes the
pilot's running code. That needs authorization.

## 5. 24-hour report

`python -m soccerlab expb report --source D:\soccerlab-research\expb\exp_b_odds.sqlite3 --hours 24`
(read-only). The first complete window ends 2026-11-20T21:58:29Z.
