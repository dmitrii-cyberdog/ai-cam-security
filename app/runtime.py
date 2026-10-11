import asyncio
import hashlib
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import cv2

from app.services.alarm import AlarmController, AlarmEffect, PlayKey, StopPlayback
from app.services.audio import AudioPlayer, resolve_sequence
from app.services.logger import logger
from app.services.models import Frame, IncidentEvent, Observation, VisionFault
from app.services.notifications import NotificationWorker
from app.services.telegram import TelegramService
from app.services.vision import VisionWorker

#
#

MODEL_HASHES = {
    "4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186",
    "8dd32b85f2d273e9047f1d6b59e0b2fd008b1076338107bb547ac28942cdf90b",
}


def check_readiness(config: Dict[str, Any]) -> list[str]:
    errors = []
    for section, key, name in (
        ("camera", "host", "RTSP_HOST"),
        ("camera", "login", "RTSP_LOGIN"),
        ("camera", "password", "RTSP_PASSWORD"),
        ("telegram", "token", "TELEGRAM_BOT_TOKEN"),
        ("telegram", "allowedUserIds", "TELEGRAM_ALLOWED_USER_IDS"),
        ("telegram", "notificationChatId", "TELEGRAM_CHAT_ID"),
    ):
        if not config[section][key]:
            errors.append(f"Missing {name}")
    try:
        model_hash = hashlib.sha256(
            Path(config["detection"]["modelPath"]).read_bytes()
        ).hexdigest()
        if model_hash not in MODEL_HASHES:
            errors.append(
                "Invalid DETECTION_MODEL_PATH: expected pinned NanoDet artifact"
            )
    except OSError:
        errors.append("Missing or unreadable DETECTION_MODEL_PATH")
    try:
        resolve_sequence(config)
    except ValueError as error:
        errors.append(str(error))
    if shutil.which("aplay") is None:
        errors.append("Audio backend unavailable: aplay")
    if config["audio"]["volume"] is not None and shutil.which("amixer") is None:
        errors.append("Volume backend unavailable: amixer")

    if not cv2.videoio_registry.hasBackend(cv2.CAP_FFMPEG):
        errors.append("Camera backend unavailable: FFmpeg")
    return errors


#
#


class Runtime:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.controller = AlarmController(config)
        self.audio = AudioPlayer(config)
        self.vision = VisionWorker(config)
        self.telegram = TelegramService(config, self.execute, self.status, self.get_frame)
        self.notifications = NotificationWorker(
            self.telegram.send, config["detection"]["maxFrameAgeSeconds"]
        )
        self._audio_task: asyncio.Task[None] | None = None
        self._audio_events: asyncio.Queue[tuple[int, str]] = asyncio.Queue(maxsize=16)
        self._audio_lock = asyncio.Lock()
        self._command_serial = 0
        self._camera_health = "unavailable"
        self._detector_health = "unavailable"
        self._audio_health = "unchecked"
        self._last_observation: float | None = None
        self._latest_frame: Frame | None = None
        self._stream_available: bool | None = None
        self._camera_check_started_at = time.monotonic()
        self._closing = False

    async def get_frame(self) -> Frame | None:
        requested_at = time.monotonic()

        while not self._closing:
            frame = self._latest_frame
            now = time.monotonic()

            if (
                frame is not None
                and frame.captured_at >= requested_at
                and 0 <= now - frame.captured_at
                <= self.config["detection"]["maxFrameAgeSeconds"]
            ):
                return frame

            await asyncio.sleep(0.05)

        return None

    async def _readiness(self, name: str) -> str | None:
        errors = await asyncio.to_thread(check_readiness, self.config)
        errors = [error for error in errors if "TELEGRAM_" not in error]

        if name == "alarm":
            errors = [
                error
                for error in errors
                if "audio" in error.lower() or "aplay" in error
            ]
        elif (
            self._last_observation is None
            or time.monotonic() - self._last_observation
            > self.config["detection"]["maxFrameAgeSeconds"]
            or self._camera_health != "healthy"
            or self._detector_health != "healthy"
        ):
            errors.append("Camera/detector needs fresh observations")
        if errors:
            return "; ".join(errors)
        try:
            await asyncio.to_thread(self.audio.validate)
        except ValueError as error:
            self._audio_health = "unavailable"
            return str(error)

        self._audio_health = "ready"

        return None

    async def execute(self, name: str) -> str:
        self._command_serial += 1
        serial = self._command_serial

        if self._closing:
            return "Service stopping"

        readiness = await self._readiness(name) if name in {"arm", "alarm"} else None

        if serial != self._command_serial or self._closing:
            return "Command superseded"

        if name == "arm" and readiness is None:
            try:
                await self.audio.set_volume()
            except ValueError as error:
                self._audio_health = "audio_fault"
                readiness = str(error)

            if serial != self._command_serial or self._closing:
                return "Command superseded"

            if readiness is None:
                readiness = await self._readiness(name)

            if serial != self._command_serial or self._closing:
                return "Command superseded"

        effects = self.controller.command(
            name, time.monotonic(), datetime.now(timezone.utc), readiness
        )
        await self._apply(effects)
        return str(self.controller.status()["lastCommandResult"])

    async def _stop_audio(self) -> None:
        task = self._audio_task
        await self.audio.stop()
        if task is not None and task is not asyncio.current_task():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._audio_task is task:
            self._audio_task = None

    def _audio_callback(self, generation: int, event: str) -> None:
        if generation == self.controller.generation and not self._closing:
            self._audio_events.put_nowait((generation, event))

    async def _play(self, effect: PlayKey) -> None:
        try:
            await self.audio.play(
                effect.key,
                repeat=effect.repeat,
                on_started=lambda: self._audio_callback(effect.generation, "started"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._audio_health = "audio_fault"
            self._audio_callback(effect.generation, "failed")
        else:
            self._audio_callback(effect.generation, "finished")

    async def _apply(self, effects: list[AlarmEffect]) -> None:
        if any(isinstance(effect, (StopPlayback, PlayKey)) for effect in effects):
            async with self._audio_lock:
                await self._stop_audio()
                for effect in effects:
                    if (
                        isinstance(effect, PlayKey)
                        and effect.generation == self.controller.generation
                        and not self._closing
                    ):
                        self._audio_task = asyncio.create_task(self._play(effect))
        for effect in effects:
            if isinstance(effect, IncidentEvent):
                self.notifications.enqueue(effect)

    def _notify_camera_status(self, now: float, utc: datetime) -> None:
        available = self._camera_health == "healthy"

        if self._stream_available is None:
            if available:
                self._stream_available = True

                return

            startup_budget = 15 + (
                self.config["camera"]["openTimeoutMs"]
                + self.config["camera"]["readTimeoutMs"]
            ) / 1000

            if now - self._camera_check_started_at < startup_budget:
                return

        if available == self._stream_available:
            return

        self._stream_available = available
        kind = "camera_available" if available else "camera_unavailable"

        self.notifications.enqueue(
            IncidentEvent(0, kind, "", kind, now, utc, None)
        )

    async def _step(self) -> None:
        now, utc = time.monotonic(), datetime.now(timezone.utc)

        if (
            self._last_observation is not None
            and now - self._last_observation
            > self.config["detection"]["maxFrameAgeSeconds"]
        ):
            self._camera_health = "stale"
        message = self.vision.poll()
        if isinstance(message, VisionFault):
            self._camera_health = "unavailable"
            self._detector_health = message.reason
            self._last_observation = None
            self._latest_frame = None
            await self._apply(self.controller.vision_fault(message.reason, now, utc))
        elif isinstance(message, Observation):
            if (
                0
                <= now - message.frame.captured_at
                <= self.config["detection"]["maxFrameAgeSeconds"]
            ):
                self._camera_health = self._detector_health = "healthy"
                self._last_observation = message.frame.captured_at
                self._latest_frame = message.frame
            await self._apply(self.controller.observe(message, now, utc))

        await self._apply(self.controller.tick(now, utc))

        self._notify_camera_status(now, utc)

        while not self._audio_events.empty():
            generation, event = self._audio_events.get_nowait()
            await self._apply(self.controller.audio_event(generation, event, now, utc))

    def status(self) -> Dict[str, Any]:
        return {
            **self.controller.status(),
            "camera": self._camera_health,
            "detector": self._detector_health,
            "audio": self._audio_health,
            "telegram": self.telegram.health(),
            "notifications": self.notifications.health(),
        }

    async def run(self, stop: asyncio.Event) -> None:
        telegram_stop, notification_stop = asyncio.Event(), asyncio.Event()
        telegram_task = notification_task = None
        try:
            if (
                self.config["camera"]["rtspUrl"]
                and Path(self.config["detection"]["modelPath"]).is_file()
            ):
                self.vision.start()
            telegram_task = asyncio.create_task(self.telegram.run(telegram_stop))
            notification_task = asyncio.create_task(
                self.notifications.run(notification_stop)
            )
            logger.info("Camera alarm service ready for diagnostics; started disarmed")
            while not stop.is_set():
                await self._step()
                try:
                    await asyncio.wait_for(stop.wait(), 0.1)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._closing = True
            self._command_serial += 1
            effects = self.controller.command(
                "disarm", time.monotonic(), datetime.now(timezone.utc)
            )
            try:
                await asyncio.wait_for(self._apply(effects), 1)
                await asyncio.wait_for(self._stop_audio(), 1)
            finally:
                try:
                    await asyncio.wait_for(self.vision.stop(), 5)
                finally:
                    notification_stop.set()
                    if notification_task is not None:
                        try:
                            await asyncio.wait_for(notification_task, 2)
                        except (asyncio.TimeoutError, Exception):
                            logger.warning(
                                "Notification worker stopped before drain completed"
                            )
                    logger.info("Notification shutdown", self.notifications.health())
                    telegram_stop.set()
                    if telegram_task is not None:
                        try:
                            await asyncio.wait_for(telegram_task, 5)
                        except (asyncio.TimeoutError, Exception):
                            logger.warning("Telegram cleanup did not complete normally")
