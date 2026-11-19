"""Append-only odds logger.

Why this exists: every model built here so far could only be validated because
football-data.co.uk happened to archive historical closing odds for 1X2. No
such archive exists for anything else. This builds one, for the markets the
balldontlie key actually exposes, so that future work is testable instead of
faith-based.

Design notes
------------
* Only fixtures kicking off within LOOKAHEAD_DAYS are polled, so a run costs a
  few calls rather than paging a whole season. Fixture dates come from the
  local schedule files, not the API.
* Storage is JSONL, append-only, one line per observation. Never rewritten, so
  a crash mid-run cannot corrupt history.
* A quote is only appended when it CHANGES. Re-logging identical prices every
  run would bloat the file and destroy the signal (line movement) this exists
  to capture. State lives in seen.json.
* Rate limit on this key is 5 req/min across all endpoints, hence SLEEP.

Run it on a schedule (see install_task.ps1). Several times a day is plenty for
1X2; the interesting movement is in the last hours before kickoff.
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from pathlib import Path

KEY = (os.environ.get("BALLDONTLIE_KEY") or
       (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "soccer-betting-bot" / "bdl.key").read_text().strip())

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEDULE_DIR = os.path.join(ROOT, "balldontlie_consolidated")
LOG_DIR = os.path.join(HERE, "log")
STATE_PATH = os.path.join(HERE, "seen.json")
RUNLOG = os.path.join(HERE, "runs.log")

LEAGUES = ["epl", "laliga", "seriea", "ligue1"]
# Same version split as the rest of this project: the Spanish/Italian/French
# feeds live at v1 and EPL at v2. Hitting the wrong one returns 404, silently
# logging nothing, so keep this in sync if a league is ever added.
VERSION = {"laliga": "v1", "seriea": "v1", "ligue1": "v1", "epl": "v2"}
SEASON = 2026
LOOKAHEAD_DAYS = 4
BATCH = 25          # match_ids per request
SLEEP = 13.0        # 5 req/min ceiling
TIMEOUT = 30

os.makedirs(LOG_DIR, exist_ok=True)


def note(msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    with open(RUNLOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url):
    req = urllib.request.Request(url, headers={"Authorization": KEY})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                note(f"  429, backing off 25s (attempt {attempt + 1})")
                time.sleep(25)
                continue
            note(f"  HTTP {e.code}: {e.read().decode('utf-8')[:200]}")
            return None
        except Exception as e:                                  # noqa: BLE001
            note(f"  {type(e).__name__}: {e} (attempt {attempt + 1})")
            time.sleep(5)
    return None


def upcoming(league):
    """Match ids kicking off between now and LOOKAHEAD_DAYS from now."""
    path = os.path.join(SCHEDULE_DIR, f"{league}_{SEASON}_matches.json")
    if not os.path.exists(path):
        note(f"  no schedule file for {league} {SEASON}")
        return []
    with open(path, encoding="utf-8") as f:
        sched = json.load(f)
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=LOOKAHEAD_DAYS)
    out = []
    for m in sched:
        try:
            d = datetime.fromisoformat(m["date"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        # Strictly pre-kickoff. An earlier version allowed a 3h grace after
        # kickoff to catch a late close, but the endpoint keeps serving the
        # market in-play and post-settlement at degenerate prices (-100000 /
        # +35000), which would land in the log looking like real pre-match
        # quotes. The last quote before kickoff is the closing line; anything
        # after it is noise at best and fake edge at worst.
        if now <= d <= until:
            out.append(m["id"])
    return out


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            note("  seen.json unreadable, starting fresh (history is intact)")
    return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)     # atomic; never leaves a half-written state


def quote_key(league, row):
    return f"{league}:{row['match_id']}:{row['vendor']}"


def quote_val(row):
    return [row.get("moneyline_home_odds"),
            row.get("moneyline_draw_odds"),
            row.get("moneyline_away_odds")]


def pages(url):
    page_url = url
    cursors = set()
    while page_url:
        data = fetch(page_url)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise RuntimeError("BDL poll failed")
        yield from data["data"]
        cursor = (data.get("meta") or {}).get("next_cursor")
        if cursor is not None and cursor in cursors:
            raise RuntimeError("Repeated pagination cursor")
        cursors.add(cursor)
        page_url = f"{url}&cursor={cursor}" if cursor is not None else None
        time.sleep(SLEEP)


def refresh_schedules():
    for league in LEAGUES:
        path = os.path.join(SCHEDULE_DIR, f"{league}_{SEASON}_matches.json")
        marker = path + ".refreshed"
        if os.path.isfile(path) and os.path.isfile(marker) and 0 <= time.time() - os.path.getmtime(marker) < 6 * 3600:
            continue
        url = f"https://api.balldontlie.io/{league}/{VERSION[league]}/matches?season={SEASON}&per_page=100"
        matches = list(pages(url))
        if not matches or any(m.get("season") != SEASON or not m.get("date")
                              or not m.get("id") for m in matches):
            raise RuntimeError(f"{league}: invalid season schedule")
        write_json(path, matches)
        Path(marker).touch()


def write_json(path, value):
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), delete=False, encoding="utf-8") as f:
        json.dump(value, f)
    os.replace(f.name, path)


def main():
    state = load_state()
    observed = datetime.now(timezone.utc).isoformat()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    snapshot = {}
    total_new = total_seen = 0

    for league in LEAGUES:
        ids = upcoming(league)
        if not ids:
            note(f"{league}: no fixtures in the next {LOOKAHEAD_DAYS}d")
            continue

        rows = []
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            q = "&".join(f"match_ids[]={m}" for m in chunk)
            url = (f"https://api.balldontlie.io/{league}/{VERSION[league]}/odds"
                   f"?{q}&per_page=100")
            rows.extend(pages(url))

        snapshot[league] = rows
        path = os.path.join(LOG_DIR, f"{league}_{month}.jsonl")
        new = 0
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                if r.get("moneyline_home_odds") is None:
                    continue
                k = quote_key(league, r)
                v = quote_val(r)
                if state.get(k) == v:
                    continue            # unchanged since last poll -- skip
                state[k] = v
                f.write(json.dumps({
                    "observed_at": observed,
                    "league": league,
                    "match_id": r["match_id"],
                    "vendor": r["vendor"],
                    "home": v[0], "draw": v[1], "away": v[2],
                    "vendor_updated_at": r.get("updated_at"),
                }, separators=(",", ":")) + "\n")
                new += 1
        total_new += new
        total_seen += len(rows)
        note(f"{league}: {len(ids)} fixtures, {len(rows)} quotes, {new} changed -> {os.path.basename(path)}")

    write_json(os.path.join(HERE, "latest_poll.json"), {"observed_at": observed, "leagues": snapshot})
    save_state(state)
    note(f"done: {total_seen} quotes seen, {total_new} new rows written")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-schedules", action="store_true")
    if parser.parse_args().refresh_schedules:
        refresh_schedules()
    main()
