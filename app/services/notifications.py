import asyncio
import time
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

import cv2

from app.services.models import IncidentEvent, Notification
from app.services.telegram import TransportError

#
#


def prepare_notification(event: IncidentEvent, max_frame_age: float) -> Notification:
    titles = {"recognized": "👤 Person recognized", "still_there": "👀 Person is still there", "activated": "🚨 Alarm activated", "deactivated": "⏹️ Alarm deactivated", "alarm_photo": "🚨 Alarm in progress"}
    reasons = {
        "presence_confirmed": "Person detected and confirmed",
        "manual": "Manual siren requested",
        "alarm_active": "Siren is active",
        "person_left": "Person left the camera view",
        "maximum_duration": "Maximum alarm duration reached",
        "stop": "Stopped with /stop",
        "disarm": "Protection disabled with /disarm",
        "audio_fault": "Audio playback failed",
        "camera_unavailable": "Camera unavailable",
        "detector_unavailable": "Detector unavailable",
    }
    reason = reasons.get(event.reason, event.reason.replace("_", " ").capitalize())
    text = (
        f"<b>{titles[event.kind]}</b>\n\n"
        f"🔔 Incident: <b>#{event.incident_id}</b>\n"
        f"🕒 Time (UTC): {event.at_utc.isoformat()}\n"
        f"📍 Phase at event: {escape(event.phase[:80].capitalize())}\n"
        f"ℹ️ Reason: {escape(reason[:180])}"
    )
    photo = None

    if event.reason == "audio_fault":
        text += "\n\n<i>Check the speaker, then use /arm. A successful /arm resumes automatic alarms.</i>"

    if event.kind != "still_there":
        if event.frame is not None:
            try:
                ok, encoded = cv2.imencode(".jpg", event.frame.image, [cv2.IMWRITE_JPEG_QUALITY, 80])

                if ok:
                    photo = encoded.tobytes()
            except Exception:
                pass

            if not 0 <= event.at - event.frame.captured_at <= max_frame_age:
                text += f"\n\n<i>📷 Latest available image (UTC): {event.frame.captured_utc.isoformat()}</i>"

        if photo is None:
            text += "\n\n<i>⚠️ Photo unavailable</i>"

    return Notification(event.incident_id, event.kind, event.at, text, photo)


#
#

CAPACITY = 20
MAX_BYTES = 32 * 1024 * 1024
TTL = 120
SEND_DEADLINE = 10


class NotificationWorker:
    def __init__(self, send: Callable[[Notification], Awaitable[None]], max_frame_age: float) -> None:
        self.send = send
        self.max_frame_age = max_frame_age
        self._pending: deque[IncidentEvent] = deque()
        self._pending_bytes = 0
        self._inflight_bytes = 0
        self._inflight = False
        self._wake = asyncio.Event()
        self._closed = False
        self._dropped = 0
        self._unreported = 0
        self._encoding: asyncio.Future[Notification] | None = None

    @staticmethod
    def _size(event: IncidentEvent) -> int:
        return event.frame.image.nbytes if event.frame is not None else 0

    def _drop(self) -> None:
        self._dropped += 1
        self._unreported += 1

    def _evict(self) -> None:
        self._pending_bytes -= self._size(self._pending.popleft())
        self._drop()

    def enqueue(self, event: IncidentEvent) -> None:
        size = self._size(event)

        if self._closed or time.monotonic() - event.at > TTL or size > MAX_BYTES:
            self._drop()
            self._wake.set()

            return

        while self._pending and (len(self._pending) + int(self._inflight) >= CAPACITY or self._pending_bytes + self._inflight_bytes + size > MAX_BYTES):
            self._evict()

        if len(self._pending) + int(self._inflight) >= CAPACITY or self._pending_bytes + self._inflight_bytes + size > MAX_BYTES:
            self._drop()
        else:
            self._pending.append(event)
            self._pending_bytes += size

        self._wake.set()

    def health(self) -> dict[str, Any]:
        return {
            "queued_count": len(self._pending) + int(self._inflight),
            "retained_image_bytes": self._pending_bytes + self._inflight_bytes,
            "dropped_count": self._dropped,
            "unreported_drops": self._unreported,
        }

    async def _deliver(self, note: Notification, stop: asyncio.Event) -> bool:
        for attempt in range(3):
            remaining = TTL - (time.monotonic() - note.occurred_at)

            if remaining <= 0:
                return False

            try:
                await asyncio.wait_for(self.send(note), min(SEND_DEADLINE, remaining))

                return True
            except TransportError as error:
                if error.permanent:
                    return False

                delay = error.retry_after if error.retry_after is not None else float(attempt + 1)
            except (OSError, asyncio.TimeoutError):
                delay = float(attempt + 1)

            if attempt == 2 or delay >= TTL - (time.monotonic() - note.occurred_at) or stop.is_set():
                return False

            try:
                await asyncio.wait_for(stop.wait(), max(0, delay))

                return False
            except asyncio.TimeoutError:
                pass

        return False

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while self._pending or not stop.is_set():
                while self._pending and time.monotonic() - self._pending[0].at > TTL:
                    self._evict()

                if self._unreported:
                    count = self._unreported
                    summary = Notification(0, "summary", time.monotonic(), f"<b>⚠️ Notification delivery interrupted</b>\n\nDropped notifications: <b>{count}</b>\n\n<i>Delivery was unavailable or the queue was overloaded.</i>", None)

                    if await self._deliver(summary, stop):
                        self._unreported -= count
                    else:
                        if stop.is_set():
                            break

                        try:
                            await asyncio.wait_for(stop.wait(), 1)
                        except asyncio.TimeoutError:
                            pass

                        continue

                if not self._pending:
                    self._wake.clear()
                    wake = asyncio.create_task(self._wake.wait())
                    stopping = asyncio.create_task(stop.wait())

                    try:
                        await asyncio.wait({wake, stopping}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        wake.cancel()
                        stopping.cancel()
                        await asyncio.gather(wake, stopping, return_exceptions=True)

                    continue

                event = self._pending.popleft()
                self._pending_bytes -= self._size(event)
                self._inflight = True
                # Reserve encoding output as well as its input until the encoder finishes.
                # Bounded 1280x720 inputs keep the conservative JPEG reserve below the queue budget.
                self._inflight_bytes = self._size(event) * 2 + 65536

                while self._pending and self._pending_bytes + self._inflight_bytes > MAX_BYTES:
                    self._evict()

                if self._inflight_bytes > MAX_BYTES:
                    self._inflight = False
                    self._inflight_bytes = 0
                    self._drop()
                    continue

                loop = asyncio.get_running_loop()
                encoding: asyncio.Future[Notification] = loop.create_future()
                self._encoding = encoding

                def encode(item: IncidentEvent, future: asyncio.Future[Notification]) -> None:
                    result = prepare_notification(item, self.max_frame_age)

                    try:
                        loop.call_soon_threadsafe(future.set_result, result)
                    except RuntimeError:
                        pass

                threading.Thread(target=encode, args=(event, encoding), daemon=True).start()

                try:
                    note = await asyncio.shield(encoding)
                finally:
                    del encoding
                    del event

                self._encoding = None
                self._inflight_bytes = len(note.photo or b"")

                if not await self._deliver(note, stop):
                    self._drop()

                del note
                self._inflight = False
                self._inflight_bytes = 0
        finally:
            self._closed = True

            if self._inflight:
                self._drop()

            if self._encoding is not None and not self._encoding.done():
                self._encoding.add_done_callback(self._encoding_finished)
            else:
                self._encoding = None
                self._inflight = False
                self._inflight_bytes = 0

            while self._pending:
                self._evict()

    def _encoding_finished(self, future: asyncio.Future[Notification]) -> None:
        self._encoding = None
        self._inflight = False
        self._inflight_bytes = 0
