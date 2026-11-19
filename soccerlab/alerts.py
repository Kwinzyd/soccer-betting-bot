"""Namespaced, deduplicated alerts. Does not touch the legacy telegram_alerts.py.

Namespaces
  legacy-production   owned by the legacy bot; soccerlab never sends here
  shadow              experimental predictions -- NEVER alerted (hard rule)
  paper               production paper picks; only from a PROMOTED model
  ops                 operational alerts (missing data, failures, staleness)

Transports
  NullTransport       default; drops everything (records nothing is sent)
  RecordingTransport  tests
  TelegramTransport   only built by `from_env()` when SOCCERLAB_ALERTS=1 and a
                      token/chat exist; never constructed in tests (the test
                      suite also blocks sockets).
Every send passes the dedup ledger in the store, so repeated polling cycles
cannot resend the same alert.
"""

import json
import os
import urllib.request
from html import escape

from .hashing import sha256_bytes
from .timeutil import iso, utcnow

SENDABLE = {"paper", "ops"}
OPS_KINDS = {"missing_data", "ingestion_failed", "model_convergence", "settlement_delayed", "odds_expired",
             "inconsistent_result", "auth_failure"}


class AlertRefused(RuntimeError):
    pass


class NullTransport:
    name = "null"

    def send(self, text):
        return True


class RecordingTransport:
    name = "recording"

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class TelegramTransport:
    name = "telegram"

    def __init__(self, token, chat_id):
        self._token, self._chat = token, chat_id

    def send(self, text):
        data = json.dumps({"chat_id": self._chat, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self._token}/sendMessage", data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:      # noqa: S310 (fixed https host)
                return bool(json.loads(r.read()).get("ok"))
        except Exception as exc:                                    # noqa: BLE001
            # never echo the URL: it contains the token
            print(f"[alerts] telegram send failed: {type(exc).__name__}")
            return False


def from_env():
    """Opt-in only. Uses SOCCERLAB_TELEGRAM_* so a research deployment never
    silently reuses the legacy bot's chat."""
    if os.environ.get("SOCCERLAB_ALERTS") != "1":
        return NullTransport()
    tok, chat = os.environ.get("SOCCERLAB_TELEGRAM_TOKEN"), os.environ.get("SOCCERLAB_TELEGRAM_CHAT")
    if not tok or not chat:
        return NullTransport()
    return TelegramTransport(tok, chat)


def format_selection(e, pred, market, warnings=()):
    """All fields the alert contract requires. `market` carries implied and
    de-vigged probabilities, clearly separated."""
    return (
        f"<b>[PAPER ONLY]</b> {escape(e['league'].upper())} · {escape(market['fixture'])}\n"
        f"Market: 1X2 (90 min) · Selection: <b>{escape(e['selection'].upper())}</b>\n"
        f"Kickoff: {e['kickoff']} · Predicted: {e['decision_time']}\n"
        f"Price: {e['decimal_odds']:.2f} @ {escape(e['vendor'])} (quote {e['quote_observed_at']})\n"
        f"Model {escape(e['model_id'])}: p={e['model_prob']:.3f}\n"
        f"Market implied (with margin): {e['implied_prob']:.3f} · de-vigged ({market['devig']}): "
        f"{market['fair_prob']:.3f}\n"
        f"Est. EV {100 * e['ev']:+.1f}% · rule: {escape(e['rule'])} · stake {e['stake']:g}u paper\n"
        f"Data freshness: quote age {market['quote_age_min']:.0f} min"
        + ("".join(f"\n⚠ {escape(w)}" for w in warnings))
        + "\n<i>No edge has been demonstrated; see promotion gates.</i>"
    )


def format_ops(kind, subject, detail):
    if kind not in OPS_KINDS:
        raise ValueError(f"unknown ops alert kind {kind!r}")
    return f"<b>[OPS] {escape(kind)}</b> · {escape(subject)}\n{escape(detail)}"


class Alerter:
    def __init__(self, store, transport=None):
        self.store = store
        self.transport = transport or NullTransport()

    def send(self, namespace, dedup_key, text):
        """Returns 'sent' | 'duplicate' | 'failed'. Raises for forbidden namespaces."""
        if namespace not in SENDABLE:
            raise AlertRefused(f"namespace {namespace!r} may not send alerts")
        if self.store.db.execute("SELECT 1 FROM alerts_sent WHERE namespace=? AND dedup_key=?",
                                 (namespace, dedup_key)).fetchone():
            return "duplicate"
        if not self.transport.send(text):
            return "failed"
        with self.store.tx() as db:
            db.execute("INSERT INTO alerts_sent (namespace, dedup_key, sent_at, transport, body_sha256) "
                       "VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", (namespace, dedup_key, iso(utcnow()), self.transport.name,
                                             sha256_bytes(text.encode())))
        return "sent"

    def selection(self, entry, model_status, pred, market, warnings=()):
        if model_status != "promoted":
            raise AlertRefused(f"{entry['model_id']} is not promoted; selection alerts refused")
        return self.send("paper", f"entry:{entry['entry_uid']}", format_selection(entry, pred, market, warnings))

    def ops(self, kind, subject, detail, day=None):
        key = f"{kind}:{subject}:{day or iso(utcnow())[:10]}"
        return self.send("ops", key, format_ops(kind, subject, detail))
