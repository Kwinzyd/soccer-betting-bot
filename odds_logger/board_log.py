"""Ingest and settle pasted sportsbook boards.

The automated logger (log_odds.py) can only capture 1X2, because that is all
the balldontlie odds endpoint exposes. Corners, cards, offsides and the rest
exist only on the book. This ingests the TSV boards by hand so that those
markets accumulate a price history too -- which is the only route to ever
testing the counting-market model, since no public archive of these lines
exists.

  python board_log.py ingest board.tsv        parse and append
  python board_log.py settle                  settle what has finished
  python board_log.py summary                 what has accumulated

Settlement note: card and corner results come from the stats feed, which
undercounts cards relative to a book that also counts managers and bench
staff -- measured at about +0.115 per match across 1,445 matches. Any bet
whose outcome sits within that margin of the line is reported as UNCERTAIN
rather than silently graded, because grading it either way would quietly
corrupt the very dataset this exists to build.
"""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONS = os.path.join(os.path.dirname(HERE), "balldontlie_consolidated")
BOARD_LOG = os.path.join(HERE, "board_log.jsonl")

LEAGUES = ["epl", "laliga", "seriea", "ligue1"]
SEASON = 2026
FEED_CARD_GAP = 0.115      # stats feed vs book-style counting, measured

SUFFIXES = (" fc", " afc", " cf", " ac", " sc", " calcio", " 1907", " cfc",
            " ssc", " rc", " rcd", " club")


def norm(name):
    """Loose team-name key: strips club suffixes, accents, punctuation."""
    import unicodedata
    s = unicodedata.normalize("NFD", name or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"[^a-z0-9 &]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(3):
        for suf in SUFFIXES:
            if s.endswith(suf):
                s = s[: -len(suf)].strip()
    return s


def parse_odds(tok):
    tok = (tok or "").strip().replace("−", "-")
    m = re.fullmatch(r"([+-]?\d+)", tok)
    return int(m.group(1)) if m else None


def parse_board(path):
    """Rows of (match, market, selection, odds). Tolerates tabs or 2+ spaces."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            ln = raw.rstrip("\n")
            if not ln.strip():
                continue
            parts = ln.split("\t")
            if len(parts) < 4:
                parts = re.split(r"\s{2,}", ln.strip())
            if len(parts) < 4:
                # last resort: odds is the final token, match contains " vs "
                m = re.match(r"^(.*? vs .*?)\s+(.*)\s+([+-]\d+)$", ln.strip())
                if not m:
                    continue
                parts = [m.group(1), m.group(2), "", m.group(3)]
            match, market, selection, odds = (parts[0].strip(), parts[1].strip(),
                                              parts[2].strip(), parts[-1].strip())
            if match.lower().startswith("match") and market.lower() == "market":
                continue
            o = parse_odds(odds)
            if o is None or " vs " not in match.lower():
                continue
            out.append({"match": match, "market": market,
                        "selection": selection, "odds": o})
    return out


def load_fixtures():
    """(normalised home, normalised away) -> fixture info."""
    idx = {}
    for lg in LEAGUES:
        mp = os.path.join(CONS, f"{lg}_{SEASON}_matches.json")
        tp = os.path.join(CONS, f"{lg}_{SEASON}_teams.json")
        if not (os.path.exists(mp) and os.path.exists(tp)):
            continue
        with open(tp, encoding="utf-8") as f:
            tn = {t["id"]: t["name"] for t in json.load(f)}
        with open(mp, encoding="utf-8") as f:
            for m in json.load(f):
                h, a = tn.get(m["home_team_id"]), tn.get(m["away_team_id"])
                if h and a:
                    idx[(norm(h), norm(a))] = {
                        "match_id": m["id"], "league": lg, "date": m["date"],
                        "home": h, "away": a,
                        "status": m.get("status_state"),
                        "hs": m.get("home_score"), "as": m.get("away_score"),
                    }
    return idx


def match_fixture(label, idx):
    parts = re.split(r"\s+vs\.?\s+", label, flags=re.I)
    if len(parts) != 2:
        return None
    h, a = norm(parts[0]), norm(parts[1])
    if (h, a) in idx:
        return idx[(h, a)]
    for (kh, ka), v in idx.items():         # substring fallback
        if (h in kh or kh in h) and (a in ka or ka in a):
            return v
    return None


def cmd_ingest(path):
    rows = parse_board(path)
    idx = load_fixtures()
    now = datetime.now(timezone.utc).isoformat()

    seen = set()
    if os.path.exists(BOARD_LOG):
        with open(BOARD_LOG, encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                seen.add((r["match"], r["market"], r["selection"], r["odds"]))

    matched = unmatched = new = 0
    unknown = set()
    with open(BOARD_LOG, "a", encoding="utf-8") as f:
        for r in rows:
            fx = match_fixture(r["match"], idx)
            if fx:
                matched += 1
            else:
                unmatched += 1
                unknown.add(r["match"])
            key = (r["match"], r["market"], r["selection"], r["odds"])
            if key in seen:
                continue          # identical price already on file
            seen.add(key)
            f.write(json.dumps({
                "observed_at": now, "match": r["match"], "market": r["market"],
                "selection": r["selection"], "odds": r["odds"],
                "match_id": fx["match_id"] if fx else None,
                "league": fx["league"] if fx else None,
                "kickoff": fx["date"] if fx else None,
            }, separators=(",", ":")) + "\n")
            new += 1

    print(f"parsed {len(rows)} rows | fixture matched {matched}, unmatched {unmatched}")
    print(f"appended {new} new price rows (identical prices skipped)")
    if unknown:
        print("\nunmatched fixtures (not in the four leagues, or name mismatch):")
        for u in sorted(unknown)[:15]:
            print(f"  {u}")


def load_stats():
    """match_id -> per-team stat rows, for settling counting markets."""
    out = defaultdict(list)
    for lg in LEAGUES:
        p = os.path.join(CONS, f"{lg}_{SEASON}_team_match_stats.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for r in json.load(f):
                    out[r["match_id"]].append(r)
    return out


TOTAL_RE = re.compile(r"(over|under)\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def cmd_settle():
    if not os.path.exists(BOARD_LOG):
        print("nothing ingested yet")
        return
    idx = load_fixtures()
    stats = load_stats()
    by_id = {v["match_id"]: v for v in idx.values()}

    graded = uncertain = pending = 0
    lines = []
    with open(BOARD_LOG, encoding="utf-8") as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            fx = by_id.get(r.get("match_id"))
            if not fx or fx.get("status") != "final" or fx.get("hs") is None:
                pending += 1
                continue
            st = stats.get(r["match_id"])
            m = TOTAL_RE.search(r["selection"] or "")
            mk = (r["market"] or "").lower()

            actual = None
            margin = 0.0
            if st and len(st) == 2:
                if "corner" in mk:
                    vals = [s.get("corners") for s in st]
                    actual = sum(v for v in vals if v is not None) if all(
                        v is not None for v in vals) else None
                elif "booking" in mk or "card" in mk:
                    y = [s.get("yellow_cards") for s in st]
                    rd = [s.get("red_cards") for s in st]
                    if all(v is not None for v in y + rd):
                        actual = sum(y) + 2.2 * sum(rd)
                        margin = FEED_CARD_GAP
                elif "offside" in mk:
                    vals = [s.get("offsides") for s in st]
                    actual = sum(v for v in vals if v is not None) if all(
                        v is not None for v in vals) else None

            if actual is None or not m:
                pending += 1
                continue

            side, line_val = m.group(1).lower(), float(m.group(2))
            if abs(actual - line_val) <= margin:
                uncertain += 1
                lines.append(f"  UNCERTAIN  {fx['home']} v {fx['away']}  "
                             f"{r['market']} {r['selection']}  actual {actual:.2f} "
                             f"vs line {line_val} (inside the {margin:.3f} feed gap)")
                continue
            won = (actual > line_val) if side == "over" else (actual < line_val)
            graded += 1
            lines.append(f"  {'WON ' if won else 'LOST'}       {fx['home']} v "
                         f"{fx['away']}  {r['market']} {r['selection']} @ {r['odds']}"
                         f"  actual {actual:.2f}")

    for l in lines[:40]:
        print(l)
    print(f"\ngraded {graded} | uncertain {uncertain} | pending {pending}")


def cmd_summary():
    if not os.path.exists(BOARD_LOG):
        print("nothing ingested yet")
        return
    rows = []
    with open(BOARD_LOG, encoding="utf-8") as f:
        for ln in f:
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    if not rows:
        print("empty")
        return
    mk = defaultdict(int)
    for r in rows:
        mk[r["market"]] += 1
    obs = sorted(r["observed_at"] for r in rows)
    print(f"  price rows      {len(rows)}")
    print(f"  fixtures        {len({r['match'] for r in rows})}")
    print(f"  linked to data  {sum(1 for r in rows if r.get('match_id'))}")
    print(f"  first / last    {obs[0][:19]}  ->  {obs[-1][:19]}")
    print("  top markets:")
    for k, v in sorted(mk.items(), key=lambda x: -x[1])[:12]:
        print(f"    {k[:44]:44s} {v}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "ingest":
        if len(sys.argv) < 3:
            sys.exit("usage: board_log.py ingest <file.tsv>")
        cmd_ingest(sys.argv[2])
    elif cmd == "settle":
        cmd_settle()
    else:
        cmd_summary()
