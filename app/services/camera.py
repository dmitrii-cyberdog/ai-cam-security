import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict

import cv2
import numpy as np

from app.services.models import Frame

#
#


class CameraCapture:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.settings = config["camera"]
        self._frame: Frame | None = None
        self._health = "camera_unavailable"
        self._lock = threading.Lock()
        self.progress_at = time.monotonic()

    def latest(self, after_number: int) -> Frame | None:
        with self._lock:
            frame = self._frame
            return frame if frame is not None and frame.number > after_number else None

    def health(self) -> str:
        with self._lock:
            return self._health

    def run(self, stop: threading.Event) -> None:
        number, stream_id, backoff = 0, 0, 1
        while not stop.is_set():
            capture = None
            try:
                self.progress_at = time.monotonic()
                capture = cv2.VideoCapture(self.settings["rtspUrl"], cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.settings["openTimeoutMs"], cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.settings["readTimeoutMs"]])
                if capture.isOpened():
                    stream_id += 1
                    while not stop.is_set():
                        self.progress_at = time.monotonic()
                        ok, image = capture.read()
                        received = time.monotonic()
                        self.progress_at = received
                        if not ok:
                            break
                        captured_utc = datetime.now(timezone.utc)
                        height, width = image.shape[:2]
                        scale = min(1, 1280 / width, 720 / height)
                        if scale < 1:
                            image = cv2.resize(image, (max(1, int(width * scale)), max(1, int(height * scale))))
                        number += 1
                        with self._lock:
                            self._frame = Frame(number, stream_id, received, captured_utc, np.array(image, dtype=np.uint8, copy=True))
                            self._health = "healthy"
                        backoff = 1
            except Exception:
                pass
            finally:
                if capture is not None:
                    capture.release()
            if stop.is_set():
                return
            with self._lock:
                self._frame = None
                self._health = "camera_unavailable"
            self.progress_at = time.monotonic()
            if stop.wait(backoff):
                return
            backoff = min(backoff * 2, 10)
