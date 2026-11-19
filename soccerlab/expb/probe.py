"""Minimal authentication / permission / rate-limit probe for the BallDontLie key.

    python -m soccerlab expb probe --schedule-dir <dir with *_2026_matches.json>

Request 1 is the smallest call the logger needs (EPL matches, per_page=1).
If it is not HTTP 200 the probe stops. Otherwise it makes one minimal
`matches` and one `odds` request per league (8 requests total, spaced 13s),
recording only HTTP status, row counts, vendors seen and any rate-limit
response headers. The key is never printed or stored.
"""

import glob
import json
import os
import time
import urllib.error
import urllib.request

from .. import provider
from ..hashing import atomic_write_json
from ..timeutil import iso, parse_utc, utcnow
from . import config as C
from .logger import load_key


def _call(url, key, timeout=30):
    if not url.startswith(provider.BASE + "/"):
        raise ValueError("refusing non-provider URL")
    req = urllib.request.Request(url, headers={"Authorization": key})  # noqa: S310 (https provider base checked)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 (provider base URL only)
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _rate_headers(h):
    return {k: v for k, v in h.items() if "rate" in k.lower() or "limit" in k.lower() or k.lower() == "retry-after"}


def run(schedule_dir, key=None, call=_call, sleep=time.sleep, now=None):
    key = key or load_key()
    if not key:
        return {"result": "no key configured"}
    now = now or utcnow()
    out = {"probed_at": iso(now), "requests": []}

    def go(league, endpoint, params):
        url = provider.url_for(league, endpoint, params)
        if not url.startswith(provider.BASE + "/"):
            raise ValueError("refusing non-provider URL")
        if out["requests"]:
            sleep(C.SPACING_SECONDS)
        t0 = time.monotonic()
        status, headers, body = call(url, key)
        rec = {"league": league, "endpoint": endpoint, "params": params, "status": status,
               "latency_ms": round(1000 * (time.monotonic() - t0)), "rate_headers": _rate_headers(headers)}
        try:
            data = json.loads(body)
            rows = data.get("data") if isinstance(data, dict) else None
            rec["rows"] = len(rows) if isinstance(rows, list) else None
            if endpoint == "odds" and isinstance(rows, list):
                rec["vendors"] = sorted({r.get("vendor") for r in rows})
        except ValueError:
            rec["rows"] = None
            rec["body_not_json"] = True
        out["requests"].append(rec)
        return status

    if go("epl", "matches", {"season": C.SEASON, "per_page": 1}) != 200:
        out["result"] = "STOPPED: smallest request not authorized"
        return out
    for lg in C.LEAGUES:
        if lg != "epl":
            go(lg, "matches", {"season": C.SEASON, "per_page": 1})
        upcoming = []
        for p in glob.glob(os.path.join(str(schedule_dir), f"{lg}_{C.SEASON}_matches.json")):
            with open(p, encoding="utf-8") as f:
                upcoming = sorted((m for m in json.load(f) if m.get("status_state") == "scheduled"
                                   and parse_utc(m["date"]) > now), key=lambda m: m["date"])
        if upcoming:
            go(lg, "odds", {"match_ids[]": [upcoming[0]["id"]], "per_page": 100})
    codes = {r["status"] for r in out["requests"]}
    out["result"] = "OK: all probed endpoints authorized" if codes == {200} else f"PARTIAL: statuses {sorted(codes)}"
    return out


def main(schedule_dir):
    rep = run(schedule_dir)
    from .db import store_dir
    path = store_dir() / f"probe_{rep.get('probed_at', iso(utcnow())).replace(':', '')}.json"
    atomic_write_json(path, rep)
    for r in rep.get("requests", []):
        print(f"  {r['league']:7s} {r['endpoint']:8s} HTTP {r['status']}  rows={r.get('rows')}  "
              f"vendors={r.get('vendors', '-')}  {r['latency_ms']}ms  {r['rate_headers']}")
    print(rep["result"], f"-> {path}")
    return 0 if rep["result"].startswith("OK") else 1
