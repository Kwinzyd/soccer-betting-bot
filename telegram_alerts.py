"""Telegram Alerting Engine for Value Bets & Odds Intelligence.

Sends real-time betting alerts, line movement notifications, and
settlement summaries directly to your Telegram chat or channel.
"""

from html import escape
import time
import json
import os
import urllib.parse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "telegram_config.json")


def load_config() -> dict:
    """Load token and chat_id from environment or telegram_config.json."""
    config = {
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "enabled": True,
    }

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                if not config["bot_token"]:
                    config["bot_token"] = file_cfg.get("bot_token", "")
                if not config["chat_id"]:
                    config["chat_id"] = str(file_cfg.get("chat_id", ""))
                config["enabled"] = file_cfg.get("enabled", True)
        except Exception:
            pass

    return config


def save_config(bot_token: str, chat_id: str, enabled: bool = True):
    """Save Telegram configuration to telegram_config.json."""
    data = {
        "bot_token": bot_token.strip(),
        "chat_id": str(chat_id).strip(),
        "enabled": enabled,
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Telegram config saved to {CONFIG_PATH}")


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured Telegram chat."""
    cfg = load_config()
    token = cfg.get("bot_token")
    chat_id = cfg.get("chat_id")

    if not cfg.get("enabled"):
        print("[Telegram] Alerts are disabled in config.")
        return False

    if not token or not chat_id:
        print("[Telegram] Bot token or chat ID is missing. Configure via telegram_config.json or environment.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            if res_data.get("ok"):
                return True
            print(f"[Telegram Error] {res_data}")
            return False
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        print(f"[Telegram HTTP {e.code}] {err_msg}")
        return False
    except Exception as e:
        print(f"[Telegram Exception] {e}")
        return False


def format_value_bet(bet: dict) -> str:
    """Format an individual value bet into clean HTML for Telegram."""
    match = bet.get("match", f"{bet.get('home')} vs {bet.get('away')}")
    league = bet.get("league", "").upper()
    selection = bet.get("selection")
    odds = bet.get("odds", "")
    edge = bet.get("edge", 0.0)
    kelly = bet.get("kelly", 0.0)
    promoted = bet.get("promoted", False)

    kickoff = bet.get("kickoff", "")
    time_str = f" ⏱ <i>{kickoff[:10]} {kickoff[11:16]} UTC</i>" if len(kickoff) >= 16 else ""

    warning = "\n⚠️ <i>Involves promoted team (prior-based rating)</i>" if promoted else ""

    return (
        f"⚽ <b>{escape(match)}</b> [{escape(league)}]{time_str}\n"
        f"🎯 Pick: <b>{escape(str(selection))}</b> @ <code>{odds}</code>\n"
        f"📊 Edge: <b>{edge * 100:+.1f}pp</b> | Paper stake: <b>1 unit</b>"
        f"{warning}\n"
    )


def send_value_bets_alert(bets: list[dict], max_per_msg: int = 8) -> bool:
    """Send a batch alert of flagged value bets, chunking to respect Telegram message limits."""
    if not bets:
        return False

    success = True
    total = len(bets)
    chunks = [bets[i:i + max_per_msg] for i in range(0, total, max_per_msg)]

    for idx, chunk in enumerate(chunks, 1):
        header = f"🔥 <b>SOCCER PAPER PICKS ({total})</b> [Part {idx}/{len(chunks)}]\n"
        header += "<i>Model probability exceeds de-vigged bookmaker line:</i>\n\n"
        body = "\n".join(format_value_bet(b) for b in chunk)
        footer = "\n🤖 <i>Value Bets Engine & Odds Logger</i>"
        if not send_message(header + body + footer):
            success = False
        if idx < len(chunks):
            time.sleep(1)

    return success


def send_roster_sync_alert(team_count: int, player_count: int) -> bool:
    """Send notification when daily roster sync completes."""
    msg = (
        f"📋 <b>Active Roster Sync Complete</b>\n"
        f"Synced <b>{team_count}</b> teams across 5 European leagues.\n"
        f"Verified <b>{player_count}</b> active players & live injury statuses.\n"
        f"Stale player props will automatically be filtered."
    )
    return send_message(msg)


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "set":
        if len(sys.argv) < 4:
            print("Usage: python telegram_alerts.py set <BOT_TOKEN> <CHAT_ID>")
            sys.exit(1)
        save_config(sys.argv[2], sys.argv[3])
    elif cmd == "test":
        print("Testing Telegram connection...")
        success = send_message("Soccer Value Bets Engine is successfully connected to Telegram!")
        if success:
            print("[OK] Test message delivered successfully!")
        else:
            print("[FAIL] Failed to deliver test message. Check token and chat_id in telegram_config.json.")
    else:
        print("Telegram Alerts CLI")
        print("  python telegram_alerts.py set <BOT_TOKEN> <CHAT_ID>  - Save credentials")
        print("  python telegram_alerts.py test                       - Send a test alert")
