import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Dict

import cv2

from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.services.logger import logger
from app.services.models import Frame, Notification

#
#

COMMANDS = {
    "status": "Show service status",
    "photo": "Get a current camera photo",
    "arm": "Enable automatic detection",
    "disarm": "Disable automatic detection and stop playback",
    "alarm": "Start the manual siren",
    "stop": "Stop playback, keeping armed mode",
    "help": "Show commands",
}
COMMAND_TITLES = {
    "status": "📊 Service status",
    "photo": "📷 Camera photo",
    "arm": "✅ Arm protection",
    "disarm": "❌ Disarm protection",
    "alarm": "🚨 Start siren",
    "stop": "⏹️ Stop playback",
    "help": "❓ Help",
}
BUTTON_COMMANDS = {title: name for name, title in COMMAND_TITLES.items()}
HELP = "<b>❓ Camera alarm commands</b>\n\n" + "\n\n".join(
    f"<b>/{name}</b>\n{description}" for name, description in COMMANDS.items()
)
COMMAND_KEYBOARD = ReplyKeyboardMarkup(
    [
        [COMMAND_TITLES[name] for name in tuple(COMMANDS)[index:index + 2]]
        for index in range(0, len(COMMANDS), 2)
    ],
    resize_keyboard=True,
    is_persistent=True,
)


class TransportError(Exception):
    def __init__(
        self, reason: str, *, permanent: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(reason)
        self.permanent = permanent
        self.retry_after = retry_after


class TelegramService:
    def __init__(
        self,
        config: Dict[str, Any],
        execute: Callable[[str], Awaitable[str]],
        get_status: Callable[[], Dict[str, Any]],
        get_frame: Callable[[], Awaitable[Frame | None]],
    ) -> None:
        self.settings = config["telegram"]
        self.execute = execute
        self.get_status = get_status
        self.get_frame = get_frame
        self._application: Any = None
        self._health = "unconfigured" if not self.settings["token"] else "offline"
        self._cutoff = datetime.now(timezone.utc)
        self._started_at = self._cutoff
        self._startup_pending = self.settings["notificationChatId"] is not None
        self._commands_registered = False
        self._seen: set[int] = set()
        self._order: deque[int] = deque()
        self._reconnect = asyncio.Event()
        self._invalid_token = False
        self._secrets = [
            self.settings["token"],
            config["camera"]["rtspUrl"],
            config["camera"]["password"],
        ]

    def health(self) -> str:
        return self._health

    def _text(self, value: Any, limit: int = 200) -> str:
        text = str(value)

        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")

        if len(text) > limit:
            text = text[:limit - 1] + "…"

        return escape(text)

    def _label(self, value: str) -> str:
        icons = {
            "healthy": "✅ ", "ready": "✅ ", "idle": "💤 ",
            "confirming": "🔎 ", "warning": "⚠️ ", "countdown": "⏳ ",
            "alarm": "🚨 ", "day": "☀️ ", "night": "🌙 ",
            "unavailable": "⚠️ ", "camera_unavailable": "⚠️ ",
            "detector_unavailable": "⚠️ ", "audio_fault": "⚠️ ",
            "offline": "⚠️ ", "transport_error": "⚠️ ",
            "invalid_credentials": "⚠️ ", "cleanup_failed": "⚠️ ",
            "unconfigured": "⚙️ ", "unchecked": "⏳ ",
        }

        return icons.get(value, "") + self._text(value).replace("_", " ").capitalize()

    def _status_reply(self) -> str:
        status = self.get_status()
        notifications = status["notifications"]
        suppression = {
            "": "None",
            "until_absence": "Waiting for the person to leave",
            "audio_fault": "Audio fault — automatic alarms paused",
        }.get(status["suppressionReason"], status["suppressionReason"])
        last_command = status["lastCommandResult"] or "None yet"

        if last_command in COMMANDS:
            last_command = f"/{last_command} completed"

        reply = (
            "<b>📊 Camera alarm status</b>\n\n"
            "<b>🛡️ Protection</b>\n"
            f"Automatic detection: <b>{'✅ Armed' if status['armed'] else '❌ Disarmed'}</b>\n"
            f"Phase: {self._label(status['phase'])}\n"
            f"Manual mode: {'✅ On' if status['manual'] else '❌ Off'}\n"
            f"Profile: {self._label(status['profile'])}\n"
            f"Siren periods used: {self._text(status['alarmCycles'])} / {self._text(status['maxAlarmCycles'])}\n"
            f"Suppression: {self._text(suppression)}\n\n"
            "<b>🩺 System health</b>\n"
            f"Camera: {self._label(status['camera'])}\n"
            f"Detector: {self._label(status['detector'])}\n"
            f"Audio: {self._label(status['audio'])}\n"
            f"Telegram: {self._label(status['telegram'])}\n\n"
            "<b>🔔 Notifications</b>\n"
            f"Queued: {notifications['queued_count']}\n"
            f"Dropped: {notifications['dropped_count']}\n"
            f"Unreported drops: {notifications['unreported_drops']}\n"
            f"Image memory: {notifications['retained_image_bytes'] / (1024 * 1024):.1f} MiB\n\n"
            f"<b>📝 Last command</b>\n{self._text(last_command)}"
        )

        if status["suppressionReason"] == "audio_fault":
            reply += "\n\n<i>Check the speaker, then use /arm. A successful /arm resumes automatic alarms.</i>"
        elif status["suppression"] and status["armed"]:
            reply += "\n\n<i>Automatic alarms resume after confirmed absence, or a successful /arm.</i>"

        return reply

    def _command_reply(self, name: str, result: str) -> str:
        if result != name:
            return f"<b>⚠️ /{name} — command not completed</b>\n\n{self._text(result, 1000)}\n\n<i>Use /status to check the service.</i>"

        if name == "stop":
            armed = self.get_status()["armed"]

            return (
                "<b>⏹️ Playback stopped</b>\n\n"
                f"{'✅' if armed else '❌'} Automatic detection remains {'armed' if armed else 'disarmed'}.\n"
                "<i>Use /status to check protection and suppression.</i>"
            )

        replies = {
            "arm": "<b>✅ Protection armed</b>\n\nAutomatic person detection is enabled.\n<i>Use /disarm to disable protection.</i>",
            "disarm": "<b>❌ Protection disarmed</b>\n\nAutomatic person detection is disabled. Playback stopped.\n<i>Use /arm to enable protection.</i>",
            "alarm": "<b>🚨 Manual siren requested</b>\n\n<i>Use /stop to stop playback.</i>",
        }

        return replies[name]

    def _build(self) -> Any:
        application = (
            Application.builder()
            .token(self.settings["token"])
            .concurrent_updates(8)
            .connect_timeout(5)
            .read_timeout(5)
            .write_timeout(5)
            .pool_timeout(5)
            .get_updates_connect_timeout(5)
            .get_updates_read_timeout(5)
            .build()
        )

        for name in COMMANDS:
            application.add_handler(CommandHandler(name, self._handle))

        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle))

        application.add_error_handler(self._handler_error)

        return application

    async def _handler_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._health = "transport_error"

    def _poll_error(self, error: TelegramError) -> None:
        self._invalid_token = isinstance(error, (InvalidToken, Forbidden))
        self._health = "invalid_credentials" if self._invalid_token else "offline"
        self._reconnect.set()

    async def _handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, chat, message = (
            update.effective_user,
            update.effective_chat,
            update.message,
        )

        if self._reconnect.is_set():
            return

        if (
            user is None
            or user.id not in self.settings["allowedUserIds"]
            or chat is None
            or chat.type != "private"
            or message is None
            or update.edited_message is not None
        ):
            return

        now = datetime.now(timezone.utc)

        if (
            message.date < self._cutoff
            or not -5 <= (now - message.date).total_seconds() <= 30
            or update.update_id in self._seen
        ):
            return

        text = message.text or ""
        name = BUTTON_COMMANDS.get(text, "")

        if not name and text.startswith("/"):
            name = text.split(" ", 1)[0].split("@", 1)[0].removeprefix("/").lower()

        if name not in COMMANDS:
            return

        self._seen.add(update.update_id)
        self._order.append(update.update_id)

        if len(self._order) > 1024:
            self._seen.remove(self._order.popleft())

        photo = None

        if name == "help":
            reply = HELP
        elif name == "status":
            reply = self._status_reply()
        elif name == "photo":
            frame = None

            try:
                frame = await asyncio.wait_for(self.get_frame(), 5)

                if frame is not None:
                    ok, encoded = await asyncio.wait_for(
                        asyncio.to_thread(
                            cv2.imencode, ".jpg", frame.image,
                            [cv2.IMWRITE_JPEG_QUALITY, 80],
                        ),
                        5,
                    )

                    if ok:
                        photo = encoded.tobytes()
            except (cv2.error, asyncio.TimeoutError):
                pass

            reply = (
                f"<b>📷 Camera photo</b>\n\nTime (UTC): {frame.captured_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                if photo is not None and frame is not None
                else "<b>⚠️ Photo unavailable</b>\n\nNo fresh camera photo could be obtained.\n<i>Use /status to check the camera.</i>"
            )
        else:
            result = await self.execute(name)
            reply = self._command_reply(name, result)

        try:
            if photo is not None:
                await asyncio.wait_for(
                    message.reply_photo(
                        photo=photo,
                        caption=reply,
                        parse_mode=ParseMode.HTML,
                        reply_markup=COMMAND_KEYBOARD,
                        read_timeout=5,
                        write_timeout=5,
                        connect_timeout=5,
                        pool_timeout=5,
                    ),
                    5,
                )
            else:
                await asyncio.wait_for(
                    message.reply_text(
                        reply,
                        parse_mode=ParseMode.HTML,
                        reply_markup=COMMAND_KEYBOARD,
                        read_timeout=5,
                        write_timeout=5,
                        connect_timeout=5,
                        pool_timeout=5,
                    ),
                    5,
                )
        except (TelegramError, asyncio.TimeoutError):
            self._health = "offline"
        else:
            if not self._reconnect.is_set():
                self._health = "healthy"

    async def _announce_startup(self) -> None:
        notification = Notification(
            0,
            "startup",
            time.monotonic(),
            "<b>🚀 Camera alarm service started</b>\n\n"
            f"Started at (UTC): {self._started_at.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            "<b>❌ Started disarmed</b>\n\n"
            "<i>Use /status to check readiness, then /arm to enable automatic detection.</i>",
            None,
        )

        try:
            await asyncio.wait_for(self.send(notification), 5)
        except TransportError as error:
            if not error.permanent:
                raise

            logger.warning("Startup notification rejected", {"reason": str(error)})

        self._startup_pending = False

    async def _cleanup(self, application: Any) -> None:
        async with asyncio.timeout(5):
            try:
                if application.updater.running:
                    await application.updater.stop()
            finally:
                try:
                    if application.running:
                        await application.stop()
                finally:
                    try:
                        await application.shutdown()
                    finally:
                        await application.bot.shutdown()

    async def run(self, stop: asyncio.Event) -> None:
        if not self.settings["token"]:
            await stop.wait()

            return

        delay = 1.0

        while not stop.is_set():
            application = None

            try:
                self._reconnect.clear()
                application = self._build()

                self._application = application

                await application.initialize()

                if not self._commands_registered:
                    await asyncio.wait_for(
                        application.bot.set_my_commands(
                            [
                                BotCommand(name, description)
                                for name, description in COMMANDS.items()
                            ],
                            read_timeout=5,
                            write_timeout=5,
                            connect_timeout=5,
                            pool_timeout=5,
                        ),
                        5,
                    )
                    self._commands_registered = True

                await application.start()

                self._cutoff = datetime.now(timezone.utc)

                await application.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=["message"],
                    error_callback=self._poll_error,
                    bootstrap_retries=0,
                    timeout=5,
                )

                self._health = "healthy"

                if self._startup_pending:
                    await self._announce_startup()

                stop_wait = asyncio.create_task(stop.wait())
                reconnect_wait = asyncio.create_task(self._reconnect.wait())

                try:
                    await asyncio.wait(
                        {stop_wait, reconnect_wait}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    stop_wait.cancel()
                    reconnect_wait.cancel()
                    await asyncio.gather(
                        stop_wait, reconnect_wait, return_exceptions=True
                    )
            except (InvalidToken, Forbidden):
                self._invalid_token = True
                self._health = "invalid_credentials"
            except RetryAfter as error:
                self._health = "offline"
                delay = (
                    error.retry_after.total_seconds()
                    if isinstance(error.retry_after, timedelta)
                    else float(error.retry_after)
                )

            except TransportError as error:
                self._health = "offline"

                if error.retry_after is not None:
                    delay = max(delay, error.retry_after)

            except (TelegramError, OSError, asyncio.TimeoutError):
                self._health = "offline"
            finally:
                self._application = None

                if application is not None:
                    try:
                        await self._cleanup(application)
                    except (TelegramError, OSError, RuntimeError, asyncio.TimeoutError):
                        self._health = "cleanup_failed"

            if self._invalid_token:
                self._health = "invalid_credentials"
                await stop.wait()

                return

            if not stop.is_set():
                self._health = "offline"

                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except asyncio.TimeoutError:
                    pass

                delay = min(delay * 2, 10)

    async def send(self, notification: Notification) -> None:
        if (
            self._application is None
            or not self._application.running
            or self._invalid_token
        ):
            raise TransportError("telegram_unavailable", permanent=self._invalid_token)

        chat_id = self.settings["notificationChatId"]

        if chat_id is None:
            raise TransportError("notification_chat_missing", permanent=True)

        try:
            if notification.photo is not None:
                await self._application.bot.send_photo(
                    chat_id=chat_id,
                    photo=notification.photo,
                    caption=notification.text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=COMMAND_KEYBOARD,
                    read_timeout=5,
                    write_timeout=5,
                    connect_timeout=5,
                    pool_timeout=5,
                )
            else:
                await self._application.bot.send_message(
                    chat_id=chat_id,
                    text=notification.text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=COMMAND_KEYBOARD,
                    read_timeout=5,
                    write_timeout=5,
                    connect_timeout=5,
                    pool_timeout=5,
                )
        except (BadRequest, Forbidden, InvalidToken):
            raise TransportError("telegram_rejected", permanent=True) from None
        except RetryAfter as error:
            delay = (
                error.retry_after.total_seconds()
                if isinstance(error.retry_after, timedelta)
                else float(error.retry_after)
            )
            raise TransportError("telegram_rate_limited", retry_after=delay) from None
        except (NetworkError, TelegramError):
            raise TransportError("telegram_network_error") from None
        else:
            if not self._reconnect.is_set():
                self._health = "healthy"
