"""Guards on the CI workflow: offline tests only, no secrets, no deployment."""

import re

from soccerlab.paths import REPO_ROOT

CI = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
RUNS = "\n".join(re.findall(r"run:\s*(.+)", CI))


def test_ci_uses_no_repository_secrets():
    assert "secrets." not in CI


def test_ci_never_deploys_or_touches_live_services():
    for forbidden in ("-Install", "Register-ScheduledTask", "schtasks", "expb run", "expb probe", "expb backup",
                      "soccerlab shadow", "soccerlab paper", "bot.py", "log_odds", "telegram_alerts", "git push"):
        assert forbidden not in RUNS, forbidden


def test_ci_uses_dummy_key_and_disables_alerts():
    assert "BALLDONTLIE_KEY: ci-dummy-not-a-key" in CI and 'SOCCERLAB_ALERTS: "0"' in CI


def test_ci_runs_history_secret_scan_and_offline_tests():
    assert "soccerlab audit --history" in RUNS and "pytest" in RUNS
    assert "fetch-depth: 0" in CI


def test_ci_permissions_read_only():
    assert re.search(r"permissions:\s*\n\s*contents:\s*read", CI)


def test_offline_guard_is_autouse():
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "@pytest.fixture(autouse=True)\ndef _no_network" in conftest
