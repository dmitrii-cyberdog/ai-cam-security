import asyncio
import multiprocessing
import os
import pickle
import queue
import threading
import time
from typing import Any, Dict, cast

from app.services.camera import CameraCapture
from app.services.detection import PersonDetector
from app.services.models import Observation, VisionFault, VisionMessage

#
#


class _LatestMessageSlot:
    def __init__(self, context: Any) -> None:
        self._buffer = context.RawArray("B", 4 * 1024 * 1024)
        self._length = context.Value("i", 0, lock=False)
        self._lock = context.Lock()

    def put_nowait(self, message: VisionMessage) -> None:
        payload = pickle.dumps(message, protocol=5)

        if len(payload) > len(self._buffer):
            raise ValueError("vision_message_too_large")

        if not self._lock.acquire(False):
            raise queue.Full

        try:
            memoryview(self._buffer).cast("B")[:len(payload)] = payload
            self._length.value = len(payload)
        finally:
            self._lock.release()

    def get_nowait(self) -> VisionMessage:
        if not self._lock.acquire(False):
            raise queue.Empty

        try:
            length = self._length.value

            if length == 0:
                raise queue.Empty

            if not 0 < length <= len(self._buffer):
                raise ValueError("vision_message_invalid")

            payload = memoryview(self._buffer).cast("B")[:length].tobytes()
            self._length.value = 0
        finally:
            self._lock.release()

        message = pickle.loads(payload)

        if not isinstance(message, (Observation, VisionFault)):
            raise ValueError("vision_message_invalid")

        return message


def _publish(output: Any, message: VisionMessage) -> None:
    try:
        output.put_nowait(message)
    except queue.Full:
        pass


def _vision_main(config: Dict[str, Any], output: Any, stop: Any, main_progress: Any, capture_progress: Any) -> None:
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 2)

    capture = CameraCapture(config)
    capture_stop = threading.Event()
    thread = None

    try:
        detector = PersonDetector(config)
        thread = threading.Thread(target=capture.run, args=(capture_stop,), daemon=True)
        thread.start()
        last_number = 0
        last_health = ""

        while not stop.is_set():
            main_progress.value = time.monotonic()
            capture_progress.value = capture.progress_at
            health = capture.health()

            if health != last_health and health != "healthy":
                _publish(output, VisionFault(health, time.monotonic()))

            last_health = health
            frame = capture.latest(last_number)

            if frame is None:
                stop.wait(0.02)
                continue

            last_number = frame.number
            _publish(output, detector.infer(frame))
    except Exception:
        _publish(output, VisionFault("detector_unavailable", time.monotonic()))
    finally:
        capture_stop.set()

        if thread is not None:
            thread.join(0.2)


#
#


class VisionWorker:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._context = multiprocessing.get_context("spawn")
        self._process: Any = None
        self._queue: Any = None
        self._stop: Any = None
        self._main_progress: Any = None
        self._capture_progress: Any = None
        self._recovery: asyncio.Task[None] | None = None
        self._closed = False
        self._restart_delay = 1.0
        self._started = 0.0
        self._last_number = 0
        self._last_capture = -1.0

    def start(self) -> None:
        if not self._closed and self._process is None:
            self._launch()

    def _launch(self) -> None:
        self._started = time.monotonic()
        self._queue = _LatestMessageSlot(self._context)
        self._stop = self._context.Event()
        self._main_progress = self._context.Value("d", self._started, lock=False)
        self._capture_progress = self._context.Value("d", self._started, lock=False)
        self._last_number = 0
        self._last_capture = -1
        self._process = self._context.Process(
            target=_vision_main,
            args=(self.config, self._queue, self._stop, self._main_progress, self._capture_progress),
            daemon=True,
        )
        self._process.start()

    def poll(self) -> VisionMessage | None:
        if (
            self._closed
            or self._process is None
            or (self._recovery is not None and not self._recovery.done())
        ):
            return None

        now = time.monotonic()
        reason = None

        if not self._process.is_alive():
            reason = "vision_exited"
        else:
            budget = 15 + (self.config["camera"]["openTimeoutMs"] + self.config["camera"]["readTimeoutMs"]) / 1000

            if now - self._started > budget and (
                now - self._main_progress.value > budget
                or now - self._capture_progress.value > budget
            ):
                reason = "vision_stalled"

        if reason:
            self._recovery = asyncio.create_task(self._recover())

            return VisionFault(reason, now)

        try:
            message = cast(VisionMessage, self._queue.get_nowait())
        except queue.Empty:
            return None
        except (OSError, EOFError, ValueError, pickle.UnpicklingError):
            self._recovery = asyncio.create_task(self._recover())

            return VisionFault("vision_message_invalid", now)

        if isinstance(message, Observation):
            frame = message.frame

            if frame.number <= self._last_number or frame.captured_at <= self._last_capture:
                return None

            self._last_number, self._last_capture = frame.number, frame.captured_at
            frame.image.setflags(write=False)

            if 0 <= now - frame.captured_at <= self.config["detection"]["maxFrameAgeSeconds"]:
                self._restart_delay = 1

        return message

    async def _reap(self) -> None:
        process, output = self._process, self._queue

        if process is not None:
            self._stop.set()
            await asyncio.to_thread(process.join, 0.5)

            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 1)

            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 1)

            if process.is_alive():
                raise RuntimeError("vision_cleanup_failed")

            process.close()
            self._process = None

        if output is not None:
            self._queue = None

    async def _recover(self) -> None:
        await self._reap()
        await asyncio.sleep(self._restart_delay)
        self._restart_delay = min(self._restart_delay * 2, 10)

        if not self._closed:
            self._launch()

    async def stop(self) -> None:
        self._closed = True

        if self._recovery is not None and not self._recovery.done():
            self._recovery.cancel()
            await asyncio.gather(self._recovery, return_exceptions=True)

        await self._reap()
