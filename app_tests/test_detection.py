import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import numpy as np

from app.services.detection import PersonDetector
from app.services.models import Frame

#
#


def outputs():
    result = []

    for stride in (8, 16, 32):
        count = (416 // stride) ** 2
        result.extend([np.zeros((1, count, 80), np.float32), np.zeros((1, count, 32), np.float32)])

    return result


class DetectionTests(unittest.TestCase):
    def detector(self, tensors):
        net = Mock()
        net.forward.return_value = tensors
        with patch("app.services.detection.cv2.dnn.readNet", return_value=net):
            return PersonDetector({"detection": {"modelPath": "test.onnx", "confidence": 0.5}})

    def test_infer_preserves_frame_identity(self):
        tensors = outputs()
        tensors[0][0, 20 * 52 + 20, 0] = 0.8
        frame = Frame(1, 1, 0, datetime.now(timezone.utc), np.zeros((208, 416, 3), np.uint8))
        result = self.detector(tensors).infer(frame)
        self.assertIs(result.frame, frame)
        self.assertEqual(len(result.people), 1)
        self.assertEqual(result.people[0].box, (135, 31, 191, 87))

    def test_person_only_threshold_and_nms(self):
        tensors = outputs()
        tensors[0][0, 1000, 0] = 0.5
        tensors[0][0, 1001, 0] = 0.9
        tensors[0][0, 1200, 0] = 0.49
        tensors[0][0, 1500, 2] = 0.99
        frame = Frame(1, 1, 0, datetime.now(timezone.utc), np.zeros((416, 416, 3), np.uint8))
        result = self.detector(tensors).infer(frame)
        self.assertEqual(len(result.people), 1)
        self.assertAlmostEqual(result.people[0].confidence, 0.9)
        tensors[0][0, 1001, 0] = 0
        self.assertEqual(len(self.detector(tensors).infer(frame).people), 1)

    def test_empty_detections(self):
        frame = Frame(1, 1, 0, datetime.now(timezone.utc), np.zeros((20, 20, 3), np.uint8))
        self.assertEqual(self.detector(outputs()).infer(frame).people, ())

    def test_model_failure_is_safe(self):
        with patch("app.services.detection.cv2.dnn.readNet", side_effect=RuntimeError("secret-token")):
            with self.assertRaisesRegex(ValueError, "model_unavailable") as error:
                PersonDetector({"detection": {"modelPath": "secret-token", "confidence": 0.5}})
        self.assertNotIn("secret-token", str(error.exception))

    def test_rgb_normalization_and_immutable_input(self):
        detector = self.detector(outputs())
        image = np.full((416, 416, 3), [10, 20, 30], dtype=np.uint8)
        frame = Frame(1, 1, 0, datetime.now(timezone.utc), image)
        detector.infer(frame)
        blob = detector.net.setInput.call_args.args[0]
        np.testing.assert_allclose(blob[0, :, 0, 0], [(30 - 103.53) / 57.375, (20 - 116.28) / 57.12, (10 - 123.675) / 58.395], atol=1e-6)
        self.assertFalse(frame.image.flags.writeable)
