"""Entry point: python main.py"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from bot import lock
from bot.broker import AlpacaBroker
from bot.config import Settings
from bot.engine import Engine

_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
_LOG_BACKUP_COUNT = 5              # keep 5 rotated files (50 MB total)


def setup_logging(log_file: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT
    )
    for handler in (logging.StreamHandler(sys.stdout), file_handler):
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

    if not lock.acquire(cfg.lock_file):
        log.error(
            "another engine instance is already running (pid %s, lock %s) — "
            "refusing to start a second one against the same account",
            lock.read_pid(cfg.lock_file), cfg.lock_file,
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
    finally:
        lock.release(cfg.lock_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
