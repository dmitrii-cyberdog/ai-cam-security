import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import cv2
import numpy as np

from app.services.telegram import COMMANDS, COMMAND_KEYBOARD, COMMAND_TITLES, TelegramService, TransportError
from app.services.models import Notification
from app_tests.test_alarm import config, observation

#
#


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = config()
        self.config["telegram"] = {"token": "123:secret-token", "allowedUserIds": [42], "notificationChatId": 42}
        self.execute = AsyncMock(return_value="disarm")
        self.get_frame = AsyncMock(return_value=None)
        self.status = {
            "armed": False, "phase": "idle", "manual": False,
            "suppression": False, "suppressionReason": "", "lastCommandResult": "Started disarmed",
            "profile": "day", "alarmCycles": 2, "maxAlarmCycles": 3,
            "camera": "healthy", "detector": "healthy", "audio": "ready", "telegram": "healthy",
            "notifications": {"queued_count": 1, "retained_image_bytes": 1048576, "dropped_count": 2, "unreported_drops": 1},
            "token": "123:secret-token",
        }
        self.service = TelegramService(self.config, self.execute, lambda: self.status, self.get_frame)
        self.service._cutoff = datetime.now(timezone.utc) - timedelta(seconds=1)

    def update(self, **overrides):
        message = SimpleNamespace(date=datetime.now(timezone.utc), text="/disarm", reply_text=AsyncMock(), reply_photo=AsyncMock())
        result = SimpleNamespace(update_id=1, effective_user=SimpleNamespace(id=42), effective_chat=SimpleNamespace(type="private"), message=message, edited_message=None)
        for name, value in overrides.items():
            setattr(result, name, value)
        return result

    def application(self):
        app = Mock(
            running=True,
            initialize=AsyncMock(),
            start=AsyncMock(),
            stop=AsyncMock(),
            shutdown=AsyncMock(),
        )
        app.updater = Mock(running=True, start_polling=AsyncMock(), stop=AsyncMock())
        app.bot = Mock(set_my_commands=AsyncMock(), send_message=AsyncMock(), shutdown=AsyncMock())

        return app

    async def test_authorized_private_command_and_duplicate(self):
        update = self.update()
        await self.service._handle(update, None)
        await self.service._handle(update, None)
        self.execute.assert_awaited_once_with("disarm")

    async def test_photo_sends_jpeg_to_requesting_chat_without_alarm_command(self):
        frame = observation(1, 0, 0).frame
        self.get_frame.return_value = frame
        self.config["telegram"]["notificationChatId"] = None
        update = self.update()
        update.message.text = "/photo"

        await self.service._handle(update, None)
        await self.service._handle(update, None)

        self.get_frame.assert_awaited_once_with()
        update.message.reply_photo.assert_awaited_once()
        update.message.reply_text.assert_not_awaited()
        self.execute.assert_not_awaited()
        sent = update.message.reply_photo.call_args.kwargs
        image = cv2.imdecode(np.frombuffer(sent["photo"], dtype=np.uint8), cv2.IMREAD_COLOR)

        self.assertEqual(image.shape, frame.image.shape)
        self.assertIn("Time (UTC): 2026-09-27T10:00:00Z", sent["caption"])
        self.assertEqual(sent["parse_mode"], "HTML")
        self.assertIs(sent["reply_markup"], COMMAND_KEYBOARD)
        self.assertEqual(self.service.health(), "healthy")

    async def test_photo_unavailable_timeout_and_encoding_failure(self):
        frame = observation(1, 0, 0).frame

        for index, (value, error, encoding) in enumerate((
            (None, None, (False, None)),
            (None, asyncio.TimeoutError(), (False, None)),
            (frame, None, (False, None)),
            (frame, None, cv2.error("encoding failed")),
        )):
            self.get_frame.return_value = value
            self.get_frame.side_effect = error
            update = self.update(update_id=index + 1)
            update.message.text = "/photo"

            with patch("app.services.telegram.cv2.imencode") as encode:
                if isinstance(encoding, Exception):
                    encode.side_effect = encoding
                else:
                    encode.return_value = encoding

                await self.service._handle(update, None)

            update.message.reply_photo.assert_not_awaited()
            self.assertIn("Photo unavailable", update.message.reply_text.call_args.args[0])

        self.execute.assert_not_awaited()

    async def test_photo_authorization_and_freshness_checks_before_capture(self):
        for fields in (
            {"effective_user": SimpleNamespace(id=100)},
            {"effective_chat": SimpleNamespace(type="group")},
            {"edited_message": object()},
        ):
            update = self.update(**fields)
            update.message.text = "/photo"

            await self.service._handle(update, None)

            update.message.reply_photo.assert_not_awaited()

        update = self.update()
        update.message.text = "/photo"
        update.message.date -= timedelta(seconds=31)

        await self.service._handle(update, None)

        self.get_frame.assert_not_awaited()

    async def test_failed_photo_delivery_sets_offline_health(self):
        from telegram.error import NetworkError

        self.get_frame.return_value = observation(1, 0, 0).frame
        update = self.update()
        update.message.text = "/photo"
        update.message.reply_photo.side_effect = NetworkError("network unavailable")

        await self.service._handle(update, None)

        self.assertEqual(self.service.health(), "offline")

    async def test_unknown_missing_group_and_edited_are_rejected(self):
        for fields in ({"effective_user": None}, {"effective_user": SimpleNamespace(id=100)}, {"effective_chat": SimpleNamespace(type="group")}, {"edited_message": object()}):
            update = self.update(**fields)

            await self.service._handle(update, None)
            update.message.reply_text.assert_not_awaited()

        self.execute.assert_not_awaited()

    async def test_stale_future_and_pre_session_commands_rejected(self):
        for delta in (-31, -2, 6):
            update = self.update()
            update.message.date = datetime.now(timezone.utc) + timedelta(seconds=delta)
            await self.service._handle(update, None)
        self.execute.assert_not_awaited()

    async def test_blocked_reply_does_not_block_disarm(self):
        blocked = self.update()
        blocked.message.text = "/status"
        blocked.message.reply_text.side_effect = lambda *a, **k: None
        async def slow(*args, **kwargs):
            await asyncio.Event().wait()
        blocked.message.reply_text.side_effect = slow
        first = asyncio.create_task(self.service._handle(blocked, None))
        await asyncio.sleep(0)
        await self.service._handle(self.update(update_id=2), None)
        self.execute.assert_awaited_once_with("disarm")
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    async def test_status_excludes_secrets(self):
        update = self.update()
        update.message.text = "/status"
        await self.service._handle(update, None)
        self.assertNotIn("secret-token", update.message.reply_text.call_args.args[0])

    async def test_status_includes_siren_period_allowance(self):
        update = self.update()
        update.message.text = "/status"
        await self.service._handle(update, None)
        reply = update.message.reply_text.call_args.args[0]

        self.assertIn("Siren periods used: 2 / 3", reply)
        self.assertIn("<b>📊 Camera alarm status</b>\n\n", reply)
        self.assertIn("Automatic detection: <b>❌ Disarmed</b>", reply)
        self.assertIn("Camera: ✅ Healthy", reply)
        self.assertIn("Queued: 1\nDropped: 2\nUnreported drops: 1\nImage memory: 1.0 MiB", reply)
        self.assertEqual(update.message.reply_text.call_args.kwargs["parse_mode"], "HTML")

    async def test_status_shows_audio_fault_recovery_and_escapes_values(self):
        self.status.update(suppression=True, suppressionReason="audio_fault", audio="audio_fault", lastCommandResult="Cannot arm: <PCM> & 123:secret-token")
        update = self.update()
        update.message.text = "/status"

        await self.service._handle(update, None)

        reply = update.message.reply_text.call_args.args[0]

        self.assertIn("Audio fault — automatic alarms paused", reply)
        self.assertIn("Audio: ⚠️ Audio fault", reply)
        self.assertIn("A successful /arm resumes automatic alarms.</i>", reply)
        self.assertIn("&lt;PCM&gt; &amp; [REDACTED]", reply)
        self.assertNotIn("secret-token", reply)

    async def test_command_confirmations_and_stop_preserve_mode(self):
        for index, (name, heading) in enumerate((
            ("arm", "✅ Protection armed"), ("disarm", "❌ Protection disarmed"),
            ("alarm", "🚨 Manual siren requested"), ("stop", "⏹️ Playback stopped"),
        )):
            self.execute.return_value = name
            update = self.update(update_id=index + 1)
            update.message.text = f"/{name}"

            await self.service._handle(update, None)

            self.assertIn(f"<b>{heading}</b>\n\n", update.message.reply_text.call_args.args[0])
            self.assertEqual(update.message.reply_text.call_args.kwargs["parse_mode"], "HTML")

        self.assertIn("remains disarmed", update.message.reply_text.call_args.args[0])
        self.status["armed"] = True
        self.assertIn("remains armed", self.service._command_reply("stop", "stop"))

    def test_suppression_guidance_does_not_claim_disarmed_protection_will_resume(self):
        self.status.update(suppression=True, suppressionReason="until_absence")

        self.assertNotIn("Automatic alarms resume", self.service._status_reply())
        self.status["armed"] = True

        self.assertIn("Automatic alarms resume after confirmed absence", self.service._status_reply())

    async def test_failed_command_is_formatted_escaped_redacted_and_bounded(self):
        self.execute.return_value = "Cannot arm: <PCM> & 123:secret-token " + "x" * 5000
        update = self.update()
        update.message.text = "/arm"

        await self.service._handle(update, None)

        reply = update.message.reply_text.call_args.args[0]

        self.assertIn("<b>⚠️ /arm — command not completed</b>", reply)
        self.assertIn("&lt;PCM&gt; &amp; [REDACTED]", reply)
        self.assertNotIn("secret-token", reply)
        self.assertNotIn("<b>✅ Protection armed</b>", reply)
        self.assertLess(len(reply), 4096)
        self.assertTrue(reply.endswith("</i>"))

    async def test_help_lists_commands_on_separate_lines(self):
        update = self.update()
        update.message.text = "/help"

        await self.service._handle(update, None)

        reply = update.message.reply_text.call_args.args[0]

        for name, description in COMMANDS.items():
            self.assertIn(f"<b>/{name}</b>\n{description}", reply)

        self.execute.assert_not_awaited()

    async def test_status_keyboard_contains_every_command(self):
        update = self.update()
        update.message.text = "/status"

        await self.service._handle(update, None)

        keyboard = update.message.reply_text.call_args.kwargs["reply_markup"]

        self.assertEqual(
            [button.text for row in keyboard.keyboard for button in row],
            [COMMAND_TITLES[name] for name in COMMANDS],
        )
        self.assertTrue(keyboard.resize_keyboard)
        self.assertTrue(keyboard.is_persistent)
        self.execute.assert_not_awaited()

    async def test_keyboard_titles_execute_commands_and_show_status_and_help(self):
        for index, (name, title) in enumerate(COMMAND_TITLES.items()):
            self.execute.reset_mock()
            self.execute.return_value = name
            update = self.update(update_id=index + 1)
            update.message.text = title

            await self.service._handle(update, None)

            update.message.reply_text.assert_awaited_once()
            self.assertIs(update.message.reply_text.call_args.kwargs["reply_markup"], COMMAND_KEYBOARD)

            if name in {"status", "help", "photo"}:
                self.execute.assert_not_awaited()

                if name == "photo":
                    self.get_frame.assert_awaited_once_with()
                else:
                    self.assertIn("Camera alarm status" if name == "status" else "Camera alarm commands", update.message.reply_text.call_args.args[0])
            else:
                self.execute.assert_awaited_once_with(name)

    async def test_keyboard_titles_keep_authorization_freshness_and_duplicate_checks(self):
        for fields in ({"effective_user": SimpleNamespace(id=100)}, {"effective_chat": SimpleNamespace(type="group")}, {"edited_message": object()}):
            update = self.update(**fields)
            update.message.text = COMMAND_TITLES["arm"]

            await self.service._handle(update, None)

            update.message.reply_text.assert_not_awaited()

        stale = self.update()
        stale.message.text = COMMAND_TITLES["arm"]
        stale.message.date = datetime.now(timezone.utc) - timedelta(seconds=31)

        await self.service._handle(stale, None)

        self.execute.assert_not_awaited()

        update = self.update(update_id=2)
        update.message.text = COMMAND_TITLES["arm"]
        self.execute.return_value = "arm"

        await self.service._handle(update, None)
        await self.service._handle(update, None)

        self.execute.assert_awaited_once_with("arm")

    async def test_plain_command_names_and_unknown_button_titles_are_ignored(self):
        for text in ("arm", "Arm protection", "✅ Arm protection extra", "unknown"):
            update = self.update()
            update.message.text = text

            await self.service._handle(update, None)

            update.message.reply_text.assert_not_awaited()

        self.execute.assert_not_awaited()

    def test_keyboard_text_handler_is_registered(self):
        from telegram import Chat, Message, Update, User
        from telegram.ext import MessageHandler

        application = self.service._build()
        handlers = [handler for handler in application.handlers[0] if isinstance(handler, MessageHandler)]

        message = Message(1, datetime.now(timezone.utc), Chat(42, "private"), from_user=User(42, "User", False), text=COMMAND_TITLES["arm"])
        update = Update(1, message=message)

        self.assertEqual(len(handlers), 1)
        self.assertTrue(handlers[0].check_update(update))
        self.assertEqual(handlers[0].callback, self.service._handle)

    async def test_lifecycle_order_and_polling_options(self):
        stop = asyncio.Event()
        calls = []
        app = Mock()
        app.running = False
        app.updater.running = False
        async def initialize():
            calls.append("initialize")
        async def start():
            calls.append("start")
            app.running = True
        async def polling(**kwargs):
            self.assertTrue(kwargs["drop_pending_updates"])
            self.assertEqual(kwargs["allowed_updates"], ["message"])
            calls.append("polling")
            app.updater.running = True
            stop.set()
        async def updater_stop():
            calls.append("updater_stop")
            app.updater.running = False
        async def app_stop():
            calls.append("app_stop")
            app.running = False
        app.initialize = initialize
        app.start = start
        app.updater.start_polling = polling
        app.updater.stop = updater_stop
        app.stop = app_stop
        app.shutdown = AsyncMock(side_effect=lambda: calls.append("shutdown"))
        app.bot.shutdown = AsyncMock()
        app.bot.set_my_commands = AsyncMock(side_effect=lambda *args, **kwargs: calls.append("commands"))
        app.bot.send_message = AsyncMock(side_effect=lambda **kwargs: calls.append("startup"))

        with patch.object(self.service, "_build", return_value=app):
            await self.service.run(stop)

        self.assertEqual(calls, ["initialize", "commands", "start", "polling", "startup", "updater_stop", "app_stop", "shutdown"])
        commands = app.bot.set_my_commands.call_args.args[0]

        self.assertEqual([command.command for command in commands], list(COMMANDS))
        self.assertEqual([command.description for command in commands], list(COMMANDS.values()))

        startup = app.bot.send_message.call_args.kwargs

        self.assertEqual(startup["chat_id"], 42)
        self.assertIn(
            f"Started at (UTC): {self.service._started_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            startup["text"],
        )
        self.assertIn("Started disarmed", startup["text"])
        self.assertIn("<b>🚀 Camera alarm service started</b>\n\n", startup["text"])
        self.assertIn("<b>❌ Started disarmed</b>", startup["text"])
        self.assertEqual(startup["parse_mode"], "HTML")
        self.assertEqual(
            [button.text for row in startup["reply_markup"].keyboard for button in row],
            [COMMAND_TITLES[name] for name in COMMANDS],
        )
        self.assertFalse(self.service._startup_pending)
        self.execute.assert_not_awaited()

    async def test_reconnect_does_not_repeat_commands_or_startup(self):
        from telegram.error import NetworkError

        stop = asyncio.Event()
        first, second = self.application(), self.application()
        first.bot.send_message.side_effect = lambda **kwargs: self.service._poll_error(NetworkError("offline"))
        second.updater.start_polling.side_effect = lambda **kwargs: stop.set()

        with patch.object(self.service, "_build", side_effect=[first, second]):
            await asyncio.wait_for(self.service.run(stop), 3)

        first.bot.send_message.assert_awaited_once()
        second.bot.send_message.assert_not_awaited()
        first.bot.set_my_commands.assert_awaited_once()
        second.bot.set_my_commands.assert_not_awaited()

    async def test_failed_startup_delivery_retries_after_reconnect(self):
        from telegram.error import NetworkError

        stop = asyncio.Event()
        first, second = self.application(), self.application()
        first.bot.send_message.side_effect = NetworkError("offline")
        second.bot.send_message.side_effect = lambda **kwargs: stop.set()

        with patch.object(self.service, "_build", side_effect=[first, second]):
            await asyncio.wait_for(self.service.run(stop), 3)

        first.bot.send_message.assert_awaited_once()
        second.bot.send_message.assert_awaited_once()
        self.assertEqual(
            first.bot.send_message.call_args.kwargs["text"],
            second.bot.send_message.call_args.kwargs["text"],
        )
        self.assertFalse(self.service._startup_pending)

    async def test_failed_command_registration_retries_before_startup_delivery(self):
        from telegram.error import NetworkError

        stop = asyncio.Event()
        first, second = self.application(), self.application()
        first.bot.set_my_commands.side_effect = NetworkError("offline")
        second.bot.send_message.side_effect = lambda **kwargs: stop.set()

        with patch.object(self.service, "_build", side_effect=[first, second]):
            await asyncio.wait_for(self.service.run(stop), 3)

        first.start.assert_not_awaited()
        first.bot.send_message.assert_not_awaited()
        second.bot.set_my_commands.assert_awaited_once()
        second.bot.send_message.assert_awaited_once()

    async def test_rejected_startup_destination_does_not_disable_polling(self):
        from telegram.error import Forbidden

        stop = asyncio.Event()
        app = self.application()

        async def rejected(**kwargs):
            stop.set()

            raise Forbidden("secret-token")

        app.bot.send_message.side_effect = rejected

        with patch.object(self.service, "_build", return_value=app), patch("app.services.telegram.logger") as logger:
            await self.service.run(stop)

        self.assertFalse(self.service._invalid_token)
        self.assertFalse(self.service._startup_pending)
        app.updater.start_polling.assert_awaited_once()
        logger.warning.assert_called_once_with("Startup notification rejected", {"reason": "telegram_rejected"})

    async def test_command_registration_honors_rate_limit(self):
        from telegram.error import RetryAfter

        stop = asyncio.Event()
        app = self.application()
        app.bot.set_my_commands.side_effect = RetryAfter(12)
        wait_for = asyncio.wait_for
        delays = []

        async def wait(awaitable, timeout):
            if timeout == 12:
                delays.append(timeout)
                stop.set()

            return await wait_for(awaitable, timeout)

        with patch.object(self.service, "_build", return_value=app), patch("app.services.telegram.asyncio.wait_for", side_effect=wait):
            await self.service.run(stop)

        self.assertEqual(delays, [12])
        self.assertFalse(self.service._commands_registered)
        app.bot.send_message.assert_not_awaited()

    async def test_missing_notification_chat_still_registers_commands(self):
        self.config["telegram"]["notificationChatId"] = None
        service = TelegramService(self.config, self.execute, lambda: {"armed": False}, self.get_frame)
        stop = asyncio.Event()
        app = self.application()
        app.updater.start_polling.side_effect = lambda **kwargs: stop.set()

        with patch.object(service, "_build", return_value=app):
            await service.run(stop)

        app.bot.set_my_commands.assert_awaited_once()
        app.bot.send_message.assert_not_awaited()

    async def test_unconfigured_telegram_does_not_initialize_or_notify(self):
        self.config["telegram"]["token"] = ""
        service = TelegramService(self.config, self.execute, lambda: {"armed": False}, self.get_frame)
        stop = asyncio.Event()
        stop.set()

        with patch.object(service, "_build") as build:
            await service.run(stop)

        build.assert_not_called()


    async def test_send_uses_event_photo_and_safe_errors(self):
        from telegram.error import BadRequest
        app = Mock()
        app.bot.send_photo = AsyncMock(side_effect=BadRequest("secret-token"))
        self.service._application = app
        self.service._health = "healthy"
        with self.assertRaises(TransportError) as raised:
            await self.service.send(Notification(1, "activated", 0, "Alarm activated", b"jpeg"))
        self.assertTrue(raised.exception.permanent)
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertEqual(app.bot.send_photo.call_args.kwargs["parse_mode"], "HTML")
        self.assertEqual(app.bot.send_photo.call_args.kwargs["photo"], b"jpeg")
        self.assertIs(app.bot.send_photo.call_args.kwargs["reply_markup"], COMMAND_KEYBOARD)

    async def test_partial_start_invalid_token_cleans_up_and_waits(self):
        from telegram.error import InvalidToken
        stop = asyncio.Event()
        app = Mock()
        app.running = app.updater.running = False
        app.initialize = AsyncMock(side_effect=InvalidToken("secret-token"))
        app.shutdown = AsyncMock()
        app.bot.shutdown = AsyncMock(side_effect=stop.set)
        with patch.object(self.service, "_build", return_value=app) as build:
            await self.service.run(stop)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(self.service.health(), "invalid_credentials")
        app.bot.shutdown.assert_awaited()

    async def test_poll_error_signals_supervisor_without_recursive_stop(self):
        from telegram.error import NetworkError
        self.service._application = Mock()
        self.service._poll_error(NetworkError("secret-token"))
        self.assertTrue(self.service._reconnect.is_set())
        self.service._application.updater.stop.assert_not_called()
        self.assertEqual(self.service.health(), "offline")

    async def test_failed_reply_does_not_disable_later_notifications(self):
        from telegram.error import NetworkError

        app = Mock()
        app.running = True
        app.bot.send_message = AsyncMock()
        self.service._application = app
        self.service._health = "healthy"
        update = self.update()
        update.message.reply_text.side_effect = NetworkError("offline")

        await self.service._handle(update, None)
        await self.service.send(Notification(1, "still_there", 0, "Person is still there", None))

        app.bot.send_message.assert_awaited_once()
        self.assertEqual(app.bot.send_message.call_args.kwargs["parse_mode"], "HTML")
        self.assertIs(app.bot.send_message.call_args.kwargs["reply_markup"], COMMAND_KEYBOARD)
        self.assertEqual(self.service.health(), "healthy")

    async def test_commands_wait_for_reconnect_cutoff(self):
        from telegram.error import NetworkError

        self.service._poll_error(NetworkError("offline"))

        await self.service._handle(self.update(), None)

        self.execute.assert_not_awaited()
