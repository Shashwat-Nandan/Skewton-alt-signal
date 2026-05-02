#!/usr/bin/env python3
"""
Taleb Dynamic Hedger — Main Runner
===================================
Authenticates with Kite, initializes the hedger, and starts the autoresearch loop.
Usage: python run.py
"""

import sys
import logging
from pathlib import Path

# Configure logging
log_dir = Path("./logs")
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "hedger.log"),
    ],
)
logger = logging.getLogger("run")

CONFIG_PATH = "config.ini"


def main():
    # ── Step 1: Authenticate with Kite ──
    logger.info("=" * 60)
    logger.info("TALEB DYNAMIC HEDGER — Starting Up")
    logger.info("=" * 60)

    from kite_auth import KiteAuthManager
    logger.info("[1/3] Authenticating with Zerodha Kite...")
    auth = KiteAuthManager(CONFIG_PATH)
    kite = auth.get_kite()
    profile = kite.profile()
    logger.info("Authenticated as: %s (%s)", profile["user_name"], profile["user_id"])
    logger.info("Exchanges: %s", ", ".join(profile["exchanges"]))

    # ── Step 2: Initialize hedger ──
    from strategies import TalebKarpathyStrategy
    logger.info("[2/3] Initializing Taleb Dynamic Hedger...")
    hedger = TalebKarpathyStrategy(kite, config_path=CONFIG_PATH)
    logger.info("Mode: %s", hedger.mode.upper())
    logger.info("Underlying: %s", hedger.underlying)
    logger.info("Capital: ₹%s", f"{hedger.immutable_params['total_capital']:,.0f}")
    logger.info("Tunable params: %s", hedger.tunable_params)

    # ── Step 3: Run autoresearch loop ──
    from autoresearch_loop import HedgeResearchLoop
    logger.info("[3/3] Starting Autoresearch Loop...")
    loop = HedgeResearchLoop(hedger, config_path=CONFIG_PATH)
    loop.run()  # Runs forever until Ctrl+C


if __name__ == "__main__":
    main()
