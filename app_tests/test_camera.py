import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.services.camera import CameraCapture

#
#


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.config = {"camera": {"rtspUrl": "rtsp://fake", "openTimeoutMs": 5000, "readTimeoutMs": 2000}}

    def test_latest_replaces_backlog(self):
        stop = threading.Event()
        frames = iter(range(100))
        cap = Mock()
        def read():
            number = next(frames)
            if number == 99:
                stop.set()
            return True, np.full((800, 1600, 3), number, np.uint8)
        cap.read.side_effect = read
        capture = CameraCapture(self.config)
        with patch("app.services.camera.cv2.VideoCapture", return_value=cap):
            capture.run(stop)
        frame = capture.latest(0)
        self.assertEqual(frame.number, 100)
        self.assertIsNone(capture.latest(100))
        self.assertLessEqual(frame.image.shape[0], 720)
        self.assertLessEqual(frame.image.shape[1], 1280)
        self.assertFalse(frame.image.flags.writeable)

    def test_reconnect_does_not_reuse_last_positive(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = [False, True]
        caps = [Mock(), Mock()]
        caps[0].read.side_effect = [(True, np.zeros((20, 20, 3), np.uint8)), (False, None)]
        caps[1].read.return_value = (False, None)
        capture = CameraCapture(self.config)
        with patch("app.services.camera.cv2.VideoCapture", side_effect=caps):
            capture.run(stop)
        self.assertIsNone(capture.latest(0))
        self.assertEqual(capture.health(), "camera_unavailable")
        for cap in caps:
            cap.release.assert_called_once()

    def test_timeouts_are_supplied_at_open(self):
        stop = threading.Event()
        cap = Mock()
        cap.isOpened.return_value = False
        def opened(*args):
            stop.set()
            return cap
        with patch("app.services.camera.cv2.VideoCapture", side_effect=opened) as constructor:
            CameraCapture(self.config).run(stop)
        self.assertIn(5000, constructor.call_args.args[2])
        self.assertIn(2000, constructor.call_args.args[2])
