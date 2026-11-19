"""Static repository audit: secrets, isolation and execution-path checks.

    python -m soccerlab audit
"""

import re
import subprocess

from .paths import REPO_ROOT

SECRET_PATTERNS = {
    "telegram bot token": re.compile(rb"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "bearer/api key assignment": re.compile(rb"(?i)(api[_-]?key|authorization|token)\s*[=:]\s*['\"][A-Za-z0-9_\-]{24,}['\"]"),
    "uuid-like key literal": re.compile(rb"['\"][0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}['\"]"),
}
MUST_BE_UNTRACKED = ["telegram_config.json", "odds_logger/paper.sqlite3", "odds_logger/seen.json"]
# hosts soccerlab talks to, plus documented provider hosts that appear only as
# descriptive strings in manifests
ALLOWED_HOSTS = {"api.balldontlie.io", "api.telegram.org", "v3.football.api-sports.io",
                 "www.football-data.co.uk"}   # last: manual CSV download URL in docs only; never fetched by code
EXECUTION_WORDS = re.compile(r"(?i)\b(place_?bet|submit_?order|execute_?wager|real_?money)\b")


def tracked():
    return subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True,
                          text=True).stdout.splitlines()


def history_problems():
    """Scan every commit reachable from any ref (not just the work tree)."""
    revs = subprocess.run(["git", "rev-list", "--all"], cwd=REPO_ROOT, check=True, capture_output=True,
                          text=True).stdout.split()
    problems = []
    names = subprocess.run(["git", "log", "--all", "--name-only", "--format="], cwd=REPO_ROOT, check=True,
                           capture_output=True, text=True).stdout.splitlines()
    for f in sorted(set(names)):
        if f in MUST_BE_UNTRACKED or f.endswith((".key", ".env")):
            problems.append(f"history: secret-bearing file {f} was committed")
    for label, pat in (("telegram bot token", r"[0-9]{8,10}:[A-Za-z0-9_-]{35}"),
                       ("uuid-like key literal", r"['\"][0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}['\"]")):
        r = subprocess.run(["git", "grep", "-I", "-l", "-E", pat, *revs], cwd=REPO_ROOT, capture_output=True, text=True)
        for hit in sorted(set(r.stdout.splitlines()))[:20]:
            path = hit.split(":", 1)[1] if ":" in hit else hit
            if not path.startswith(("balldontlie_", "fixtures/", "xg/", "rosters/", "odds_hist/")):
                problems.append(f"history: possible {label} in {hit}")
    return problems, len(revs)


def main(history=False):
    files = tracked()
    problems, notes = [], []
    for f in files:
        if not f.endswith((".py", ".ps1", ".md", ".json", ".toml", ".yml", ".yaml", ".txt")):
            continue
        if f.startswith(("balldontlie_", "fixtures/", "xg/", "rosters/", "odds_hist/")) or f.endswith("_matches.json"):
            continue
        data = (REPO_ROOT / f).read_bytes() if (REPO_ROOT / f).exists() else b""
        for name, pat in SECRET_PATTERNS.items():
            if pat.search(data):
                problems.append(f"possible {name} in tracked file {f}")
    for f in MUST_BE_UNTRACKED:
        if f in files:
            problems.append(f"{f} is tracked by git")
    for f in files:
        if f.startswith("soccerlab/") and f.endswith(".py"):
            src = (REPO_ROOT / f).read_text(encoding="utf-8")
            for host in re.findall(r"https?://([a-zA-Z0-9.\-]+)", src):
                if host not in ALLOWED_HOSTS:
                    problems.append(f"{f}: unexpected outbound host {host}")
            if f != "soccerlab/audit.py" and EXECUTION_WORDS.search(src):
                problems.append(f"{f}: mentions a real-money execution primitive")
    notes.append(f"scanned {len(files)} tracked files")
    if history:
        hp, n = history_problems()
        problems += hp
        notes.append(f"scanned {n} commits across all refs")
    for n in notes:
        print(f"  {n}")
    for p in problems:
        print(f"  FAIL {p}")
    print("AUDIT", "PASSED" if not problems else "FAILED")
    return 0 if not problems else 1
