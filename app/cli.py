import argparse
import asyncio
import signal
from collections.abc import Sequence
from typing import Any, Dict

from app.config._ import load_config
from app.runtime import Runtime, check_readiness
from app.services.logger import configure_logging, logger

#
#


async def main(config: Dict[str, Any]) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    previous_handlers = {}

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(
            signum, lambda _signum, _frame: loop.call_soon_threadsafe(stop.set)
        )

    try:
        logger.info(
            "Service started", {"storage_folder": config["server"]["storageFolder"]}
        )

        await Runtime(config).run(stop)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        logger.info("Service stopped")


#
#


def start(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="RTSP camera AI and Telegram alarm service"
    )

    parser.add_argument("-e", "--env", help="Environment name, for example dev or prod")
    parser.add_argument(
        "--check", action="store_true", help="Check startup configuration and exit"
    )
    parser.add_argument(
        "--check-ready",
        action="store_true",
        help="Check configured model, audio, credentials and backends offline",
    )

    args = parser.parse_args(argv)

    try:
        config = load_config(args.env)
    except ValueError as error:
        parser.error(str(error))

    configure_logging(
        config["logs"]["level"],
        secrets=[
            config["camera"]["rtspUrl"],
            config["camera"]["login"],
            config["camera"]["password"],
            config["telegram"]["token"],
        ],
    )
    try:
        config["server"]["storageFolder"].mkdir(parents=True, exist_ok=True)

        if args.check_ready:
            errors = check_readiness(config)

            if errors:
                logger.error("Readiness check failed", {"errors": errors})
                raise SystemExit(1)

            logger.info(
                "Offline readiness check passed; live hardware verification still required"
            )

            return
        if args.check:
            logger.info(
                "Startup check passed",
                {
                    "storage_folder": config["server"]["storageFolder"],
                    "mode": "disarmed",
                    "camera_configured": bool(config["camera"]["rtspUrl"]),
                    "telegram_configured": bool(config["telegram"]["token"]),
                },
            )
            return

        asyncio.run(main(config))
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Service failed")
        raise SystemExit(1)
