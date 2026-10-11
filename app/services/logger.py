import json
import logging
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TextIO
from urllib.parse import quote, unquote

# crit | Critical Conditions. | "socket: Failed to get a socket, exiting child"
# error | Error conditions. | "Premature end of script headers"
# warn | Warning conditions. | "child process 1234 did not exit, sending another SIGHUP"
# info | Informational. | "Server seems busy, (you may need to increase StartServers, or Min/MaxSpareServers)..."
# debug | Debug-level messages | "Opening config file ..."

#
#

LOGGER_NAME = "ai-cam-security"


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        variants = {
            variant
            for secret in secrets
            if secret
            for variant in (secret, unquote(secret), quote(secret, safe=""))
        }
        variants.update(
            json.dumps(value, ensure_ascii=False)[1:-1] for value in tuple(variants)
        )
        self.secrets = sorted(variants, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "service": LOGGER_NAME,
            "message": record.getMessage(),
            "context": getattr(record, "context", {}),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        output = json.dumps(entry, ensure_ascii=False, default=str)
        for secret in self.secrets:
            output = output.replace(secret, "[REDACTED]")
        return output


def configure_logging(
    level: str = "INFO", *, stream: TextIO | None = None, secrets: Sequence[str] = ()
) -> None:
    entity = logging.getLogger(LOGGER_NAME)
    entity.setLevel(level)
    entity.propagate = False

    for handler in entity.handlers[:]:
        entity.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(secrets))
    entity.addHandler(handler)

    root = logging.getLogger()
    for previous in root.handlers[:]:
        root.removeHandler(previous)
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("httpx", "httpcore", "telegram"):
        third_party = logging.getLogger(name)
        for previous in third_party.handlers[:]:
            third_party.removeHandler(previous)
        third_party.propagate = True
        third_party.setLevel(logging.WARNING)


#
#


class Logger:
    def __init__(self, default_extra: Mapping[str, object] | None = None) -> None:
        self.extra = dict(default_extra or {})
        self.entity = logging.getLogger(LOGGER_NAME)

    def process_log(
        self,
        msg_type: str,
        msg: str,
        extra: Mapping[str, object] | None = None,
        *,
        exc_info: bool = False,
    ) -> None:
        self.entity.log(
            logging.getLevelNamesMapping()[msg_type.upper()],
            msg,
            extra={"context": {**self.extra, **(extra or {})}},
            exc_info=exc_info,
        )

    def critical(self, msg: str, extra: Mapping[str, object] | None = None) -> None:
        self.process_log("critical", msg, extra)

    def error(self, msg: str, extra: Mapping[str, object] | None = None) -> None:
        self.process_log("error", msg, extra)

    def warning(self, msg: str, extra: Mapping[str, object] | None = None) -> None:
        self.process_log("warning", msg, extra)

    def info(self, msg: str, extra: Mapping[str, object] | None = None) -> None:
        self.process_log("info", msg, extra)

    def debug(self, msg: str, extra: Mapping[str, object] | None = None) -> None:
        self.process_log("debug", msg, extra)

    def exception(self, msg: str, extra: Mapping[str, object] | None = None) -> None:
        self.process_log("error", msg, extra, exc_info=True)


logger = Logger()
