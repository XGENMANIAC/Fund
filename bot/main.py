"""
Main entry point — runs the meme coin detection bot.

DISCLAIMER: This is part of an educational system for Solana devnet only.
Use exclusively on Solana devnet/testnet.
Do NOT use on mainnet without full legal and compliance review.

Usage:
  # Monitor only (safe, no deployments):
  python -m bot.main --mode monitor-only

  # Detect + score (no deployments):
  python -m bot.main --mode analyze-only

  # Full pipeline with approval gate:
  python -m bot.main --mode full --network devnet

  # Mainnet (requires YES_MAINNET confirmation at deploy time):
  python -m bot.main --mode full --network mainnet

  # Run once and exit:
  python -m bot.main --mode full --once

  # With custom config:
  python -m bot.main --config /path/to/config.yaml
"""

import argparse
import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from bot.pipeline import BotPipeline
from storage.database import initialize_db
from utils.helpers import ensure_data_dir, load_env, load_config
from utils.logger import get_logger, log_banner

logger = get_logger(__name__)

NETWORK = os.getenv("NETWORK", "devnet").lower()

BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║   SOLANA MEME COIN EDUCATIONAL DETECTION SYSTEM         ║
║   ⚠️  DEVNET ONLY — FOR RESEARCH PURPOSES ONLY          ║
║   No real funds. No mainnet. Educational use only.      ║
╚══════════════════════════════════════════════════════════╝
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Solana Meme Coin Educational Detection Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m bot.main --mode monitor-only
  python -m bot.main --mode full --network devnet --once
  python -m bot.main --mode full --network mainnet
  python -m bot.main --mode analyze-only --interval 30
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["monitor-only", "analyze-only", "full"],
        default="monitor-only",
        help="Pipeline mode (default: monitor-only — safest)",
    )
    parser.add_argument(
        "--network",
        choices=["devnet", "testnet", "mainnet"],
        default=NETWORK,
        help="Solana network (default: NETWORK env var or devnet)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (useful for testing)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Seconds between cycles (overrides config poll_interval)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to config YAML file",
    )
    return parser.parse_args()


async def run_bot(args: argparse.Namespace) -> None:
    """Main async bot loop."""
    # Propagate --network flag to env so subprocesses and modules pick it up
    os.environ["NETWORK"] = args.network

    load_env()
    ensure_data_dir()
    initialize_db()

    cfg = load_config()
    poll_interval = args.interval or cfg.get("monitor", {}).get("poll_interval", 60)

    print(BANNER)
    logger.info(f"Mode: {args.mode} | Network: {args.network} | Interval: {poll_interval}s")
    logger.info(f"DB: {os.getenv('DB_PATH', 'data/meme_detector.db')}")

    if args.mode == "full":
        logger.info("Full pipeline mode — APPROVAL GATE IS ACTIVE")
        logger.info("You will be prompted to approve each deployment.")
        if args.network == "mainnet":
            logger.warning("MAINNET MODE — deployments use real SOL. Handle with care.")

    pipeline = BotPipeline(mode=args.mode)
    cycle = 0
    running = True

    def handle_signal(sig, frame):
        nonlocal running
        logger.info(f"Received signal {sig} — shutting down after current cycle")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        cycle += 1
        cycle_start = time.monotonic()

        try:
            result = await pipeline.run_cycle()
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            break
        except Exception as exc:
            logger.exception(f"Cycle {cycle} crashed: {exc}")

        if args.once:
            logger.info("--once flag set, exiting after first cycle")
            break

        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0, poll_interval - elapsed)

        if running and sleep_time > 0:
            logger.info(
                f"Sleeping {sleep_time:.0f}s before next cycle "
                f"(next cycle at T+{poll_interval}s from start)"
            )
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break

    logger.info("Bot shut down cleanly.")


def main():
    args = parse_args()
    try:
        asyncio.run(run_bot(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
