"""Run the pregame soccer moneyline poll, model, paper ledger and alerts."""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ODDS_LOGGER_DIR = os.path.join(HERE, "odds_logger")
MODEL_DIR = os.path.join(HERE, "model")


def log(msg: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def run_odds_logger():
    """Polls upcoming fixtures for odds changes."""
    log("Polling live sportsbook odds from BallDontLie...")
    cmd = [sys.executable, os.path.join(ODDS_LOGGER_DIR, "log_odds.py"), "--refresh-schedules"]
    subprocess.run(cmd, cwd=ODDS_LOGGER_DIR, check=True)


def run_model_and_alerts(preview=False):
    """Executes the prediction engine and dispatches Telegram alerts."""
    log("Running Dixon-Coles model & checking for value edges...")
    cmd = [sys.executable, os.path.join(MODEL_DIR, "predict.py")]
    if preview:
        cmd.append("--preview")
    subprocess.run(cmd, cwd=MODEL_DIR, check=True)


def run_cycle(preview=False):
    """Runs one full end-to-end intelligence cycle."""
    log("=" * 70)
    log("STARTING VALUE BET ENGINE CYCLE")
    log("=" * 70)

    run_odds_logger()

    # 3. Fit ratings, de-vig lines, calculate Kelly stakes & send Telegram alerts
    run_model_and_alerts(preview)

    log("Cycle complete.\n")


def main():
    parser = argparse.ArgumentParser(description="Value Bets Engine Bot Runner")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously in a loop at regular intervals",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=180,
        help="Interval in minutes between cycles when running in loop mode (default: 180 = 3h)",
    )
    parser.add_argument("--preview", action="store_true", help="Show the card locally without sending alerts")
    parser.add_argument("--performance", action="store_true", help="Settle and summarize paper picks from local results")
    args = parser.parse_args()
    if args.performance:
        sys.path.insert(0, MODEL_DIR)
        import paper
        paper.record([])
        return

    if args.interval <= 0:
        parser.error("--interval must be positive")

    if args.loop:
        log(f"Starting continuous bot runner (Cycle every {args.interval} minutes). Press Ctrl+C to stop.")
        while True:
            try:
                run_cycle(args.preview)
            except Exception as e:
                log(f"Error during cycle: {e}")
            log(f"Sleeping for {args.interval} minutes until next cycle...")
            time.sleep(args.interval * 60)
    else:
        run_cycle(args.preview)


if __name__ == "__main__":
    main()
