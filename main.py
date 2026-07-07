"""Entry point: python main.py"""

import logging
import sys

from bot.broker import AlpacaBroker
from bot.config import Settings
from bot.engine import Engine


def setup_logging(log_file: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_file)):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def main() -> int:
    cfg = Settings.load()  # defaults + clamped overrides from tuned_params.json
    setup_logging(cfg.log_file)
    log = logging.getLogger("bot")

    try:
        cfg.validate()
    except ValueError as e:
        log.error("config error: %s", e)
        return 1
    if not cfg.api_key or not cfg.secret_key:
        log.error(
            "No Alpaca keys found. Copy .env.example to .env and add your "
            "PAPER API key/secret (free at https://app.alpaca.markets)."
        )
        return 1

    if cfg.dry_run:
        log.info("DRY_RUN is ON — orders will be logged, not submitted")
    else:
        log.info("DRY_RUN is OFF — orders WILL be submitted to the PAPER account")

    engine = Engine(AlpacaBroker(cfg), cfg)
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        log.info("shutdown requested — exiting cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
