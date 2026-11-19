"""Shared fixtures. Tests are offline: any attempt to open a network socket fails."""

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def guard(*a, **k):
        raise RuntimeError("network access attempted in an offline test")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    yield


@pytest.fixture(autouse=True)
def _isolated_var(tmp_path, monkeypatch):
    """Research state goes to a temp dir; Telegram creds are scrubbed."""
    monkeypatch.setenv("SOCCERLAB_VAR", str(tmp_path / "var"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    yield


from soccerlab.synthetic import synth_league, write_files  # noqa: E402,F401


@pytest.fixture
def synth_dir(tmp_path):
    return write_files(tmp_path / "synth", synth_league())
