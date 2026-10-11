import logging
import ipaddress
import math
import os
import re
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

#
#

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_config(
    env_name: str | None = None, *, root: Path = PROJECT_ROOT
) -> Dict[str, Any]:
    environment = env_name if env_name is not None else os.environ.get("ENV", "")
    if environment and not re.fullmatch(r"[A-Za-z0-9_-]+", environment):
        raise ValueError(
            "Environment name may contain only letters, numbers, '_' and '-'"
        )

    paths = [root / ".env", root / ".env.local"]
    if environment:
        paths.extend([root / f".env.{environment}", root / f".env.{environment}.local"])

    values: dict[str, str | None] = {}
    for path in paths:
        values.update(dotenv_values(path))
    values.update(os.environ)

    #
    #

    debug_value = (values.get("DEBUG") or "0").strip().lower()
    if debug_value not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
        raise ValueError("DEBUG must be a boolean (0/1, true/false, yes/no, on/off)")
    debug = debug_value in {"1", "true", "yes", "on"}
    log_level = (
        (values.get("LOG_LEVEL") or ("DEBUG" if debug else "INFO")).strip().upper()
    )
    if log_level not in logging.getLevelNamesMapping():
        raise ValueError("Invalid LOG_LEVEL")

    storage_folder = Path(values.get("STORAGE_FOLDER") or "volume").expanduser()
    if not storage_folder.is_absolute():
        storage_folder = root / storage_folder

    #
    #

    config: Dict[str, Any] = {
        "debug": debug,
        "server": {
            "storageFolder": storage_folder.resolve(),
        },
        "logs": {
            "level": log_level,
        },
    }

    def number(name: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(values.get(name) or str(default))
        except ValueError:
            raise ValueError(f"Invalid {name}") from None
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"Invalid {name}")
        return value

    def asset_path(name: str, default: str) -> Path:
        path = Path(values.get(name) or default).expanduser()
        return (path if path.is_absolute() else root / path).resolve()

    timezone_name = values.get("DETECTION_TIMEZONE") or "Europe/Prague"
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Invalid DETECTION_TIMEZONE") from None
    times = {}
    for key, default in (
        ("DETECTION_NIGHT_START", "22:00"),
        ("DETECTION_NIGHT_END", "06:00"),
    ):
        value = values.get(key) or default
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError(f"Invalid {key}")
        times[key] = value
    config["detection"] = {
        "modelPath": asset_path("DETECTION_MODEL_PATH", "system/models/person.onnx"),
        "confidence": number("DETECTION_CONFIDENCE", 0.5, 0.001, 1),
        "dayConfirmSeconds": number("DETECTION_DAY_CONFIRM_SECONDS", 2, 0.01, 3600),
        "nightConfirmSeconds": number("DETECTION_NIGHT_CONFIRM_SECONDS", 3, 0.01, 3600),
        "absenceSeconds": number("DETECTION_ABSENCE_SECONDS", 2, 0.01, 3600),
        "maxFrameAgeSeconds": number("DETECTION_MAX_FRAME_AGE_SECONDS", 2, 0.01, 60),
        "onAlarmSendPhotoEveryMillisecs": number(
            "DETECTION_ON_ALARM_SEND_PHOTO_EVERY_MS", 0, 0, 3600000
        ),
        "timezone": timezone_name,
        "nightStart": times["DETECTION_NIGHT_START"],
        "nightEnd": times["DETECTION_NIGHT_END"],
    }

    #
    #

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        raw = values.get(name)
        try:
            value = int(raw if raw is not None else default)
        except ValueError:
            raise ValueError(f"Invalid {name}") from None
        if not minimum <= value <= maximum:
            raise ValueError(f"Invalid {name}")
        return value

    login = values.get("RTSP_LOGIN", "admin") or ""
    password = values.get("RTSP_PASSWORD") or ""
    host = values.get("RTSP_HOST") or ""
    url_host = host
    if host:
        if ":" in host:
            try:
                url_host = f"[{ipaddress.IPv6Address(host)}]"
            except ValueError:
                raise ValueError("Invalid RTSP_HOST") from None
        elif not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError("Invalid RTSP_HOST")
    port = integer("RTSP_PORT", 554, 1, 65535)
    rtsp_path = values.get("RTSP_PATH", "/ch1/main") or ""
    if not rtsp_path.startswith("/") or any(char in rtsp_path for char in "\r\n\x00#"):
        raise ValueError("Invalid RTSP_PATH")
    config["camera"] = {
        "login": login,
        "password": password,
        "host": host,
        "port": port,
        "path": rtsp_path,
        "rtspUrl": (
            f"rtsp://{quote(login, safe='')}:{quote(password, safe='')}@{url_host}:{port}{rtsp_path}"
            if host and login and password
            else ""
        ),
        "openTimeoutMs": integer("RTSP_OPEN_TIMEOUT_MS", 5000, 1, 60000),
        "readTimeoutMs": integer("RTSP_READ_TIMEOUT_MS", 2000, 1, 60000),
    }

    #
    #

    def names(name: str, default: str) -> list[str]:
        items = [
            item.strip()
            for item in (values.get(name, default) or "").split(",")
            if item.strip()
        ]
        if not items or any(
            not re.fullmatch(r"[A-Za-z0-9_-]+", item) for item in items
        ):
            raise ValueError(f"Invalid {name}")
        return items

    main_language = values.get("AUDIO_MAIN_LANGUAGE", "cz") or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", main_language):
        raise ValueError("Invalid AUDIO_MAIN_LANGUAGE")
    sequence = names(
        "AUDIO_SEQUENCE", "first_warning,countdown_1,countdown_2,countdown_3,alarm"
    )
    if (
        len(sequence) < 2
        or sequence[0] != "first_warning"
        or sequence[-1] != "alarm"
        or len(set(sequence)) != len(sequence)
    ):
        raise ValueError("Invalid AUDIO_SEQUENCE")
    device = values.get("AUDIO_DEVICE", "default") or ""
    if not device or any(char in device for char in "\r\n\x00"):
        raise ValueError("Invalid AUDIO_DEVICE")
    config["audio"] = {
        "soundsFolder": asset_path("AUDIO_SOUNDS_FOLDER", "system/sounds"),
        "mainLanguage": main_language,
        "languages": names("AUDIO_LANGUAGES", "en,cz"),
        "sequence": sequence,
        "device": device,
        "volume": (
            integer("AUDIO_VOLUME", 100, 0, 100)
            if values.get("AUDIO_VOLUME") not in {None, ""}
            else None
        ),
        "maxAlarmSeconds": number("AUDIO_MAX_ALARM_SECONDS", 60, 0.01, 60),
        "maxAlarmCycles": integer("AUDIO_MAX_ALARM_CYCLES", 3, 1, 1000),
    }

    #
    #

    ids = []
    for item in (values.get("TELEGRAM_ALLOWED_USER_IDS") or "").split(","):
        if item.strip():
            try:
                user_id = int(item.strip())
            except ValueError:
                raise ValueError("Invalid TELEGRAM_ALLOWED_USER_IDS") from None
            if user_id <= 0:
                raise ValueError("Invalid TELEGRAM_ALLOWED_USER_IDS")
            ids.append(user_id)
    chat = values.get("TELEGRAM_CHAT_ID") or ""
    try:
        chat_id = int(chat) if chat else None
    except ValueError:
        raise ValueError("Invalid TELEGRAM_CHAT_ID") from None
    if chat_id == 0:
        raise ValueError("Invalid TELEGRAM_CHAT_ID")
    token = values.get("TELEGRAM_BOT_TOKEN") or ""
    if token and (":" not in token or any(char.isspace() for char in token)):
        raise ValueError("Invalid TELEGRAM_BOT_TOKEN")
    config["telegram"] = {
        "token": token,
        "allowedUserIds": list(dict.fromkeys(ids)),
        "notificationChatId": chat_id,
    }

    return config
